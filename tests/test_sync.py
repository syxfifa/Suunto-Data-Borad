from datetime import date, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from suunto.mcp import MCPError, NeedsAuthorization
from suunto.store import Store
from suunto.sync import SOURCES, Synchronizer, TZ
from suunto.views import workout_chart
from suunto.web import create_app


class SampleMCP:
    def __init__(self):
        self.calls = []
        self.fail_sleep = False
        self.no_auth = False

    def tools(self):
        if self.no_auth:
            raise NeedsAuthorization("等待颂拓账号授权。")
        return [{"name": name} for name in [*(source[0] for source in SOURCES.values()), "get_workout_detail"]]

    def sample(self, name, args, discovered):
        self.calls.append((name, args))
        stamp = "2026-11-16T21:00:00+08:00"
        window_covers_stamp = args.get("startDate", "") <= stamp < args.get("endDate", "z")
        workout = {"workoutId": "workout-a", "startTime": stamp, "totalDistance": 5000, "totalTimeSeconds": 1500}
        if name == "list_user_workouts":
            data = [workout, {"startTime": "invalid"}] if window_covers_stamp else []
        elif name == "get_workout_detail":
            data = {**workout, "summary": {"pte": 3}}
        elif name == "get_sleep_summary":
            if self.fail_sleep:
                raise MCPError("暂时无法读取睡眠记录。")
            record = {"timestamp": stamp, "entryData": {"SleepId": 1, "Duration": 24000}}
            data = {"data": [record, record] if window_covers_stamp else []}
        elif name == "get_recovery_summary":
            data = {"data": [{"timestamp": stamp, "entryData": {"Balance": 0.3, "StressState": 0}}]
                    if window_covers_stamp else []}
        elif name == "get_daily_activity_summary":
            data = {"data": [{"Name": "stepcount", "Aggregation": "sum", "Sources": [{
                "Name": "device-a", "Samples": [{"TimeISO8601": stamp, "Value": 123}],
            }]}] if window_covers_stamp else []}
        elif name == "list_user_routes":
            data = [{"routeId": "route-a", "description": "A", "totalDistance": 2000, "modified": None,
                     "startPoint": {"latitude": 30.5, "longitude": 120.5},
                     "centerPoint": {"latitude": 30.51, "longitude": 120.53},
                     "endPoint": {"latitude": 30.52, "longitude": 120.57}},
                    {"routeId": "route-b", "description": "B",
                     "endPoint": {"latitude": 999, "longitude": 120.6}}]
        return {"content": [{"type": "text", "text": json.dumps(data)}]}


class SyncTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name)
        self.store.initialize()
        self.mcp = SampleMCP()
        self.sync = Synchronizer(self.mcp, self.store)
        self.stop = datetime(2026, 11, 17, 12, tzinfo=TZ)

    def test_all_sources_deduplicate_and_incremental_routes(self):
        first = self.sync.run("test", stop=self.stop)
        self.assertEqual(first["status"], "partial")
        status = self.store.status()
        self.assertEqual(status["counts"], {name: 2 if name == "routes" else 1 for name in SOURCES})
        self.assertEqual(self.store.record("workouts", "workout-a")["summary"]["pte"], 3)
        self.assertTrue(any(error["source"] == "workouts" for error in status["errors"]))
        self.mcp.calls.clear()
        self.sync.run("test", stop=self.stop + timedelta(minutes=30))
        self.assertEqual(self.store.status()["runs"][0]["changed"], 0)
        self.assertFalse(any(name == "get_workout_detail" for name, _ in self.mcp.calls))
        route_calls = [args for name, args in self.mcp.calls if name == "list_user_routes"]
        self.assertIn("sinceDate", route_calls[0])
        workout_start = next(args["startDate"] for name, args in self.mcp.calls if name == "list_user_workouts")
        self.assertEqual(workout_start, (self.stop - timedelta(days=2)).isoformat())

    def test_failure_leaves_source_cursor_and_other_sources_continue(self):
        self.sync.run("test", stop=self.stop)
        self.mcp.fail_sleep = True
        next_stop = self.stop + timedelta(days=1)
        self.sync.run("test", stop=next_stop)
        self.assertEqual(self.store.cursor("sleep")["scanned_until"], self.stop.isoformat())
        self.assertEqual(self.store.cursor("recovery")["scanned_until"], next_stop.isoformat())

    def test_authorization_required(self):
        self.mcp.no_auth = True
        self.assertEqual(self.sync.run("test", stop=self.stop)["status"], "authorization_required")
        self.assertIsNone(self.store.cursor("workouts"))

    def test_dashboard_filters_settings_and_csv(self):
        self.sync.run("test", stop=self.stop)
        data_dir = self.store.path.parent
        with TestClient(create_app(data_dir)) as browser:
            self.assertEqual(browser.get("/healthz").json(), {"status": "ok"})
            page = browser.get("/activities?start=2026-11-16&end=2026-11-16")
            self.assertEqual(page.status_code, 200)
            self.assertIn("2026-11-16T21:00:00+08:00", page.text)
            self.assertIn("训练效果 PTE", page.text)
            self.assertIn('id="activity-days"', page.text)
            self.assertIn('<option value="2026-11-16">', page.text)
            self.assertIn('name="end" type="text"', page.text)
            self.assertIn("本地活动日期范围", page.text)
            for query in ("start=2026-11-16&end=", "start=&end=2026-11-16", "start=&end="):
                self.assertIn("2026-11-16T21:00:00+08:00", browser.get(f"/activities?{query}").text)
            self.assertIn("2026-11-16T21:00:00+08:00", browser.get("/activities?end=2026%2F11%2F16").text)
            self.assertIn("请选择有效的日期", browser.get(
                "/activities?start=2026-11-17&end=2026-11-16").text)
            self.assertNotIn("2026-11-16T21:00:00+08:00", browser.get(
                "/activities?start=2026-11-17").text)
            for path in ("/", "/training?period=week", "/sleep-recovery", "/daily-activity", "/routes",
                         "/sync-status", "/settings", "/static/chart.umd.min.js"):
                self.assertEqual(browser.get(path).status_code, 200)
            self.assertEqual(browser.post("/settings", data={"minutes": "0"}).status_code, 422)
            response = browser.post("/settings", data={"minutes": "15"})
            self.assertIn("已保存同步间隔", response.text)
            self.assertIn("MCP 连接状态", response.text)
            self.assertIn("连接 Suunto", response.text)
            self.assertIn("同步记录", response.text)
            self.assertNotIn("/oauth/start", browser.get("/").text)
            routes = browser.get("/routes").text
            self.assertIn('data-lat="30.5"', routes)
            self.assertNotIn('data-lat="999"', routes)
            self.assertIn("暂无位置点", routes)
            self.assertIn("路线位置预览", routes)
            self.assertNotIn("<iframe", routes)
            self.assertNotIn("openstreetmap.org", routes)
            config = json.loads((Path(data_dir) / "config.json").read_bytes())
            self.assertEqual(config, {"sync_interval_minutes": 15, "timezone": "Asia/Shanghai"})
            download = browser.get("/export.csv")
            self.assertEqual(download.status_code, 200)
            self.assertIn("attachment;", download.headers["content-disposition"])
            self.assertIn("workout-a", download.text)

    def test_training_includes_empty_days_and_weeks(self):
        first, last = date(2026, 11, 16), date(2026, 11, 24)
        entries = [("2026-11-17T09:00:00+08:00", {"totalDistance": 5000})]
        distance, duration = workout_chart(entries, False, first, last)
        self.assertEqual(distance["columns"]["距离"], [0, 5, 0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(duration["columns"]["时长"][:3], [0, None, 0])
        distance, _ = workout_chart(entries, True, first, last)
        self.assertEqual(distance["labels"], ["2026-11-16", "2026-11-23"])
        self.assertEqual(distance["columns"]["距离"], [5, 0])
