"""Phase A contracts: temporary SQLite, local fake SMTP/upstream, no network."""
import ast
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import secrets
import unittest
from unittest.mock import patch
from fastapi import HTTPException

import test_credit_gateway as fixture
from app import auth, db, ai_catalog
from app.ai_usage import usage_analytics
from app.credit_ledger import add_credit, reserve_credit, settle_credit
from app.credit_pricing import (PRICE_DATA, PricingError, prepare_billable_request, calculate_charge)
from app.email_verification import claim_email_send, hash_verification_token
from app.image_input import IMAGE_INPUT_TOKEN_CEILING, MAX_IMAGE_BYTES, supports_vision
from app.utils.timefmt import now_iso

ORIGIN = {"Origin": fixture.BASE_URL, "Accept": "application/json"}
gateway = importlib.import_module("app.cliproxy_gateway")


class BackendCase(unittest.TestCase):
    setUp = fixture.CreditGatewayTests.setUp
    cleanup = fixture.CreditGatewayTests.cleanup
    open_database = fixture.CreditGatewayTests.open_database
    upstream = fixture.CreditGatewayTests.upstream
    register_and_login = fixture.CreditGatewayTests.register_and_login
    issue_key = fixture.CreditGatewayTests.issue_key
    fund = fixture.CreditGatewayTests.fund
    request_payload = fixture.CreditGatewayTests.request_payload
    generate = fixture.CreditGatewayTests.generate
    accounting = fixture.CreditGatewayTests.accounting
    assert_settled = fixture.CreditGatewayTests.assert_settled
    assert_pending = fixture.CreditGatewayTests.assert_pending

    def execute(self, sql, args=()):
        with closing(self.open_database()) as conn, conn:
            return conn.execute(sql, args).lastrowid

    def rows(self, sql, args=()):
        with closing(self.open_database()) as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def new_user(self, *, role="developer", status="active"):
        name = "v2_" + secrets.token_hex(6)
        return self.execute("INSERT INTO users(username,email,password_hash,role,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                            (name, name+"@example.test", auth.hash_password(fixture.PASSWORD), role, status, now_iso(), now_iso()))

    def cookie(self, token):
        self.client.cookies.clear()
        self.client.cookies.set(auth.SESSION_COOKIE, token)

    def as_admin(self):
        self.execute("UPDATE users SET role='admin' WHERE id=?", (self.user_id,))

    def change_password(self, **updates):
        fields = dict(current_password=fixture.PASSWORD, new_password="New-password-2026", confirm_password="New-password-2026")
        return self.client.post("/account/password", data={**fields, **updates}, headers=ORIGIN)

    def reset_token(self):
        with patch("app.email_verification.send_password_reset_email") as mail:
            response = self.client.post("/forgot-password", data={"email": "gateway@example.test"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mail.call_count, 1)
        return mail.call_args.args[2]

    def reset(self, token, **updates):
        return self.client.post("/reset-password", params={"token": token}, data={"new_password": "Reset-password-2026", "confirm_password": "Reset-password-2026", **updates}, headers=ORIGIN)

    def play(self, *, key_id=None, protocol="chat", payload=None):
        return self.client.post("/developer/playground/run", json={"key_id": self.key_id if key_id is None else key_id,
            "endpoint": "chat.completions" if protocol == "chat" else "responses", "payload": payload or self.request_payload(protocol)}, headers=ORIGIN)


class SessionTests(BackendCase):
    def test_cookie_is_random_opaque_and_only_hash_is_persisted(self):
        token = self.client.cookies.get(auth.SESSION_COOKIE)
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        row = self.rows("SELECT * FROM user_sessions WHERE user_id=?", (self.user_id,))[0]
        self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps(row))
        self.assertEqual((datetime.fromisoformat(row["expires_at"])-datetime.fromisoformat(row["created_at"])).total_seconds(), 43200)
        self.assertIn("testclient", row["user_agent"].lower())

    def test_legacy_signed_cookie_is_rejected_even_with_valid_hmac(self):
        name = "gateway_developer"
        payload = base64.urlsafe_b64encode(f"{name}:9999999999".encode()).decode()
        token = payload+"."+hmac.new(auth.settings.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        self.cookie(token)
        self.assertEqual(self.client.get("/account", headers=ORIGIN).status_code, 401)
        self.assertIsNone(auth.verify_session(token))
        self.assertNotIn("账户设置", self.client.get("/").text)

    def test_logout_revokes_cookie_even_if_attacker_replays_it(self):
        token = self.client.cookies.get(auth.SESSION_COOKIE)
        self.assertEqual(self.client.post("/logout", headers=ORIGIN).status_code, 303)
        self.cookie(token)
        self.assertEqual(self.client.get("/account", headers=ORIGIN).status_code, 401)
        self.assertIsNotNone(self.rows("SELECT revoked_at FROM user_sessions")[0]["revoked_at"])

    def test_expired_session_and_inactive_owner_are_not_optional_users(self):
        for status in ("suspended", "disabled", "unknown"):
            self.execute("UPDATE users SET status=? WHERE id=?", (status, self.user_id))
            self.assertEqual(self.client.get("/account", headers=ORIGIN).status_code, 401)
            self.assertNotIn("账户设置", self.client.get("/").text)
        self.execute("UPDATE users SET status='active' WHERE id=?", (self.user_id,))
        self.execute("UPDATE user_sessions SET expires_at=?", ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
        self.assertEqual(self.client.get("/account", headers=ORIGIN).status_code, 401)

    def test_last_seen_writes_at_most_once_per_five_minutes(self):
        before = self.rows("SELECT * FROM user_sessions")[0]
        for _ in range(3):
            self.assertEqual(self.client.get("/account").status_code, 200)
        self.assertEqual(self.rows("SELECT * FROM user_sessions")[0]["last_seen_at"], before["last_seen_at"])
        old = (datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()
        self.execute("UPDATE user_sessions SET last_seen_at=?", (old,))
        self.client.get("/account")
        seen = self.rows("SELECT * FROM user_sessions")[0]["last_seen_at"]
        self.assertGreater(seen, old)
        self.client.get("/account")
        self.assertEqual(self.rows("SELECT * FROM user_sessions")[0]["last_seen_at"], seen)

    def test_account_shows_profile_current_device_and_escaped_truncated_ua(self):
        other = auth.create_session(self.user_id, user_agent="<script>"+"x"*300)
        page = self.client.get("/account")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("gateway@example.test", page.text)
        self.assertIn("未验证", page.text)
        self.assertIn("&lt;script&gt;", page.text)
        self.assertNotIn("x"*121, page.text)
        self.assertEqual(sum(row["current"] for row in page.context["sessions"]), 1)
        self.assertNotIn(other, page.text)

    def test_single_device_revocation_is_owner_scoped(self):
        other_token = auth.create_session(self.user_id)
        session_id = self.rows("SELECT id FROM user_sessions WHERE token_hash=?", (auth.hash_session_token(other_token),))[0]["id"]
        foreign_token = auth.create_session(self.new_user())
        foreign_id = self.rows("SELECT id FROM user_sessions WHERE token_hash=?", (auth.hash_session_token(foreign_token),))[0]["id"]
        self.assertEqual(self.client.post(f"/account/sessions/{foreign_id}/revoke", headers=ORIGIN).status_code, 404)
        self.assertIsNotNone(auth.verify_session(foreign_token))
        self.assertEqual(self.client.post(f"/account/sessions/{session_id}/revoke", headers=ORIGIN).status_code, 303)
        self.assertIsNone(auth.verify_session(other_token))
        self.assertEqual(self.client.get("/account").status_code, 200)

    def test_revoke_others_preserves_only_current_and_is_csrf_protected(self):
        other = auth.create_session(self.user_id)
        self.assertEqual(self.client.post("/account/sessions/revoke-others", headers={"Origin":"https://attacker.test"}).status_code, 403)
        self.assertIsNotNone(auth.verify_session(other))
        self.assertEqual(self.client.post("/account/sessions/revoke-others", headers=ORIGIN).status_code, 303)
        self.assertIsNone(auth.verify_session(other))
        self.assertEqual(self.client.get("/account").status_code, 200)

    def test_can_revoke_current_device_and_cookie_is_deleted(self):
        token = self.client.cookies.get(auth.SESSION_COOKIE)
        session_id = self.rows("SELECT id FROM user_sessions")[0]["id"]
        response = self.client.post(f"/account/sessions/{session_id}/revoke", headers=ORIGIN)
        self.assertEqual(response.headers["location"], "/login")
        self.assertIsNone(auth.verify_session(token))
        self.assertNotIn(auth.SESSION_COOKIE, self.client.cookies)

    def test_password_change_revokes_others_and_invalidates_pending_reset(self):
        other = auth.create_session(self.user_id)
        token = self.reset_token()
        response = self.change_password()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(auth.verify_session(other))
        self.assertEqual(self.client.get("/account").status_code, 200)
        self.assertEqual(self.reset(token).status_code, 400)
        user = auth.get_user_by_username("gateway_developer")
        self.assertTrue(auth.verify_password("New-password-2026", user["password_hash"]))
        self.assertFalse(auth.verify_password(fixture.PASSWORD, user["password_hash"]))

    def test_password_validation_never_changes_credentials_or_sessions(self):
        other = auth.create_session(self.user_id)
        before = self.rows("SELECT password_hash FROM users WHERE id=?", (self.user_id,))
        for fields in ({"current_password":"wrong"}, {"new_password":"short","confirm_password":"short"},
                       {"confirm_password":"different-password"}, {"new_password":fixture.PASSWORD,"confirm_password":fixture.PASSWORD}):
            with self.subTest(fields=list(fields)):
                self.assertEqual(self.change_password(**fields).status_code, 400)
        self.assertEqual(self.rows("SELECT password_hash FROM users WHERE id=?", (self.user_id,)), before)
        self.assertIsNotNone(auth.verify_session(other))

    def test_admin_suspension_and_disable_revoke_all_sessions_permanently(self):
        self.as_admin()
        for status in ("suspended", "disabled"):
            target = self.new_user()
            tokens = [auth.create_session(target) for _ in range(2)]
            self.assertEqual(self.client.post(f"/admin/users/{target}/status", data={"status_value":status}, headers=ORIGIN).status_code, 303)
            self.assertTrue(all(auth.verify_session(token) is None for token in tokens))
            self.client.post(f"/admin/users/{target}/status", data={"status_value":"active"}, headers=ORIGIN)
            self.assertTrue(all(auth.verify_session(token) is None for token in tokens))

    def test_login_cannot_issue_session_using_password_snapshot_before_reset(self):
        old = auth.get_user_by_username("gateway_developer")
        self.assertEqual(self.change_password().status_code, 200)
        with self.assertRaises(HTTPException):
            auth.create_session(self.user_id, expected_password_hash=old["password_hash"])


class PasswordResetTests(BackendCase):
    def test_forgot_password_is_indistinguishable_for_known_unknown_and_throttled_email(self):
        with patch("app.email_verification.send_password_reset_email") as mail:
            responses = [self.client.post("/forgot-password", data={"email":email}, headers=ORIGIN) for email in
                         ("gateway@example.test", "unknown@example.test", "gateway@example.test")]
        self.assertEqual(mail.call_count, 1)
        self.assertTrue(all(r.status_code == 200 and r.text == responses[0].text for r in responses))
        self.assertEqual(responses[0].headers["cache-control"], "no-store")

    def test_smtp_missing_logs_no_secrets_and_keeps_same_hint(self):
        with self.assertLogs("luomoapi.mail", level="WARNING") as logs:
            response = self.client.post("/forgot-password", data={"email":"gateway@example.test"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertIn("password_reset_mail_failed", logs.output[0])
        self.assertNotIn("gateway@example.test", logs.output[0])
        self.assertNotIn("token=", logs.output[0])

    def test_reset_hash_only_get_is_read_only_and_success_revokes_every_device(self):
        other = auth.create_session(self.user_id)
        token = self.reset_token()
        stored = self.rows("SELECT * FROM password_reset_tokens")[0]
        self.assertEqual(stored["token_hash"], hash_verification_token(token))
        self.assertNotIn(token, json.dumps(stored))
        self.assertEqual((datetime.fromisoformat(stored["expires_at"])-datetime.fromisoformat(stored["created_at"])).total_seconds(), 3600)
        for _ in range(2):
            page = self.client.get("/reset-password", params={"token":token})
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.headers["cache-control"], "no-store")
            self.assertEqual(page.headers["referrer-policy"], "no-referrer")
        self.assertIsNone(self.rows("SELECT used_at FROM password_reset_tokens")[0]["used_at"])
        response = self.reset(token)
        self.assertEqual((response.status_code,response.headers["location"]), (303,"/login?reset=1"))
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIsNone(auth.verify_session(other))
        self.assertTrue(all(r["revoked_at"] for r in self.rows("SELECT revoked_at FROM user_sessions WHERE user_id=?", (self.user_id,))))
        self.assertEqual(self.reset(token).status_code, 400)
        self.assertIn("密码已重置", self.client.get("/login?reset=1").text)
        self.assertIsNotNone(auth.authenticate_user("gateway_developer", "Reset-password-2026"))

    def test_expired_unknown_and_used_tokens_never_render_password_form(self):
        token = self.reset_token()
        self.execute("UPDATE password_reset_tokens SET expires_at=?", ((datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat(),))
        before = self.rows("SELECT password_hash FROM users WHERE id=?", (self.user_id,))
        for invalid in (token, "unknown", ""):
            page = self.client.get("/reset-password", params={"token":invalid})
            self.assertEqual(page.status_code, 400)
            self.assertNotIn('name="new_password"', page.text)
            self.assertEqual(page.headers["referrer-policy"], "no-referrer")
            self.assertEqual(self.reset(invalid).status_code, 400)
        self.assertEqual(self.rows("SELECT password_hash FROM users WHERE id=?", (self.user_id,)), before)

    def test_bad_password_does_not_consume_valid_token(self):
        token = self.reset_token()
        for fields in ({"new_password":"short","confirm_password":"short"}, {"confirm_password":"does-not-match"}):
            self.assertEqual(self.reset(token, **fields).status_code, 400)
            self.assertIsNone(self.rows("SELECT used_at FROM password_reset_tokens")[0]["used_at"])
        self.assertEqual(self.reset(token).status_code, 303)

    def test_concurrent_token_consumption_has_one_winner(self):
        token = self.reset_token()
        def attempt(_):
            return self.reset(token).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(attempt, range(2)))
        self.assertEqual(sorted(statuses), [303,400])
        self.assertIsNotNone(self.rows("SELECT used_at FROM password_reset_tokens")[0]["used_at"])

    def test_disabled_user_cannot_reset_even_with_earlier_token(self):
        token = self.reset_token()
        self.execute("UPDATE users SET status='disabled' WHERE id=?", (self.user_id,))
        self.assertEqual(self.reset(token).status_code, 400)
        with patch("app.email_verification.send_password_reset_email") as mail:
            self.client.post("/forgot-password", data={"email":"gateway@example.test"}, headers=ORIGIN)
        mail.assert_not_called()

    def test_reset_rate_limit_is_persistent_atomic_and_checks_source_and_email(self):
        def attempt(_):
            return claim_email_send("atomic@example.test", "198.51.100.8", table="password_reset_limits")
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(8))), 1)
        self.assertFalse(claim_email_send(" ATOMIC@example.test ", "198.51.100.9", table="password_reset_limits"))
        old = (datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
        for count in range(1,5):
            self.execute("UPDATE password_reset_limits SET last_sent_at=?", (old,))
            self.assertTrue(attempt(count))
        self.execute("UPDATE password_reset_limits SET last_sent_at=?", (old,))
        self.assertFalse(attempt(0))
        self.execute("UPDATE password_reset_limits SET send_count=120 WHERE bucket_hash=?", (hashlib.sha256(b"source:198.51.100.8").hexdigest(),))
        self.assertFalse(claim_email_send("different@example.test", "198.51.100.8", table="password_reset_limits"))
        self.execute("UPDATE password_reset_limits SET window_started_at=?,last_sent_at=?", ((datetime.now(timezone.utc)-timedelta(hours=2)).isoformat(),old))
        self.assertTrue(attempt(0))

    def test_password_reset_email_uses_existing_smtp_and_sixty_minute_link(self):
        from dataclasses import replace
        import app.email_verification as mail
        configured = replace(mail.settings, smtp_host="smtp.example.test", smtp_username="fixture", smtp_password="fixture", smtp_from="sender@example.test")
        with patch.object(mail,"settings",configured), patch.object(mail,"send_message") as send:
            mail.send_password_reset_email("dev@example.test", "developer", "fixture-token")
        message = send.call_args.args[0]
        self.assertEqual(message["To"], "dev@example.test")
        self.assertIn("/reset-password?token=fixture-token", message.get_content())
        self.assertIn("60", message.get_content())


class PlaygroundTests(BackendCase):
    def test_playground_and_v1_have_identical_quote_forwarding_and_settlement(self):
        self.fund()
        first = self.generate()
        self.assertEqual(first.status_code,200)
        normal, normal_hold, _, _ = self.accounting()
        response = self.play()
        self.assertEqual(response.status_code,200)
        play, play_hold, wallet, _ = self.accounting()
        self.assertEqual(self.calls[0],self.calls[1])
        self.assertEqual(normal_hold["pricing_snapshot"],play_hold["pricing_snapshot"])
        self.assertEqual(normal_hold["amount_units"],play_hold["amount_units"])
        self.assertEqual(normal_hold["actual_units"],play_hold["actual_units"])
        self.assertEqual(wallet["balance_units"],fixture.FUND_UNITS-2*fixture.EXPECTED_CHARGE_UNITS)
        self.assertEqual(play["public_path"],"developer/playground/run/chat.completions")
        self.assertEqual(response.headers["cache-control"],"no-store")

    def test_responses_uses_identical_settlement(self):
        self.fund()
        self.response_json = fixture.responses_response()
        response = self.play(protocol="responses")
        self.assertEqual(response.status_code,200)
        self.assert_settled()
        self.assertEqual(self.calls[-1]["path"],"/v1/responses")
        self.assertEqual(self.calls[-1]["body"]["store"],False)

    def test_playground_stream_relays_sse_and_settles(self):
        self.fund()
        self.response_sse = fixture.sse_event(fixture.chat_response())+fixture.sse_event("[DONE]")
        response = self.play(payload=self.request_payload(stream=True))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.content,self.response_sse)
        self.assertIn("text/event-stream",response.headers["content-type"])
        self.assert_settled()

    def test_playground_missing_receipt_uses_existing_reconciliation(self):
        self.fund()
        self.response_json = {"choices":[{"message":{"content":"reply"}}]}
        self.assertEqual(self.play().status_code,200)
        self.assert_pending()

    def test_key_ownership_and_active_status_are_mandatory(self):
        foreign = self.new_user()
        self.execute("UPDATE api_keys SET user_id=? WHERE id=?",(foreign,self.key_id))
        self.assertEqual(self.play().status_code,403)
        self.execute("UPDATE api_keys SET user_id=?,status='revoked' WHERE id=?",(self.user_id,self.key_id))
        self.assertEqual(self.play().status_code,403)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.rows("SELECT * FROM api_usage_logs"),[])

    def test_session_login_role_and_origin_are_required(self):
        self.assertEqual(self.client.post("/developer/playground/run",json={},headers={"Origin":"https://attacker.test"}).status_code,403)
        self.cookie("legacy.invalid")
        self.assertEqual(self.play().status_code,401)
        other=self.new_user(role="viewer")
        self.cookie(auth.create_session(other))
        self.assertEqual(self.play().status_code,403)
        self.assertEqual(self.calls,[])

    def test_playground_requires_json_valid_endpoint_and_payload(self):
        for headers in ({**ORIGIN,"Content-Type":"text/plain"},ORIGIN):
            response=self.client.post("/developer/playground/run",content='{}',headers=headers)
            self.assertEqual(response.status_code,415)
        for envelope in ({}, {"key_id":True,"endpoint":"responses","payload":{}},
                         {"key_id":self.key_id,"endpoint":"files","payload":{}},
                         {"key_id":self.key_id,"endpoint":[],"payload":{}},
                         {"key_id":2**65,"endpoint":"responses","payload":{}},
                         {"key_id":self.key_id,"endpoint":"responses","payload":{"model":"gpt-6.1-sol","stream":"yes"}}):
            self.assertEqual(self.client.post("/developer/playground/run",json=envelope,headers=ORIGIN).status_code,400)
        self.assertEqual(self.calls,[])

    def test_limits_scopes_models_and_funds_share_v1_checks(self):
        self.assertEqual(self.play().status_code,402)
        self.execute("UPDATE api_keys SET scopes='models:read' WHERE id=?",(self.key_id,))
        self.assertEqual(self.play().status_code,403)
        self.execute("UPDATE api_keys SET scopes=? WHERE id=?",(fixture.SELF_SCOPES,self.key_id))
        self.execute("DELETE FROM api_key_models WHERE api_key_id=?",(self.key_id,))
        self.assertEqual(self.play().status_code,403)
        self.assertEqual(self.calls,[])

    def test_rpm_and_daily_quota_count_both_entry_points(self):
        self.fund()
        self.execute("UPDATE api_keys SET rate_limit_per_minute=1,daily_quota=1 WHERE id=?",(self.key_id,))
        self.assertEqual(self.generate().status_code,200)
        self.assertEqual(self.play().status_code,429)
        self.execute("UPDATE api_keys SET rate_limit_per_minute=0 WHERE id=?",(self.key_id,))
        self.assertEqual(self.play().json()["error"]["code"],"quota_exceeded")
        self.assertEqual(len(self.calls),1)

    def test_session_revoke_after_read_before_reservation_denies_dispatch(self):
        self.fund()
        original=gateway._reserve_gateway_usage
        def revoked(*args,**kwargs):
            self.execute("UPDATE user_sessions SET revoked_at=? WHERE user_id=?",(now_iso(),self.user_id))
            return original(*args,**kwargs)
        with patch.object(gateway,"_reserve_gateway_usage",side_effect=revoked):
            self.assertEqual(self.play().status_code,403)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.rows("SELECT * FROM credit_holds"),[])

    def test_model_list_is_billable_available_and_key_scoped(self):
        fake={"models":[{"id":fixture.MODEL},{"id":"unpriced"},{"id":"gpt-image-2"},{"id":"gpt-6-luna"}]}
        self.execute("DELETE FROM api_key_models WHERE api_key_id=? AND model_id!=?",(self.key_id,fixture.MODEL))
        async def snapshot():
            return fake
        with patch.object(ai_catalog,"snapshot",snapshot):
            response=self.client.get("/developer/playground/models",params={"key_id":self.key_id},headers=ORIGIN)
        self.assertEqual(response.status_code,200)
        self.assertEqual([r["id"] for r in response.json()["data"]],[fixture.MODEL])
        self.assertEqual(response.headers["cache-control"],"no-store")
        foreign=self.new_user()
        self.execute("UPDATE api_keys SET user_id=? WHERE id=?",(foreign,self.key_id))
        self.assertEqual(self.client.get("/developer/playground/models",params={"key_id":self.key_id}).status_code,403)

    def test_playground_page_needs_no_raw_key(self):
        page=self.client.get("/developer/playground")
        self.assertEqual(page.status_code,200)
        self.assertIn(f'value="{self.key_id}"',page.text)
        self.assertNotIn('id="play-key" class="mono" type="password"',page.text)
        self.assertNotIn(self.key,page.text)


