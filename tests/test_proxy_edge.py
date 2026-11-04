"""Behaviour behind the production chain: Cloudflare -> cloudflared -> docker-proxy (172.20.0.1) -> app.

No startup hooks run here (the client is not used as a context manager), so no database is touched.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="proxy-edge-")
os.environ.setdefault("DATABASE_PATH", str(Path(_tmp) / "import.sqlite"))
os.environ.setdefault("SESSION_SECRET", "proxy-edge-fixture-session-secret-more-than-32-characters")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

BRIDGE = "172.20.0.1"  # docker-proxy hop, on TRUSTED_PROXY_PEERS by default
STRANGER = "10.9.9.9"  # another container on a shared network
EDGE = {"x-forwarded-proto": "https", "cf-visitor": '{"scheme":"https"}', "cf-connecting-ip": "203.0.113.9"}


def client_from(peer: str) -> TestClient:
    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (peer, 40000)}
        await app(scope, receive, send)
    return TestClient(wrapped, base_url="http://api.march7th.cn")


class ProxyEdgeTests(unittest.TestCase):
    def test_trailing_slash_redirect_keeps_https_behind_trusted_proxy(self):
        response = client_from(BRIDGE).get("/v1", headers=EDGE, follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "https://api.march7th.cn/v1/")

    def test_untrusted_peer_cannot_claim_https(self):
        response = client_from(STRANGER).get("/v1", headers=EDGE, follow_redirects=False)
        self.assertTrue(response.headers["location"].startswith("http://"))

    def test_error_redirect_reports_visitor_not_bridge(self):
        response = client_from(BRIDGE).get("/no-such-page", headers=EDGE, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("ip=203.0.113.9", response.headers["location"])
        self.assertNotIn(BRIDGE, response.headers["location"])

    def test_untrusted_peer_cannot_choose_reported_address(self):
        response = client_from(STRANGER).get("/no-such-page", headers=EDGE, follow_redirects=False)
        self.assertNotIn("203.0.113.9", response.headers["location"])

    def test_font_is_served_as_woff2(self):
        response = client_from(BRIDGE).get("/static/fonts/interchange-mono.woff2")
        self.assertEqual(response.headers["content-type"], "font/woff2")

    def test_security_txt_has_expires(self):
        lines = client_from(BRIDGE).get("/.well-known/security.txt").text.splitlines()
        self.assertTrue(any(line.startswith("Expires: ") for line in lines))


if __name__ == "__main__":
    unittest.main()
