from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from suunto.store import Store
from suunto.web import create_app


class AmapTest(unittest.TestCase):
    def test_opt_in_and_server_only_security_code(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize()
            at = "2026-01-01T08:00:00+08:00"
            run = store.start("fixture", at)
            store.commit(run, "routes", [("route", at, {"routeId": "route", "name": "Test route",
                         "startPoint": {"latitude": 30.5, "longitude": 120.5}})])
            client = TestClient(create_app(directory))
            endpoint = "/_AMapService/v3/assistant/coordinate/convert"
            self.assertNotIn("amap-preview.js", client.get("/routes").text)
            self.assertEqual(client.get(endpoint).status_code, 404)
            self.assertEqual(client.post("/settings/amap", data={"enabled": "true"}).status_code, 422)
            key, secret = "a" * 32, "b" * 32
            enabled = {"enabled": "true", "coordinates": "gps"}
            self.assertEqual(client.post("/settings/amap", data={**enabled, "key": key, "secret": secret},
                                         follow_redirects=False).status_code, 303)
            self.assertNotIn(secret, client.get("/settings").text)
            self.assertNotIn(secret, client.get("/routes").text)
            self.assertIn(f'data-amap-key="{key}"', client.get("/routes").text)
            self.assertNotIn("amap-preview.js", client.get("/routes?q=unknown").text)
            self.assertEqual(client.get("/_AMapService/v3/place/text").status_code, 404)
            self.assertEqual(client.get(endpoint, headers={"sec-fetch-site": "cross-site"}).status_code, 403)
            with patch("suunto.amap.httpx.get", return_value=httpx.Response(
                    200, text='/**/amapDone({"status":"1"});')) as upstream:
                reply = client.get(endpoint, params={"key": "spoof", "jscode": "spoof", "callback": "amapDone"})
                self.assertEqual(reply.status_code, 200)
                self.assertEqual(reply.text, 'amapDone({"status": "1"});')
                self.assertEqual(upstream.call_args.args[0], f"https://restapi.amap.com{endpoint.removeprefix('/_AMapService')}")
                self.assertEqual(dict(upstream.call_args.kwargs["params"]),
                                 {"key": key, "jscode": secret, "callback": "amapDone"})
                upstream.return_value = httpx.Response(429)
                self.assertEqual(client.get(endpoint).status_code, 429)
                upstream.return_value = httpx.Response(200, json={"reflected": secret})
                self.assertNotIn(secret, client.get(endpoint).text)
            client.post("/settings/amap", data=enabled)
            self.assertIn(f'data-amap-key="{key}"', client.get("/routes").text)
            client.post("/settings/amap", data={"clear": "true"})
            self.assertEqual(client.get(endpoint).status_code, 404)
            self.assertNotIn("amap-preview.js", client.get("/routes").text)
