"""Explicit dispatch queue contracts through routes, locks and real SQLite."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

import httpx

import legacy_main_schema
import test_backend_v2 as fixture
import test_credit_gateway as gateway_fixture
import test_repair_r3 as previous
import test_repair_r5 as ordering
from app import ai_usage as usage, credit_ledger as ledger, db, dispatch_recovery as recovery, main
from app.upstream_security import PinnedTarget


class QueueRouteTests(fixture.BackendCase):
    def setUp(self):
        super().setUp()
        self.fund()
        self.public_calls = []
        api_id = self.execute("""INSERT INTO apis(name,slug,base_url,created_at,updated_at)
            VALUES ('queue','queue','https://catalog.example.test',?,?)""", (usage.iso_now(), usage.iso_now()))
        self.execute("""INSERT INTO public_gateway_routes
            (name,public_path,target_api_id,target_method,target_path,enabled,allow_anonymous,created_at,updated_at)
            VALUES ('queue','queue/keyed',?,'GET','/status',1,0,?,?)""", (api_id, usage.iso_now(), usage.iso_now()))
        self.stack.enter_context(patch('app.http_clients.create_http_client', side_effect=lambda timeout:
            gateway_fixture.create_http_client(timeout, transport=httpx.MockTransport(self.public_upstream))))
        self.stack.enter_context(patch.object(previous.public_gateway, 'validate_target_url',
            side_effect=lambda url: PinnedTarget(url, None, None)))

    async def public_upstream(self, request):
        self.public_calls.append(request)
        return httpx.Response(200, json={'status': 'ok'}, request=request)

    def public_request(self):
        return self.client.get('/api/public/v1/queue/keyed',
                               headers={'Authorization': 'Bearer ' + self.key, 'X-Request-ID': 'queue-public'})

    def assert_queue(self):
        logs = self.rows('SELECT * FROM api_usage_logs')
        expected = {r['id'] for r in logs if r['dispatch_state'] == 'reserved' and r['completed_at'] is None}
        queued = self.rows('SELECT * FROM usage_dispatch_pending')
        self.assertEqual({r['usage_id'] for r in queued}, expected)
        for row in queued:
            created = datetime.fromisoformat(row['created_at'])
            due = datetime.fromisoformat(row['next_attempt_at'])
            self.assertEqual(due - created, timedelta(minutes=15))
            self.assertTrue(row['created_at'].endswith('+00:00'))
            self.assertRegex(row['created_at'], r'\.\d{6}\+00:00$')
        self.assert_watch()

    def assert_watch(self):
        holds = self.rows('SELECT * FROM credit_holds')
        expected = {h['usage_id'] for h in holds if h['status'] == 'held' and h['dispatch_state'] == 'dispatched'}
        watched = self.rows('SELECT * FROM credit_hold_watch')
        self.assertEqual({r['usage_id'] for r in watched}, expected)
        pending = {r['usage_id'] for r in self.rows('SELECT * FROM usage_dispatch_pending')}
        self.assertTrue(expected.isdisjoint(pending))
        for row in watched:
            hold = next(h for h in holds if h['usage_id'] == row['usage_id'])
            self.assertEqual(datetime.fromisoformat(row['due_at']),
                             datetime.fromisoformat(hold['created_at']) + timedelta(minutes=15))
            self.assertRegex(row['due_at'], r'\.\d{6}\+00:00$')

    def test_success_routes_enqueue_and_atomically_dequeue_before_send(self):
        original = usage.mark_usage_dispatched
        markers = []

        def mark(conn, reservation):
            self.assert_queue()
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending WHERE usage_id=?',
                                          (reservation.id,)).fetchone()[0], 1)
            result = original(conn, reservation)
            self.assert_queue()
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending WHERE usage_id=?',
                                          (reservation.id,)).fetchone()[0], 0)
            markers.append(reservation.id)
            return result

        with patch.object(previous.private_gateway, 'mark_usage_dispatched', side_effect=mark), \
             patch.object(previous.public_gateway, 'mark_usage_dispatched', side_effect=mark):
            self.assertEqual(self.generate().status_code, 200)
            self.assert_queue()
            self.assertEqual(self.public_request().status_code, 200)
            self.assert_queue()
        self.assertEqual(len(markers), 2)
        logs = self.rows('SELECT * FROM api_usage_logs ORDER BY id')
        self.assertTrue(all(r['dispatch_state'] == 'dispatched' and r['completed_at'] for r in logs))
        self.assertEqual(len(self.rows('SELECT * FROM credit_holds')), 1)

    def test_full_v1_hold_request_enqueues_once_with_matching_values(self):
        enqueue = recovery.enqueue_usage
        mark = usage.mark_usage_dispatched
        calls = []
        sent = []

        def track(source, conn, usage_id, request_id, created_at, **options):
            enqueue(conn, usage_id, request_id, created_at, **options)
            calls.append((source, usage_id, request_id, created_at))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending WHERE usage_id=?',
                                          (usage_id,)).fetchone()[0], 1)
            row = conn.execute('SELECT * FROM usage_dispatch_pending WHERE usage_id=?', (usage_id,)).fetchone()
            self.assertEqual((row['request_id'], row['created_at']), (request_id, created_at))
            self.assertEqual(datetime.fromisoformat(row['next_attempt_at']),
                             datetime.fromisoformat(created_at) + timedelta(minutes=15))

        def send(conn, reservation):
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM credit_holds WHERE usage_id=?',
                                          (reservation.id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending WHERE usage_id=?',
                                          (reservation.id,)).fetchone()[0], 1)
            sent.append(reservation.id)
            return mark(conn, reservation)

        with patch.object(recovery, 'enqueue_usage', side_effect=lambda *args, **kw: track('ledger', *args, **kw)), \
             patch.object(usage, 'enqueue_usage', side_effect=lambda *args, **kw: track('usage', *args, **kw)), \
             patch.object(previous.private_gateway, 'mark_usage_dispatched', side_effect=send):
            self.assertEqual(self.generate().status_code, 200)
        self.assertEqual([call[0] for call in calls], ['ledger', 'usage'])
        self.assertEqual(calls[0][1:], calls[1][1:])
        self.assertEqual(sent, [calls[0][1]])
        self.assertEqual(len(self.calls), 1)
        self.assert_settled()
        self.assert_queue()

    def test_both_routes_marker_failure_never_sends_and_dequeues(self):
        for module, request in ((previous.private_gateway, self.generate),
                                (previous.public_gateway, self.public_request)):
            for failure in (False, sqlite3.OperationalError('marker failed')):
                with self.subTest(module=module.__name__, failure=str(failure)):
                    options = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                    with patch.object(module, 'mark_usage_dispatched', **options):
                        self.assertEqual(request().status_code, 500)
                    self.assert_queue()
                    record = self.rows('SELECT * FROM api_usage_logs ORDER BY id DESC LIMIT 1')[0]
                    self.assertEqual(record['dispatch_state'], 'not_dispatched')
                    self.assertIsNotNone(record['completed_at'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.public_calls, [])

    def test_presend_build_failure_dequeues_both_routes(self):
        with patch.object(main.app.state.codex_http_client, 'build_request', side_effect=RuntimeError('build failed')):
            self.assertEqual(self.generate().status_code, 500)
        self.assert_queue()
        with patch.object(previous.public_gateway, 'public_upstream_client', side_effect=RuntimeError('client failed')):
            self.assertEqual(self.public_request().status_code, 500)
        self.assert_queue()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.public_calls, [])
        self.assertTrue(all(r['dispatch_state'] == 'not_dispatched' for r in self.rows('SELECT * FROM api_usage_logs')))

    def test_sent_failure_keeps_charge_semantics_and_has_no_queue(self):
        async def timeout(request, **kwargs):
            self.calls.append(request)
            raise httpx.ReadTimeout('sent timeout', request=request)

        with patch.object(main.app.state.codex_http_client, 'send', side_effect=timeout):
            self.assertEqual(self.generate().status_code, 504)
        self.assert_queue()
        hold = self.rows('SELECT * FROM credit_holds')[0]
        self.assertEqual((hold['dispatch_state'], hold['status']), ('dispatched', 'reconciliation_required'))
        self.assertGreater(hold['amount_units'], 0)
        with patch('app.http_clients.create_http_client', side_effect=lambda timeout:
            gateway_fixture.create_http_client(timeout, transport=httpx.MockTransport(timeout_upstream))):
            self.assertEqual(self.public_request().status_code, 502)
        self.assert_queue()
        self.assertTrue(all(r['dispatch_state'] == 'dispatched' for r in self.rows('SELECT * FROM api_usage_logs')))

    def test_watch_insert_failure_rolls_back_send_marker_and_never_calls_upstream(self):
        self.execute("""CREATE TRIGGER fail_watch_insert BEFORE INSERT ON credit_hold_watch
            BEGIN SELECT RAISE(ABORT,'watch insert failed'); END""")
        self.assertEqual(self.generate().status_code, 500)
        self.assert_queue()
        self.assertEqual(self.calls, [])
        record, hold, wallet, entries = self.accounting()
        self.assertEqual((record['dispatch_state'], hold['status']), ('not_dispatched', 'released'))
        self.assertEqual(wallet['reserved_units'], 0)
        self.assertEqual(sum(e['kind'] == 'credit_release' for e in entries), 1)

    def test_stream_completion_and_disconnect_remove_watch(self):
        for protocol in ('chat', 'responses'):
            if protocol == 'chat':
                self.response_sse = gateway_fixture.sse_event(gateway_fixture.chat_response()) + gateway_fixture.sse_event('[DONE]')
            else:
                self.response_sse = gateway_fixture.sse_event(
                    {'response': gateway_fixture.responses_response()}, 'response.completed')
            self.assertEqual(self.generate(protocol, stream=True).status_code, 200)
            self.assertEqual(self.accounting()[1]['status'], 'settled')
            self.assert_queue()
        self.response_sse = None

        async def public_sse(request):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=b'data: ok\n\n', request=request)

        with patch.object(self, 'public_upstream', side_effect=public_sse):
            self.assertEqual(self.public_request().status_code, 200)
        self.assert_queue()

        # Exercise the actual metered relay and its cancellation finalizer.
        self.old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        self.pending_ids = []
        usage_id = previous.DatabaseRecoveryRepairTests.pending(self)
        request_id = self.rows('SELECT request_id FROM api_usage_logs WHERE id=?', (usage_id,))[0]['request_id']
        reservation = usage.UsageReservation(usage_id, request_id, self.old)
        with closing(self.open_database()) as conn:
            usage.mark_usage_dispatched(conn, reservation)
        self.assert_queue()

        async def disconnect():
            upstream = httpx.Response(200, headers={'content-type': 'text/event-stream'},
                stream=gateway_fixture.FixtureStream(gateway_fixture.sse_event({'choices': [{'delta': {'content': 'partial'}}]})))
            relay = previous.private_gateway._relay_stream(upstream, None, reservation=reservation,
                key_row={'id': self.key_id}, request_id=request_id, protocol='chat.completions',
                started=time.monotonic(), payload={'stream': True}, sse_expected=True)
            await anext(relay)
            await relay.aclose()
            self.assertTrue(upstream.is_closed)

        asyncio.run(disconnect())
        self.assert_queue()
        record = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
        self.assertEqual(record['outcome'], 'client_disconnect')
        self.assertIsNotNone(record['completed_at'])
        self.assertEqual(self.rows('SELECT status FROM credit_holds WHERE usage_id=?',
                                   (usage_id,))[0]['status'], 'reconciliation_required')

    def test_cancellation_in_both_routes_removes_watch(self):
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        for module, request, seam in ((previous.private_gateway, self.generate, 'private'),
                                      (previous.public_gateway, self.public_request, 'public')):
            with self.subTest(route=seam):
                target = patch.object(main.app.state.codex_http_client, 'send', side_effect=cancelled) if seam == 'private' else (
                    patch.object(self, 'public_upstream', side_effect=cancelled))
                with target:
                    self.assertEqual(request().status_code, 500)
                self.assert_queue()
                record = self.rows('SELECT * FROM api_usage_logs ORDER BY id DESC LIMIT 1')[0]
                self.assertEqual(record['outcome'], 'client_disconnect')
                self.assertEqual(record['dispatch_state'], 'dispatched')
                self.assertIsNotNone(record['completed_at'])


async def timeout_upstream(request):
    raise httpx.ReadTimeout('sent timeout', request=request)


class QueueLockTests(fixture.BackendCase):
    # Exercise the existing real BEGIN EXCLUSIVE fixtures, including the full
    # request and both failed cleanup connections, with queue assertions added.
    request_with_exclusive_lock = previous.PublicDispatchRepairTests.request_with_exclusive_lock
    async def upstream(self, request):
        if request.url.host == 'catalog.example.test':
            return await previous.PublicDispatchRepairTests.upstream(self, request)
        return await gateway_fixture.CreditGatewayTests.upstream(self, request)

    request_route = previous.PublicDispatchRepairTests.request_route

    def setUp(self):
        super().setUp()
        self.fund()
        self.execute('UPDATE api_keys SET rate_limit_per_minute=1,daily_quota=1,monthly_quota=1 WHERE id=?', (self.key_id,))
        api_id = self.execute("""INSERT INTO apis(name,slug,base_url,created_at,updated_at)
            VALUES ('locks','locks','https://catalog.example.test',?,?)""", (usage.iso_now(), usage.iso_now()))
        route_id = self.execute("""INSERT INTO public_gateway_routes
            (name,public_path,target_api_id,target_method,target_path,enabled,allow_anonymous,created_at,updated_at)
            VALUES ('locks','locks/keyed',?,'GET','/status',1,0,?,?)""", (api_id, usage.iso_now(), usage.iso_now()))
        self.routes = {False: ('locks/keyed', route_id)}
        self.stack.enter_context(patch('app.http_clients.create_http_client', side_effect=lambda timeout:
            gateway_fixture.create_http_client(timeout, transport=httpx.MockTransport(self.upstream))))
        self.stack.enter_context(patch.object(previous.public_gateway, 'validate_target_url',
            side_effect=lambda url: PinnedTarget(url, None, None)))

    def test_no_hold_lock_failure_recovers_daily_and_monthly_quota(self):
        with self.assertLogs('luomoapi', level='ERROR'):
            self.assertEqual(self.request_with_exclusive_lock(keep_lock_during_cleanup=True).status_code, 500)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows('SELECT * FROM credit_holds'), [])
        pending = self.rows('SELECT * FROM usage_dispatch_pending')
        self.assertEqual(len(pending), 1)
        with closing(self.open_database()) as conn:
            key = conn.execute('SELECT * FROM api_keys WHERE id=?', (self.key_id,)).fetchone()
            self.assertIsNotNone(usage.quota_rejection(conn, key))
        old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        self.execute('UPDATE api_usage_logs SET started_at=?', (old,))
        self.execute('UPDATE usage_dispatch_pending SET next_attempt_at=?', (old,))
        with closing(self.open_database()) as conn:
            self.assertEqual(ledger.recover_stale_usage(conn, force=True), 1)
            key = conn.execute('SELECT * FROM api_keys WHERE id=?', (self.key_id,)).fetchone()
            self.assertIsNone(usage.quota_rejection(conn, key))
        log = self.rows('SELECT * FROM api_usage_logs')[0]
        self.assertEqual(log['dispatch_state'], 'not_dispatched')
        self.assertIsNotNone(log['completed_at'])
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending'), [])
        self.assertEqual(self.request_route(False).status_code, 200)

    def assert_released(self):
        previous.GenerationDispatchRepairTests.assert_released(self)
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending'), [])

    def test_generation_lock_failure_recovers_hold_and_quota(self):
        previous.GenerationDispatchRepairTests.test_dispatch_marker_database_lock_and_cleanup_lock_recover_after_timeout(self)
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending'), [])


class QueueRecoveryTests(fixture.BackendCase):
    pending = previous.DatabaseRecoveryRepairTests.pending
    recover = previous.DatabaseRecoveryRepairTests.recover
    assert_release = previous.DatabaseRecoveryRepairTests.assert_release
    manual = ordering.RecoveryOrderingTests.manual
    quota_count = ordering.RecoveryOrderingTests.quota_count
    test_one_hundred_failures_yield_and_retry = ordering.RecoveryOrderingTests.test_one_hundred_failures_yield_to_next_record_and_retry_after_backoff

    def setUp(self):
        super().setUp()
        self.fund()
        self.old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        self.pending_ids = []

    def test_retry_update_failure_keeps_other_releases_committed(self):
        first, broken, last = self.pending(), self.pending(), self.pending()
        self.execute('UPDATE credit_holds SET actual_units=12 WHERE usage_id=?', (broken,))
        original = self.rows('SELECT * FROM usage_dispatch_pending WHERE usage_id=?', (broken,))[0]
        self.execute(f"""CREATE TRIGGER fail_queue_backoff BEFORE UPDATE ON usage_dispatch_pending
            WHEN OLD.usage_id={broken} BEGIN SELECT RAISE(ABORT,'backoff write failed'); END""")
        with self.assertLogs('luomoapi.credits', level='ERROR') as captured:
            self.assertEqual(self.recover(), 2)
        self.assertEqual(len(captured.output), 2)
        self.assertTrue(all(f'usage_id={broken} request_id=r4-pending-1' in line for line in captured.output))
        self.assert_release(first)
        self.assert_release(last)
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending WHERE usage_id=?', (broken,))[0], original)
        with closing(self.open_database()) as conn, conn, self.assertLogs('luomoapi.credits', level='ERROR'):
            conn.execute('BEGIN IMMEDIATE')
            self.assertEqual(ledger.recover_stale_holds(conn, force=True), 0)
        self.assert_release(first)
        self.assert_release(last)

    def admin_pending_ids(self):
        self.as_admin()
        response = self.client.get('/admin/credits')
        self.assertEqual(response.status_code, 200)
        return {row['usage_id'] for row in response.context['holds']}

    def test_repeated_due_failures_do_not_starve_sent_hold_and_escalate_without_money_changes(self):
        broken = [self.pending() for _ in range(100)]
        good, sent = self.pending(), self.pending()
        self.execute('UPDATE credit_holds SET actual_units=12 WHERE usage_id<=?', (broken[-1],))
        with closing(self.open_database()) as conn:
            self.assertTrue(usage.mark_usage_dispatched(conn,
                usage.UsageReservation(sent, f'r4-pending-{len(broken) + 1}', self.old)))
        original_holds = self.rows('SELECT * FROM credit_holds WHERE usage_id<=? ORDER BY usage_id', (broken[-1],))
        original_logs = self.rows('SELECT * FROM api_usage_logs WHERE id<=? ORDER BY id', (broken[-1],))
        initial_wallet = self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0]
        now = datetime.now(timezone.utc)
        # These are distinct due retries, beyond both the process gate and backoff.
        with patch.object(ledger, 'datetime', wraps=datetime) as clock, \
             patch.object(ledger, 'monotonic') as monotonic, \
             self.assertLogs('luomoapi.credits', level='ERROR') as captured:
            for iteration in range(recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS + 2):
                clock.now.return_value = now + timedelta(seconds=61 * iteration)
                monotonic.return_value = 10_000_000 + 61 * iteration
                with closing(self.open_database()) as conn:
                    changed = ledger.recover_stale_usage(conn)
                if iteration == 0:
                    self.assertEqual(changed, 1)
                    self.assertIn(sent, self.admin_pending_ids())
                    self.assertEqual(self.rows('SELECT status FROM credit_holds WHERE usage_id=?',
                                               (sent,))[0]['status'], 'reconciliation_required')
                if iteration == 1:
                    self.assert_release(good)
                    wallet_after_release = self.rows('SELECT * FROM credit_wallets WHERE user_id=?',
                                                     (self.user_id,))[0]
                if not self.rows('SELECT * FROM usage_dispatch_pending'):
                    break
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending'), [])
        self.assertTrue(set(broken + [sent]).issubset(self.admin_pending_ids()))
        for before, before_log, usage_id in zip(original_holds, original_logs, broken):
            hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
            record = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
            self.assertEqual((hold['status'], hold['reason']),
                             ('reconciliation_required', 'dispatch_recovery_failed'))
            self.assertEqual((record['credit_status'], record['credit_reason']),
                             ('reconciliation_required', 'dispatch_recovery_failed'))
            self.assertEqual({k: v for k, v in hold.items() if k not in {'status', 'reason', 'updated_at'}},
                             {k: v for k, v in before.items() if k not in {'status', 'reason', 'updated_at'}})
            self.assertEqual({k: v for k, v in record.items() if k not in {'credit_status', 'credit_reason'}},
                             {k: v for k, v in before_log.items() if k not in {'credit_status', 'credit_reason'}})
            self.assertTrue(any('dispatch_recovery_escalated' in line and f'usage_id={usage_id} ' in line
                                and f'request_id={record["request_id"]}' in line and 'last_error=' in line
                                for line in captured.output))
        final_wallet = self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0]
        self.assertEqual(final_wallet, wallet_after_release)
        self.assertEqual(final_wallet['balance_units'], initial_wallet['balance_units'])
        self.assertEqual(final_wallet['reserved_units'], initial_wallet['reserved_units'] - 1000)
        self.assertEqual(final_wallet['reserved_units'], sum(row['amount_units'] for row in self.rows(
            "SELECT amount_units FROM credit_holds WHERE status IN ('held','reconciliation_required')")))
        self.assertEqual([r['usage_id'] for r in self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release'")], [good])
        self.assert_release(good)
        # The escalated records are accepted by the existing audited admin path.
        self.assertEqual(self.manual(broken[0])['status'], 'released')

    def test_conflict_removal_persists_reason_and_admin_visibility(self):
        usage_id = self.pending()
        self.execute("UPDATE credit_holds SET dispatch_state='dispatched' WHERE usage_id=?", (usage_id,))
        before = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
        wallet = self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0]
        with self.assertLogs('luomoapi.credits', level='ERROR'):
            self.assertEqual(self.recover(), 0)
        hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
        record = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
        self.assertEqual((hold['status'], hold['reason']), ('reconciliation_required', 'dispatch_conflict'))
        self.assertEqual((record['credit_status'], record['credit_reason']),
                         ('reconciliation_required', 'dispatch_conflict'))
        self.assertEqual({k: v for k, v in hold.items() if k not in {'status', 'reason', 'updated_at'}},
                         {k: v for k, v in before.items() if k not in {'status', 'reason', 'updated_at'}})
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending'), [])
        self.assertEqual(self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0], wallet)
        self.assertIn(usage_id, self.admin_pending_ids())

    def test_conflict_preserves_existing_reconciliation_evidence(self):
        usage_id = self.pending()
        self.execute("""UPDATE credit_holds SET status='reconciliation_required',dispatch_state='dispatched',
            reason='existing review',actual_units=12,usage_json='{"receipt":12}' WHERE usage_id=?""", (usage_id,))
        before = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
        before_log = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
        with self.assertLogs('luomoapi.credits', level='ERROR'):
            self.assertEqual(self.recover(), 0)
        self.assertEqual(self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0], before)
        self.assertEqual(self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0], before_log)
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending'), [])
        self.assertIn(usage_id, self.admin_pending_ids())

    def test_escalation_failure_rolls_back_record_and_preserves_other_releases(self):
        broken, good = self.pending(), self.pending()
        self.execute('UPDATE credit_holds SET actual_units=12 WHERE usage_id=?', (broken,))
        self.execute('UPDATE usage_dispatch_pending SET attempts=? WHERE usage_id=?',
                     (recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS - 1, broken))
        before = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (broken,))[0]
        before_log = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (broken,))[0]
        self.execute(f"""CREATE TRIGGER fail_escalation BEFORE DELETE ON usage_dispatch_pending
            WHEN OLD.usage_id={broken} BEGIN SELECT RAISE(ABORT,'escalation delete failed'); END""")
        with self.assertLogs('luomoapi.credits', level='ERROR') as captured:
            self.assertEqual(self.recover(), 1)
        self.assertTrue(any('stale_usage_escalation_failed' in line for line in captured.output))
        self.assert_release(good)
        self.assertEqual(self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (broken,))[0], before)
        self.assertEqual(self.rows('SELECT * FROM api_usage_logs WHERE id=?', (broken,))[0], before_log)
        queued = self.rows('SELECT * FROM usage_dispatch_pending WHERE usage_id=?', (broken,))[0]
        self.assertEqual(queued['attempts'], recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS)
        # Even if the conflicting receipt disappears, reaching the limit is final
        # for automatic release. Only the isolated escalation is retried.
        self.execute('DROP TRIGGER fail_escalation')
        self.execute('UPDATE credit_holds SET actual_units=NULL WHERE usage_id=?', (broken,))
        with patch.object(ledger, 'datetime', wraps=datetime) as clock, \
             self.assertLogs('luomoapi.credits', level='ERROR'):
            clock.now.return_value = datetime.fromisoformat(queued['next_attempt_at'])
            self.assertEqual(self.recover(), 0)
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending'), [])
        self.assertEqual(self.rows('SELECT reason FROM credit_holds WHERE usage_id=?',
                                   (broken,))[0]['reason'], 'dispatch_recovery_failed')
        self.assertEqual(self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release' AND usage_id=?", (broken,)), [])

    def test_continuous_pending_arrivals_do_not_delay_sent_watch_expiry(self):
        start = datetime.now(timezone.utc)
        self.old = recovery.timestamp(start)
        sent = self.pending()
        with closing(self.open_database()) as conn:
            usage.mark_usage_dispatched(conn, usage.UsageReservation(sent, 'r4-pending-0', self.old))
        with patch.object(ledger, 'datetime', wraps=datetime) as clock, \
             patch.object(ledger, 'monotonic') as monotonic:
            for iteration in range(23):
                now = start + timedelta(seconds=61 * iteration)
                self.old = recovery.timestamp(now)
                # Batch fixture writes so each iteration models 100 committed
                # fresh reservations without invoking extra recovery passes.
                with closing(self.open_database()) as conn, conn:
                    conn.execute('BEGIN IMMEDIATE')
                    for i in range(100):
                        request_id = f'arrival-{iteration}-{i}'
                        usage_id = conn.execute("""INSERT INTO api_usage_logs
                            (created_at,started_at,user_id,api_key_id,method,public_path,request_id,
                             protocol,dispatch_state,outcome)
                            VALUES (?,?,?,?,'POST','v1/responses',?,'responses','reserved','in_flight')""",
                            (self.old, self.old, self.user_id, self.key_id, request_id)).lastrowid
                        ledger.reserve_credit(conn, usage_id, self.user_id, 1000, '{}')
                        conn.execute('UPDATE credit_holds SET created_at=? WHERE usage_id=?', (self.old, usage_id))
                        recovery.enqueue_usage(conn, usage_id, request_id, self.old)
                clock.now.return_value = now
                monotonic.return_value = 20_000_000 + 61 * iteration
                with closing(self.open_database()) as conn:
                    ledger.recover_stale_usage(conn)
                hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (sent,))[0]
                if now < start + timedelta(minutes=15):
                    self.assertEqual(hold['status'], 'held')
                else:
                    self.assertEqual((hold['status'], hold['reason']), ('reconciliation_required', 'stale_hold'))
                    self.assertIn(sent, self.admin_pending_ids())
                    self.assertEqual(self.rows('SELECT * FROM credit_hold_watch WHERE usage_id=?', (sent,)), [])
                self.assertEqual(self.rows("SELECT * FROM credit_ledger WHERE usage_id=? AND kind='credit_release'", (sent,)), [])

    def test_one_hundred_failed_escalations_back_off_and_yield_to_new_request(self):
        broken = [self.pending() for _ in range(100)]
        self.execute('UPDATE usage_dispatch_pending SET attempts=?', (recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS,))
        self.execute(f"""CREATE TRIGGER fail_manual_update BEFORE UPDATE ON credit_holds
            WHEN OLD.usage_id<={broken[-1]} AND NEW.status='reconciliation_required'
            BEGIN SELECT RAISE(ABORT,'manual update failed'); END""")
        start = datetime.now(timezone.utc)
        self.old = recovery.timestamp(start)
        good = self.pending()
        before_wallet = self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0]
        before_holds = self.rows('SELECT * FROM credit_holds WHERE usage_id<=? ORDER BY usage_id', (broken[-1],))
        previous_due = {}
        with patch.object(ledger, 'datetime', wraps=datetime) as clock, \
             patch.object(ledger, 'monotonic') as monotonic, \
             self.assertLogs('luomoapi.credits', level='ERROR'):
            for iteration in range(18):
                now = start + timedelta(seconds=61 * iteration)
                clock.now.return_value = now
                monotonic.return_value = 30_000_000 + 61 * iteration
                with closing(self.open_database()) as conn:
                    ledger.recover_stale_usage(conn)
                queued = self.rows('SELECT * FROM usage_dispatch_pending WHERE usage_id<=? ORDER BY usage_id', (broken[-1],))
                self.assertEqual(len(queued), 100)
                advanced = 0
                for row in queued:
                    self.assertEqual(row['attempts'], recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS)
                    self.assertEqual(row['last_error'], 'manual update failed')
                    if row['usage_id'] in previous_due:
                        self.assertGreaterEqual(row['next_attempt_at'], previous_due[row['usage_id']])
                        advanced += row['next_attempt_at'] > previous_due[row['usage_id']]
                if previous_due:
                    self.assertEqual(advanced, 99 if iteration == 15 else 100)
                previous_due = {r['usage_id']: r['next_attempt_at'] for r in queued}
                if iteration >= 15:
                    self.assert_release(good)
        self.assertEqual(self.rows('SELECT * FROM credit_holds WHERE usage_id<=? ORDER BY usage_id', (broken[-1],)), before_holds)
        self.execute('DROP TRIGGER fail_manual_update')
        with patch.object(ledger, 'datetime', wraps=datetime) as clock, self.assertLogs('luomoapi.credits', level='ERROR'):
            clock.now.return_value = start + timedelta(seconds=61 * 18)
            self.assertEqual(self.recover(), 0)
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending'), [])
        self.assertTrue(set(broken).issubset(self.admin_pending_ids()))
        wallet = self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0]
        self.assertEqual(wallet['balance_units'], before_wallet['balance_units'])
        self.assertEqual(wallet['reserved_units'], before_wallet['reserved_units'] - 1000)
        self.assertEqual([r['usage_id'] for r in self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release'")], [good])

    def test_eight_connections_release_each_request_once(self):
        ids = [self.pending() for _ in range(12)]
        barrier = threading.Barrier(8)

        def run(_):
            with closing(db.connect_database(self.path)) as conn:
                barrier.wait(timeout=10)
                return ledger.recover_stale_usage(conn, force=True)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, range(8)))
        self.assertEqual(sum(results), len(ids))
        for usage_id in ids:
            self.assert_release(usage_id)
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending'), [])
        self.assertEqual(self.rows('SELECT reserved_units FROM credit_wallets WHERE user_id=?', (self.user_id,))[0]['reserved_units'], 0)

    def test_watch_cleanup_in_settlement_release_review_and_manual_transactions(self):
        for actual in (12, 0, None):
            with self.subTest(actual=actual):
                usage_id = self.pending()
                request_id = self.rows('SELECT request_id FROM api_usage_logs WHERE id=?', (usage_id,))[0]['request_id']
                with closing(self.open_database()) as conn:
                    usage.mark_usage_dispatched(conn, usage.UsageReservation(usage_id, request_id, self.old))
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM credit_hold_watch WHERE usage_id=?', (usage_id,)).fetchone()[0], 1)
                    conn.execute('BEGIN IMMEDIATE')
                    result = ledger.settle_credit(conn, usage_id, actual, reason='fixture settlement')
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM credit_hold_watch WHERE usage_id=?', (usage_id,)).fetchone()[0], 0)
                    conn.rollback()
                    self.assertEqual(conn.execute('SELECT status FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone()[0], 'held')
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM credit_hold_watch WHERE usage_id=?', (usage_id,)).fetchone()[0], 1)
                    conn.execute('BEGIN IMMEDIATE')
                    result = ledger.settle_credit(conn, usage_id, actual, reason='fixture settlement')
                    conn.commit()
                self.assertEqual(result['status'], 'reconciliation_required' if actual is None else ('settled' if actual else 'released'))
                self.assertEqual(self.rows('SELECT * FROM credit_hold_watch WHERE usage_id=?', (usage_id,)), [])
                if actual is None:
                    self.assertEqual(self.manual(usage_id)['status'], 'released')
                    self.assertEqual(self.rows('SELECT * FROM credit_hold_watch WHERE usage_id=?', (usage_id,)), [])

    def test_manual_resolution_and_conflict_cleanup_remove_residual_watch(self):
        for action in ('manual', 'conflict', 'escalation'):
            with self.subTest(action=action):
                usage_id = self.pending()
                with closing(self.open_database()) as conn, conn:
                    conn.execute('BEGIN IMMEDIATE')
                    # Model a corrupt leftover watch entry, retaining its
                    # cleanup obligation during every status-changing path.
                    recovery.enqueue_hold_watch(conn, usage_id, self.old)
                    if action == 'manual':
                        conn.execute("UPDATE credit_holds SET status='reconciliation_required' WHERE usage_id=?", (usage_id,))
                    elif action == 'conflict':
                        conn.execute("UPDATE credit_holds SET dispatch_state='dispatched' WHERE usage_id=?", (usage_id,))
                    else:
                        conn.execute('UPDATE usage_dispatch_pending SET attempts=? WHERE usage_id=?',
                            (recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS, usage_id))
                if action == 'manual':
                    self.assertEqual(self.manual(usage_id)['status'], 'released')
                else:
                    wallet = self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0]
                    with self.assertLogs('luomoapi.credits', level='ERROR'):
                        self.assertEqual(self.recover(), 0)
                    self.assertEqual(self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0], wallet)
                self.assertEqual(self.rows('SELECT * FROM credit_hold_watch WHERE usage_id=?', (usage_id,)), [])

    def test_dispatch_and_recovery_lock_order_determines_single_winner(self):
        for dispatch_first in (True, False):
            with self.subTest(dispatch_first=dispatch_first):
                usage_id = self.pending()
                request_id = self.rows('SELECT request_id FROM api_usage_logs WHERE id=?', (usage_id,))[0]['request_id']
                reservation = usage.UsageReservation(usage_id, request_id, self.old)
                waiting = threading.Event()

                def second():
                    with closing(db.connect_database(self.path)) as conn:
                        conn.set_trace_callback(lambda sql: waiting.set() if sql == 'BEGIN IMMEDIATE' else None)
                        if dispatch_first:
                            return ledger.recover_stale_usage(conn, force=True)
                        try:
                            usage.mark_usage_dispatched(conn, reservation)
                        except RuntimeError:
                            return False
                        return True

                with closing(self.open_database()) as conn, ThreadPoolExecutor(max_workers=1) as pool:
                    conn.execute('BEGIN IMMEDIATE')
                    future = pool.submit(second)
                    self.assertTrue(waiting.wait(10))
                    if dispatch_first:
                        self.assertTrue(usage.mark_usage_dispatched(conn, reservation))
                    else:
                        self.assertEqual(ledger.recover_stale_holds(conn, force=True), 1)
                        conn.commit()
                    self.assertEqual(future.result(timeout=10), 1 if dispatch_first else False)
                row = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
                hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
                self.assertEqual(row['dispatch_state'], 'dispatched' if dispatch_first else 'not_dispatched')
                self.assertEqual(hold['status'], 'reconciliation_required' if dispatch_first else 'released')
                releases = self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release' AND usage_id=?", (usage_id,))
                self.assertEqual(len(releases), int(not dispatch_first))
                self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending WHERE usage_id=?', (usage_id,)), [])

    def test_manual_zero_and_recovery_delete_queue_in_both_orders(self):
        for manual_first in (True, False):
            with self.subTest(manual_first=manual_first):
                usage_id = self.pending()
                self.execute("UPDATE credit_holds SET status='reconciliation_required' WHERE usage_id=?", (usage_id,))
                if manual_first:
                    self.manual(usage_id)
                    self.assertEqual(self.recover(), 0)
                else:
                    self.assertEqual(self.recover(), 1)
                    self.manual(usage_id)
                self.assert_release(usage_id)
                self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending WHERE usage_id=?', (usage_id,)), [])
                with closing(self.open_database()) as conn:
                    self.assertEqual(usage._usage_count(conn, self.key_id, datetime.now(timezone.utc) - timedelta(days=1)), 0)
                audits = self.rows("SELECT * FROM credit_admin_audit WHERE action='resolve_hold' AND usage_id=?", (usage_id,))
                self.assertEqual(len(audits), int(manual_first))

    def test_corrupt_queue_persists_conflict_without_changing_money_or_dispatch(self):
        for state, completed in (('dispatched', None), (None, None), ('unknown', None), ('reserved', self.old)):
            with self.subTest(state=state, completed=completed):
                usage_id = self.pending()
                self.execute('UPDATE api_usage_logs SET dispatch_state=?,completed_at=? WHERE id=?', (state, completed, usage_id))
                hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
                log = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
                wallet = self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0]
                if state != 'reserved':
                    with self.assertLogs('luomoapi.credits', level='ERROR'):
                        self.assertEqual(self.recover(), 0)
                    after = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
                    after_log = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
                    self.assertEqual((after['status'], after['reason']), ('reconciliation_required', 'dispatch_conflict'))
                    self.assertEqual((after_log['credit_status'], after_log['credit_reason']),
                                     ('reconciliation_required', 'dispatch_conflict'))
                    self.assertEqual({k: v for k, v in after.items() if k not in {'status', 'reason', 'updated_at'}},
                                     {k: v for k, v in hold.items() if k not in {'status', 'reason', 'updated_at'}})
                    self.assertEqual({k: v for k, v in after_log.items() if k not in {'credit_status', 'credit_reason'}},
                                     {k: v for k, v in log.items() if k not in {'credit_status', 'credit_reason'}})
                else:
                    self.assertEqual(self.recover(), 0)
                    self.assertEqual(self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0], hold)
                    self.assertEqual(self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0], log)
                self.assertEqual(self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0], wallet)
                self.assertEqual(self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release' AND usage_id=?", (usage_id,)), [])
                self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending WHERE usage_id=?', (usage_id,)), [])
                # Retire this fixture so the independent legacy held pass does
                # not legitimately change it during the next subtest.
                self.execute("UPDATE credit_holds SET status='reconciliation_required' WHERE usage_id=?", (usage_id,))

    def test_unsafe_holds_leave_money_frozen_and_exit_queue(self):
        for status, dispatch in (('held', 'dispatched'), ('reconciliation_required', 'dispatched'),
                                 ('settled', 'reserved'), ('released', 'reserved')):
            with self.subTest(status=status, dispatch=dispatch):
                usage_id = self.pending()
                self.execute('UPDATE credit_holds SET status=?,dispatch_state=? WHERE usage_id=?', (status, dispatch, usage_id))
                before = self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0]
                with self.assertLogs('luomoapi.credits', level='ERROR'):
                    self.assertEqual(self.recover(), 0)
                self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending WHERE usage_id=?', (usage_id,)), [])
                self.assertEqual(self.rows('SELECT * FROM credit_wallets WHERE user_id=?', (self.user_id,))[0], before)
                self.assertEqual(self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release' AND usage_id=?", (usage_id,)), [])
                self.execute("UPDATE credit_holds SET status='reconciliation_required' WHERE usage_id=?", (usage_id,))


class QueueQueryTests(unittest.TestCase):
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    old = '2026-11-09T23:44:00.000000+00:00'

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.executescript(db.SCHEMA)
        db._ensure_columns(self.conn)
        self.conn.execute("""INSERT INTO users(id,username,email,password_hash,created_at,updated_at)
            VALUES (1,'query','query@example.test','unused',?,?)""", (self.old, self.old))
        ledger.initialize_credit_schema(self.conn)

    def add_rows(self, count=1, *, log_state='reserved', dispatch='reserved', queued=False,
                 created=None, started=None):
        created = self.old if created is None else created
        started = created if started is None else started
        first = self.conn.execute('SELECT COALESCE(MAX(id),0)+1 FROM api_usage_logs').fetchone()[0]
        ids = list(range(first, first + count))
        self.conn.executemany("""INSERT INTO api_usage_logs
            (id,created_at,started_at,user_id,method,public_path,dispatch_state,request_id)
            VALUES (?,?,?,1,'POST','v1/responses',?,?)""",
            ((i, created, started, log_state, f'queue-{i}') for i in ids))
        self.conn.executemany("""INSERT INTO credit_holds
            (usage_id,user_id,amount_units,status,dispatch_state,pricing_snapshot,reason,created_at,updated_at)
            VALUES (?,1,0,'reconciliation_required',?,'{}','query fixture',?,?)""",
            ((i, dispatch, created, created) for i in ids))
        if queued:
            for usage_id in ids:
                recovery.enqueue_usage(self.conn, usage_id, f'queue-{usage_id}', self.old)
        return ids

    def steps(self):
        count = 0

        def progress():
            nonlocal count
            count += 1
            return 0

        self.conn.set_progress_handler(progress, 1)
        try:
            rows = self.conn.execute(recovery.PENDING_QUERY, (recovery.timestamp(self.now), 100)).fetchall()
            self.conn.execute(recovery.WATCH_QUERY, (recovery.timestamp(self.now), 100)).fetchall()
        finally:
            self.conn.set_progress_handler(None, 0)
        return [row[0] for row in rows], count

    def test_three_100k_backlogs_keep_candidate_vm_growth_below_two(self):
        for log_state, dispatch in ((None, 'reserved'), ('dispatched', 'reserved'), ('dispatched', 'dispatched')):
            with self.subTest(log_state=log_state, dispatch=dispatch):
                # Each shape has its own database population and measurements.
                self.conn.execute('DELETE FROM usage_dispatch_pending')
                self.conn.execute('DELETE FROM credit_holds')
                self.conn.execute('DELETE FROM api_usage_logs')
                expected = self.add_rows(5, queued=True)
                self.add_rows(1000, log_state=log_state, dispatch=dispatch)
                small_ids, small_steps = self.steps()
                self.add_rows(99_000, log_state=log_state, dispatch=dispatch)
                large_ids, large_steps = self.steps()
                self.assertEqual(small_ids, expected)
                self.assertEqual(large_ids, expected)
                self.assertGreater(small_steps, 0)
                self.assertLessEqual(large_steps, small_steps * 2, (small_steps, large_steps))
                details = '\n'.join(row[3] for row in self.conn.execute('EXPLAIN QUERY PLAN ' + recovery.PENDING_QUERY,
                    (recovery.timestamp(self.now), 100)))
                self.assertIn('SEARCH usage_dispatch_pending USING INDEX idx_usage_dispatch_pending_next', details)
                self.assertNotIn('SCAN ', details)
                self.assertNotIn('USE TEMP B-TREE', details)
        held_plan = '\n'.join(row[3] for row in self.conn.execute('EXPLAIN QUERY PLAN ' + recovery.WATCH_QUERY,
            (recovery.timestamp(self.now), 100)))
        self.assertIn('idx_credit_hold_watch_due', held_plan)
        self.assertNotIn('USE TEMP B-TREE', held_plan)
        for query in (recovery.PENDING_QUERY, recovery.WATCH_QUERY):
            self.assertNotIn('julianday', query)
            self.assertNotIn('strftime', query)
            self.assertNotIn('JOIN', query)

    def watch(self, ids):
        for usage_id in ids:
            self.conn.execute("UPDATE credit_holds SET status='held' WHERE usage_id=?", (usage_id,))
            created = self.conn.execute('SELECT created_at FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone()[0]
            recovery.enqueue_hold_watch(self.conn, usage_id, created)

    def test_watch_ignores_one_hundred_pending_holds_and_accepts_later_dispatch(self):
        blocked = self.add_rows(100, queued=True)
        good = self.add_rows(log_state='dispatched')[0]
        self.conn.execute("UPDATE credit_holds SET status='held'")
        self.watch([good])
        self.conn.execute('UPDATE usage_dispatch_pending SET next_attempt_at=?',
                          (recovery.timestamp(self.now + timedelta(days=1)),))
        self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 1)
        self.assertEqual(self.conn.execute('SELECT status FROM credit_holds WHERE usage_id=?',
                                          (good,)).fetchone()[0], 'reconciliation_required')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], len(blocked))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM credit_hold_watch').fetchone()[0], 0)
        # Dispatch atomically moves a formerly pending hold into the watch.
        usage.mark_usage_dispatched(self.conn, usage.UsageReservation(blocked[0], f'queue-{blocked[0]}', self.old))
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 1)

    def test_watch_one_hundred_failures_back_off_and_allow_record_101(self):
        broken = self.add_rows(100, log_state='dispatched', dispatch='dispatched')
        good = self.add_rows(log_state='dispatched', dispatch='dispatched')[0]
        self.watch(broken + [good])
        self.conn.execute(f"""CREATE TRIGGER fail_held_recovery BEFORE UPDATE ON credit_holds
            WHEN OLD.usage_id<={broken[-1]} BEGIN SELECT RAISE(ABORT,'held update failed'); END""")
        with self.assertLogs('luomoapi.credits', level='ERROR') as captured:
            self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 0)
        self.assertEqual(len(captured.output), 100)
        self.conn.commit()
        backed_off = self.conn.execute('SELECT * FROM credit_hold_watch WHERE usage_id<>? ORDER BY usage_id', (good,)).fetchall()
        self.assertEqual(len(backed_off), 100)
        for row in backed_off:
            self.assertEqual(row['attempts'], 1)
            self.assertEqual(row['last_error'], 'held update failed')
            self.assertEqual(row['due_at'], recovery.timestamp(self.now + timedelta(seconds=60)))
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 1)
        self.assertEqual(self.conn.execute('SELECT status FROM credit_holds WHERE usage_id=?',
                                          (good,)).fetchone()[0], 'reconciliation_required')
        self.conn.execute('DROP TRIGGER fail_held_recovery')
        self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now + timedelta(seconds=61)), 100)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM credit_hold_watch').fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM credit_holds WHERE status='reconciliation_required'").fetchone()[0], 101)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM credit_ledger').fetchone()[0], 0)

    def test_legacy_unknown_timestamps_do_not_block_valid_watch_expiry(self):
        invalid = self.add_rows(100, created='0000-invalid')
        good = self.add_rows()[0]
        self.conn.execute("UPDATE credit_holds SET status='held'")
        self.conn.execute('DROP TABLE credit_hold_watch')
        with self.assertLogs('luomoapi.credits', level='ERROR') as captured:
            recovery.initialize_dispatch_schema(self.conn)
        self.assertEqual(len(captured.output), 100)
        self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 1)
        self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 0)
        self.assertEqual(self.conn.execute('SELECT status FROM credit_holds WHERE usage_id=?',
                                          (good,)).fetchone()[0], 'reconciliation_required')
        rows = self.conn.execute('SELECT * FROM credit_hold_watch ORDER BY usage_id').fetchall()
        self.assertEqual([r['usage_id'] for r in rows], invalid)
        self.assertTrue(all(r['last_error'] for r in rows))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM credit_holds WHERE status='held'").fetchone()[0], 100)

    def test_watch_backoff_failure_isolated_from_other_records_and_both_entry_points(self):
        first, broken, last = self.add_rows(3, log_state='dispatched', dispatch='dispatched')
        self.watch([first, broken, last])
        original = dict(self.conn.execute('SELECT * FROM credit_hold_watch WHERE usage_id=?', (broken,)).fetchone())
        self.conn.execute(f"""CREATE TRIGGER fail_watch_review BEFORE UPDATE ON credit_holds
            WHEN OLD.usage_id={broken} BEGIN SELECT RAISE(ABORT,'review failed'); END""")
        self.conn.execute(f"""CREATE TRIGGER fail_watch_backoff BEFORE UPDATE ON credit_hold_watch
            WHEN OLD.usage_id={broken} BEGIN SELECT RAISE(ABORT,'watch backoff failed'); END""")
        self.conn.commit()
        for entry in (ledger.recover_stale_usage, ledger.recover_stale_holds):
            with patch.object(ledger, 'datetime', wraps=datetime) as clock, \
                 self.assertLogs('luomoapi.credits', level='ERROR') as captured:
                clock.now.return_value = self.now
                if entry is ledger.recover_stale_holds:
                    self.conn.execute('BEGIN IMMEDIATE')
                self.assertEqual(entry(self.conn, force=True), 2 if entry is ledger.recover_stale_usage else 0)
                self.conn.commit()
            self.assertEqual(len(captured.output), 2)
            self.assertTrue(all(f'usage_id={broken} request_id=queue-{broken}' in line for line in captured.output))
            self.assertEqual(dict(self.conn.execute('SELECT * FROM credit_hold_watch WHERE usage_id=?', (broken,)).fetchone()), original)
            self.assertEqual([r[0] for r in self.conn.execute("SELECT usage_id FROM credit_holds WHERE status='reconciliation_required' ORDER BY usage_id")], [first, last])
        self.conn.execute('DROP TRIGGER fail_watch_review')
        self.conn.execute('DROP TRIGGER fail_watch_backoff')
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM credit_hold_watch').fetchone()[0], 0)

    def test_watch_repairs_nonheld_rows_without_rewriting_evidence(self):
        for status in ('reconciliation_required', 'settled', 'released'):
            usage_id = self.add_rows(log_state='dispatched', dispatch='dispatched')[0]
            self.watch([usage_id])
            self.conn.execute('UPDATE credit_holds SET status=? WHERE usage_id=?', (status, usage_id))
            hold = dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone())
            record = dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone())
            self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 0)
            self.assertEqual(dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone()), hold)
            self.assertEqual(dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone()), record)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM credit_hold_watch').fetchone()[0], 0)

    def test_watch_transition_rolls_back_if_log_update_or_dequeue_fails(self):
        for table, operation in (('api_usage_logs', 'UPDATE'), ('credit_hold_watch', 'DELETE')):
            with self.subTest(table=table):
                usage_id = self.add_rows(log_state='dispatched', dispatch='dispatched')[0]
                self.watch([usage_id])
                id_column = 'id' if table == 'api_usage_logs' else 'usage_id'
                self.conn.execute(f"""CREATE TRIGGER fail_transition BEFORE {operation} ON {table}
                    WHEN OLD.{id_column}={usage_id} BEGIN SELECT RAISE(ABORT,'transition failed'); END""")
                hold = dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone())
                record = dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone())
                with self.assertLogs('luomoapi.credits', level='ERROR'):
                    self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 0)
                self.assertEqual(dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone()), hold)
                self.assertEqual(dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone()), record)
                watch = self.conn.execute('SELECT * FROM credit_hold_watch WHERE usage_id=?', (usage_id,)).fetchone()
                self.assertEqual(watch['attempts'], 1)
                self.assertEqual(watch['last_error'], 'transition failed')
                self.conn.execute('DROP TRIGGER fail_transition')
                self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now + timedelta(seconds=61)), 1)
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM credit_hold_watch').fetchone()[0], 0)

    def test_watch_migration_is_atomic_and_excludes_pending_and_nonheld_holds(self):
        pending = self.add_rows(2, queued=True)
        expected = self.add_rows(2, log_state=None)
        review = self.add_rows()[0]
        self.conn.execute("UPDATE credit_holds SET status='held' WHERE usage_id<>?", (review,))
        self.conn.execute('DROP TABLE credit_hold_watch')
        self.conn.commit()
        self.conn.execute('BEGIN IMMEDIATE')
        enqueue = recovery.enqueue_hold_watch

        def fail_second(conn, usage_id, created_at, **kwargs):
            if usage_id == expected[-1]:
                raise sqlite3.OperationalError('migration interrupted')
            return enqueue(conn, usage_id, created_at, **kwargs)

        with patch.object(recovery, 'enqueue_hold_watch', side_effect=fail_second):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'migration interrupted'):
                recovery.initialize_dispatch_schema(self.conn)
        self.conn.rollback()
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='credit_hold_watch'").fetchone())
        self.conn.execute('BEGIN IMMEDIATE')
        recovery.initialize_dispatch_schema(self.conn)
        self.conn.commit()
        self.assertEqual([r[0] for r in self.conn.execute('SELECT usage_id FROM credit_hold_watch ORDER BY usage_id')], expected)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT usage_id FROM usage_dispatch_pending ORDER BY usage_id')], pending)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM credit_ledger').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_watch_due_time_uses_exact_utc_microseconds_and_retains_retry_evidence(self):
        for created in ('2026-11-09T23:44:00+00:00', '2026-11-09T23:44:00.123456Z',
                        '2026-11-09 23:44:00', '2026-11-10T07:44:00+08:00',
                        '2026-11-10T00:44:00.000000+01:00'):
            usage_id = self.add_rows(log_state='dispatched', dispatch='dispatched', created=created)[0]
            self.watch([usage_id])
            row = self.conn.execute('SELECT * FROM credit_hold_watch WHERE usage_id=?', (usage_id,)).fetchone()
            due = recovery.parse_timestamp(created) + timedelta(minutes=15)
            self.assertEqual(row['due_at'], recovery.timestamp(due))
            self.assertRegex(row['due_at'], r'\.\d{6}\+00:00$')
            # Preserve an existing cooldown on idempotent lower-level markers.
            self.conn.execute("UPDATE credit_hold_watch SET due_at=?,attempts=3,last_error='retry' WHERE usage_id=?",
                (recovery.timestamp(self.now + timedelta(seconds=60)), usage_id))
            before = dict(self.conn.execute('SELECT * FROM credit_hold_watch WHERE usage_id=?', (usage_id,)).fetchone())
            ledger.mark_credit_dispatched(self.conn, usage_id)
            self.assertEqual(dict(self.conn.execute('SELECT * FROM credit_hold_watch WHERE usage_id=?', (usage_id,)).fetchone()), before)

    def test_phase_limits_and_total_selected_rows_are_bounded(self):
        self.add_rows(150, queued=True)
        sent = self.add_rows(150, log_state='dispatched', dispatch='dispatched')
        self.watch(sent)
        traced = []
        self.conn.set_trace_callback(traced.append)
        try:
            self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 200)
        finally:
            self.conn.set_trace_callback(None)
        pending_selects = [sql for sql in traced if sql.startswith('SELECT usage_id, request_id FROM usage_dispatch_pending')]
        held_selects = [sql for sql in traced if sql.startswith('SELECT usage_id FROM credit_hold_watch')]
        self.assertEqual(len(pending_selects), 1)
        self.assertEqual(len(held_selects), 1)
        for sql in pending_selects + held_selects:
            self.assertTrue(sql.rstrip().endswith('LIMIT 100'), sql)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], 50)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM credit_holds WHERE status='held'").fetchone()[0], 50)

    def test_watch_orders_equal_due_times_by_id_with_one_shared_limit(self):
        later = self.add_rows(40, dispatch='dispatched', created='2026-11-09T23:44:01.000000+00:00')
        earlier = self.add_rows(120, dispatch='dispatched')
        self.watch(later + earlier)
        params = (recovery.timestamp(self.now), 100)
        rows = self.conn.execute(recovery.WATCH_QUERY, params).fetchall()
        self.assertEqual([r[0] for r in rows], earlier[:100])
        self.assertEqual(len({r[0] for r in rows}), 100)
        self.conn.executemany('DELETE FROM credit_hold_watch WHERE usage_id=?', ((r[0],) for r in rows))
        self.assertEqual([r[0] for r in self.conn.execute(recovery.WATCH_QUERY, params)], earlier[100:] + later)

    def query_steps(self, query):
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return 0

        self.conn.set_progress_handler(progress, 1)
        try:
            rows = self.conn.execute(query, (recovery.timestamp(self.now), 100)).fetchall()
        finally:
            self.conn.set_progress_handler(None, 0)
        return [r[0] for r in rows], steps

    def test_both_due_indexes_keep_vm_growth_below_two_with_100k_watch_rows(self):
        for due in (True, False):
            with self.subTest(due=due):
                self.conn.execute('DELETE FROM usage_dispatch_pending')
                self.conn.execute('DELETE FROM credit_hold_watch')
                self.conn.execute('DELETE FROM credit_holds')
                self.conn.execute('DELETE FROM api_usage_logs')
                expected_pending = self.add_rows(5, queued=True)
                expected_watch = self.add_rows(5, log_state='dispatched', dispatch='dispatched')
                self.watch(expected_watch)
                created = self.old if due else recovery.timestamp(self.now + timedelta(days=1))
                self.watch(self.add_rows(1000, log_state='dispatched', dispatch='dispatched', created=created))
                small = [self.query_steps(q) for q in (recovery.PENDING_QUERY, recovery.WATCH_QUERY)]
                self.watch(self.add_rows(99_000, log_state='dispatched', dispatch='dispatched', created=created))
                # A large not-yet-due pending prefix is independent of watch.
                self.add_rows(100_000, queued=True)
                self.conn.execute('UPDATE usage_dispatch_pending SET next_attempt_at=? WHERE usage_id>?',
                    (recovery.timestamp(self.now + timedelta(days=1)), expected_pending[-1]))
                large = [self.query_steps(q) for q in (recovery.PENDING_QUERY, recovery.WATCH_QUERY)]
                self.assertEqual(small[0][0], expected_pending)
                self.assertEqual(small[1][0][:5], expected_watch)
                self.assertEqual(len(small[1][0]), 100 if due else 5)
                for query, index, before, after in zip((recovery.PENDING_QUERY, recovery.WATCH_QUERY),
                        ('idx_usage_dispatch_pending_next', 'idx_credit_hold_watch_due'), small, large):
                    self.assertEqual(after[0], before[0])
                    self.assertGreater(before[1], 0)
                    self.assertLessEqual(after[1], before[1] * 2, (query, before[1], after[1]))
                    plan = '\n'.join(r[3] for r in self.conn.execute('EXPLAIN QUERY PLAN ' + query,
                        (recovery.timestamp(self.now), 100)))
                    self.assertIn(index, plan)
                    self.assertNotIn('SCAN ', plan)
                    self.assertNotIn('USE TEMP B-TREE', plan)

    def test_failed_queue_without_hold_escalates_at_limit_without_releasing_quota(self):
        usage_id = self.add_rows(queued=True)[0]
        self.conn.execute('DELETE FROM credit_holds WHERE usage_id=?', (usage_id,))
        before = dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone())
        with patch.object(ledger, '_complete_stale_unsent', side_effect=RuntimeError('business cleanup failed')), \
             self.assertLogs('luomoapi.credits', level='ERROR') as captured:
            for iteration in range(recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS):
                self.assertEqual(recovery.recover_dispatch_queue(self.conn,
                    self.now + timedelta(seconds=61 * iteration)), 0)
                if iteration < recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS - 1:
                    queued = self.conn.execute('SELECT attempts FROM usage_dispatch_pending WHERE usage_id=?', (usage_id,)).fetchone()
                    self.assertEqual(queued['attempts'], iteration + 1)
                    self.assertNotEqual(self.conn.execute('SELECT credit_reason FROM api_usage_logs WHERE id=?',
                        (usage_id,)).fetchone()[0], 'dispatch_recovery_failed')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], 0)
        record = dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone())
        self.assertEqual((record['credit_status'], record['credit_reason']),
                         ('reconciliation_required', 'dispatch_recovery_failed'))
        self.assertEqual({k: v for k, v in record.items() if k not in {'credit_status', 'credit_reason'}},
                         {k: v for k, v in before.items() if k not in {'credit_status', 'credit_reason'}})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM credit_ledger').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT reserved_units FROM credit_wallets WHERE user_id=1').fetchone()[0], 0)
        self.assertTrue(any('dispatch_recovery_escalated' in line and 'last_error=business cleanup failed' in line
                            for line in captured.output))

    def test_retry_limit_updates_existing_review_reason_preserving_receipts(self):
        usage_id = self.add_rows(queued=True)[0]
        self.conn.execute("""UPDATE credit_holds SET actual_units=12,usage_json='{"receipt":12}',
            reason='old review' WHERE usage_id=?""", (usage_id,))
        self.conn.execute('UPDATE usage_dispatch_pending SET attempts=? WHERE usage_id=?',
                          (recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS - 1, usage_id))
        hold = dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone())
        with self.assertLogs('luomoapi.credits', level='ERROR'):
            self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 0)
        after = dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone())
        self.assertEqual(after['reason'], 'dispatch_recovery_failed')
        self.assertEqual({k: v for k, v in after.items() if k not in {'reason', 'updated_at'}},
                         {k: v for k, v in hold.items() if k not in {'reason', 'updated_at'}})
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], 0)

    def test_retry_limit_preserves_terminal_hold_and_log(self):
        for status in ('settled', 'released'):
            with self.subTest(status=status):
                usage_id = self.add_rows(queued=True)[0]
                self.conn.execute('UPDATE credit_holds SET status=?,actual_units=0 WHERE usage_id=?', (status, usage_id))
                self.conn.execute('UPDATE usage_dispatch_pending SET attempts=?,last_error=? WHERE usage_id=?',
                    (recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS, 'prior failure', usage_id))
                hold = dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone())
                log = dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone())
                with self.assertLogs('luomoapi.credits', level='ERROR'):
                    self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 0)
                self.assertEqual(dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone()), hold)
                self.assertEqual(dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone()), log)
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], 0)
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM credit_ledger').fetchone()[0], 0)

    def test_retry_limit_still_repairs_completed_or_nonreserved_queue_entries(self):
        for state, completed in (('dispatched', None), (None, None), ('reserved', self.old)):
            with self.subTest(state=state, completed=completed):
                usage_id = self.add_rows(queued=True)[0]
                self.conn.execute('UPDATE usage_dispatch_pending SET attempts=? WHERE usage_id=?',
                    (recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS, usage_id))
                self.conn.execute('UPDATE api_usage_logs SET dispatch_state=?,completed_at=? WHERE id=?',
                    (state, completed, usage_id))
                hold = dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone())
                log = dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone())
                self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 0)
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], 0)
                self.assertEqual(dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone()), hold)
                self.assertEqual(dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone()), log)

    def test_normalized_queue_and_exact_legacy_time_rechecks_preserve_originals(self):
        eligible = ('2026-11-09T23:44:00+00:00', '2026-11-09T23:44:00.123456Z',
                    '2026-11-09 23:44:00', '2026-11-10T07:44:00+08:00',
                    '2026-11-10T00:44:00.000000+01:00')
        ineligible = ('2026-11-09T18:50:00-05:00', '2026-11-09T23:45:00.000001Z',
                      'invalid timestamp', 'now', '', '2461354.5')
        for value in eligible + ineligible:
            for changed_field in ('created', 'started'):
                with self.subTest(value=value, changed_field=changed_field):
                    usage_id = self.add_rows(queued=True, **{changed_field: value})[0]
                    before = dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone())
                    queue = self.conn.execute('SELECT * FROM usage_dispatch_pending WHERE usage_id=?', (usage_id,)).fetchone()
                    self.assertEqual(queue['created_at'], self.old)
                    self.assertEqual(queue['next_attempt_at'], '2026-11-09T23:59:00.000000+00:00')
                    with patch.object(ledger, 'datetime', wraps=datetime) as clock:
                        clock.now.return_value = self.now
                        self.assertEqual(ledger._recover_stale_holds(self.conn), int(value in eligible))
                    after = self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone()
                    self.assertEqual(after['created_at'], before['created_at'])
                    if value not in eligible:
                        self.assertEqual(dict(after), before)
                    self.conn.execute('DELETE FROM usage_dispatch_pending WHERE usage_id=?', (usage_id,))
        # An absent start time still falls back to the original creation time.
        usage_id = self.add_rows(queued=True)[0]
        self.conn.execute('UPDATE api_usage_logs SET started_at=NULL WHERE id=?', (usage_id,))
        with patch.object(ledger, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = self.now
            self.assertEqual(ledger._recover_stale_holds(self.conn), 1)

    def test_retry_time_controls_candidates_and_log_time_is_rechecked(self):
        usage_id = self.add_rows(queued=True)[0]
        self.assertEqual(self.steps()[0], [usage_id])
        self.conn.execute('UPDATE usage_dispatch_pending SET next_attempt_at=? WHERE usage_id=?',
            (recovery.timestamp(self.now + timedelta(seconds=60)), usage_id))
        self.assertEqual(self.steps()[0], [])
        later = self.now + timedelta(seconds=61)
        self.assertEqual([row[0] for row in self.conn.execute(recovery.PENDING_QUERY, (recovery.timestamp(later), 100))], [usage_id])
        self.conn.execute('UPDATE credit_holds SET created_at=? WHERE usage_id=?', (self.now.isoformat(), usage_id))
        with patch.object(ledger, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = later
            self.assertEqual(ledger._recover_stale_holds(self.conn), 0)
        self.assertEqual(self.conn.execute('SELECT status FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone()[0], 'reconciliation_required')
        self.assertIsNone(self.conn.execute('SELECT completed_at FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone()[0])

    def test_populated_legacy_held_migration_is_idempotent_and_keeps_freeze(self):
        usage_id = self.add_rows(log_state=None)[0]
        self.conn.execute("UPDATE credit_holds SET status='held',amount_units=1000 WHERE usage_id=?", (usage_id,))
        self.conn.execute('UPDATE credit_wallets SET balance_units=10000,reserved_units=1000 WHERE user_id=1')
        hold = dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone())
        log = dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone())
        self.conn.execute('DROP TABLE credit_hold_watch')
        ledger.initialize_credit_schema(self.conn)
        ledger.initialize_credit_schema(self.conn)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone()), hold)
        self.assertEqual(dict(self.conn.execute('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,)).fetchone()), log)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], 0)
        with patch.object(ledger, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = self.now
            self.assertEqual(ledger._recover_stale_holds(self.conn), 1)
            self.assertEqual(ledger._recover_stale_holds(self.conn), 0)
        row = self.conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,)).fetchone()
        self.assertEqual((row['status'], row['dispatch_state']), ('reconciliation_required', 'reserved'))
        self.assertEqual(self.conn.execute('SELECT reserved_units FROM credit_wallets WHERE user_id=1').fetchone()[0], 1000)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE kind='credit_release'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.conn.execute('PRAGMA foreign_key_check').fetchall(), [])


class DirectLedgerQueueTests(unittest.TestCase):
    now = QueueQueryTests.now
    old = QueueQueryTests.old

    def setUp(self):
        QueueQueryTests.setUp(self)
        ledger.add_credit(self.conn, 1, 10000, reference='direct-fund', actor_id=None,
                          kind='admin_credit', note='direct reserve fixture')
        self.conn.commit()

    def new_usage(self, *, state=None, created=None):
        created = recovery.timestamp(self.now) if created is None else created
        usage_id = self.conn.execute("""INSERT INTO api_usage_logs
            (created_at,user_id,method,public_path,request_id,dispatch_state)
            VALUES (?,1,'POST','v1/responses','direct-request',?)""", (created, state)).lastrowid
        self.conn.commit()
        return usage_id

    def reserve(self, usage_id):
        if not self.conn.in_transaction:
            self.conn.execute('BEGIN IMMEDIATE')
        with patch.object(ledger, 'now_iso', return_value=recovery.timestamp(self.now)):
            ledger.reserve_credit(self.conn, usage_id, 1, 80, '{}')

    def row(self, table, usage_id):
        column = 'id' if table == 'api_usage_logs' else 'usage_id'
        row = self.conn.execute(f'SELECT * FROM {table} WHERE {column}=?', (usage_id,)).fetchone()
        return dict(row) if row else None

    def test_direct_reserve_enqueues_in_the_hold_transaction_without_inventing_dispatch(self):
        usage_id = self.new_usage()
        self.reserve(usage_id)
        queued = self.row('usage_dispatch_pending', usage_id)
        self.assertEqual((queued['usage_id'], queued['request_id'], queued['created_at'], queued['next_attempt_at']),
                         (usage_id, 'direct-request', recovery.timestamp(self.now), recovery.timestamp(self.now)))
        self.assertEqual((self.row('credit_holds', usage_id)['status'],
                          self.row('credit_holds', usage_id)['dispatch_state']), ('held', 'reserved'))
        self.assertIsNone(self.row('api_usage_logs', usage_id)['dispatch_state'])
        self.assertEqual(ledger.wallet_snapshot(self.conn, 1)['reserved_units'], 80)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIsNone(self.row('credit_holds', usage_id))
        self.assertIsNone(self.row('usage_dispatch_pending', usage_id))
        self.assertEqual(ledger.wallet_snapshot(self.conn, 1)['reserved_units'], 0)

    def test_repeated_reserve_and_enqueue_preserve_one_row_and_all_retry_evidence(self):
        usage_id = self.new_usage(state='reserved')
        self.reserve(usage_id)
        self.conn.execute("""UPDATE usage_dispatch_pending SET attempts=3,last_error='prior failure',
            next_attempt_at=? WHERE usage_id=?""", (recovery.timestamp(self.now + timedelta(hours=1)), usage_id))
        queued = self.row('usage_dispatch_pending', usage_id)
        self.reserve(usage_id)
        recovery.enqueue_usage(self.conn, usage_id, 'different retry value',
                               recovery.timestamp(self.now + timedelta(hours=2)), review_only=True)
        self.assertEqual(self.row('usage_dispatch_pending', usage_id), queued)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE kind='credit_freeze'").fetchone()[0], 1)
        self.assertEqual(ledger.wallet_snapshot(self.conn, 1)['reserved_units'], 80)
        ledger.mark_credit_dispatched(self.conn, usage_id)
        self.reserve(usage_id)
        self.assertIsNone(self.row('usage_dispatch_pending', usage_id))
        self.assertIsNotNone(self.row('credit_hold_watch', usage_id))

    def test_queue_insert_failure_rolls_back_hold_freeze_and_ledger(self):
        usage_id = self.new_usage()
        self.conn.execute("""CREATE TRIGGER fail_direct_enqueue BEFORE INSERT ON usage_dispatch_pending
            BEGIN SELECT RAISE(ABORT,'direct enqueue failed'); END""")
        self.conn.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'direct enqueue failed'):
            self.reserve(usage_id)
        self.conn.rollback()
        self.assertIsNone(self.row('credit_holds', usage_id))
        self.assertIsNone(self.row('usage_dispatch_pending', usage_id))
        self.assertEqual(ledger.wallet_snapshot(self.conn, 1)['reserved_units'], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE kind='credit_freeze'").fetchone()[0], 0)

    def test_stale_direct_hold_is_reviewed_even_when_log_and_queue_are_recent(self):
        for state in (None, 'unknown', 'dispatched'):
            for attempts in (0, recovery.MAX_DISPATCH_RECOVERY_ATTEMPTS):
                with self.subTest(state=state, attempts=attempts):
                    usage_id = self.new_usage(state=state)
                    self.reserve(usage_id)
                    self.conn.execute('UPDATE credit_holds SET created_at=? WHERE usage_id=?',
                                      (recovery.timestamp(self.now - timedelta(minutes=16)), usage_id))
                    self.conn.execute('UPDATE usage_dispatch_pending SET attempts=? WHERE usage_id=?', (attempts, usage_id))
                    hold = self.row('credit_holds', usage_id)
                    log = self.row('api_usage_logs', usage_id)
                    wallet = ledger.wallet_snapshot(self.conn, 1)
                    with self.assertLogs('luomoapi.credits', level='ERROR'):
                        self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 0)
                    after = self.row('credit_holds', usage_id)
                    after_log = self.row('api_usage_logs', usage_id)
                    self.assertEqual((after['status'], after['reason']), ('reconciliation_required', 'dispatch_conflict'))
                    self.assertEqual((after_log['credit_status'], after_log['credit_reason']),
                                     ('reconciliation_required', 'dispatch_conflict'))
                    self.assertEqual({k: v for k, v in after.items() if k not in {'status', 'reason', 'updated_at'}},
                                     {k: v for k, v in hold.items() if k not in {'status', 'reason', 'updated_at'}})
                    self.assertEqual({k: v for k, v in after_log.items() if k not in {'credit_status', 'credit_reason'}},
                                     {k: v for k, v in log.items() if k not in {'credit_status', 'credit_reason'}})
                    self.assertEqual(ledger.wallet_snapshot(self.conn, 1), wallet)
                    self.assertIsNone(self.row('usage_dispatch_pending', usage_id))
                    self.assertIsNone(self.row('credit_hold_watch', usage_id))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE kind IN ('credit_release','usage_charge')").fetchone()[0], 0)

    def test_recent_direct_hold_defers_then_reviews_without_releasing_money(self):
        usage_id = self.new_usage(created=recovery.timestamp(self.now - timedelta(minutes=16)))
        self.reserve(usage_id)
        before = self.row('credit_holds', usage_id)
        wallet = ledger.wallet_snapshot(self.conn, 1)
        self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 0)
        self.assertEqual(self.row('credit_holds', usage_id), before)
        self.assertEqual(self.row('usage_dispatch_pending', usage_id)['next_attempt_at'],
                         recovery.timestamp(self.now + timedelta(seconds=ledger.RECOVERY_RETRY_SECONDS)))
        with self.assertLogs('luomoapi.credits', level='ERROR'):
            self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now + timedelta(minutes=15)), 0)
        self.assertEqual(self.row('credit_holds', usage_id)['reason'], 'dispatch_conflict')
        self.assertIsNone(self.row('usage_dispatch_pending', usage_id))
        self.assertEqual(ledger.wallet_snapshot(self.conn, 1), wallet)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE kind='credit_release'").fetchone()[0], 0)

    def test_stale_hold_with_recent_reserved_log_cannot_release(self):
        usage_id = self.new_usage(state='reserved')
        self.reserve(usage_id)
        self.conn.execute('UPDATE credit_holds SET created_at=? WHERE usage_id=?',
                          (recovery.timestamp(self.now - timedelta(minutes=16)), usage_id))
        self.conn.execute('UPDATE usage_dispatch_pending SET next_attempt_at=? WHERE usage_id=?',
                          (recovery.timestamp(self.now), usage_id))
        wallet = ledger.wallet_snapshot(self.conn, 1)
        self.assertEqual(recovery.recover_dispatch_queue(self.conn, self.now), 0)
        self.assertEqual(self.row('credit_holds', usage_id)['status'], 'held')
        self.assertIsNotNone(self.row('usage_dispatch_pending', usage_id))
        self.assertIsNone(self.row('api_usage_logs', usage_id)['completed_at'])
        self.assertEqual(ledger.wallet_snapshot(self.conn, 1), wallet)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE kind='credit_release'").fetchone()[0], 0)


class QueueMigrationTests(fixture.BackendCase):
    def test_main_held_holds_backfill_watch_once_without_releasing_money(self):
        path = Path(self.directory.name) / 'main-held.sqlite'
        created = datetime.now(timezone.utc)
        created_at = recovery.timestamp(created)
        with closing(db.connect_database(path)) as conn, conn:
            conn.executescript(legacy_main_schema.DB_SCHEMA)
            for statement in legacy_main_schema.LEDGER_SCHEMA:
                conn.execute(statement)
            # main adds these usage columns in initialize_credit_schema.
            conn.execute('ALTER TABLE api_usage_logs ADD COLUMN dispatch_state TEXT')
            conn.execute("""INSERT INTO users(id,username,email,password_hash,created_at,updated_at)
                VALUES (1,'legacy-held','legacy-held@example.test','unused',?,?)""", (created_at, created_at))
            conn.execute('INSERT INTO credit_wallets VALUES (1,10000,4000,?)', (created_at,))
            for state in ('reserved', 'dispatched', 'reserved', 'dispatched'):
                usage_id = conn.execute("""INSERT INTO api_usage_logs
                    (created_at,started_at,user_id,method,public_path,dispatch_state)
                    VALUES (?,?,1,'POST','history',NULL)""", (created_at, created_at)).lastrowid
                conn.execute("""INSERT INTO credit_holds
                    (usage_id,user_id,amount_units,status,dispatch_state,pricing_snapshot,reason,created_at,updated_at)
                    VALUES (?,1,1000,'held',?,'{}','historical',?,?)""", (usage_id, state, created_at, created_at))
            holds_before = [dict(r) for r in conn.execute('SELECT * FROM credit_holds ORDER BY usage_id')]
            logs_before = [dict(r) for r in conn.execute('SELECT * FROM api_usage_logs ORDER BY id')]
            wallet_before = dict(conn.execute('SELECT * FROM credit_wallets').fetchone())
        with patch.object(db, 'settings', replace(db.settings, database_path=path)), \
             patch.object(db, 'connect', side_effect=lambda: db.connect_database(path)), \
             patch.object(ledger, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = created
            db.init_db()
            with closing(db.connect_database(path)) as conn, conn:
                watched = [dict(r) for r in conn.execute('SELECT * FROM credit_hold_watch ORDER BY usage_id')]
                self.assertEqual([r['usage_id'] for r in watched], [r['usage_id'] for r in holds_before])
                self.assertTrue(all(r['due_at'] == recovery.timestamp(created + timedelta(minutes=15)) for r in watched))
                self.assertEqual([dict(r) for r in conn.execute('SELECT * FROM credit_holds ORDER BY usage_id')], holds_before)
                for before, after in zip(logs_before, conn.execute('SELECT * FROM api_usage_logs ORDER BY id')):
                    for key, value in before.items():
                        self.assertEqual(after[key], value)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], 0)
                # Retry metadata survives restart, and newly inserted held rows
                # outside the enqueue paths cannot trigger another backfill.
                conn.execute("UPDATE credit_hold_watch SET attempts=2,last_error='keep evidence' WHERE usage_id=1")
                watched = [dict(r) for r in conn.execute('SELECT * FROM credit_hold_watch ORDER BY usage_id')]
                later_id = conn.execute("""INSERT INTO api_usage_logs(created_at,user_id,method,public_path)
                    VALUES (?,1,'POST','after-migration')""", (created_at,)).lastrowid
                conn.execute("""INSERT INTO credit_holds
                    (usage_id,user_id,amount_units,status,pricing_snapshot,reason,created_at,updated_at)
                    VALUES (?,1,0,'held','{}','after migration',?,?)""", (later_id, created_at, created_at))
            db.init_db()
            with closing(db.connect_database(path)) as conn:
                self.assertEqual([dict(r) for r in conn.execute('SELECT * FROM credit_hold_watch ORDER BY usage_id')], watched)
                self.assertEqual(dict(conn.execute('SELECT * FROM credit_wallets').fetchone()), wallet_before)
                clock.now.return_value = created + timedelta(minutes=15, microseconds=1)
                self.assertEqual(ledger.recover_stale_usage(conn, force=True), 4)
                self.assertEqual(ledger.recover_stale_usage(conn, force=True), 0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM credit_hold_watch').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT status FROM credit_holds WHERE usage_id=?', (later_id,)).fetchone()[0], 'held')
                for before in holds_before:
                    after = dict(conn.execute('SELECT * FROM credit_holds WHERE usage_id=?', (before['usage_id'],)).fetchone())
                    self.assertEqual((after['status'], after['reason']), ('reconciliation_required', 'stale_hold'))
                    self.assertEqual({k: v for k, v in after.items() if k not in {'status', 'reason', 'updated_at'}},
                                     {k: v for k, v in before.items() if k not in {'status', 'reason', 'updated_at'}})
                self.assertEqual(dict(conn.execute('SELECT * FROM credit_wallets').fetchone()), wallet_before)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE kind='credit_release'").fetchone()[0], 0)
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_main_database_migrates_twice_without_backfill_or_releasing_history(self):
        schema = legacy_main_schema.DB_SCHEMA
        path = Path(self.directory.name) / 'main.sqlite'
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with closing(db.connect_database(path)) as conn, conn:
            conn.executescript(schema)
            conn.execute("""INSERT INTO users(id,username,email,password_hash,created_at,updated_at)
                VALUES (1,'main','main@example.test','unused',?,?)""", (old, old))
            conn.execute("""INSERT INTO api_usage_logs
                (created_at,user_id,method,public_path,status_code,success) VALUES (?,1,'POST','historical',200,1)""", (old,))
            original = dict(conn.execute('SELECT * FROM api_usage_logs').fetchone())
        with patch.object(db, 'settings', replace(db.settings, database_path=path)), \
             patch.object(db, 'connect', side_effect=lambda: db.connect_database(path)):
            db.init_db()
            with closing(db.connect_database(path)) as conn, conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], 0)
                # Simulate historical held/unknown and pending/sent states. They
                # remain outside the queue even after another startup.
                conn.execute("UPDATE credit_wallets SET balance_units=10000,reserved_units=3000 WHERE user_id=1")
                for state in (None, 'dispatched', 'dispatched'):
                    usage_id = conn.execute("""INSERT INTO api_usage_logs
                        (created_at,started_at,user_id,method,public_path,dispatch_state)
                        VALUES (?,?,1,'POST','history',?)""", (old, old, state)).lastrowid
                    conn.execute("""INSERT INTO credit_holds
                        (usage_id,user_id,amount_units,status,dispatch_state,pricing_snapshot,reason,created_at,updated_at)
                        VALUES (?,1,1000,'reconciliation_required',?,'{}','historical',?,?)""",
                        (usage_id, 'dispatched' if usage_id == 4 else 'reserved', old, old))
                historical_holds = [dict(row) for row in conn.execute('SELECT * FROM credit_holds ORDER BY usage_id')]
            db.init_db()
        with closing(db.connect_database(path)) as conn:
            row = conn.execute('SELECT * FROM api_usage_logs WHERE id=1').fetchone()
            for key, value in original.items():
                self.assertEqual(row[key], value, key)
            self.assertIsNone(row['dispatch_state'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_dispatch_pending').fetchone()[0], 0)
            self.assertEqual([dict(row) for row in conn.execute('SELECT * FROM credit_holds ORDER BY usage_id')], historical_holds)
            self.assertEqual(conn.execute('SELECT reserved_units FROM credit_wallets WHERE user_id=1').fetchone()[0], 3000)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM credit_ledger WHERE kind='credit_release'").fetchone()[0], 0)
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            columns = {r['name'] for r in conn.execute('PRAGMA table_xinfo(credit_holds)')}
            self.assertNotIn('recovery_next_at', columns)
            self.assertNotIn('recovery_due_at', columns)
            self.assertNotIn('recovery_started_at', {r['name'] for r in conn.execute('PRAGMA table_xinfo(api_usage_logs)')})


if __name__ == '__main__':
    unittest.main()
