from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from suunto.access import Access
from suunto.web import create_app
from suunto.web_security import MAX_BODY


class WebSecurityTest(unittest.TestCase):
    def test_request_policy_preserves_valid_lan_and_proxy_origins(self):
        with TemporaryDirectory() as directory, TestClient(create_app(directory)) as browser:
            for host in ("192.168.8.10:7383", "private.example.test", "[fd00::8]:7383"):
                self.assertEqual(browser.get("/healthz", headers={"Host": host}).status_code, 200)
            response = browser.get("/healthz", headers={"Host": "invalid host"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["x-frame-options"], "DENY")
            self.assertEqual(browser.post("/settings", data={"minutes": 17}, follow_redirects=False).status_code, 303)
            self.assertEqual(browser.post("/settings", data={"minutes": 17}, headers={"Sec-Fetch-Site": "same-site"}).status_code, 403)
            self.assertEqual(browser.post("/settings", content=b"a" * (MAX_BODY + 1)).status_code, 413)
            self.assertEqual(browser.post("/settings", content=iter((b"a" * MAX_BODY, b"b"))).status_code, 413)
            self.assertEqual(browser.post("/settings", files={"unused": ("sample.txt", "sample")}).status_code, 415)
            response = browser.post("/oauth/start", headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
            self.assertIn("请从看板页面发起授权", response.text)

    def test_private_routes_keep_password_gate(self):
        with TemporaryDirectory() as directory:
            Access(directory).set_password("fixture-password-for-tests")
            with TestClient(create_app(directory), base_url="https://dashboard.example.test") as browser:
                self.assertEqual(browser.get("/healthz").status_code, 200)
                self.assertEqual(browser.get("/export.csv").status_code, 401)
                self.assertEqual(browser.get("/export.csv", auth=("suunto", "fixture-password-for-tests")).status_code, 200)
