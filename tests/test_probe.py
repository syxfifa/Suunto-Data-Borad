import json
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import httpx

from suunto.mcp import Client, ISSUER, REDIRECT
from suunto.web import create_app


class ProbeTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.grants = []
        self.tool_calls = []
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
            self.assertEqual(json.loads(request.content)["redirect_uris"], [REDIRECT])
            return httpx.Response(201, json={"client_id": "client"})
        if path == "/token":
            form = parse_qs(request.content.decode())
            self.grants.append(form["grant_type"][0])
            if "code" in form:
                self.assertEqual(form["code"], ["one-time-code"])
                self.assertIn("code_verifier", form)
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
            result = {"content": [{"type": "text", "text": '[{"distance":2.5},{"distance":null}]'}]}
        packet = {"id": packet["id"], "jsonrpc": "2.0", "result": result}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=f"data: {json.dumps(packet)}\n\n")

    def test_callback_pagination_and_only_read_tools(self):
        with TestClient(create_app(client=self.mcp)) as browser:
            self.assertEqual(browser.post("/api/probe").json()["status"], "authorization_required")
            browser.post("/oauth/start")
            state = browser.cookies["suunto_oauth_state"]
            browser.get("/oauth/callback", params={"code": "one-time-code", "state": "invalid"})
            self.assertFalse(self.grants)
            browser.get("/oauth/callback", params={"code": "one-time-code", "state": state})
            tools = browser.post("/api/probe").json()["tools"]
            self.assertEqual([tool["allowed_read"] for tool in tools], [True, False])
            for name in ("activate_training_program", "list_user_workouts"):
                self.assertEqual(browser.post("/api/probe/sample", json={"tool": name, "arguments": {}}).status_code, 400)
            payload = browser.post("/api/probe/sample", json={"tool": "list_user_routes", "arguments": {}}).json()
            self.assertEqual(payload["observed_fields"]["content[][]/distance"], ["null", "number"])
            self.assertEqual(self.tool_calls, ["list_user_routes"])

    def test_manual_code_and_refresh(self):
        with TestClient(create_app(client=self.mcp)) as browser:
            browser.post("/oauth/start")
            browser.post("/oauth/manual", data={"authorization": "one-time-code"})
            self.assertEqual(self.grants, ["authorization_code"])
            with self.mcp.lock:
                state = self.mcp.credential()
                state["refresh_at"] = 0
                self.mcp.path.write_text(json.dumps(state))
            self.assertEqual(browser.post("/api/probe").json()["status"], "ready")
            self.assertEqual(self.grants, ["authorization_code", "refresh_token"])
