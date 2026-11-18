import json
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import httpx

from suunto.mcp import Client, ISSUER, REDIRECT, save_json
from suunto.web import create_app


class ProbeTest(unittest.TestCase):
    lan_origin = "http://192.168.1.50:8000"

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.grants = []
        self.tool_calls = []
        self.redirect = self.lan_origin + "/oauth/callback"
        transport = httpx.MockTransport(self.server)
        self.mcp = Client(directory.name, httpx.Client(transport=transport))
        self.addCleanup(self.mcp.http.close)

    def server(self, request):
        path = request.url.path
        if path.startswith("/.well-known/"):
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                "registration_endpoint": ISSUER + "/register", "token_endpoint": ISSUER + "/token",
                "code_challenge_methods_supported": ["S256"],
            })
        if path == "/register":
            self.assertEqual(json.loads(request.content)["redirect_uris"], [self.redirect])
            return httpx.Response(201, json={"client_id": "client"})
        if path == "/token":
            form = parse_qs(request.content.decode())
            self.grants.append(form["grant_type"][0])
            if "code" in form:
                self.assertEqual(form["code"], ["one-time-code"])
                self.assertIn("code_verifier", form)
                self.assertEqual(form["redirect_uri"], [self.redirect])
            return httpx.Response(200, json={
                "access_token": "token", "refresh_token": "refresh", "expires_in": 3600,
            })
        packet = json.loads(request.content)
        method = packet["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18"}
        elif method == "tools/list":
            if packet["params"].get("cursor") == "next":
                result = {"tools": [{"name": "activate_training_program"}]}
            else:
                result = {"tools": [{"name": "list_user_routes", "inputSchema": {"type": "object"}}],
                          "nextCursor": "next"}
        else:
            self.tool_calls.append(packet["params"]["name"])
            data = [{"routeId": "route-a"}] if "page" in packet["params"]["arguments"] else [
                {"distance": 2.5}, {"distance": None}]
            result = {"content": [{"type": "text", "text": json.dumps(data)}]}
        packet = {"id": packet["id"], "jsonrpc": "2.0", "result": result}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=f"data: {json.dumps(packet)}\n\n")

    def test_callback_pagination_and_only_read_tools(self):
        with TestClient(create_app(client=self.mcp), base_url=self.lan_origin) as browser:
            self.assertEqual(browser.post("/api/probe").json()["status"], "authorization_required")
            self.assertNotIn("/oauth/manual", browser.get("/").text)
            start = browser.post("/oauth/start", follow_redirects=False)
            self.assertEqual(start.status_code, 303)
            location = urlsplit(start.headers["location"])
            self.assertEqual(location.netloc, "mcp.suunto.cn")
            self.assertEqual(parse_qs(location.query)["redirect_uri"], [self.redirect])
            state = browser.cookies["suunto_oauth_state"]
            browser.get("/oauth/callback", params={"code": "one-time-code", "state": "invalid"})
            self.assertFalse(self.grants)
            callback = browser.get("/oauth/callback", params={"code": "one-time-code", "state": state})
            self.assertIn("Suunto 授权成功", callback.text)
            sync = browser.get("/api/status").json()
            self.assertEqual(sync["runs"][0]["trigger"], "oauth")
            self.assertEqual(sync["counts"]["routes"], 1)
            self.tool_calls.clear()
            tools = browser.post("/api/probe").json()["tools"]
            self.assertEqual([tool["allowed_read"] for tool in tools], [True, False])
            for name in ("activate_training_program", "list_user_workouts"):
                self.assertEqual(browser.post("/api/probe/sample", json={"tool": name, "arguments": {}}).status_code, 400)
            payload = browser.post("/api/probe/sample", json={"tool": "list_user_routes", "arguments": {}}).json()
            self.assertEqual(payload["observed_fields"]["content[][]/distance"], ["null", "number"])
            self.assertEqual(self.tool_calls, ["list_user_routes"])

    def test_lan_registration_preserves_existing_token_until_callback(self):
        save_json(self.mcp.path, {"client_id": "previous", "redirect_uri": REDIRECT,
                                 "access_token": "existing", "refresh_at": 9_999_999_999})
        with TestClient(create_app(client=self.mcp), base_url=self.lan_origin) as browser:
            browser.post("/oauth/start", follow_redirects=False)
            self.assertEqual(self.mcp.credential()["access_token"], "existing")
            state = browser.cookies["suunto_oauth_state"]
            browser.get("/oauth/callback", params={"code": "one-time-code", "state": state})
            self.assertEqual(self.grants, ["authorization_code"])
            self.assertEqual(self.mcp.credential()["redirect_uri"], self.redirect)
            with self.mcp.lock:
                state = self.mcp.credential()
                state["refresh_at"] = 0
                self.mcp.path.write_text(json.dumps(state))
            self.assertEqual(browser.post("/api/probe").json()["status"], "ready")
            self.assertEqual(self.grants, ["authorization_code", "refresh_token"])

    def assert_oauth_at(self, origin):
        self.redirect = origin + "/oauth/callback"
        with TestClient(create_app(client=self.mcp), base_url=origin) as browser:
            self.assertEqual(browser.get("/").headers["referrer-policy"], "same-origin")
            for invalid_origin in ("null", "https://other.example.com"):
                response = browser.post("/oauth/start", headers={"Origin": invalid_origin}, follow_redirects=False)
                self.assertIn('<p role="alert">请从看板页面发起授权。</p>', response.text)
                self.assertIsNone(self.mcp.pending)
            start = browser.post("/oauth/start", headers={"Origin": origin}, follow_redirects=False)
            self.assertEqual(start.status_code, 303)
            params = parse_qs(urlsplit(start.headers["location"]).query)
            self.assertEqual(params["redirect_uri"], [self.redirect])
            callback = browser.get("/oauth/callback", params={"code": "one-time-code", "state": params["state"][0]},
                                   follow_redirects=False)
            self.assertEqual(callback.headers["location"], "/?authorized=true")
            self.assertEqual(self.grants, ["authorization_code"])
            self.assertEqual(browser.get("/api/status").json()["runs"][0]["trigger"], "oauth")

    def test_custom_http_domain_and_port_complete_oauth(self):
        self.assert_oauth_at("http://dashboard.example.com:7383")

    def test_custom_https_domain_and_port_complete_oauth(self):
        self.assert_oauth_at("https://training.example.org:9443")

    def test_https_domain_with_default_port_completes_oauth(self):
        self.assert_oauth_at("https://sports.example.net")

    def test_private_ip_with_custom_port_completes_oauth(self):
        self.assert_oauth_at("http://10.72.0.42:18000")
