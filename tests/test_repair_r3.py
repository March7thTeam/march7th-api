"""Third review repairs through real routes, SQLite locks and fake upstreams."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import sqlite3
import subprocess
import threading
import unittest
from unittest.mock import patch

import httpx

import test_backend_v2 as fixture
import test_credit_gateway as gateway_fixture
import test_repair_r1 as repair_fixture
import test_repair_r2 as previous_repair
from app import ai_usage as usage, credit_ledger, db, main
from app.dispatch_recovery import enqueue_usage
from app.credit_ledger import reserve_credit, wallet_snapshot
from app.upstream_security import PinnedTarget
from app.utils.timefmt import now_iso

public_gateway = repair_fixture.public_gateway
private_gateway = importlib.import_module("app.cliproxy_gateway")


class PublicDispatchRepairTests(fixture.BackendCase):
    upstream = repair_fixture.PublicRouteRepairTests.upstream
    request_route = repair_fixture.PublicRouteRepairTests.request_route
    while_paused = previous_repair.PublicSnapshotRepairTests.while_paused

    def setUp(self):
        super().setUp()
        self.as_admin()
        self.execute("UPDATE api_keys SET rate_limit_per_minute=1,daily_quota=1,monthly_quota=1 WHERE id=?", (self.key_id,))
        self.api_id = self.execute("""INSERT INTO apis(name,slug,base_url,created_at,updated_at)
            VALUES ('r3','r3','https://catalog.example.test',?,?)""", (now_iso(), now_iso()))
        self.routes = {}
        for anonymous in (True, False):
            path = 'r3/anonymous' if anonymous else 'r3/keyed'
            route_id = self.execute("""INSERT INTO public_gateway_routes
                (name,public_path,target_api_id,target_method,target_path,enabled,allow_anonymous,created_at,updated_at)
                VALUES ('r3',?,?,'GET','/status',1,?,?,?)""",
                (path, self.api_id, int(anonymous), now_iso(), now_iso()))
            self.routes[anonymous] = (path, route_id)
        self.stack.enter_context(patch('app.http_clients.create_http_client', side_effect=lambda timeout:
            gateway_fixture.create_http_client(timeout, transport=httpx.MockTransport(self.upstream))))
        self.stack.enter_context(patch.object(public_gateway, 'validate_target_url',
            side_effect=lambda url: PinnedTarget(url, None, None)))

    def request_with_exclusive_lock(self, *, keep_lock_during_cleanup):
        reached, resume = threading.Event(), threading.Event()
        original_client = public_gateway.public_upstream_client
        original_final_check = public_gateway._final_public_route_rejection

        @asynccontextmanager
        async def paused_client(app, hostname):
            reached.set()
            if not await asyncio.to_thread(resume.wait, 10):
                raise TimeoutError('exclusive lock was not established')
            async with original_client(app, hostname) as client:
                yield client

        def short_connection():
            conn = self.open_database()
            conn.execute('PRAGMA busy_timeout=25')
            return conn

        with closing(self.open_database()) as locker:
            def final_check(route, reservation=None):
                try:
                    return original_final_check(route, reservation)
                finally:
                    if not keep_lock_during_cleanup:
                        locker.rollback()

            with patch.object(public_gateway, 'connect', short_connection), \
                 patch.object(public_gateway, 'public_upstream_client', paused_client), \
                 patch.object(public_gateway, '_final_public_route_rejection', final_check), \
                 ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.request_route, False)
                try:
                    self.assertTrue(reached.wait(10))
                    locker.execute('BEGIN EXCLUSIVE')
                    resume.set()
                    response = pending.result(timeout=10)
                finally:
                    locker.rollback()
                    resume.set()
            return response

    def assert_unsent(self):
        record = self.rows('SELECT * FROM api_usage_logs ORDER BY id')[0]
        self.assertEqual((record['dispatch_state'], record['outcome'], record['status_code']),
                         ('not_dispatched', 'gateway_error_not_dispatched', 500))
        self.assertEqual(record['request_id'], 'repair-request')
        self.assertIsNotNone(record['completed_at'])
        self.assertEqual((record['success'], record['credit_charged_units'], record['credit_reserved_units']), (0, 0, 0))
        self.assertEqual(self.rows('SELECT * FROM credit_holds'), [])
        with closing(self.open_database()) as conn:
            wallet = wallet_snapshot(conn, self.user_id)
        self.assertEqual((wallet['balance_units'], wallet['reserved_units']), (0, 0))

    def test_final_read_database_lock_returns_quota_and_finishes_unsent_log(self):
        response = self.request_with_exclusive_lock(keep_lock_during_cleanup=False)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.calls, [])
        self.assert_unsent()
        self.assertEqual(self.request_route(False).status_code, 200)
        self.assertEqual(len(self.calls), 1)

    def test_cleanup_database_lock_keeps_database_evidence_and_logs_ids(self):
        with self.assertLogs('luomoapi', level='ERROR') as captured:
            response = self.request_with_exclusive_lock(keep_lock_during_cleanup=True)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.calls, [])
        self.assertTrue(any('unsent_cleanup_failed usage_id=' in entry and 'request_id=repair-request' in entry
                            for entry in captured.output))
        record = self.rows('SELECT * FROM api_usage_logs')[0]
        self.assertEqual((record['dispatch_state'], record['request_id']), ('reserved', 'repair-request'))
        self.assertIsNone(record['completed_at'])
        self.assertEqual(self.rows('SELECT * FROM credit_holds'), [])
        old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        self.execute('UPDATE api_usage_logs SET started_at=? WHERE id=?', (old, record['id']))
        self.execute('UPDATE usage_dispatch_pending SET next_attempt_at=? WHERE usage_id=?', (old, record['id']))
        db.init_db()
        recovered = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (record['id'],))[0]
        self.assertEqual(recovered['dispatch_state'], 'not_dispatched')
        self.assertIsNotNone(recovered['completed_at'])
        self.assertEqual(self.rows('SELECT * FROM usage_dispatch_pending'), [])
        with closing(self.open_database()) as conn:
            key = conn.execute('SELECT * FROM api_keys WHERE id=?', (self.key_id,)).fetchone()
            self.assertIsNone(usage.quota_rejection(conn, key))

    def test_dispatch_marker_exception_or_false_prevents_public_send(self):
        for failure in (sqlite3.OperationalError('marker failed'), False, None):
            with self.subTest(failure=failure):
                options = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                with patch.object(public_gateway, 'mark_usage_dispatched', **options):
                    response = self.request_route(False)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(self.calls, [])
                self.assert_unsent()
        self.assertEqual(self.request_route(False).status_code, 200)
        self.assertEqual(len(self.calls), 1)

    def test_client_acquisition_error_is_unsent_and_returns_quota(self):
        @asynccontextmanager
        async def failed_client(app, hostname):
            raise RuntimeError('client unavailable')
            yield

        with patch.object(public_gateway, 'public_upstream_client', failed_client):
            response = self.request_route(False)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.calls, [])
        self.assert_unsent()
        self.assertEqual(self.request_route(False).status_code, 200)

    def test_active_and_legacy_quota_reservations_still_count(self):
        route, key, error = public_gateway._load_public_route(self.routes[False][0], 'GET', self.key)
        self.assertIsNone(error)
        reservation, rejection = public_gateway._reserve_public_usage(route, key, 'r3-pending',
            'GET', self.routes[False][0], None, None)
        self.assertIsNone(rejection)
        self.assertIsNotNone(reservation)
        self.assertEqual(self.request_route(False).status_code, 429)
        self.assertEqual(self.calls, [])
        self.execute('UPDATE api_usage_logs SET dispatch_state=NULL WHERE id=?', (reservation.id,))
        self.assertEqual(self.request_route(False).status_code, 429)

    def test_client_acquisition_transport_error_is_also_unsent(self):
        @asynccontextmanager
        async def failed_client(app, hostname):
            raise httpx.ConnectError('client unavailable')
            yield

        with patch.object(public_gateway, 'public_upstream_client', failed_client):
            response = self.request_route(False)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.calls, [])
        record = self.rows('SELECT * FROM api_usage_logs')[0]
        self.assertEqual((record['dispatch_state'], record['outcome']),
                         ('not_dispatched', 'gateway_error_not_dispatched'))
        self.assertEqual(self.request_route(False).status_code, 200)

    def test_final_configuration_rejection_has_explicit_unsent_state(self):
        response = self.while_paused(False, lambda: self.execute(
            'UPDATE public_gateway_routes SET enabled=0 WHERE id=?', (self.routes[False][1],)), stage='client')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.calls, [])
        record = self.rows('SELECT * FROM api_usage_logs')[0]
        self.assertEqual((record['dispatch_state'], record['outcome']), ('not_dispatched', 'route_disabled'))
        self.execute('UPDATE public_gateway_routes SET enabled=1 WHERE id=?', (self.routes[False][1],))
        self.assertEqual(self.request_route(False).status_code, 200)

    def test_final_check_reads_configuration_after_waiting_for_dispatch_lock(self):
        reached, resume, acquiring_lock = threading.Event(), threading.Event(), threading.Event()
        original_client = public_gateway.public_upstream_client

        @asynccontextmanager
        async def paused_client(app, hostname):
            reached.set()
            if not await asyncio.to_thread(resume.wait, 10):
                raise TimeoutError('configuration transaction was not established')
            async with original_client(app, hostname) as client:
                yield client

        def trace(sql):
            if sql == 'BEGIN IMMEDIATE':
                acquiring_lock.set()

        def traced_connection():
            conn = self.open_database()
            conn.set_trace_callback(trace)
            return conn

        with closing(self.open_database()) as editor, \
             patch.object(public_gateway, 'connect', traced_connection), \
             patch.object(public_gateway, 'public_upstream_client', paused_client), \
             ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.request_route, False)
            try:
                self.assertTrue(reached.wait(10))
                acquiring_lock.clear()
                editor.execute('BEGIN IMMEDIATE')
                editor.execute('UPDATE public_gateway_routes SET enabled=0 WHERE id=?', (self.routes[False][1],))
                resume.set()
                self.assertTrue(acquiring_lock.wait(10))
                editor.commit()
                response = pending.result(timeout=10)
            finally:
                editor.rollback()
                resume.set()
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()['error'], 'route_disabled')
        self.assertEqual(self.calls, [])
        record = self.rows('SELECT * FROM api_usage_logs')[0]
        self.assertEqual((record['dispatch_state'], record['outcome']), ('not_dispatched', 'route_disabled'))
        self.execute('UPDATE public_gateway_routes SET enabled=1 WHERE id=?', (self.routes[False][1],))
        self.assertEqual(self.request_route(False).status_code, 200)

    def test_sent_transport_failure_still_consumes_request_quota(self):
        async def failed_upstream(request):
            self.calls.append(str(request.url))
            raise httpx.ConnectError('sent request failed', request=request)

        with patch('app.http_clients.create_http_client', side_effect=lambda timeout:
            gateway_fixture.create_http_client(timeout, transport=httpx.MockTransport(failed_upstream))):
            response = self.request_route(False)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.calls), 1)
        record = self.rows('SELECT * FROM api_usage_logs')[0]
        self.assertEqual((record['dispatch_state'], record['outcome']), ('dispatched', 'upstream_error'))
        self.assertEqual(self.request_route(False).status_code, 429)

    def test_route_deleted_during_client_wait_keeps_one_nullable_route_log(self):
        path, route_id = self.routes[True]

        def remove():
            response = self.client.post(f'/admin/routes/{route_id}/delete', headers=fixture.ORIGIN)
            self.assertEqual(response.status_code, 303, response.text)

        response = self.while_paused(True, remove, stage='client')
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()['error'], 'not_found')
        self.assertEqual(self.calls, [])
        records = self.rows('SELECT * FROM api_usage_logs')
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIsNone(record['route_id'])
        self.assertEqual((record['request_id'], record['public_path'], record['outcome']),
                         ('repair-request', path, 'not_found'))
        self.assertEqual(record['error_summary'], 'Public route not found.')
        self.assertEqual((record['dispatch_state'], record['status_code'], record['success']), ('not_dispatched', 404, 0))

    def test_unsuccessful_rejection_log_insert_is_reported(self):
        with patch.object(public_gateway, 'log_usage', return_value=None), \
             self.assertLogs('luomoapi.usage', level='ERROR') as captured:
            response = self.while_paused(True, lambda: self.execute(
                'DELETE FROM public_gateway_routes WHERE id=?', (self.routes[True][1],)), stage='client')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.calls, [])
        self.assertTrue(any('public_usage_finalization_failed request_id=repair-request' in entry
                            for entry in captured.output))


class GenerationDispatchRepairTests(fixture.BackendCase):
    def setUp(self):
        super().setUp()
        self.fund()
        self.execute('UPDATE api_keys SET rate_limit_per_minute=1,daily_quota=1,monthly_quota=1 WHERE id=?', (self.key_id,))

    def assert_released(self):
        record, hold, wallet, ledger = self.accounting()
        self.assertEqual((record['dispatch_state'], record['outcome']), ('not_dispatched', 'gateway_error_not_dispatched'))
        self.assertEqual((hold['status'], hold['dispatch_state'], hold['actual_units']), ('released', 'reserved', 0))
        self.assertEqual((record['credit_reserved_units'], record['credit_charged_units']), (0, 0))
        self.assertEqual((wallet['balance_units'], wallet['reserved_units']), (gateway_fixture.FUND_UNITS, 0))
        self.assertEqual([entry for entry in ledger if entry['kind'] == 'usage_charge'], [])
        self.assertEqual(len([entry for entry in ledger
                              if entry['kind'] == 'credit_release' and entry['usage_id'] == record['id']]), 1)
        self.assertIsNotNone(record['completed_at'])

    def test_build_failure_for_both_generation_protocols_releases_credit_and_quota(self):
        client = main.app.state.codex_http_client
        for protocol in ('chat', 'responses'):
            with self.subTest(protocol=protocol):
                self.execute('UPDATE api_keys SET rate_limit_per_minute=1,daily_quota=1,monthly_quota=1 WHERE id=?', (self.key_id,))
                with patch.object(client, 'build_request', side_effect=RuntimeError('request construction failed')):
                    response = self.generate(protocol)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(self.calls, [])
                self.assert_released()
        self.assertEqual(self.generate().status_code, 200)
        self.assertEqual(len(self.calls), 1)
        self.assert_settled()

    def test_missing_client_after_reservation_releases_credit_and_quota(self):
        client = main.app.state.codex_http_client
        del main.app.state.codex_http_client
        try:
            response = self.generate()
        finally:
            main.app.state.codex_http_client = client
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.calls, [])
        self.assert_released()
        self.assertEqual(self.generate().status_code, 200)

    def test_dispatch_marker_database_lock_and_cleanup_lock_recover_after_timeout(self):
        reached, resume = threading.Event(), threading.Event()
        original_mark = private_gateway._mark_dispatch

        def short_connection():
            conn = self.open_database()
            conn.execute('PRAGMA busy_timeout=25')
            return conn

        def paused_mark(reservation):
            reached.set()
            if not resume.wait(10):
                raise TimeoutError('exclusive lock was not established')
            return original_mark(reservation)

        with closing(self.open_database()) as locker, \
             patch.object(private_gateway, 'connect', short_connection), \
             patch.object(private_gateway, '_mark_dispatch', paused_mark), \
             self.assertLogs('luomoapi', level='ERROR'), \
             ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.generate)
            try:
                self.assertTrue(reached.wait(10))
                locker.execute('BEGIN EXCLUSIVE')
                resume.set()
                response = pending.result(timeout=10)
            finally:
                locker.rollback()
                resume.set()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.calls, [])
        record, hold, wallet, _ = self.accounting()
        self.assertEqual((record['dispatch_state'], hold['dispatch_state'], hold['status']),
                         ('reserved', 'reserved', 'held'))
        self.assertGreater(wallet['reserved_units'], 0)
        old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        self.execute('UPDATE credit_holds SET created_at=?', (old,))
        self.execute('UPDATE api_usage_logs SET started_at=?', (old,))
        self.execute('UPDATE usage_dispatch_pending SET next_attempt_at=?', (old,))
        with closing(self.open_database()) as conn:
            self.assertEqual(credit_ledger.recover_stale_usage(conn, force=True), 1)
        self.assert_released()
        with closing(self.open_database()) as conn:
            key = conn.execute('SELECT * FROM api_keys WHERE id=?', (self.key_id,)).fetchone()
            self.assertIsNone(usage.quota_rejection(conn, key))
        self.assertEqual(self.generate().status_code, 200)
        rows = self.rows('SELECT * FROM api_usage_logs ORDER BY id')
        self.assertEqual((rows[0]['dispatch_state'], rows[0]['credit_status']), ('not_dispatched', 'released'))
        self.assertEqual((rows[0]['credit_reserved_units'], rows[0]['credit_charged_units']), (0, 0))
        self.assert_settled()

    def test_startup_recovers_stale_database_reservation_idempotently(self):
        client = main.app.state.codex_http_client
        with patch.object(client, 'build_request', side_effect=RuntimeError('request construction failed')), \
             patch.object(usage, 'complete_usage', return_value=False), \
             self.assertLogs('luomoapi.usage', level='ERROR'):
            response = self.generate()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.calls, [])
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.execute('UPDATE credit_holds SET created_at=?', (old,))
        self.execute('UPDATE api_usage_logs SET started_at=?', (old,))
        self.execute('UPDATE usage_dispatch_pending SET next_attempt_at=?', (old,))
        db.init_db()
        self.assert_released()
        db.init_db()
        self.assert_released()
        self.assertEqual(self.generate().status_code, 200)

    def test_cleanup_uses_two_independent_connections_with_a_short_retry(self):
        client = main.app.state.codex_http_client
        original_complete = usage.complete_usage
        connections = []

        def complete(conn, *args, **kwargs):
            connections.append(conn)
            if len(connections) == 1:
                return False
            return original_complete(conn, *args, **kwargs)

        with patch.object(client, 'build_request', side_effect=RuntimeError('request construction failed')), \
             patch.object(usage, 'complete_usage', side_effect=complete), \
             patch.object(usage.time, 'sleep') as sleep:
            response = self.generate()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.calls, [])
        self.assert_released()
        self.assertEqual(len(connections), 2)
        self.assertIsNot(connections[0], connections[1])
        sleep.assert_called_once_with(0.05)
        self.assertEqual(self.generate().status_code, 200)

    def test_dispatch_marker_exception_or_false_prevents_both_generation_sends(self):
        for protocol in ('chat', 'responses'):
            for failure in (sqlite3.OperationalError('marker failed'), False, None):
                with self.subTest(protocol=protocol, failure=failure):
                    options = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                    with patch.object(private_gateway, 'mark_usage_dispatched', **options):
                        response = self.generate(protocol)
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(self.calls, [])
                    self.assert_released()
        self.assertEqual(self.generate().status_code, 200)

    def test_cleanup_stops_after_two_failures_and_logs_both_ids(self):
        client = main.app.state.codex_http_client
        with patch.object(client, 'build_request', side_effect=RuntimeError('build failed')), \
             patch.object(usage, 'complete_usage', return_value=False) as complete, \
             patch.object(usage.time, 'sleep') as sleep, \
             self.assertLogs('luomoapi.usage', level='ERROR') as captured:
            response = self.generate()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.calls, [])
        self.assertEqual(complete.call_count, 2)
        sleep.assert_called_once_with(0.05)
        record, hold, wallet, _ = self.accounting()
        self.assertTrue(any(f'usage_id={record["id"]} request_id={record["request_id"]}' in entry
                            for entry in captured.output))
        self.assertEqual((record['dispatch_state'], hold['status']), ('reserved', 'held'))
        self.assertGreater(wallet['reserved_units'], 0)

    def test_sent_timeout_keeps_quota_and_reconciliation_freeze(self):
        client = main.app.state.codex_http_client

        async def sent_timeout(request, **kwargs):
            self.calls.append(request)
            raise httpx.ReadTimeout('upstream timed out', request=request)

        with patch.object(client, 'send', side_effect=sent_timeout):
            response = self.generate()
        self.assertEqual(response.status_code, 504)
        self.assertEqual(len(self.calls), 1)
        record, hold, wallet, _ = self.accounting()
        self.assertEqual((record['dispatch_state'], record['outcome']), ('dispatched', 'upstream_timeout'))
        self.assertEqual((hold['dispatch_state'], hold['status']), ('dispatched', 'reconciliation_required'))
        self.assertGreater(wallet['reserved_units'], 0)
        self.assertEqual(self.generate().status_code, 429)


class DatabaseRecoveryRepairTests(fixture.BackendCase):
    def setUp(self):
        super().setUp()
        self.fund()
        self.old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        self.pending_ids = []

    def pending(self):
        with closing(self.open_database()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            usage_id = conn.execute("""INSERT INTO api_usage_logs
                (created_at,started_at,user_id,api_key_id,method,public_path,request_id,
                 protocol,dispatch_state,outcome)
                VALUES (?,?,?,?,'POST','v1/chat/completions',?,'chat.completions','reserved','in_flight')""",
                (self.old, self.old, self.user_id, self.key_id, f'r4-pending-{len(self.pending_ids)}')).lastrowid
            reserve_credit(conn, usage_id, self.user_id, 1000, '{}')
            conn.execute('UPDATE credit_holds SET created_at=? WHERE usage_id=?', (self.old, usage_id))
            enqueue_usage(conn, usage_id, f'r4-pending-{len(self.pending_ids)}', self.old)
        self.pending_ids.append(usage_id)
        return usage_id

    def recover(self):
        with closing(self.open_database()) as conn:
            return credit_ledger.recover_stale_usage(conn, force=True)

    def assert_release(self, usage_id):
        record = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
        hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
        release = self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release' AND usage_id=?", (usage_id,))
        self.assertEqual((record['dispatch_state'], record['outcome'], record['credit_status']),
                         ('not_dispatched', 'gateway_error_not_dispatched', 'released'))
        self.assertIsNotNone(record['completed_at'])
        self.assertEqual((record['success'], record['credit_reserved_units'], record['credit_charged_units']), (0, 0, 0))
        self.assertEqual((hold['status'], hold['dispatch_state'], hold['actual_units']), ('released', 'reserved', 0))
        self.assertEqual(len(release), 1)
        self.assertEqual((release[0]['reference'], release[0]['delta_units'], release[0]['delta_reserved_units']),
                         (f'usage:{usage_id}:release', 0, -1000))
        self.assertEqual(self.rows("SELECT * FROM credit_ledger WHERE kind='usage_charge' AND usage_id=?", (usage_id,)), [])

    def test_mutually_reserved_pending_hold_releases_once(self):
        usage_id = self.pending()
        self.execute("UPDATE credit_holds SET status='reconciliation_required',reason='stale_hold' WHERE usage_id=?", (usage_id,))
        self.execute("UPDATE api_usage_logs SET credit_status='reconciliation_required' WHERE id=?", (usage_id,))
        self.assertEqual(self.recover(), 1)
        self.assert_release(usage_id)
        self.assertEqual(self.recover(), 0)
        with closing(self.open_database()) as conn:
            wallet = wallet_snapshot(conn, self.user_id)
            key = conn.execute('SELECT * FROM api_keys WHERE id=?', (self.key_id,)).fetchone()
            self.assertIsNone(usage.quota_rejection(conn, key))
        self.assertEqual((wallet['balance_units'], wallet['reserved_units']), (gateway_fixture.FUND_UNITS, 0))

    def test_sent_null_and_unknown_log_states_retain_freeze_and_quota(self):
        for log_state, hold_state in (('dispatched', 'reserved'), ('reserved', 'dispatched'),
                                      ('dispatched', 'dispatched'), (None, 'reserved'), ('unknown', 'reserved')):
            with self.subTest(log_state=log_state, hold_state=hold_state):
                usage_id = self.pending()
                self.execute('UPDATE api_usage_logs SET dispatch_state=? WHERE id=?', (log_state, usage_id))
                self.execute('UPDATE credit_holds SET dispatch_state=? WHERE usage_id=?', (hold_state, usage_id))
                self.assertEqual(self.recover(), 0)
                self.assertEqual(self.rows("SELECT * FROM usage_dispatch_pending WHERE usage_id=?", (usage_id,)), [])
                conflict = log_state != 'reserved' or hold_state != 'reserved'
                if conflict:
                    # Conflict removal now persists review in the first call.
                    hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
                    record = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
                    self.assertEqual((hold['status'], hold['reason']), ('reconciliation_required', 'dispatch_conflict'))
                    self.assertEqual((record['credit_status'], record['credit_reason']),
                                     ('reconciliation_required', 'dispatch_conflict'))
                if not conflict:
                    # Historical unknown dispatch evidence is enrolled once
                    # by migration, never rediscovered by runtime hold scans.
                    from app.dispatch_recovery import enqueue_hold_watch
                    with closing(self.open_database()) as conn, conn:
                        conn.execute('BEGIN IMMEDIATE')
                        enqueue_hold_watch(conn, usage_id, self.old, legacy=True)
                self.assertEqual(self.recover(), int(not conflict))
                self.assertEqual(self.recover(), 0)
                hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
                record = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (usage_id,))[0]
                self.assertEqual((hold['status'], hold['dispatch_state']), ('reconciliation_required', hold_state))
                self.assertEqual(record['dispatch_state'], log_state)
                self.assertIsNone(record['completed_at'])
                self.assertEqual(record['credit_reserved_units'], 1000)
                self.assertEqual(self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release' AND usage_id=?", (usage_id,)), [])
                with closing(self.open_database()) as conn, conn:
                    conn.execute('BEGIN IMMEDIATE')
                    result = credit_ledger.settle_credit(conn, usage_id, 0, reason='No receipt supplied.')
                self.assertEqual(result['status'], 'reconciliation_required')
        with closing(self.open_database()) as conn:
            wallet = wallet_snapshot(conn, self.user_id)
            self.assertEqual(usage._usage_count(conn, self.key_id, datetime.now(timezone.utc) - timedelta(days=1)), 5)
        self.assertEqual((wallet['balance_units'], wallet['reserved_units']), (gateway_fixture.FUND_UNITS, 5000))

    def test_recent_usage_or_recent_hold_and_completed_usage_do_not_release(self):
        for kind in ('recent_usage', 'recent_hold', 'completed'):
            with self.subTest(kind=kind):
                usage_id = self.pending()
                if kind == 'recent_usage':
                    self.execute('UPDATE api_usage_logs SET started_at=? WHERE id=?', (now_iso(), usage_id))
                elif kind == 'recent_hold':
                    self.execute('UPDATE credit_holds SET created_at=? WHERE usage_id=?', (now_iso(), usage_id))
                else:
                    self.execute('UPDATE api_usage_logs SET completed_at=? WHERE id=?', (self.old, usage_id))
                self.recover()
                hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (usage_id,))[0]
                self.assertIn(hold['status'], ('held', 'reconciliation_required'))
                self.assertIsNone(hold['actual_units'])
                self.assertEqual(self.rows("SELECT * FROM credit_ledger WHERE kind='credit_release' AND usage_id=?", (usage_id,)), [])

    def test_concurrent_connections_only_release_once(self):
        usage_id = self.pending()
        barrier = threading.Barrier(8)

        def recover():
            with closing(db.connect_database(self.path)) as conn:
                barrier.wait(timeout=10)
                return credit_ledger.recover_stale_usage(conn, force=True)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: recover(), range(8)))
        self.assertEqual(sorted(results), [0] * 7 + [1])
        self.assert_release(usage_id)
        with closing(self.open_database()) as conn:
            self.assertEqual(wallet_snapshot(conn, self.user_id)['reserved_units'], 0)

    def test_anomalous_release_reference_is_rolled_back_and_does_not_block_startup(self):
        broken, good = self.pending(), self.pending()
        self.execute("""INSERT INTO credit_ledger(user_id,reference,kind,delta_units,
            balance_after_units,reserved_after_units,usage_id,note,created_at)
            VALUES (?,?,'anomalous_fixture',0,?,2000,?,'inconsistent release reference',?)""",
            (self.user_id, f'usage:{broken}:release', gateway_fixture.FUND_UNITS, broken, self.old))
        with self.assertLogs('luomoapi.credits', level='ERROR') as captured:
            db.init_db()
        self.assertTrue(any(f'usage_id={broken} request_id=r4-pending-0' in entry for entry in captured.output))
        self.assert_release(good)
        record = self.rows('SELECT * FROM api_usage_logs WHERE id=?', (broken,))[0]
        hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (broken,))[0]
        self.assertEqual((record['dispatch_state'], hold['status']), ('reserved', 'held'))
        self.assertIsNone(record['completed_at'])
        with closing(self.open_database()) as conn:
            wallet = wallet_snapshot(conn, self.user_id)
        self.assertEqual((wallet['balance_units'], wallet['reserved_units']), (gateway_fixture.FUND_UNITS, 1000))
        self.assertEqual(self.generate().status_code, 200)

    def test_billing_evidence_on_reserved_hold_is_skipped(self):
        broken, good = self.pending(), self.pending()
        self.execute('UPDATE credit_holds SET actual_units=12 WHERE usage_id=?', (broken,))
        with self.assertLogs('luomoapi.credits', level='ERROR'):
            self.assertEqual(self.recover(), 1)
        self.assert_release(good)
        hold = self.rows('SELECT * FROM credit_holds WHERE usage_id=?', (broken,))[0]
        self.assertEqual((hold['status'], hold['actual_units']), ('held', 12))

    def test_recovery_batch_limit_is_enforced(self):
        ids = [self.pending() for _ in range(credit_ledger.RECOVERY_BATCH_SIZE + 3)]
        statements = []
        with closing(self.open_database()) as conn:
            conn.set_trace_callback(statements.append)
            self.assertEqual(credit_ledger.recover_stale_usage(conn, force=True), credit_ledger.RECOVERY_BATCH_SIZE)
        queries = [sql for sql in statements if 'FROM usage_dispatch_pending' in sql
                   and 'ORDER BY next_attempt_at' in sql]
        self.assertEqual(len(queries), 1)
        self.assertIn('LIMIT 100', queries[0])
        self.assertEqual(self.recover(), 3)
        self.assertEqual(self.recover(), 0)
        for usage_id in ids:
            self.assert_release(usage_id)

    def test_request_recovery_has_one_shared_per_minute_gate(self):
        with patch.dict(credit_ledger._last_recovery, clear=True), \
             patch.object(credit_ledger, 'monotonic', return_value=100) as clock, \
             patch.object(credit_ledger, '_recover_stale_holds', wraps=credit_ledger._recover_stale_holds) as recover:
            for _ in range(4):
                self.assertEqual(self.generate().status_code, 200)
                self.assertEqual(self.client.get('/api/developer/credits').status_code, 200)
            self.assertEqual(recover.call_count, 1)
            clock.return_value = 159
            self.assertEqual(self.generate().status_code, 200)
            self.assertEqual(recover.call_count, 1)
            clock.return_value = 160
            self.assertEqual(self.generate().status_code, 200)
            self.assertEqual(recover.call_count, 2)

    def test_failed_recovery_is_also_throttled_and_admission_continues(self):
        with patch.dict(credit_ledger._last_recovery, clear=True), \
             patch.object(credit_ledger, 'monotonic', return_value=100), \
             patch.object(credit_ledger, '_recover_stale_holds', side_effect=sqlite3.OperationalError('recovery unavailable')) as recover, \
             self.assertLogs('luomoapi.credits', level='ERROR'):
            for _ in range(3):
                self.assertEqual(self.generate().status_code, 200)
        self.assertEqual(recover.call_count, 1)

    def test_source_has_no_removed_file_mechanism(self):
        result = subprocess.run(['grep', '-rn', '--include=*.py', 'undispatched', 'app/'],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')


class ProtocolScopeRepairTests(fixture.BackendCase):
    def seed_protocols(self):
        for protocol in ('models', 'public', 'chat.completions', 'responses'):
            usage_id = fixture.AnalyticsTests.log(self)
            self.execute("UPDATE api_usage_logs SET protocol=?,outcome='succeeded' WHERE id=?", (protocol, usage_id))

    def test_mixed_protocol_pages_share_chart_card_detail_and_daily_counts(self):
        self.seed_protocols()
        self.as_admin()
        for path in ('/developer/usage', '/admin/ai-usage'):
            for days in (7, 14, 30):
                with self.subTest(path=path, days=days):
                    response = self.client.get(path, params={'days': days})
                    self.assertEqual(response.status_code, 200)
                    chart, detail = response.context['analytics'], response.context['usage']
                    self.assertEqual(chart['totals']['requests'], 2)
                    self.assertEqual(detail['summary']['requests'], 2)
                    self.assertEqual(detail['summary']['succeeded'], 2)
                    self.assertEqual(len(detail['recent']), 2)
                    self.assertEqual({row['protocol'] for row in detail['recent']}, set(usage.GENERATION_PROTOCOLS))
                    self.assertEqual(sum(row['requests'] for row in chart['daily']), 2)
                    self.assertEqual(sum(row['requests'] for row in detail['daily']), 2)
                    self.assertEqual(sum(row['requests'] for row in chart['by_model']), 2)
                    self.assertEqual(sum(row['requests'] for row in chart['by_key']), 2)

    def test_models_only_page_has_matching_empty_chart_and_details(self):
        usage_id = fixture.AnalyticsTests.log(self)
        self.execute("UPDATE api_usage_logs SET protocol='models' WHERE id=?", (usage_id,))
        response = self.client.get('/developer/usage')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['analytics']['totals']['requests'], 0)
        self.assertEqual(response.context['usage']['summary']['requests'], 0)
        self.assertEqual(response.context['usage']['recent'], [])

    def test_default_api_scopes_and_explicit_empty_scope_are_compatible(self):
        self.seed_protocols()
        self.as_admin()
        for path in ('/api/developer/usage/analytics', '/api/admin/usage/analytics'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['totals']['requests'], 4)
        response = self.client.get('/api/developer/usage')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['summary']['requests'], 2)
        self.assertEqual(usage.usage_analytics(self.user_id)['totals']['requests'], 4)
        self.assertEqual(usage.usage_analytics(self.user_id, protocols=[])['totals']['requests'], 0)
        self.assertEqual(usage.usage_analytics(self.user_id, protocols=['models'])['totals']['requests'], 1)

    def test_filtered_analytics_still_uses_both_expression_range_indexes(self):
        self.seed_protocols()
        for user_id, index in ((None, 'idx_api_usage_effective_at'), (self.user_id, 'idx_api_usage_user_effective_at')):
            statements = []

            def traced_connect():
                conn = self.open_database()
                conn.set_trace_callback(statements.append)
                return conn

            with patch.object(db, 'connect', traced_connect):
                chart = usage.usage_analytics(user_id, protocols=usage.GENERATION_PROTOCOLS)
            self.assertEqual(chart['totals']['requests'], 2)
            queries = [sql for sql in statements if 'FROM api_usage_logs' in sql]
            self.assertEqual(len(queries), 1)
            with closing(self.open_database()) as conn:
                plan = ' '.join(row['detail'] for row in conn.execute('EXPLAIN QUERY PLAN ' + queries[0]))
            self.assertIn('USING INDEX ' + index, plan)
            self.assertIn('<expr>>? AND <expr><?', plan)
            self.assertNotIn('SCAN a', plan)


if __name__ == '__main__':
    unittest.main()