class AnalyticsTests(BackendCase):
    def log(self, *, user_id=None, day=None, model=fixture.MODEL, key_id=None, success=1, charged=None, status="settled", tokens=(100,5)):
        user_id = self.user_id if user_id is None else user_id
        stamp = day or now_iso()
        key_id = self.key_id if key_id is None and user_id == self.user_id else key_id
        usage_id = self.execute("""INSERT INTO api_usage_logs(created_at,started_at,completed_at,user_id,api_key_id,model,method,public_path,success,input_tokens,output_tokens,usage_reported)
            VALUES (?,?,?,?,?,?,'POST','v1/responses',?,?,?,1)""",(stamp,stamp,stamp,user_id,key_id,model,success,*tokens))
        if charged is not None:
            with closing(self.open_database()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                add_credit(conn,user_id,charged+100,reference=f"analytics-fund:{usage_id}",actor_id=None,kind="fixture",note="temporary")
                reserve_credit(conn,usage_id,user_id,charged+1,'{"fixture":true}')
                settle_credit(conn,usage_id,charged,reason="fixture")
                if status != "settled":
                    conn.execute("UPDATE credit_holds SET status=? WHERE usage_id=?",(status,usage_id))
                # The aggregates must read authoritative holds, never this denormalized preview.
                conn.execute("UPDATE api_usage_logs SET credit_charged_units=999999999 WHERE id=?",(usage_id,))
        return usage_id

    def test_missing_days_are_zero_and_all_windows_include_today(self):
        self.log(charged=1)
        for days in (7,14,30):
            data=usage_analytics(self.user_id,days)
            self.assertEqual(len(data["daily"]),days)
            self.assertEqual(data["daily"][-1]["requests"],1)
            self.assertEqual(data["daily"][-1]["credits"],"0.000001")
            self.assertTrue(all(r["requests"] == 0 and r["credits"] == "0" for r in data["daily"][:-1]))
            self.assertEqual(data["timezone"],"UTC")

    def test_settled_holds_only_and_exact_microcredits_include_failed_generations(self):
        self.log(charged=1234567)
        self.log(charged=1,success=0)
        self.log(charged=2000000,status="reconciliation_required")
        self.log(charged=3000000,status="released")
        self.log(charged=None)
        data=usage_analytics(self.user_id,14)
        self.assertEqual(data["totals"]["credits"],"1.234568")
        self.assertEqual(data["totals"]["requests"],5)
        self.assertEqual((data["totals"]["succeeded"],data["totals"]["failed"]),(4,1))
        self.assertEqual((data["totals"]["input_tokens"],data["totals"]["output_tokens"]),(500,25))
        self.assertEqual(data["by_model"][0]["credits"],"1.234568")
        self.assertEqual(data["by_key"][0]["credits"],"1.234568")

    def test_user_isolation_and_admin_total(self):
        other=self.new_user()
        self.log(charged=1000001)
        self.log(user_id=other,model="other-model",charged=2222222)
        own=usage_analytics(self.user_id,14)
        self.assertEqual(own["totals"]["credits"],"1.000001")
        self.assertEqual([r["model"] for r in own["by_model"]],[fixture.MODEL])
        self.assertEqual(usage_analytics(None,14)["totals"]["credits"],"3.222223")
        api=self.client.get("/api/developer/usage/analytics?days=14",headers=ORIGIN)
        self.assertEqual(api.json(),own)
        self.assertEqual(api.headers["cache-control"],"no-store")
        self.assertEqual(self.client.get("/api/admin/usage/analytics",headers=ORIGIN).status_code,403)
        self.as_admin()
        admin=self.client.get("/api/admin/usage/analytics?days=14",headers=ORIGIN)
        self.assertEqual(admin.json()["totals"]["credits"],"3.222223")
        self.assertEqual(admin.headers["cache-control"],"no-store")

    def test_timestamps_are_grouped_by_utc_day_and_outside_window_excluded(self):
        today=datetime.now(timezone.utc).date()
        self.log(day=str(today)+"T00:30:00+02:00",charged=1)
        self.log(day=str(today-timedelta(days=14))+"T12:00:00Z",charged=2)
        self.log(day=str(today+timedelta(days=1))+"T12:00:00Z",charged=3)
        data=usage_analytics(self.user_id,14)
        self.assertEqual(data["daily"][-2]["requests"],1)
        self.assertEqual(data["daily"][-1]["requests"],0)
        self.assertEqual(data["totals"]["credits"],"0.000001")

    def test_top_ten_and_other_preserve_all_requests_tokens_and_exact_credits(self):
        for index in range(13):
            key_id=self.execute("INSERT INTO api_keys(user_id,name,key_prefix,key_hash,status,created_at) VALUES (?,?,?,'unused','active',?)",(self.user_id,f"key{index}",f"prefix{index}",now_iso()))
            self.log(model=f"model{index:02}",key_id=key_id,charged=index+1)
        data=usage_analytics(self.user_id,14)
        for dimension in ("by_model","by_key"):
            self.assertEqual(len(data[dimension]),11)
            self.assertEqual(data[dimension][-1]["name"],"其他")
            self.assertTrue(data[dimension][-1]["other"])
            self.assertEqual(sum(r["requests"] for r in data[dimension]),13)
            self.assertEqual(sum(Decimal(r["credits"]) for r in data[dimension]),Decimal("0.000091"))

    def test_invalid_windows_and_unauthenticated_access_are_rejected(self):
        for days in (0,1,8,31):
            with self.assertRaises(ValueError):
                usage_analytics(self.user_id,days)
            self.assertEqual(self.client.get(f"/api/developer/usage/analytics?days={days}",headers=ORIGIN).status_code,400)
        self.cookie("invalid")
        self.assertEqual(self.client.get("/api/developer/usage/analytics",headers=ORIGIN).status_code,401)

    def test_fourteen_day_context_is_available_to_server_rendered_pages(self):
        for path in ("/developer","/developer/usage"):
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.context["analytics"]["days"],14)
            self.assertEqual(len(response.context["analytics"]["daily"]),14)


