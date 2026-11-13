"""Phase B console contracts: browser form errors, range picker, device labels, playground preselect, charts."""
import re
import unittest
from contextlib import closing

from test_backend_v2 import BackendCase, ORIGIN
from app.utils.timefmt import now_iso
from app.web.templating import device

BROWSER = {"Origin": ORIGIN["Origin"], "Accept": "text/html,application/xhtml+xml"}


class BrowserFormErrorTests(BackendCase):
    def make_route_with_history(self):
        self.as_admin()
        api_id = self.execute("INSERT INTO apis(name,slug,base_url,enabled,created_at,updated_at) VALUES ('UI','ui-'||hex(randomblob(4)),'https://example.test',1,?,?)", (now_iso(), now_iso()))
        route_id = self.execute("""INSERT INTO public_gateway_routes(name,public_path,target_api_id,target_method,target_path,required_scope,enabled,public_docs,allow_anonymous,created_at,updated_at)
            VALUES ('UI route','ui/'||hex(randomblob(4)),?,'GET','/x','ui:read',0,1,0,?,?)""", (api_id, now_iso(), now_iso()))
        self.execute("INSERT INTO api_usage_logs(created_at,user_id,route_id,method,public_path,status_code,success) VALUES (?,?,?,'GET','ui/x',200,1)",
                     (now_iso(), self.user_id, route_id))
        return route_id

    def test_browser_form_failure_renders_html_page_with_original_status(self):
        route_id = self.make_route_with_history()
        response = self.client.post(f"/admin/routes/{route_id}/delete", headers=BROWSER)
        self.assertEqual(response.status_code, 409)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("已有调用记录", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIsNone(re.search(r"\sstyle\s*=", response.text))

    def test_api_style_clients_still_receive_json_errors(self):
        route_id = self.make_route_with_history()
        response = self.client.post(f"/admin/routes/{route_id}/delete", headers=ORIGIN)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "此路由已有调用记录，不能删除，请改为停用。")

    def test_browser_validation_error_is_html_but_gateway_stays_json(self):
        response = self.client.post("/account/password", data={"current_password": "x"}, headers=BROWSER)
        self.assertEqual(response.status_code, 422)
        self.assertIn("表单内容不完整", response.text)
        gateway = self.client.post("/v1/chat/completions", content=b"{", headers={**BROWSER, "Authorization": "Bearer nope", "Content-Type": "application/json"})
        self.assertIn("application/json", gateway.headers["content-type"])

    def test_api_root_paths_keep_json_errors_for_browsers(self):
        for path in ("/api", "/api/", "/v1"):
            with self.subTest(path=path):
                response = self.client.post(path, headers=BROWSER, follow_redirects=False)
                self.assertNotEqual(response.status_code, 200)
                self.assertNotIn("text/html", response.headers.get("content-type", ""), path)

    def test_login_redirect_is_not_swallowed_by_form_error_page(self):
        self.client.cookies.clear()
        response = self.client.post("/account/password", data={"current_password": "a", "new_password": "b", "confirm_password": "b"}, headers=BROWSER, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")


class ConsolePageTests(BackendCase):
    def test_usage_range_picker_accepts_only_known_spans(self):
        for value, label in (("7", "7"), ("30", "30"), ("999", "14"), ("-1", "14"), ("", "14")):
            with self.subTest(days=value):
                page = self.client.get("/developer/usage", params={"days": value})
                self.assertEqual(page.status_code, 200)
                self.assertIn(f'<a href="/developer/usage?days={label}" aria-current="page">', page.text)
                self.assertIn(f"每日请求，共 {label} 天" if self.has_calls() else "这段时间还没有调用", page.text)

    def has_calls(self):
        with closing(self.open_database()) as conn:
            return conn.execute("SELECT COUNT(*) FROM api_usage_logs WHERE user_id=?", (self.user_id,)).fetchone()[0] > 0

    def test_recent_table_follows_the_selected_range_like_the_chart(self):
        from datetime import datetime, timedelta, timezone
        stamp = (datetime.now(timezone.utc) - timedelta(days=10)).replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
        self.execute("""INSERT INTO api_usage_logs(created_at,started_at,completed_at,user_id,api_key_id,method,public_path,status_code,success,model,outcome,protocol,request_id)
            VALUES (?,?,?,?,?,'POST','v1/chat/completions',200,1,'gpt-6-luna','succeeded','chat.completions','ten-days-ago')""",
                     (stamp, stamp, stamp, self.user_id, self.key_id))
        wide = self.client.get("/developer/usage", params={"days": "30"}).text
        self.assertIn("ten-days-ago", wide)
        self.assertIn("每日请求，共 30 天", wide)
        narrow = self.client.get("/developer/usage", params={"days": "7"}).text
        self.assertNotIn("ten-days-ago", narrow)

    def test_dashboard_chart_renders_svg_bars_without_inline_styles(self):
        self.execute("""INSERT INTO api_usage_logs(created_at,started_at,completed_at,user_id,api_key_id,method,public_path,status_code,success,model,outcome)
            VALUES (?,?,?,?,?,'POST','v1/chat/completions',200,1,'gpt-6-luna','succeeded')""", (now_iso(), now_iso(), now_iso(), self.user_id, self.key_id))
        page = self.client.get("/developer")
        self.assertEqual(page.status_code, 200)
        self.assertIn('class="bar-a"', page.text)
        self.assertIn("可以正常领取密钥和使用 API", page.text)
        self.assertIsNone(re.search(r"\sstyle\s*=", page.text))

    def test_playground_preselects_only_the_callers_own_key(self):
        page = self.client.get("/developer/playground", params={"key": str(self.key_id)})
        self.assertRegex(page.text, rf'<option value="{self.key_id}" selected>')
        other_user = self.new_user()
        other_key = self.execute("""INSERT INTO api_keys(user_id,name,key_prefix,key_hash,status,scopes,created_at)
            VALUES (?,'other','lapi_other','x','active','chat:completions',?)""", (other_user, now_iso()))
        page = self.client.get("/developer/playground", params={"key": str(other_key)})
        self.assertNotIn(f'value="{other_key}"', page.text)

    def test_account_page_lists_current_device_with_readable_label(self):
        page = self.client.get("/account")
        self.assertEqual(page.status_code, 200)
        self.assertIn("当前设备", page.text)


class DeviceLabelTests(unittest.TestCase):
    def test_common_user_agents(self):
        cases = {
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36": "Chrome · macOS",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36 Edg/141.0": "Edge · Windows",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Mobile/15E148 Safari/604.1": "Safari · iOS",
            "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0": "Firefox · Linux",
            "curl/8.7.1": "curl",
            "": "未知设备",
            None: "未知设备",
        }
        for agent, expected in cases.items():
            with self.subTest(agent=agent):
                self.assertEqual(device(agent), expected)


if __name__ == "__main__":
    unittest.main()
