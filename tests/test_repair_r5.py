"""Stale recovery ordering, durable retries and indexed candidate selection."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import test_backend_v2 as fixture
import test_credit_gateway as gateway_fixture
import test_repair_r3 as previous
from app import ai_usage as usage, credit_ledger as ledger


class RecoveryOrderingTests(fixture.BackendCase):
    pending = previous.DatabaseRecoveryRepairTests.pending
    recover = previous.DatabaseRecoveryRepairTests.recover
    assert_release = previous.DatabaseRecoveryRepairTests.assert_release

    def setUp(self):
        super().setUp()
        self.fund()
        self.old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        self.pending_ids = []

    def manual(self, usage_id, amount=0):
        with closing(self.open_database()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            return ledger.resolve_credit_hold(conn, usage_id, amount, actor_id=self.user_id,
                                              reason='Audited zero/actual charge.')

    def make_pending(self):
        usage_id = self.pending()
        self.execute("UPDATE credit_holds SET status='reconciliation_required',reason='stale_hold' WHERE usage_id=?", (usage_id,))
        self.execute("UPDATE api_usage_logs SET credit_status='reconciliation_required' WHERE id=?", (usage_id,))
        return usage_id

    def quota_count(self):
        with closing(self.open_database()) as conn:
            return usage._usage_count(conn, self.key_id, datetime.now(timezone.utc) - timedelta(days=1))

    def test_manual_zero_and_recovery_have_same_result_in_both_orders(self):
        for manual_first in (True, False):
            with self.subTest(manual_first=manual_first):
                usage_id = self.make_pending()
                self.assertEqual(self.quota_count(), 1)
                if manual_first:
                    self.assertEqual(self.manual(usage_id)['status'], 'released')
                    # Completion is atomic with the admin release, before recovery.
                    self.assert_release(usage_id)
                    self.assertEqual(self.quota_count(), 0)
                    self.assertEqual(self.recover(), 0)
                else:
                    self.assertEqual(self.recover(), 1)
                    self.assertEqual(self.manual(usage_id)['status'], 'released')
                self.assertEqual(self.manual(usage_id)['status'], 'released')
                self.assertEqual(self.recover(), 0)
                self.assert_release(usage_id)
                self.assertEqual(self.quota_count(), 0)
                record = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
                self.assertEqual((record['status_code'], record['error_summary']),
                                 (500, 'Recovered stale unsent request.'))
                with closing(self.open_database()) as conn:
                    wallet = ledger.wallet_snapshot(conn, self.user_id)
                self.assertEqual((wallet['balance_units'], wallet['reserved_units']),
                                 (gateway_fixture.FUND_UNITS, 0))
                audit = self.rows("SELECT * FROM credit_admin_audit WHERE action='resolve_hold' AND usage_id=?", (usage_id,))
                # A replay of an already released hold remains a no-op, as before.
                self.assertEqual(len(audit), int(manual_first))
                if manual_first:
                    self.assertEqual((audit[0]['actor_id'], audit[0]['amount_units'], audit[0]['reason']),
                                     (self.user_id, 0, 'Audited zero/actual charge.'))

    def test_manual_nonzero_preserves_log_quota_charge_and_audit_behavior(self):
        for amount in (12, 1800):
            with self.subTest(amount=amount):
                usage_id = self.make_pending()
                before = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
                count = self.quota_count()
                result = self.manual(usage_id, amount)
                self.assertEqual((result['status'], result['charged_units']), ('settled', amount))
                self.assertEqual(self.recover(), 0)
                self.manual(usage_id, amount)
                record = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
                for name in ('dispatch_state', 'completed_at', 'outcome', 'status_code', 'success', 'error_summary'):
                    self.assertEqual(record[name], before[name], name)
                self.assertEqual(self.quota_count(), count)
                self.assertEqual((record['credit_status'], record['credit_charged_units']), ('settled', amount))
                releases = self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release' AND usage_id=?", (usage_id,))
                charges = self.rows("SELECT * FROM credit_ledger WHERE kind='usage_charge' AND usage_id=?", (usage_id,))
                self.assertEqual(len(releases), 1)
                self.assertEqual(len(charges), 1)
                self.assertEqual(charges[0]['delta_units'], -amount)
                audit = self.rows("SELECT * FROM credit_admin_audit WHERE action='resolve_hold' AND usage_id=?", (usage_id,))
                self.assertEqual(len(audit), 1)
                self.assertEqual(audit[0]['amount_units'], amount)
        with closing(self.open_database()) as conn:
            wallet = ledger.wallet_snapshot(conn, self.user_id)
        self.assertEqual((wallet['balance_units'], wallet['reserved_units']),
                         (gateway_fixture.FUND_UNITS - 1812, 0))

    def test_manual_zero_does_not_complete_unproven_or_recent_requests(self):
        for kind in ('sent_log', 'sent_hold', 'legacy', 'unknown', 'completed', 'recent_log', 'recent_hold'):
            with self.subTest(kind=kind):
                usage_id = self.make_pending()
                if kind in ('sent_log', 'legacy', 'unknown'):
                    state = {'sent_log': 'dispatched', 'legacy': None, 'unknown': 'unknown'}[kind]
                    self.execute('UPDATE api_usage_logs SET dispatch_state=? WHERE id=?', (state, usage_id))
                elif kind == 'sent_hold':
                    self.execute("UPDATE credit_holds SET dispatch_state='dispatched' WHERE usage_id=?", (usage_id,))
                elif kind == 'completed':
                    self.execute('UPDATE api_usage_logs SET completed_at=? WHERE id=?', (self.old, usage_id))
                elif kind == 'recent_log':
                    self.execute('UPDATE api_usage_logs SET started_at=? WHERE id=?', (ledger.now_iso(), usage_id))
                else:
                    self.execute('UPDATE credit_holds SET created_at=? WHERE usage_id=?', (ledger.now_iso(), usage_id))
                before = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
                self.assertEqual(self.manual(usage_id)['status'], 'released')
                record = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
                for name in ('dispatch_state', 'completed_at', 'outcome', 'status_code', 'success', 'error_summary'):
                    self.assertEqual(record[name], before[name], name)

    def test_one_hundred_failures_yield_to_next_record_and_retry_after_backoff(self):
        broken = [self.pending() for _ in range(100)]
        good = self.pending()
        self.execute("""UPDATE credit_holds SET actual_units=12,usage_json='{"receipt":12}',
            reason='Keep this evidence' WHERE usage_id<>?""", (good,))
        before = self.rows('SELECT * FROM credit_holds WHERE usage_id<>? ORDER BY usage_id', (good,))
        with self.assertLogs('luomoapi.credits', level='ERROR') as captured:
            self.assertEqual(self.recover(), 0)
        self.assertEqual(len(captured.output), 100)
        for i, usage_id in enumerate(broken):
            self.assertTrue(any(f'usage_id={usage_id} request_id=r4-pending-{i}' in line for line in captured.output))
        # Independent connections see the committed cooldown even with force=True.
        self.assertEqual(self.recover(), 1)
        self.assertEqual(self.recover(), 0)
        self.assert_release(good)
        after = self.rows('SELECT * FROM credit_holds WHERE usage_id<>? ORDER BY usage_id', (good,))
        self.assertEqual(after, before)
        queued = self.rows('SELECT * FROM usage_dispatch_pending ORDER BY usage_id')
        self.assertEqual(len(queued), 100)
        for row in queued:
            self.assertEqual(row['attempts'], 1)
            self.assertTrue(row['last_error'])
        self.assertEqual(self.quota_count(), 100)
        with closing(self.open_database()) as conn:
            self.assertEqual(ledger.wallet_snapshot(conn, self.user_id)['reserved_units'], 100_000)
        retry_at = max(datetime.fromisoformat(row['next_attempt_at']) for row in queued) + timedelta(microseconds=1)
        with patch.object(ledger, 'datetime', wraps=datetime) as clock, \
             self.assertLogs('luomoapi.credits', level='ERROR') as retried:
            clock.now.return_value = retry_at
            self.assertEqual(self.recover(), 0)
        self.assertEqual(len(retried.output), 100)
        latest = self.rows('SELECT * FROM usage_dispatch_pending ORDER BY usage_id')
        for old, new in zip(queued, latest):
            self.assertEqual(new['attempts'], 2)
            self.assertGreater(new['next_attempt_at'], old['next_attempt_at'])
        self.assertEqual(self.rows('SELECT * FROM credit_holds WHERE usage_id<>? ORDER BY usage_id', (good,)), before)
        self.assert_release(good)
        self.assertEqual(self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release' AND usage_id<>?", (good,)), [])

    def test_mixed_branches_share_one_batch_limit(self):
        ids = [self.pending() for _ in range(60)] + [self.make_pending() for _ in range(60)]
        self.assertEqual(self.recover(), 100)
        self.assertEqual(self.recover(), 20)
        self.assertEqual(self.recover(), 0)
        for usage_id in ids:
            self.assert_release(usage_id)


if __name__ == '__main__':
    unittest.main()