class CatalogTests(BackendCase):
    def setUp(self):
        super().setUp()
        self.as_admin()
        self.api_id=self.execute("INSERT INTO apis(name,slug,base_url,created_at,updated_at) VALUES ('fixture','fixture','https://example.test',?,?)",(now_iso(),now_iso()))
        self.fields=dict(name="Test route",public_path="test/status",target_api_id=self.api_id,target_method="GET",target_path="/status",required_scope="test:read",enabled="on",public_docs="on")

    def add(self, **updates):
        return self.client.post("/admin/routes",data={**self.fields,**updates},headers=ORIGIN)

    def saved_route(self):
        return self.rows("SELECT * FROM public_gateway_routes WHERE public_path='test/status'")[0]

    def test_public_path_validation_and_uniqueness_apply_to_create_and_edit(self):
        self.assertEqual(self.add().status_code,303)
        route_id=self.saved_route()["id"]
        self.assertEqual(self.add().status_code,400)
        for path in ("bad/../path","has..dots","Bad/upper","bad//path","bad/%2e","bad?x=1","bad path",".","test/./path",""):
            with self.subTest(path=path):
                fields={**self.fields,"public_path":path}
                expected=400
                self.assertEqual(self.add(public_path=path).status_code,expected)
                self.assertEqual(self.client.post(f"/admin/routes/{route_id}/edit",data=fields,headers=ORIGIN).status_code,expected)
        self.assertEqual(self.saved_route()["target_path"],"/status")

    def test_target_method_scope_and_api_validation_precede_insert(self):
        for fields in ({"target_path":"https://evil.test/a"},{"target_path":"//evil.test/a"},{"target_path":"missing-slash"},
                       {"target_path":"/\\evil"},{"target_method":"TRACE"},{"required_scope":"test:*"},{"required_scope":"test:read:all"},{"required_scope":"*"},{"target_api_id":99999}):
            with self.subTest(fields=fields):
                self.assertEqual(self.add(**fields).status_code,400)
        self.assertEqual(self.rows("SELECT * FROM public_gateway_routes"),[])

    def test_route_edit_changes_every_domain_field_and_endpoint_must_belong_to_api(self):
        self.add()
        route_id=self.saved_route()["id"]
        endpoint_id=self.execute("INSERT INTO endpoints(api_id,name,method,path,created_at,updated_at) VALUES (?,'endpoint','POST','/new',?,?)",(self.api_id,now_iso(),now_iso()))
        fields={**self.fields,"name":"Changed","public_path":"changed/path","target_method":"POST","target_path":"/new?x=1","target_endpoint_id":endpoint_id,"required_scope":"changed:write","allow_anonymous":"on"}
        fields.pop("enabled"); fields.pop("public_docs")
        self.assertEqual(self.client.post(f"/admin/routes/{route_id}/edit",data=fields,headers=ORIGIN).status_code,303)
        row=self.rows("SELECT * FROM public_gateway_routes WHERE id=?",(route_id,))[0]
        self.assertEqual((row["name"],row["public_path"],row["target_method"],row["target_path"],row["target_endpoint_id"],row["required_scope"],row["enabled"],row["public_docs"],row["allow_anonymous"]),
                         ("Changed","changed/path","POST","/new?x=1",endpoint_id,"changed:write",0,0,1))
        other=self.execute("INSERT INTO apis(name,slug,base_url,created_at,updated_at) VALUES ('other','other','https://example.test',?,?)",(now_iso(),now_iso()))
        fields["target_api_id"]=other
        self.assertEqual(self.client.post(f"/admin/routes/{route_id}/edit",data=fields,headers=ORIGIN).status_code,400)

    def test_route_copy_is_unique_and_disabled_and_delete_of_unused_copy_succeeds(self):
        self.add()
        route_id=self.saved_route()["id"]
        for _ in range(2):
            self.assertEqual(self.client.post(f"/admin/routes/{route_id}/copy",headers=ORIGIN).status_code,303)
        copies=self.rows("SELECT * FROM public_gateway_routes WHERE id!=? ORDER BY id",(route_id,))
        self.assertEqual([r["public_path"] for r in copies],["test/status-copy","test/status-copy-2"])
        self.assertTrue(all(r["enabled"] == 0 and r["required_scope"] == "test:read" for r in copies))
        self.assertEqual(self.client.post(f'/admin/routes/{copies[0]["id"]}/delete',headers=ORIGIN).status_code,303)
        self.assertEqual(len(self.rows("SELECT * FROM public_gateway_routes")),2)

    def test_route_with_any_usage_cannot_be_deleted_and_can_be_disabled(self):
        self.add()
        route_id=self.saved_route()["id"]
        self.execute("INSERT INTO api_usage_logs(created_at,route_id,method,public_path,success) VALUES (?,?,'GET','test/status',0)",(now_iso(),route_id))
        response=self.client.post(f"/admin/routes/{route_id}/delete",headers=ORIGIN)
        self.assertEqual(response.status_code,409)
        self.assertIn("停用",response.json()["detail"])
        self.assertEqual(self.client.post(f"/admin/routes/{route_id}/toggle",data={"enabled":0},headers=ORIGIN).status_code,303)
        self.assertEqual(self.saved_route()["enabled"],0)
        self.assertEqual(len(self.rows("SELECT * FROM api_usage_logs")),1)

    def test_route_list_counts_seven_days_and_latest_call_and_edit_page_renders(self):
        self.add()
        route_id=self.saved_route()["id"]
        for stamp in (now_iso(),(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()):
            self.execute("INSERT INTO api_usage_logs(created_at,route_id,method,public_path) VALUES (?,?,'GET','test/status')",(stamp,route_id))
        response=self.client.get("/admin/routes")
        self.assertEqual(response.status_code,200)
        row=response.context["routes"][0]
        self.assertEqual(row["calls_7d"],1)
        self.assertIsNotNone(row["last_called_at"])
        self.assertEqual(self.client.get(f"/admin/routes/{route_id}/edit").status_code,200)

    def test_api_create_and_edit_reuse_https_no_userinfo_validation(self):
        fields=dict(name="New",slug="new",base_url="https://api.example.test",auth_type="bearer",secret_ref="FIXTURE_ENV",description="fixture",tags="qa",enabled="on")
        for url in ("http://example.test","https://user:pass@example.test","https://@example.test","https:///missing","https://example.test:invalid","https://example.test/#fragment"):
            for path in ("/apis",f"/apis/{self.api_id}/edit"):
                self.assertEqual(self.client.post(path,data={**fields,"base_url":url},headers=ORIGIN).status_code,400)
        self.assertEqual(self.client.post(f"/apis/{self.api_id}/edit",data=fields,headers=ORIGIN).status_code,303)
        saved=self.rows("SELECT * FROM apis WHERE id=?",(self.api_id,))[0]
        self.assertEqual((saved["name"],saved["slug"],saved["secret_ref"],saved["tags"]),("New","new","FIXTURE_ENV","qa"))
        self.assertEqual(self.client.post(f"/apis/{self.api_id}/toggle",data={"enabled":0},headers=ORIGIN).status_code,303)
        self.assertEqual(self.rows("SELECT enabled FROM apis WHERE id=?",(self.api_id,))[0]["enabled"],0)
        self.assertEqual(self.client.get(f"/apis/{self.api_id}").status_code,200)

    def test_endpoint_edit_and_delete_preserve_fixed_route_snapshots(self):
        fields=dict(name="Endpoint",method="GET",path="/first",default_headers="{}",default_query="{}",default_body="",enabled="on")
        self.assertEqual(self.client.post(f"/apis/{self.api_id}/endpoints",data=fields,headers=ORIGIN).status_code,303)
        endpoint_id=self.rows("SELECT id FROM endpoints")[0]["id"]
        self.add(target_endpoint_id=endpoint_id)
        fields.update(name="Edited",method="POST",path="/second",description="new",default_headers='{"X-Test":"fixture"}',default_query='{"n":2}',default_body='{}')
        self.assertEqual(self.client.post(f"/apis/{self.api_id}/endpoints/{endpoint_id}/edit",data=fields,headers=ORIGIN).status_code,303)
        row=self.rows("SELECT * FROM endpoints")[0]
        self.assertEqual((row["name"],row["method"],row["path"]),("Edited","POST","/second"))
        self.assertEqual(self.client.post(f"/apis/{self.api_id}/endpoints/{endpoint_id}/delete",headers=ORIGIN).status_code,303)
        self.assertEqual(self.rows("SELECT * FROM endpoints"),[])
        self.assertEqual((self.saved_route()["target_endpoint_id"],self.saved_route()["target_path"]),(None,"/status"))

    def test_invalid_key_status_is_400_and_does_not_revoke(self):
        response=self.client.post(f"/admin/api-keys/{self.key_id}/status",data={"status_value":"typo"},headers=ORIGIN)
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.rows("SELECT status FROM api_keys WHERE id=?",(self.key_id,))[0]["status"],"active")

    def test_catalog_writes_require_admin_and_same_origin(self):
        self.assertEqual(self.client.post("/admin/routes",data=self.fields,headers={"Origin":"https://attacker.test"}).status_code,403)
        self.execute("UPDATE users SET role='developer' WHERE id=?",(self.user_id,))
        self.assertEqual(self.add().status_code,403)
        self.assertEqual(self.client.post(f"/apis/{self.api_id}/toggle",headers=ORIGIN).status_code,403)


class ImageInputTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{"GATEWAY_IMAGE_INPUT":"true"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def payload(self, url="https://example.test/image.png", *, protocol="chat.completions", count=1, model=fixture.MODEL):
        image={"type":"image_url","image_url":{"url":url,"detail":"auto"}} if protocol == "chat.completions" else {"type":"input_image","image_url":url,"detail":"high"}
        return {"model":model, "messages" if protocol == "chat.completions" else "input":[{"role":"user","content":[{"type":"text" if protocol == "chat.completions" else "input_text","text":"what is this"}]+[image]*count}],
                "max_completion_tokens" if protocol == "chat.completions" else "max_output_tokens":8}

    def quote(self, payload, protocol="chat.completions"):
        return prepare_billable_request(payload,protocol=protocol)

    def test_image_input_is_disabled_by_default_and_text_quote_is_unchanged(self):
        with patch.dict(os.environ,{"GATEWAY_IMAGE_INPUT":""}):
            with self.assertRaises(PricingError):
                self.quote(self.payload())
            outgoing,quote=self.quote({"model":fixture.MODEL,"messages":[{"role":"user","content":"text"}],"max_completion_tokens":8})
        bound=len(json.dumps(outgoing,ensure_ascii=False,separators=(",",":")).encode())+4096
        self.assertEqual(json.loads(quote.snapshot)["input_bound"],bound)
        self.assertNotIn("images",json.loads(quote.snapshot))

