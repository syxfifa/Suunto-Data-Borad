from datetime import datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import Mock
from urllib.parse import unquote
from zipfile import ZipFile

from fastapi.testclient import TestClient
import httpx

from suunto import VERSION
from suunto.access import Access, USERNAME
from suunto.maintenance import backup, last_verified_backup, restore, verify, volume_lock
from suunto.mcp import Client, MCPError, save_json
from suunto.store import SCHEMA_VERSION, Store
from suunto.sync import TZ
from suunto.web import create_app


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = Store(self.directory)
        self.store.initialize()
        self.today = datetime.now(TZ).date()
        self.at = f"{self.today}T01:00:00+08:00"
        self.run_id = self.store.start("test", self.at)

    def test_export_current_filters_and_shared_field_allowlist(self):
        rows = [
            ("workout-a", self.at, {"workoutId": "workout-a", "activityName": "Running", "totalDistance": 6000,
                                   "workoutName": "Hidden training name", "tss": 43}),
            ("workout-b", self.at, {"workoutId": "workout-b", "activityName": "Cycling", "totalDistance": 6000,
                                   "workoutName": "Hidden training name", "tss": 50}),
            ("workout-c", self.at, {"workoutId": "workout-c", "activityName": "Running", "totalDistance": 2000,
                                   "workoutName": "Small run", "tss": 10}),
        ]
        self.store.commit(self.run_id, "workouts", rows)
        self.store.commit(self.run_id, "routes", [("route-a", self.at, {
            "description": "Hidden route", "totalDistance": 6000, "startPoint": {"latitude": 30.56789},
        })])
        with TestClient(create_app(self.directory)) as browser:
            page = browser.get(f"/activities?end={self.today}&sport=Running&km=3&tss=30&q=Hidden")
            self.assertIn("导出当前筛选", page.text)
            self.assertIn("source=workouts", unquote(page.text))
            response = browser.get(f"/export.csv?source=workouts&end={self.today}&sport=Running&km=3&tss=30&q=Hidden")
            self.assertIn("workout-a", response.text)
            self.assertNotIn("workout-b", response.text)
            self.assertNotIn("workout-c", response.text)
            shared = browser.get("/export.csv?share=true&format=raw").text
            for secret in ("workout-a", "Hidden route", "Hidden training name", "30.56789", "payload"):
                self.assertNotIn(secret, shared)
            self.assertIn("distance_km", shared)
            self.assertIn("6.0", shared)
            self.assertEqual(browser.get("/export.csv?source=unknown").status_code, 422)
            self.assertEqual(browser.get("/export.csv?start=2026-01-02&end=2026-01-01").status_code, 422)

    def test_backup_verify_and_locked_restore(self):
        self.store.commit(self.run_id, "routes", [("route-a", self.at, {"routeId": "route-a"})])
        save_json(self.directory / "config.json", {"sync_interval_minutes": 15})
        save_json(self.directory / "token.json", {"access_token": "fake-token"})
        password = "fixture-password-100"
        Access(self.directory).set_password(password)
        basic = BytesIO()
        backup(self.directory, basic)
        self.assertEqual(verify(basic), ["database", "settings"])
        self.assertIsNone(last_verified_backup(self.directory))
        self.assertEqual(verify(basic, record_to=self.directory), ["database", "settings"])
        verified_at = last_verified_backup(self.directory)
        self.assertTrue(verified_at.endswith("+08:00"))
        with TestClient(create_app(self.directory), base_url="https://testserver") as browser:
            self.assertIn(verified_at, browser.get("/maintenance", auth=(USERNAME, password)).text)
        secret = BytesIO()
        backup(self.directory, secret, include_token=True)
        self.assertEqual(verify(secret), ["database", "settings", "token"])
        with TemporaryDirectory() as target:
            with volume_lock(target):
                with self.assertRaisesRegex(ValueError, "停止 app"):
                    restore(target, basic, "database")
            restore(target, basic, "database")
            restore(target, basic, "settings")
            self.assertEqual(Store(target).record("routes", "route-a"), {"routeId": "route-a"})
            self.assertEqual(json.loads(Path(target, "config.json").read_bytes())["sync_interval_minutes"], 15)
            with self.assertRaisesRegex(ValueError, "--replace"):
                restore(target, basic, "database")
            with self.assertRaisesRegex(ValueError, "--same-account"):
                restore(target, secret, "token")
            restore(target, secret, "token", same_account=True)
            self.assertFalse(Path(target, "access.json").exists())
            with self.assertRaisesRegex(ValueError, "--same-account"):
                restore(target, basic, "database", replace=True)
            restore(target, basic, "database", replace=True, same_account=True)
        invalid = BytesIO()
        with ZipFile(invalid, "w") as archive:
            archive.writestr("suunto.sqlite", "broken")
            archive.writestr("../unexpected.json", "{}")
        with self.assertRaises(ValueError):
            verify(invalid, record_to=self.directory)
        self.assertEqual(last_verified_backup(self.directory), verified_at)

    def test_manifest_detects_changed_file_even_with_valid_zip_crc(self):
        settings = self.directory / "config.json"
        save_json(settings, {"sync_interval_minutes": 15})
        archive = BytesIO()
        backup(self.directory, archive)
        with ZipFile(archive) as source:
            files = {name: source.read(name) for name in source.namelist()}
        manifest = json.loads(files["manifest.json"])
        self.assertEqual((manifest["format"], manifest["app_version"], manifest["sqlite_user_version"]),
                         (1, VERSION, SCHEMA_VERSION))
        self.assertEqual(set(manifest["files"]), {"suunto.sqlite", "config.json"})
        self.assertTrue(manifest["created_at"].endswith("+00:00"))

        changed = BytesIO()
        with ZipFile(changed, "w") as rebuilt:
            for name, content in files.items():
                rebuilt.writestr(name, b'{"sync_interval_minutes":25}' if name == "config.json" else content)
        with self.assertRaisesRegex(ValueError, "与清单不一致"):
            verify(changed)

        # A pre-manifest archive containing an unversioned database remains restorable.
        with self.store.connect() as db:
            db.execute("PRAGMA user_version=0")
        legacy = BytesIO()
        unversioned = BytesIO()
        backup(self.directory, unversioned)
        with ZipFile(unversioned) as source, ZipFile(legacy, "w") as rebuilt:
            for name in ("suunto.sqlite", "config.json"):
                rebuilt.writestr(name, source.read(name))
        self.assertEqual(verify(legacy), ["database", "settings"])
        with TemporaryDirectory() as target:
            restore(target, legacy, "database")
            migrated = Store(target)
            self.assertEqual(migrated.query("PRAGMA user_version")[0]["user_version"], 0)
            migrated.initialize()
            self.assertEqual(migrated.query("PRAGMA user_version")[0]["user_version"], SCHEMA_VERSION)

    def test_live_backup_sees_committed_wal_and_excludes_uncommitted_rows(self):
        with self.store.connect() as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO records VALUES ('workouts', 'committed', ?, '{}')", (self.at,))
            writer.commit()
            writer.execute("INSERT INTO records VALUES ('workouts', 'pending', ?, '{}')", (self.at,))
            self.assertTrue(Path(self.directory, "suunto.sqlite-wal").is_file())
            archive = BytesIO()
            backup(self.directory, archive)
            writer.rollback()
        with TemporaryDirectory() as target:
            restore(target, archive, "database")
            self.assertIsNotNone(Store(target).record("workouts", "committed"))
            self.assertIsNone(Store(target).record("workouts", "pending"))

    def test_cli_binary_archive_matches_between_stdout_and_stdin(self):
        def run(directory, command, data=None):
            return subprocess.run([sys.executable, "-m", "suunto", "--data-dir", str(directory), command],
                                  input=data, capture_output=True, check=True).stdout
        archive = run(self.directory, "backup")
        self.assertEqual(json.loads(run(self.directory, "verify", archive))["components"], ["database"])
        with TemporaryDirectory() as target:
            run(target, "restore", archive)
            self.assertTrue(Path(target, "suunto.sqlite").exists())

    def test_prune_preserves_records_last_success_and_running(self):
        old = (datetime.now(TZ) - timedelta(days=150)).isoformat()
        recent_cutoff = (datetime.now(TZ) - timedelta(days=90)).isoformat()
        good = self.store.start("scheduled", old)
        self.store.finish(good, old, "ok")
        failed = self.store.start("test", old)
        self.store.error(failed, "sleep", "old private log")
        self.store.finish(failed, old, "failed")
        self.store.commit(good, "workouts", [("a", old, {"workoutId": "a", "activityName": "Running"})])
        self.store.prune_logs(recent_cutoff)
        self.assertEqual(self.store.status()["last_success_at"], old)
        self.assertEqual({r["id"] for r in self.store.status()["runs"]}, {good, self.run_id})
        self.assertEqual(self.store.status()["errors"], [])
        self.assertEqual(self.store.record("workouts", "a")["workoutId"], "a")
        with self.store.connect() as db:
            plan = db.execute("""EXPLAIN QUERY PLAN SELECT * FROM records
                              WHERE source='workouts' AND json_extract(payload,'$.activityName')=? AND occurred_at>=?""",
                              ("Running", old)).fetchall()
        self.assertIn("workouts_sport_time", " ".join(row[3] for row in plan))

    def test_https_access_gate_and_sanitized_diagnostics(self):
        self.store.error(self.run_id, "workouts", "secret-token dashboard.example.test 30.56789")
        password = "valid-long-test-password"
        with TestClient(create_app(self.directory), base_url="https://dashboard.example.test:9443") as browser:
            self.assertEqual(browser.get("/healthz").json(), {"status": "ok"})
            self.assertEqual(browser.get("/readyz").status_code, 503)
            denied = browser.post("/maintenance/access", data={"mode": "enable", "password": password},
                                  headers={"Origin": "https://other.example.test"})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(browser.post("http://dashboard.example.test:9443/maintenance/access",
                data={"mode": "enable", "password": password}).status_code, 403)
            enabled = browser.post("/maintenance/access", data={"mode": "enable", "password": password},
                                   follow_redirects=False)
            self.assertEqual(enabled.status_code, 303)
            for path in ("/", "/settings", "/export.csv", "/diagnostics.json", "/readyz"):
                self.assertEqual(browser.get(path).status_code, 401)
            self.assertEqual(browser.get("http://dashboard.example.test:9443/").status_code, 403)
            self.assertEqual(browser.get("/healthz").status_code, 200)
            self.assertEqual(browser.get("/export.csv", auth=("suunto", "wrong")).status_code, 401)
            report = browser.get("/diagnostics.json", auth=("suunto", password))
            self.assertEqual(report.json()["version"], VERSION)
            for forbidden in ("secret-token", "dashboard.example.test", "30.56789", password):
                self.assertNotIn(forbidden, report.text)

    def test_disconnect_keeps_records_and_requires_explicit_reconnect(self):
        mcp = Client(self.directory)
        mcp.tools = Mock(return_value=[])
        mcp.begin = Mock(return_value=("https://mcp.suunto.cn/authorize", "test-state"))
        save_json(mcp.path, {"access_token": "fake-token", "refresh_token": "fake-refresh"})
        self.store.commit(self.run_id, "workouts", [("old", self.at, {"workoutId": "old"})])
        with TestClient(create_app(client=mcp), base_url="http://localhost:8000") as browser:
            self.assertEqual(browser.post("/disconnect", follow_redirects=False).status_code, 303)
            self.assertFalse(mcp.path.exists())
            self.assertEqual(browser.get("/readyz").json()["status"], "authorization_required")
            self.assertIsNotNone(self.store.record("workouts", "old"))
            warning = browser.post("/oauth/start", follow_redirects=False)
            self.assertIn("新账号请使用独立数据卷", warning.text)
            mcp.begin.assert_not_called()
            self.assertEqual(browser.post("/oauth/start", data={"same_account": "true"},
                                          follow_redirects=False).status_code, 303)
            mcp.begin.assert_called_once()

    def test_mcp_retry_after_blocks_followup_requests(self):
        attempts = []
        def rate_limited(request):
            attempts.append(request)
            return httpx.Response(429, headers={"Retry-After": "30"})
        with httpx.Client(transport=httpx.MockTransport(rate_limited)) as http:
            client = Client(self.directory, http)
            with self.assertRaisesRegex(MCPError, "429"):
                client.send("GET", "https://mcp.suunto.cn/")
            self.assertGreater(client.cooldown, monotonic() + 25)
            with self.assertRaisesRegex(MCPError, "Retry-After"):
                client.send("GET", "https://mcp.suunto.cn/")
            restarted = Client(self.directory, http)
            self.assertGreater(restarted.cooldown, monotonic() + 25)
            with self.assertRaisesRegex(MCPError, "Retry-After"):
                restarted.send("GET", "https://mcp.suunto.cn/")
            self.assertEqual(len(attempts), 1)
