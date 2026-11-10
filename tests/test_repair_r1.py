"""Scoped review repairs: isolated SQLite and fake upstreams, no network."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib
import secrets
import threading
import unittest
from unittest.mock import patch

import httpx
import test_backend_v2 as fixture
import test_credit_gateway as gateway_fixture
from app import auth, db, login_throttle
from app.client_ip import ClientIdentity
from app.email_verification import hash_verification_token
from app.upstream_security import PinnedTarget
from app.utils.timefmt import now_iso

public_gateway = importlib.import_module("app.public_gateway_secure")
usage_module = importlib.import_module("app.ai_usage")


class PublicRouteRepairTests(fixture.BackendCase):
    def setUp(self):
        super().setUp()
        self.as_admin()
        self.api_id = self.execute("""INSERT INTO apis(name,slug,base_url,created_at,updated_at)
            VALUES ('repair','repair','https://catalog.example.test',?,?)""", (now_iso(), now_iso()))
        self.routes = {}
        for anonymous in (True, False):
            path = "repair/anonymous" if anonymous else "repair/keyed"
            route_id = self.execute("""INSERT INTO public_gateway_routes
                (name,public_path,target_api_id,target_method,target_path,enabled,allow_anonymous,created_at,updated_at)
                VALUES ('repair',?,?,'GET','/status',1,?,?,?)""",
                (path, self.api_id, int(anonymous), now_iso(), now_iso()))
            self.routes[anonymous] = (path, route_id)
        self.stack.enter_context(patch("app.http_clients.create_http_client", side_effect=lambda timeout:
            gateway_fixture.create_http_client(timeout, transport=httpx.MockTransport(self.upstream))))
        self.pin = self.stack.enter_context(patch.object(public_gateway, "validate_target_url",
            side_effect=lambda url: PinnedTarget(url, None, None)))

    async def upstream(self, request):
        self.assertEqual(request.url.host, "catalog.example.test")
        with self.calls_lock:
            self.calls.append(str(request.url))
        return httpx.Response(200, json={"status": "ok"}, request=request)

    def request_route(self, anonymous):
        headers = {"X-Request-ID": "repair-request"}
        if not anonymous:
            headers["Authorization"] = "Bearer " + self.key
        return self.client.get("/api/public/v1/" + self.routes[anonymous][0], headers=headers)

    def toggle(self, enabled):
        response = self.client.post(f"/apis/{self.api_id}/toggle", data={"enabled": enabled}, headers=fixture.ORIGIN)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.rows("SELECT enabled FROM apis WHERE id=?", (self.api_id,))[0]["enabled"], enabled)

    def test_upstream_toggle_blocks_anonymous_and_keyed_routes_then_recovers(self):
        # Compare the complete rejection body and logging to a route's own switch.
        disabled = {}
        for anonymous, (_, route_id) in self.routes.items():
            self.execute("UPDATE public_gateway_routes SET enabled=0 WHERE id=?", (route_id,))
            response = self.request_route(anonymous)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"], "route_disabled")
            disabled[anonymous] = response.json()
            self.execute("UPDATE public_gateway_routes SET enabled=1 WHERE id=?", (route_id,))
        before = self.rows("SELECT * FROM api_usage_logs")
        self.toggle(0)
        for anonymous in self.routes:
            response = self.request_route(anonymous)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), disabled[anonymous])
            self.assertEqual(response.headers["x-request-id"], "repair-request")
        self.assertEqual(self.calls, [])
        self.pin.assert_not_called()
        self.assertEqual(self.rows("SELECT * FROM api_usage_logs"), before)
        self.toggle(1)
        for anonymous in self.routes:
            response = self.request_route(anonymous)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(len(self.calls), 2)

    def test_disable_during_dns_is_rechecked_for_both_admission_paths(self):
        for anonymous in self.routes:
            for switch in ("apis", "public_gateway_routes"):
                with self.subTest(anonymous=anonymous, switch=switch):
                    self.execute("UPDATE apis SET enabled=1 WHERE id=?", (self.api_id,))
                    route_id = self.routes[anonymous][1]
                    self.execute("UPDATE public_gateway_routes SET enabled=1 WHERE id=?", (route_id,))
                    def disable_after_initial_lookup(url):
                        target_id = self.api_id if switch == "apis" else route_id
                        self.execute(f"UPDATE {switch} SET enabled=0 WHERE id=?", (target_id,))
                        return PinnedTarget(url, None, None)
                    with patch.object(public_gateway, "validate_target_url", side_effect=disable_after_initial_lookup):
                        response = self.request_route(anonymous)
                    self.assertEqual(response.status_code, 404, response.text)
                    self.assertEqual(response.json()["error"], "route_disabled")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows("SELECT * FROM api_usage_logs"), [])

    def test_keyed_route_and_upstream_are_reread_inside_reservation_lock(self):
        path, route_id = self.routes[False]
        for switch in ("apis", "public_gateway_routes"):
            with self.subTest(switch=switch):
                self.execute("UPDATE apis SET enabled=1 WHERE id=?", (self.api_id,))
                self.execute("UPDATE public_gateway_routes SET enabled=1 WHERE id=?", (route_id,))
                route, key, error = public_gateway._load_public_route(path, "GET", self.key)
                self.assertIsNone(error)
                target_id = self.api_id if switch == "apis" else route_id
                with closing(self.open_database()) as writer, ThreadPoolExecutor(max_workers=1) as pool:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute(f"UPDATE {switch} SET enabled=0 WHERE id=?", (target_id,))
                    ready = threading.Event()
                    def reserve():
                        ready.set()
                        return public_gateway._reserve_public_usage(route, key, "race", "GET", path, None, None)
                    result = pool.submit(reserve)
                    self.assertTrue(ready.wait(5))
                    writer.commit()
                    reservation, rejection = result.result(timeout=10)
                self.assertIsNone(reservation)
                self.assertEqual((rejection.status_code, rejection.code), (404, "route_disabled"))
        self.assertEqual(self.rows("SELECT * FROM api_usage_logs"), [])


class PasswordRepairTests(fixture.BackendCase):
    def token(self):
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        self.execute("INSERT INTO password_reset_tokens(token_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
            (hash_verification_token(token), self.user_id, now.isoformat(), (now + timedelta(hours=1)).isoformat()))
        return token

    def test_invalid_tokens_never_hash_passwords(self):
        token = self.token()
        with patch("app.routes.auth.hash_password") as hashing:
            for invalid in ("", "unknown"):
                self.assertEqual(self.reset(invalid).status_code, 400)
            self.execute("UPDATE password_reset_tokens SET used_at=?", (now_iso(),))
            self.assertEqual(self.reset(token).status_code, 400)
            self.execute("UPDATE password_reset_tokens SET used_at=NULL,expires_at=?", ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
            self.assertEqual(self.reset(token).status_code, 400)
            disabled = self.token()
            self.execute("UPDATE users SET status='disabled' WHERE id=?", (self.user_id,))
            self.assertEqual(self.reset(disabled).status_code, 400)
        hashing.assert_not_called()

    def test_invalid_new_password_never_hashes_or_consumes_token(self):
        token = self.token()
        with patch("app.routes.auth.hash_password") as hashing:
            for fields in ({"new_password": "short", "confirm_password": "short"}, {"confirm_password": "mismatch"}):
                self.assertEqual(self.reset(token, **fields).status_code, 400)
        hashing.assert_not_called()
        self.assertIsNone(self.rows("SELECT used_at FROM password_reset_tokens")[0]["used_at"])

    def test_rotating_invalid_tokens_hits_source_limit_with_retry_after(self):
        with patch("app.routes.auth.client_identity", return_value=ClientIdentity("8.8.8.8", True)), patch("app.routes.auth.hash_password") as hashing:
            for index in range(login_throttle.MAX_RESET_ATTEMPTS_PER_SOURCE):
                self.assertEqual(self.reset(f"invalid-{index}").status_code, 400)
            response = self.reset("another-invalid-token")
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers["retry-after"]), 0)
        self.assertLessEqual(int(response.headers["retry-after"]), login_throttle.RESET_WINDOW_SECONDS)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        hashing.assert_not_called()

    def test_token_limit_survives_source_rotation_and_blocks_before_hashing(self):
        token = self.token()
        with patch("app.routes.auth.hash_password") as hashing:
            for index in range(login_throttle.MAX_RESET_ATTEMPTS_PER_TOKEN):
                with patch("app.routes.auth.client_identity", return_value=ClientIdentity(f"198.51.100.{index+1}", True)):
                    self.assertEqual(self.reset(token, confirm_password="mismatch").status_code, 400)
            with patch("app.routes.auth.client_identity", return_value=ClientIdentity("198.51.100.99", True)):
                response = self.reset(token)
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers["retry-after"]), 0)
        hashing.assert_not_called()
        self.assertIsNone(self.rows("SELECT used_at FROM password_reset_tokens")[0]["used_at"])

    def test_limiter_is_atomic_persistent_and_expires(self):
        digest = hash_verification_token("fixture-token")
        def attempt(_):
            return login_throttle.claim_password_reset_attempt(digest, "198.51.100.8")
        with patch("app.login_throttle.time.time", return_value=1000):
            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(attempt, range(12)))
            self.assertEqual(results.count(0), login_throttle.MAX_RESET_ATTEMPTS_PER_TOKEN)
            self.assertTrue(all(value == login_throttle.RESET_WINDOW_SECONDS for value in results if value))
            stored = self.rows("SELECT * FROM password_reset_attempts")
            self.assertEqual(len(stored), 2)
            self.assertTrue(all(row["attempt_count"] == 8 for row in stored))
            self.assertEqual(attempt(None), login_throttle.RESET_WINDOW_SECONDS)
            self.assertEqual(self.rows("SELECT * FROM password_reset_attempts"), stored)
        with patch("app.login_throttle.time.time", return_value=1000 + login_throttle.RESET_WINDOW_SECONDS):
            self.assertEqual(attempt(None), 0)
        self.assertTrue(all(row["attempt_count"] == 1 for row in self.rows("SELECT * FROM password_reset_attempts")))

    def test_valid_token_hashes_once_outside_write_transaction_and_replay_skips_hash(self):
        token = self.token()
        def hash_without_write_lock(password):
            # A second connection can acquire the write lock while PBKDF2 runs.
            with closing(self.open_database()) as conn:
                conn.execute("PRAGMA busy_timeout=0")
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            return auth.hash_password(password)
        with patch("app.routes.auth.hash_password", side_effect=hash_without_write_lock) as hashing:
            self.assertEqual(self.reset(token).status_code, 303)
            self.assertEqual(self.reset(token).status_code, 400)
        self.assertEqual(hashing.call_count, 1)
        self.assertIsNotNone(auth.authenticate_user("gateway_developer", "Reset-password-2026"))

    def test_token_expiry_and_consumption_are_rechecked_after_hashing(self):
        original = self.rows("SELECT password_hash FROM users WHERE id=?", (self.user_id,))
        for change in ("used", "expired"):
            token = self.token()
            def race(password):
                column, value = ("used_at", now_iso()) if change == "used" else ("expires_at", "2000-01-01T00:00:00Z")
                self.execute(f"UPDATE password_reset_tokens SET {column}=? WHERE token_hash=?", (value, hash_verification_token(token)))
                return auth.hash_password(password)
            with patch("app.routes.auth.hash_password", side_effect=race):
                self.assertEqual(self.reset(token).status_code, 400)
        self.assertEqual(self.rows("SELECT password_hash FROM users WHERE id=?", (self.user_id,)), original)

    def test_concurrent_preflight_success_still_consumes_token_only_once(self):
        token = self.token()
        barrier = threading.Barrier(2)
        def simultaneous_hash(password):
            barrier.wait(timeout=10)
            return auth.hash_password(password)
        with patch("app.routes.auth.hash_password", side_effect=simultaneous_hash) as hashing:
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(lambda _: self.reset(token).status_code, range(2)))
        self.assertEqual(sorted(statuses), [303, 400])
        self.assertEqual(hashing.call_count, 2)
        self.assertIsNotNone(self.rows("SELECT used_at FROM password_reset_tokens")[0]["used_at"])

    def test_account_cheap_validation_precedes_pbkdf2_and_keeps_messages(self):
        with patch("app.routes.account.verify_password") as verify, patch("app.routes.account.hash_password") as hashing:
            response = self.change_password(new_password="short", confirm_password="short")
            self.assertEqual(response.status_code, 400)
            self.assertIn("新密码至少需要 10 个字符。", response.text)
            response = self.change_password(confirm_password="mismatch")
            self.assertEqual(response.status_code, 400)
            self.assertIn("两次输入的新密码不一致。", response.text)
        verify.assert_not_called()
        hashing.assert_not_called()
        with patch("app.routes.account.verify_password", wraps=auth.verify_password) as verify, patch("app.routes.account.hash_password") as hashing:
            response = self.change_password(current_password="wrong")
            self.assertEqual(response.status_code, 400)
            self.assertIn("当前密码不正确。", response.text)
            self.assertEqual(verify.call_count, 1)
            verify.reset_mock()
            response = self.change_password(new_password=gateway_fixture.PASSWORD, confirm_password=gateway_fixture.PASSWORD)
            self.assertEqual(response.status_code, 400)
            self.assertIn("新密码不得与当前密码相同。", response.text)
            self.assertEqual(verify.call_count, 1)
        hashing.assert_not_called()

    def test_forgot_preflight_failures_skip_token_creation_and_keep_hint(self):
        with patch("app.email_verification.send_password_reset_email"):
            initial = self.client.post("/forgot-password", data={"email": "gateway@example.test"}, headers=fixture.ORIGIN)
        with patch("app.routes.auth.secrets.token_urlsafe") as generate, patch("app.routes.auth.hash_password") as hashing, patch("app.email_verification.send_password_reset_email") as mail:
            for email in ("gateway@example.test", "unknown@example.test"):
                response = self.client.post("/forgot-password", data={"email": email}, headers=fixture.ORIGIN)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, initial.text)
            self.execute("DELETE FROM password_reset_limits")
            self.execute("UPDATE users SET status='disabled' WHERE id=?", (self.user_id,))
            response = self.client.post("/forgot-password", data={"email": "gateway@example.test"}, headers=fixture.ORIGIN)
            self.assertEqual(response.status_code, 200)
            self.assertIn(importlib.import_module("app.routes.auth").RESET_MESSAGE, response.text)
        generate.assert_not_called()
        hashing.assert_not_called()
        mail.assert_not_called()


class AnalyticsRepairTests(fixture.BackendCase):
    log = fixture.AnalyticsTests.log

    def test_actual_admin_and_developer_queries_search_expression_indexes(self):
        self.log(charged=1)
        for user_id, index in ((None, "idx_api_usage_effective_at"), (self.user_id, "idx_api_usage_user_effective_at")):
            with self.subTest(user_id=user_id):
                statements = []
                def traced_connect():
                    conn = self.open_database()
                    conn.set_trace_callback(statements.append)
                    return conn
                with patch("app.db.connect", side_effect=traced_connect):
                    result = usage_module.usage_analytics(user_id, 14)
                self.assertEqual(result["totals"]["requests"], 1)
                queries = [sql for sql in statements if "FROM api_usage_logs a LEFT JOIN" in sql]
                self.assertEqual(len(queries), 1)
                with closing(self.open_database()) as conn:
                    plan = [row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + queries[0])]
                detail = " ".join(plan)
                self.assertIn("SEARCH a USING INDEX " + index, detail)
                self.assertIn("<expr>>? AND <expr><?", detail)
                self.assertNotIn("SCAN a", detail)

    def test_iso_variants_and_offset_boundaries_preserve_utc_window(self):
        fixed = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        start = fixed.date() - timedelta(days=13)
        end = fixed.date() + timedelta(days=1)
        stamps = [
            f"{start-timedelta(days=1)}T23:30:00-02:00",
            f"{start}T00:30:00+02:00",
            f"{end}T00:30:00+02:00",
            f"{end-timedelta(days=1)}T23:30:00-02:00",
            f"{start-timedelta(days=1)}T23:59:59.999999Z",
        ]
        for day in (start, end):
            stamps.extend(f"{day}{suffix}" for suffix in ("T00:00:00+00:00", "T00:00:00.000000+00:00", "T00:00:00Z", " 00:00:00"))
        expected = {}
        for stamp in stamps:
            usage_id = self.log(day=stamp, charged=1)
            # Exercise legacy rows whose only timestamp is created_at.
            if stamp.endswith("Z"):
                self.execute("UPDATE api_usage_logs SET started_at=NULL WHERE id=?", (usage_id,))
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            day = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()
            if start <= day < end:
                expected[str(day)] = expected.get(str(day), 0) + 1
        for user_id in (None, self.user_id):
            with patch.object(usage_module, "datetime") as clock:
                clock.now.return_value = fixed
                data = usage_module.usage_analytics(user_id, 14)
            self.assertEqual(data["totals"]["requests"], sum(expected.values()))
            self.assertEqual(data["totals"]["credits"], "0.000006")
            for day in data["daily"]:
                self.assertEqual(day["requests"], expected.get(day["day"], 0))

    def test_additive_migration_restores_tables_indexes_without_changing_rows(self):
        self.log()
        before = {table: self.rows(f"SELECT * FROM {table}") for table in ("users", "api_keys", "api_usage_logs")}
        self.execute("DROP TABLE password_reset_attempts")
        for index in ("idx_api_usage_effective_at", "idx_api_usage_user_effective_at"):
            self.execute(f"DROP INDEX {index}")
        db.init_db()
        db.init_db()
        for table, rows in before.items():
            self.assertEqual(self.rows(f"SELECT * FROM {table}"), rows)
        self.assertEqual(self.rows("SELECT * FROM password_reset_attempts"), [])
        names = {row["name"] for row in self.rows("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"idx_api_usage_effective_at", "idx_api_usage_user_effective_at", "idx_password_reset_attempts_window"} <= names)


if __name__ == "__main__":
    unittest.main()