    def test_both_protocols_accept_https_and_supported_data_mime_types(self):
        for protocol in ("chat.completions","responses"):
            for url in ("https://example.test/a.png",*[f"data:image/{mime};base64,aGVsbG8=" for mime in ("png","jpeg","webp","gif")]):
                with self.subTest(protocol=protocol,mime=url[:30]):
                    payload=self.payload(url,protocol=protocol)
                    outgoing,quote=self.quote(payload,protocol)
                    self.assertEqual(outgoing["messages" if protocol == "chat.completions" else "input"],payload["messages" if protocol == "chat.completions" else "input"])
                    self.assertEqual(json.loads(quote.snapshot)["images"],1)

    def test_invalid_schemes_mime_and_base64_are_rejected(self):
        for url in ("http://example.test/a.png","file:///a.png","https:///missing","https://user:pw@example.test/i","data:image/svg+xml;base64,aA==","data:image/png,hello","data:image/png;base64,@@@","data:image/png;base64,abc","data:image/png;base64,","data:image/jpg;base64,aA==","data:image/png;base64,a A=="):
            with self.subTest(url=url),self.assertRaises(PricingError):
                self.quote(self.payload(url))

    def test_image_count_maximum_sixteen_and_seventeenth_is_rejected(self):
        _,quote=self.quote(self.payload(count=16))
        self.assertEqual(json.loads(quote.snapshot)["images"],16)
        with self.assertRaises(PricingError):
            self.quote(self.payload(count=17))

