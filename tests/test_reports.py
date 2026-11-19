from datetime import date, datetime, timedelta
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from suunto.reports import acwr_series, daily_report, dates, intensity_charts, midnight, wellness
from suunto.store import Store
from suunto.sync import Synchronizer, TZ
from suunto.web import create_app


class ReportsTest(unittest.TestCase):
    def test_acwr_requires_all_activity_tss_and_28_day_coverage(self):
        last = date(2026, 1, 28)
        first = last - timedelta(days=27)
        covered = [{"start_at": midnight(first).isoformat(),
                    "end_at": midnight(last + timedelta(days=1)).isoformat()}]
        rows = [(midnight(day).isoformat(), {"tss": 10}) for day in dates(first, last)]
        self.assertEqual(acwr_series(rows, last, last, covered)["columns"]["ACWR"], [1])
        self.assertEqual(acwr_series(rows, last, last, [])["columns"]["ACWR"], [None])
        rows[0][1].pop("tss")
        self.assertEqual(acwr_series(rows, last, last, covered)["columns"]["ACWR"], [None])

    def test_sleep_revisions_and_real_optional_fields(self):
        day = date(2026, 1, 2)
        sleep = [(f"2026-01-02T{hour}:00:00+08:00", {"entryData": {
            "Duration": duration, "SleepId": 7, "StartObserved": "2026-01-01T23:00:00+08:00",
            "EndObserved": "2026-01-02T06:00:00+08:00", "DeepObserved": 3600}})
            for hour, duration in (("07", 21600), ("08", 25200))]
        recovery = [("2026-01-02T07:05:00+08:00", {"entryData": {"Balance": 0.8, "StressState": 1,
                                                               "HrvObserved": 42}})]
        charts, rows, count, latest = wellness(sleep, recovery, [], [day], {
            "bedtime": "StartObserved", "wake": "EndObserved", "deep": "DeepObserved", "hrv": "HrvObserved"
        }, "seconds")
        self.assertEqual(count, 1)
        self.assertEqual(rows[0][-1], 7)
        self.assertEqual(charts[0]["columns"]["时长"], [7])
        self.assertEqual(charts[1]["columns"]["深睡"], [60])
        self.assertEqual(latest[0][1], 42)

    def test_daily_goals_keep_independent_sources_and_missing_days(self):
        at = "2026-01-02T06:00:00+08:00"
        entries = [(at, {"Name": "stepcount", "Aggregation": "sum", "Source": source, "Sample": {"Value": steps}})
                   for source, steps in (("watch-a", 3000), ("watch-b", 9000))]
        charts, _, _, stats = daily_report(entries, dates(date(2026, 1, 2), date(2026, 1, 3)),
                                           "watch-a", 5000, "day")
        self.assertEqual(charts[0]["columns"]["watch-a"], [3000, None])
        self.assertEqual(stats, {"days": 1, "missing": 1, "average": 3000, "goal_days": 0})

    def test_workout_routes_and_preferences_web_paths(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize()
            at = datetime.now(TZ).isoformat()
            run = store.start("test", at)
            records = [{"workoutId": key, "activityName": sport, "workoutName": name, "totalDistance": distance,
                        "totalTimeSeconds": 1800, "tss": 45, "summary": {"ascent": distance/100,
                        "zones": {"heartRate": {"zone1": {"totalTime": 120}}}}}
                       for key, sport, name, distance in [
                           ("a", "Running", "Test-run", 5000), ("b", "Running", "Demo-run", 8000),
                           ("c", "Cycling", "Demo-bike", 10000)]]
            store.commit(run, "workouts", [(w["workoutId"], at, w) for w in records])
            store.commit(run, "routes", [("route-a", at, {
                "routeId": "route-a", "description": "Demo route", "visibility": "PRIVATE", "watchEnabled": True,
                "activityNames": ["Running"], "totalDistance": 5000,
                "startPoint": {"latitude": 30.5, "longitude": 120.5, "altitude": 14}
            })])
            self.assertEqual(intensity_charts(records[0])[0]["columns"]["心率"], [2, None, None, None, None])
            with TestClient(create_app(directory)) as browser:
                page = browser.get("/activities?start=&end=&sport=Running&q=run&km=6&minutes=&tss=&sort=distance")
                self.assertIn("共 1 条活动", page.text)
                self.assertIn("/workout?id=b", page.text)
                self.assertIn("平均配速（min/km）", browser.get("/workout?id=a").text)
                self.assertIn("心率区间（分钟）", browser.get("/workout?id=a").text)
                self.assertIn("运动对比", browser.get("/compare?sport=Running&first=a&second=b").text)
                self.assertIn("请选择两条不同的同类型活动", browser.get("/compare?sport=Running&first=a&second=c").text)
                self.assertIn("急慢性负荷比", browser.get("/training?sport=Running&period=week").text)
                self.assertNotIn('id="route-map-link"', browser.get("/routes?watch=on").text)
                self.assertNotIn("Demo route", browser.get("/routes?watch=off").text)
                self.assertEqual(browser.post("/preferences", data={"hrv": "unobserved"}, follow_redirects=False).status_code, 422)
                result = browser.post("/preferences", data={"route_basemap": "true", "weekly_km": "",
                                      "step_goal": ""}, follow_redirects=False)
                self.assertEqual(result.status_code, 303)
                self.assertIn('id="route-map-link"', browser.get("/routes?watch=on&km=").text)
                browser.post("/settings", data={"minutes": 15})
                self.assertIn('id="route-map-link"', browser.get("/routes").text)

    def test_route_detail_uses_live_tool_and_retains_points_on_list_refresh(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize()
            stamp = datetime(2026, 1, 1, tzinfo=TZ)
            run = store.start("test", stamp.isoformat())
            stored = {"routeId": "route-a", "description": "A"}
            store.commit(run, "routes", [("route-a", None, stored)])
            mcp = Mock()
            mcp.tools.return_value = [{"name": "get_route_summary"}, {"name": "list_user_routes"}]
            point = {"latitude": 30.5, "longitude": 120.5}
            mcp.sample.side_effect = lambda name, *_: {"structuredContent":
                {**stored, "startPoint": point} if name == "get_route_summary" else [stored]}
            sync = Synchronizer(mcp, store)
            self.assertEqual(sync.run("route_details", detail_key="route-a")["status"], "ok")
            sync.routes(run, "list_user_routes", stamp + timedelta(days=1), {"list_user_routes"})
            self.assertEqual(store.record("routes", "route-a")["startPoint"], point)
