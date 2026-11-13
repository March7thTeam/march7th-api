"""Second review repairs with isolated SQLite, concurrent barriers and fake HTTP."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request

import test_backend_v2 as fixture
import test_credit_gateway as gateway_fixture
import test_repair_r1 as repair_fixture
from app.client_ip import client_identity
from app import login_throttle, main
from app.upstream_security import PinnedTarget
from app.utils.timefmt import now_iso

gateway = repair_fixture.public_gateway
usage = repair_fixture.usage_module


class PublicSnapshotRepairTests(fixture.BackendCase):
    upstream = repair_fixture.PublicRouteRepairTests.upstream
    request_route = repair_fixture.PublicRouteRepairTests.request_route

    def setUp(self):
        super().setUp()
        self.as_admin()
        self.api_id = self.execute("""INSERT INTO apis(name,slug,base_url,created_at,updated_at)
            VALUES ('snapshot','snapshot','https://catalog.example.test',?,?)""", (now_iso(), now_iso()))
        self.other_api = self.execute("""INSERT INTO apis(name,slug,base_url,created_at,updated_at)
            VALUES ('other','other','https://other.example.test',?,?)""", (now_iso(), now_iso()))
        self.routes = {}
        for anonymous in (True, False):
            path = 'snapshot/anonymous' if anonymous else 'snapshot/keyed'
            route_id = self.execute("""INSERT INTO public_gateway_routes
                (name,public_path,target_api_id,target_method,target_path,enabled,allow_anonymous,created_at,updated_at)
                VALUES ('snapshot',?,?,'GET','/status',1,?,?,?)""",
                (path, self.api_id, int(anonymous), now_iso(), now_iso()))
            self.routes[anonymous] = (path, route_id)
        self.stack.enter_context(patch('app.http_clients.create_http_client', side_effect=lambda timeout:
            gateway_fixture.create_http_client(timeout, transport=httpx.MockTransport(self.upstream))))
        self.stack.enter_context(patch.object(gateway, 'validate_target_url',
            side_effect=lambda url: PinnedTarget(url, None, None)))

    def edit_route(self, anonymous, **changes):
        path, route_id = self.routes[anonymous]
        data = dict(name='snapshot', public_path=path, target_api_id=self.api_id,
                    target_method='GET', target_path='/status', enabled='1')
        if anonymous:
            data['allow_anonymous'] = '1'
        data.update(changes)
        response = self.client.post(f'/admin/routes/{route_id}/edit', data=data, headers=fixture.ORIGIN)
        self.assertEqual(response.status_code, 303, response.text)

    def while_paused(self, anonymous, change, *, stage='dns'):
        reached, resume = threading.Event(), threading.Event()

        def pause():
            reached.set()
            if not resume.wait(10):
                raise TimeoutError('concurrent mutation did not complete')

        def paused_dns(url):
            pause()
            return PinnedTarget(url, None, None)

        original_client = gateway.public_upstream_client

        @asynccontextmanager
        async def paused_client(app, hostname):
            await asyncio.to_thread(pause)
            async with original_client(app, hostname) as client:
                yield client

        seam = 'validate_target_url' if stage == 'dns' else 'public_upstream_client'
        replacement = paused_dns if stage == 'dns' else paused_client
        with patch.object(gateway, seam, replacement), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.request_route, anonymous)
            try:
                self.assertTrue(reached.wait(10))
                change()
            finally:
                resume.set()
            return pending.result(timeout=10)

    def test_retarget_then_disable_original_during_dns_rejects_both_paths(self):
        for anonymous in self.routes:
            with self.subTest(anonymous=anonymous):
                self.execute('UPDATE apis SET enabled=1 WHERE id=?', (self.api_id,))

                def change():
                    self.edit_route(anonymous, target_api_id=self.other_api)
                    response = self.client.post(f'/apis/{self.api_id}/toggle',
                        data={'enabled': 0}, headers=fixture.ORIGIN)
                    self.assertEqual(response.status_code, 303)

                response = self.while_paused(anonymous, change)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()['error'], 'route_changed')
                self.assertEqual(response.headers['x-request-id'], 'repair-request')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows('SELECT * FROM api_usage_logs'), [])

    def test_tightening_anonymous_permission_during_dns_rejects(self):
        response = self.while_paused(True, lambda: self.edit_route(True, allow_anonymous=''))
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()['error'], 'route_changed')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows('SELECT * FROM api_usage_logs'), [])

    def test_unchanged_config_still_dispatches_anonymous_and_keyed(self):
        for anonymous in self.routes:
            self.assertEqual(self.request_route(anonymous).status_code, 200)
        self.assertEqual(len(self.calls), 2)

    def test_every_target_and_authorization_field_is_compared_without_timestamp_updates(self):
        changes = [
            ('public_gateway_routes', 'target_api_id', self.other_api),
            ('apis', 'base_url', 'https://other.example.test'),
            ('public_gateway_routes', 'target_method', 'POST'),
            ('public_gateway_routes', 'target_path', '/changed'),
            ('public_gateway_routes', 'required_scope', 'changed:read'),
            ('public_gateway_routes', 'allow_anonymous', None),
            ('public_gateway_routes', 'enabled', 0),
            ('apis', 'enabled', 0),
            ('apis', 'auth_type', 'bearer'),
            ('apis', 'secret_ref', 'CHANGED_SECRET_REF'),
        ]
        for anonymous, (path, route_id) in self.routes.items():
            for table, column, value in changes:
                with self.subTest(anonymous=anonymous, table=table, column=column):
                    route, key, error = gateway._load_public_route(path, 'GET', self.key)
                    self.assertIsNone(error)
                    target_id = self.api_id if table == 'apis' else route_id
                    original = self.rows(f'SELECT {column} FROM {table} WHERE id=?', (target_id,))[0][column]
                    replacement = 1 - original if column == 'allow_anonymous' else value
                    self.execute(f'UPDATE {table} SET {column}=? WHERE id=?', (replacement, target_id))
                    reservation, rejection = gateway._reserve_public_usage(route, key, 'snapshot', 'GET', path, None, None)
                    self.assertIsNone(reservation)
                    self.assertEqual(rejection.code, 'route_disabled' if column == 'enabled' else 'route_changed')
                    self.execute(f'UPDATE {table} SET {column}=? WHERE id=?', (original, target_id))
        self.assertEqual(self.rows('SELECT * FROM api_usage_logs'), [])

    def test_snapshot_comparison_occurs_after_acquiring_transaction_lock(self):
        for anonymous, (path, route_id) in self.routes.items():
            route, key, error = gateway._load_public_route(path, 'GET', self.key)
            self.assertIsNone(error)
            with closing(self.open_database()) as writer, ThreadPoolExecutor(max_workers=1) as pool:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('UPDATE public_gateway_routes SET target_api_id=? WHERE id=?', (self.other_api, route_id))
                reached = threading.Event()

                def reserve():
                    reached.set()
                    return gateway._reserve_public_usage(route, key, 'locked', 'GET', path, None, None)

                result = pool.submit(reserve)
                self.assertTrue(reached.wait(10))
                writer.commit()
                reservation, rejection = result.result(timeout=10)
            self.assertIsNone(reservation)
            self.assertEqual(rejection.code, 'route_changed')
        self.assertEqual(self.rows('SELECT * FROM api_usage_logs'), [])

    def test_final_check_after_client_wait_releases_reservation_and_records_rejection(self):
        self.execute('UPDATE api_keys SET rate_limit_per_minute=1,daily_quota=1,monthly_quota=1 WHERE id=?', (self.key_id,))
        for anonymous, (_, route_id) in self.routes.items():
            for table, column, changed in (
                ('apis', 'enabled', 0),
                ('public_gateway_routes', 'enabled', 0),
                ('public_gateway_routes', 'target_path', '/changed'),
                ('apis', 'secret_ref', 'CHANGED_SECRET_REF'),
            ):
                with self.subTest(anonymous=anonymous, table=table, column=column):
                    target_id = self.api_id if table == 'apis' else route_id
                    original = self.rows(f'SELECT {column} FROM {table} WHERE id=?', (target_id,))[0][column]
                    before = len(self.rows('SELECT * FROM api_usage_logs'))
                    response = self.while_paused(anonymous,
                        lambda: self.execute(f'UPDATE {table} SET {column}=? WHERE id=?', (changed, target_id)), stage='client')
                    code = 'route_disabled' if column == 'enabled' else 'route_changed'
                    self.assertEqual(response.status_code, 404, response.text)
                    self.assertEqual(response.json()['error'], code)
                    logs = self.rows('SELECT * FROM api_usage_logs ORDER BY id')
                    self.assertEqual(len(logs), before + 1)
                    record = logs[-1]
                    self.assertEqual((record['outcome'], record['success'], record['status_code']), (code, 0, 404))
                    self.assertIsNotNone(record['completed_at'])
                    self.assertIsNone(record['first_byte_ms'])
                    self.assertEqual(record['credit_charged_units'], 0)
                    self.assertEqual(self.rows('SELECT * FROM credit_holds'), [])
                    self.execute(f'UPDATE {table} SET {column}=? WHERE id=?', (original, target_id))
        self.assertEqual(self.calls, [])
        # All pre-dispatch rejections returned their request-quota reservations.
        self.assertEqual(self.request_route(False).status_code, 200)
        self.assertEqual(len(self.calls), 1)


class PasswordSourceRepairTests(fixture.BackendCase):
    token = repair_fixture.PasswordRepairTests.token

    def reset_client(self, peer):
        async def with_peer(scope, receive, send):
            await main.app({**scope, 'client': (peer, 50000)}, receive, send)

        client = TestClient(with_peer, base_url=gateway_fixture.BASE_URL, follow_redirects=False)
        self.addCleanup(client.close)
        return client

    def submit(self, client, token, **updates):
        return client.post('/reset-password', params={'token': token}, headers=fixture.ORIGIN,
            data={'new_password': 'Reset-password-2026', 'confirm_password': 'Reset-password-2026', **updates})

    def test_unknown_shared_proxy_and_trusted_proxy_without_visitor_cannot_block_valid_token(self):
        for peer in ('172.31.0.1', '127.0.0.1'):
            with self.subTest(peer=peer):
                client = self.reset_client(peer)
                with patch('app.routes.auth.hash_password') as hashing:
                    for attempt in range(45):
                        self.assertEqual(self.submit(client, f'{peer}-invalid-{attempt}').status_code, 400)
                hashing.assert_not_called()
                self.assertEqual(self.submit(client, self.token()).status_code, 303)

    def test_verified_source_limits_invalid_attempts_but_valid_tokens_bypass_source_bucket(self):
        client = self.reset_client('8.8.8.8')
        with patch('app.routes.auth.hash_password') as hashing:
            for attempt in range(login_throttle.MAX_RESET_ATTEMPTS_PER_SOURCE):
                self.assertEqual(self.submit(client, f'invalid-{attempt}').status_code, 400)
            response = self.submit(client, 'new-invalid')
        hashing.assert_not_called()
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers['retry-after']), 0)
        source_bucket = login_throttle._digest('reset-source', '8.8.8.8')
        before = self.rows('SELECT * FROM password_reset_attempts WHERE bucket_hash=?', (source_bucket,))
        token = self.token()
        self.assertEqual(self.submit(client, token).status_code, 303)
        self.assertEqual(self.rows('SELECT * FROM password_reset_attempts WHERE bucket_hash=?', (source_bucket,)), before)
        # Used tokens remain invalid, with no PBKDF2 even when source-throttled.
        with patch('app.routes.auth.hash_password') as hashing:
            self.assertEqual(self.submit(client, token).status_code, 429)
        hashing.assert_not_called()

    def test_unverified_source_still_has_per_token_limit(self):
        client = self.reset_client('172.31.0.1')
        for token in (self.token(), 'same-invalid-token'):
            with self.subTest(token_valid=token != 'same-invalid-token'), patch('app.routes.auth.hash_password') as hashing:
                for _ in range(login_throttle.MAX_RESET_ATTEMPTS_PER_TOKEN):
                    self.assertEqual(self.submit(client, token, confirm_password='mismatch').status_code, 400)
                response = self.submit(client, token)
                self.assertEqual(response.status_code, 429)
                self.assertGreater(int(response.headers['retry-after']), 0)
            hashing.assert_not_called()

    def test_untrusted_forwarded_headers_cannot_select_a_verified_identity(self):
        for headers in ({}, {'cf-connecting-ip': '8.8.8.8', 'x-forwarded-for': '1.1.1.1'}):
            request = Request({'type': 'http', 'client': ('172.31.0.1', 50000),
                               'headers': [(key.encode(), value.encode()) for key, value in headers.items()]})
            identity = client_identity(request)
            self.assertEqual(identity.address, '172.31.0.1')
            self.assertFalse(identity.verified)


class UsageWindowRepairTests(fixture.BackendCase):
    fixed = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)

    def log(self, stamp, *, legacy=False):
        usage_id = fixture.AnalyticsTests.log(self, day=stamp)
        self.execute("UPDATE api_usage_logs SET protocol='responses',outcome='succeeded' WHERE id=?", (usage_id,))
        if legacy:
            self.execute('UPDATE api_usage_logs SET started_at=NULL WHERE id=?', (usage_id,))
        return usage_id

    def test_report_formats_have_identical_chart_and_detail_counts(self):
        cases = [
            ('2026-10-27 12:00:00', 1),
            ('2026-10-26T23:30:00-02:00', 1),
            ('2026-10-27T00:30:00+02:00', 0),
            ('2026-11-10T00:00:00+00:00', 0),
        ]
        for legacy in (False, True):
            for stamp, expected in cases:
                with self.subTest(legacy=legacy, stamp=stamp):
                    self.execute('DELETE FROM api_usage_logs')
                    self.log(stamp, legacy=legacy)
                    with patch.object(usage, 'datetime') as clock:
                        clock.now.return_value = self.fixed
                        window = usage.usage_window(14)
                    for user_id in (None, self.user_id):
                        chart = usage.usage_analytics(user_id, 14, window=window)
                        detail = usage.read_usage_summary(user_id, window=window)
                        self.assertEqual(chart['totals']['requests'], expected)
                        self.assertEqual(detail['summary']['requests'], expected)
                        self.assertEqual(sum(row['requests'] for row in detail['daily']), expected)
                        self.assertEqual(len(detail['recent']), expected)
                        if expected:
                            self.assertEqual(detail['daily'][0]['day'], '2026-10-27')

    def test_all_windows_normalize_z_offsets_spaces_and_naive_utc_boundaries(self):
        for days in (7, 14, 30):
            with patch.object(usage, 'datetime') as clock:
                clock.now.return_value = self.fixed
                window = usage.usage_window(days)
            start, end = window.start.date(), window.end.date()
            stamps = [
                f'{start - timedelta(days=1)}T23:30:00-02:00',
                f'{start}T00:30:00+02:00',
                f'{end}T00:30:00+02:00',
                f'{end - timedelta(days=1)}T23:30:00-02:00',
                f'{start - timedelta(days=1)}T23:59:59.999999Z',
                f'{end - timedelta(days=1)}T23:59:59.999999Z',
            ]
            for boundary in (start, end):
                stamps.extend(f'{boundary}{suffix}' for suffix in
                    ('T00:00:00+00:00', 'T00:00:00.000000+00:00', 'T00:00:00Z', ' 00:00:00', 'T00:00:00'))
            self.execute('DELETE FROM api_usage_logs')
            expected = {}
            for stamp in stamps:
                self.log(stamp, legacy=stamp.endswith('Z'))
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                normalized = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
                if window.start <= normalized < window.end:
                    day = str(normalized.date())
                    expected[day] = expected.get(day, 0) + 1
            for user_id in (None, self.user_id):
                with self.subTest(days=days, user_id=user_id):
                    chart = usage.usage_analytics(user_id, days, window=window)
                    detail = usage.read_usage_summary(user_id, window=window)
                    self.assertEqual(chart['totals']['requests'], sum(expected.values()))
                    self.assertEqual(detail['summary']['requests'], sum(expected.values()))
                    self.assertEqual({row['day']: row['requests'] for row in detail['daily']}, expected)
                    self.assertEqual({row['day']: row['requests'] for row in chart['daily'] if row['requests']}, expected)

    def test_actual_chart_and_detail_queries_use_expression_range_indexes(self):
        self.log(now_iso())
        window = usage.usage_window(14)
        for user_id, index in ((None, 'idx_api_usage_effective_at'), (self.user_id, 'idx_api_usage_user_effective_at')):
            statements = []

            def traced_connect():
                conn = self.open_database()
                conn.set_trace_callback(statements.append)
                return conn

            with patch('app.db.connect', side_effect=traced_connect):
                usage.usage_analytics(user_id, 14, window=window)
                usage.read_usage_summary(user_id, window=window)
            queries = [sql for sql in statements if 'FROM api_usage_logs' in sql]
            self.assertEqual(len(queries), 5)
            with closing(self.open_database()) as conn:
                for sql in queries:
                    with self.subTest(user_id=user_id, sql=sql):
                        detail = ' '.join(row['detail'] for row in conn.execute('EXPLAIN QUERY PLAN ' + sql))
                        self.assertIn('USING INDEX ' + index, detail)
                        self.assertIn('<expr>>? AND <expr><?', detail)
                        self.assertNotIn('SCAN a', detail)
                        self.assertNotIn('SCAN api_usage_logs', detail)

    def test_both_pages_compute_window_once_even_across_utc_midnight(self):
        self.log('2026-10-27 12:00:00')
        self.log('2026-11-10T00:00:00Z')
        self.as_admin()
        for path in ('/developer/usage', '/admin/ai-usage'):
            with patch.object(usage, 'datetime') as clock:
                clock.now.side_effect = [self.fixed.replace(hour=23, minute=59, second=59), self.fixed + timedelta(days=1)]
                response = self.client.get(path, params={'days': 14})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(clock.now.call_count, 1)
            self.assertEqual(response.context['analytics']['totals']['requests'], 1)
            self.assertEqual(response.context['usage']['summary']['requests'], 1)
            self.assertEqual(response.context['usage']['daily'][0]['day'], '2026-10-27')

    def test_summary_without_window_preserves_rolling_cutoff_and_no_end_boundary(self):
        self.log('2026-11-02T11:59:59+00:00')
        self.log('2026-11-02T12:00:00+00:00')
        self.log('2026-11-10T00:00:00+00:00')
        with patch.object(usage, 'datetime') as clock:
            clock.now.return_value = self.fixed
            result = usage.read_usage_summary(self.user_id)
        self.assertEqual(result['summary']['requests'], 2)


class DedicatedHostnameRepairTests(fixture.BackendCase):
    def test_non_v1_prefix_paths_return_gateway_json_for_get_and_post(self):
        for method in ('GET', 'POST'):
            for path in ('/', '/v1', '/api', '/api/', '/v1-other'):
                with self.subTest(method=method, path=path):
                    response = self.client.request(method, path,
                        headers={'Host': 'cli.luomo.moe', 'Accept': 'text/html', 'X-Request-ID': 'dedicated-host'})
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.headers['content-type'], 'application/json')
                    self.assertEqual(response.json()['error'], {'type': 'not_found', 'message': 'Not Found'})
                    self.assertEqual(response.headers['x-request-id'], 'dedicated-host')

    def test_health_and_v1_routes_still_reach_application(self):
        response = self.client.get('/health', headers={'Host': 'cli.luomo.moe'})
        self.assertEqual(response.status_code, 200)
        response = self.client.get('/v1/models', headers={'Host': 'cli.luomo.moe'})
        self.assertEqual(response.status_code, 401)


if __name__ == '__main__':
    unittest.main()