    def test_decoded_data_size_accepts_exactly_eight_mb_and_rejects_one_more_byte(self):
        for size,valid in ((MAX_IMAGE_BYTES,True),(MAX_IMAGE_BYTES+1,False)):
            payload=self.payload("data:image/png;base64,"+base64.b64encode(b"a"*size).decode())
            if valid:
                _,quote=self.quote(payload)
                self.assertLess(json.loads(quote.snapshot)["input_bound"],13000)
            else:
                with self.assertRaises(PricingError):
                    self.quote(payload)

    def test_quote_replaces_data_url_bytes_adds_image_ceiling_and_uses_input_rate(self):
        payload=self.payload("data:image/png;base64,"+base64.b64encode(b"a"*20000).decode(),count=2)
        outgoing,quote=self.quote(payload)
        safe=json.loads(json.dumps(outgoing))
        for part in safe["messages"][0]["content"][1:]:
            part["image_url"]["url"]="[image]"
        bound=len(json.dumps(safe,ensure_ascii=False,separators=(",",":")).encode())+4096+2*8000
        snapshot=json.loads(quote.snapshot)
        self.assertEqual((snapshot["input_bound"],snapshot["images"],snapshot["image_token_ceiling"]),(bound,2,8000))
        card=PRICE_DATA["models"][fixture.MODEL]
        expected=int(((bound*max(Decimal(card["input"]),Decimal(card["cache_write"] or card["input"]))+8*Decimal(card["output"]))*720).to_integral_value(rounding=ROUND_CEILING))
        self.assertEqual(quote.amount_units,expected)
        self.assertEqual(snapshot["version"],PRICE_DATA["version"])

    def test_image_ceiling_participates_in_long_context_threshold(self):
        payload=self.payload(count=16)
        payload["messages"][0]["content"][0]["text"]="a"*150000
        _,quote=self.quote(payload)
        snapshot=json.loads(quote.snapshot)
        bound=snapshot["input_bound"]
        card=snapshot["card"]
        self.assertGreater(bound,card["long_context_threshold"])
        expected=int(((bound*max(Decimal(card["long_input"]),Decimal(card["long_cache_write"] or card["long_input"]))+8*Decimal(card["long_output"]))*720).to_integral_value(rounding=ROUND_CEILING))
        self.assertEqual(quote.amount_units,expected)

    def test_data_url_in_text_is_not_replaced_or_under_reserved(self):
        payload=self.payload()
        payload["messages"][0]["content"][0]["text"]="data:image/png;base64,"+"a"*100000
        outgoing,quote=self.quote(payload)
        full=len(json.dumps(outgoing,ensure_ascii=False,separators=(",",":")).encode())+4096+8000
        self.assertEqual(json.loads(quote.snapshot)["input_bound"],full)

    def test_capabilities_are_explicit_cover_all_priced_text_and_exclude_spark_and_images(self):
        expected={m for m,c in PRICE_DATA["models"].items() if c.get("output") is not None and not m.startswith("gpt-image-") and "codex-spark" not in m}
        data=json.loads(Path("app/model_capabilities.json").read_text())
        self.assertEqual(set(data["vision_models"]),expected)
        for model in ("gpt-5.3-codex-spark","gpt-image-2","unknown-model"):
            self.assertFalse(supports_vision(model))
            with self.assertRaises(PricingError):
                self.quote(self.payload(model=model))

    def test_files_audio_tools_bad_shapes_and_misplaced_images_still_rejected(self):
        shapes=[{"type":"input_file","file_id":"file_fixture"},{"type":"input_audio","input_audio":{}},{"type":"image_url","image_url":"https://example.test/a"},
                {"type":"image_url","image_url":{"url":"https://example.test/a","detail":"invalid"}},
                {"type":"input_image","image_url":"https://example.test/a"}]
        for shape in shapes:
            payload=self.payload()
            payload["messages"][0]["content"].append(shape)
            with self.subTest(shape=shape),self.assertRaises(PricingError):
                self.quote(payload)
        payload=self.payload()
        payload["tools"]=[{"type":"web_search"}]
        with self.assertRaises(PricingError):
            self.quote(payload)
        payload=self.payload()
        payload["metadata"]={"type":"image_url","image_url":{"url":"https://example.test/a"}}
        with self.assertRaises(PricingError):
            self.quote(payload)


class MultimodalGatewayTests(BackendCase):
    def test_image_settlement_uses_real_upstream_tokens_and_overrun_uses_existing_review(self):
        self.fund(500000000)
        payload=ImageInputTests().payload()
        with patch.dict(os.environ,{"GATEWAY_IMAGE_INPUT":"true"}):
            response=self.play(payload=payload)
        self.assertEqual(response.status_code,200)
        usage,hold,wallet,_=self.accounting()
        self.assertEqual(hold["actual_units"],fixture.EXPECTED_CHARGE_UNITS)
        self.assertEqual(hold["status"],"settled")
        self.assertEqual(json.loads(hold["pricing_snapshot"])["image_token_ceiling"],8000)
        self.assertEqual(self.calls[-1]["body"]["messages"],payload["messages"])
        self.response_json=fixture.chat_response()
        self.response_json["usage"].update(prompt_tokens=500000,total_tokens=500005)
        with patch.dict(os.environ,{"GATEWAY_IMAGE_INPUT":"true"}):
            self.assertEqual(self.play(payload=payload).status_code,200)
        self.assertEqual(self.accounting()[1]["status"],"reconciliation_required")
        self.assertEqual(self.accounting()[2]["balance_units"],500000000-fixture.EXPECTED_CHARGE_UNITS)

    def test_twenty_mb_request_bound_applies_to_playground_and_v1(self):
        self.fund()
        for path,body in (("/v1/chat/completions",self.request_payload()),("/developer/playground/run",{"key_id":self.key_id,"endpoint":"chat.completions","payload":self.request_payload()})):
            headers={**ORIGIN,"Authorization":"Bearer "+self.key,"Content-Length":str(20*1024*1024+1)}
            response=self.client.post(path,json=body,headers=headers)
            self.assertEqual(response.status_code,413)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.rows("SELECT * FROM credit_holds"),[])

    def test_public_catalog_and_models_context_have_vision_booleans(self):
        old_cache,old_expiry=ai_catalog._cache,ai_catalog._expires
        self.addCleanup(setattr,ai_catalog,"_cache",old_cache)
        self.addCleanup(setattr,ai_catalog,"_expires",old_expiry)
        ai_catalog._cache=None
        ai_catalog._expires=0
        async def get(client,*args,**kwargs):
            return fixture.httpx.Response(200,json={"data":[{"id":m} for m in (fixture.MODEL,"gpt-5.3-codex-spark","gpt-image-2")]})
        with patch("app.ai_catalog.httpx.AsyncClient.get",get):
            response=self.client.get("/api/public/ai-catalog")
        flags={r["id"]:r["vision"] for r in response.json()["models"]}
        self.assertEqual(flags,{fixture.MODEL:True,"gpt-5.3-codex-spark":False,"gpt-image-2":False})
        self.assertEqual({r["id"]:r["vision"] for r in self.client.get("/models").context["catalog"]["models"]},flags)


class MigrationTests(BackendCase):
    def test_legacy_database_migration_is_additive_and_idempotent(self):
        # Simulate an existing database that predates the new tables, while
        # retaining users/keys/accounting rows and their exact values.
        before=self.rows("SELECT * FROM users")
        keys=self.rows("SELECT * FROM api_keys")
        for table in ("user_sessions","password_reset_tokens","password_reset_limits"):
            self.execute(f"DROP TABLE {table}")
        db.init_db()
        db.init_db()
        self.assertEqual(self.rows("SELECT * FROM users"),before)
        self.assertEqual(self.rows("SELECT * FROM api_keys"),keys)
        for table in ("user_sessions","password_reset_tokens","password_reset_limits"):
            self.assertEqual(self.rows(f"SELECT * FROM {table}"),[])

    def test_route_registry_factory_has_no_duplicate_methods_and_uses_lifespan(self):
        app=fixture.main.create_app()
        pairs=[]
        for route in app.routes:
            for method in getattr(route,"methods",[]):
                pairs.append((route.path,method))
        self.assertEqual(len(pairs),len(set(pairs)))
        self.assertFalse(app.router.on_startup)
        self.assertFalse(app.router.on_shutdown)
        self.assertIn(("/account","GET"),pairs)
        self.assertIn(("/reset-password","POST"),pairs)
        self.assertNotIn("app.gateway",__import__("sys").modules)


class CatalogSeedTests(BackendCase):
    def test_seed_never_undoes_edits_or_restores_deleted_routes_and_endpoints(self):
        from scripts.seed_default_apis import seed
        from scripts.seed_default_apis import DEFAULT_APIS
        # Self-hosted defaults leave integrations absent; explicitly configure this fixture.
        with patch.dict(DEFAULT_APIS[0], {'base_url': 'https://core.example.test'}):
            seed()
        seed()
        api=self.rows("SELECT * FROM apis WHERE slug='luomocore'")[0]
        self.execute("UPDATE apis SET name='Custom',slug='custom',base_url='https://custom.example.test',enabled=0 WHERE id=?",(api["id"],))
        self.execute("DELETE FROM endpoints WHERE api_id=?",(api["id"],))
        self.execute("DELETE FROM public_gateway_routes WHERE public_path='luomocore/health'")
        seed()
        seed()
        saved=self.rows("SELECT * FROM apis WHERE id=?",(api["id"],))[0]
        self.assertEqual((saved["name"],saved["slug"],saved["base_url"],saved["enabled"]),("Custom","custom","https://custom.example.test",0))
        self.assertEqual(self.rows("SELECT * FROM apis WHERE slug='luomocore'"),[])
        self.assertEqual(self.rows("SELECT * FROM endpoints WHERE api_id=?",(api["id"],)),[])
        self.assertEqual(self.rows("SELECT * FROM public_gateway_routes WHERE public_path='luomocore/health'"),[])

    def test_existing_catalog_is_adopted_without_overwriting_domain_fields(self):
        from scripts.seed_default_apis import seed
        self.execute("INSERT INTO apis(name,slug,base_url,enabled,created_at,updated_at) VALUES ('Existing','luomocore','https://custom.example.test',0,?,?)",(now_iso(),now_iso()))
        seed()
        row=self.rows("SELECT * FROM apis WHERE slug='luomocore'")[0]
        self.assertEqual((row["name"],row["base_url"],row["enabled"]),("Existing","https://custom.example.test",0))


class BrowserFixtureTests(unittest.TestCase):
    def test_local_fixture_stays_inside_project_and_is_idempotent_with_sixty_settled_rows(self):
        import shutil
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory(prefix="march7th-fixture-check-") as directory:
            root=Path(directory)
            repo=Path(__file__).resolve().parents[1]
            (root/"app").symlink_to(repo/"app",target_is_directory=True)
            shutil.copytree(repo/"scripts",root/"scripts",ignore=shutil.ignore_patterns("__pycache__"))
            code='''import runpy,json
ns=runpy.run_path("scripts/credit_browser_fixture.py")
ns["seed_fixture"]()
with ns["connect"]() as c:
    user=c.execute("SELECT id FROM users WHERE username='qa_dev'").fetchone()[0]
    from app.auth import verify_password
    result={"keys":c.execute("SELECT COUNT(*) FROM api_keys WHERE user_id=?",(user,)).fetchone()[0],
        "usage":c.execute("SELECT COUNT(*),COUNT(DISTINCT substr(created_at,1,10)),SUM(success=0),COUNT(DISTINCT model) FROM api_usage_logs WHERE user_id=?",(user,)).fetchone()[:],
        "settled":c.execute("SELECT COUNT(*) FROM credit_holds WHERE status='settled' AND actual_units>0").fetchone()[0],
        "routes":c.execute("SELECT COUNT(*) FROM public_gateway_routes WHERE public_path LIKE 'qa/%'").fetchone()[0],
        "passwords":all(verify_password("FixtureOnly-2026",r[0]) for r in c.execute("SELECT password_hash FROM users")),
        "admin":c.execute("SELECT role FROM users WHERE username='qa_admin'").fetchone()[0],
        "path":str(ns["project"] / "output" / "browser-fixture.sqlite")}
print(json.dumps(result))
'''
            result=subprocess.run([sys.executable,"-c",code],cwd=root,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr[-1000:])
            data=json.loads(result.stdout)
            self.assertEqual(data["keys"],3)
            self.assertEqual(data["usage"],[60,14,12,3])
            self.assertEqual(data["settled"],60)
            self.assertEqual(data["routes"],2)
            self.assertTrue(data["passwords"])
            self.assertEqual(data["admin"],"admin")
            self.assertEqual(Path(data["path"]).resolve(),(root/"output"/"browser-fixture.sqlite").resolve())


class ImageOverrunTests(BackendCase):
    def test_funded_image_overrun_still_requires_review_and_audited_resolution(self):
        from app.credit_ledger import resolve_credit_hold
        self.fund(500000000)
        payload=ImageInputTests().payload()
        self.response_json=fixture.chat_response()
        self.response_json["usage"].update(prompt_tokens=50000,total_tokens=50005)
        with patch.dict(os.environ,{"GATEWAY_IMAGE_INPUT":"true"}):
            self.assertEqual(self.play(payload=payload).status_code,200)
        usage,hold,wallet,_=self.accounting()
        self.assertGreater(hold["actual_units"],hold["amount_units"])
        self.assertLess(hold["actual_units"],wallet["balance_units"])
        self.assertEqual(hold["status"],"reconciliation_required")
        self.assertEqual(wallet["balance_units"],500000000)
        self.assertEqual(usage["credit_charged_units"],0)
        self.assertEqual(self.play().status_code,409)
        self.as_admin()
        with closing(self.open_database()) as conn,conn:
            conn.execute("BEGIN IMMEDIATE")
            resolved=resolve_credit_hold(conn,usage["id"],hold["actual_units"],actor_id=self.user_id,reason="Verified image receipt")
        self.assertEqual(resolved["status"],"settled")
        self.assertEqual(self.accounting()[2]["balance_units"],500000000-hold["actual_units"])
        self.assertEqual(len(self.rows("SELECT * FROM credit_admin_audit WHERE action='resolve_hold'")),1)

    def test_key_list_invalid_integer_never_becomes_a_server_error(self):
        for key_id in (0,-1,2**70):
            self.assertEqual(self.client.get("/developer/playground/models",params={"key_id":key_id}).status_code,422)

    def test_extracted_auth_and_legacy_patch_seams_both_remain_effective(self):
        from app.routes.compat import legacy_override
        from app.routes import auth as routes_auth
        original=routes_auth.authenticate_user
        sentinel=object()
        with patch.object(routes_auth,"authenticate_user",sentinel):
            self.assertIs(legacy_override("authenticate_user",routes_auth.authenticate_user),sentinel)
        with patch.object(fixture.main,"authenticate_user",sentinel):
            self.assertIs(legacy_override("authenticate_user",original),sentinel)
