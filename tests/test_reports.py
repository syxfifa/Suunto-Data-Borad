from datetime import date, datetime, timedelta
from csv import DictReader
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from suunto.reports import acwr_series, aurora_week, compare_metrics, daily_report, dates, intensity_charts, midnight, period_summary, wellness
from suunto.store import Store
from suunto.sync import Synchronizer, TZ
from suunto.web import create_app
from suunto.views import csv_cells, table_rows, workout_metrics


class ReportsTest(unittest.TestCase):
    def test_activity_listing_detail_comparison_and_export_share_units(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize()
            at = "2026-01-01T08:00:00+08:00"
            row = {"activityName": "Running", "totalDistance": 1234.56, "totalTimeSeconds": 611,
                   "hrAvgValue": 132, "tss": 16, "summary": {"pte": 2.7, "ascent": 22}}
            run = store.start("workout", at)
            store.commit(run, "workouts", [("example", at, row)])
            store.commit(run, "recovery", [("hrv", at, {"entryData": {"CapturedHRV": 43.5}})])
            listed = table_rows([(at, row)], "workouts")[0]
            detail = workout_metrics(row)
            compared = compare_metrics(row)
            exported = list(DictReader(csv_cells(store, format="columns", hrv_field="CapturedHRV")))
            activity = next(item for item in exported if item["record_key"] == "example")
            hrv = next(item for item in exported if item["record_key"] == "hrv")
            self.assertEqual((listed[2], detail["距离（km）"], compared[0], float(activity["distance_km"])), (1.23,) * 4)
            self.assertEqual((listed[3], detail["时长（分钟）"], compared[1], float(activity["duration_min"])), (10.18,) * 4)
            self.assertEqual((activity["pte"], hrv["hrv_ms"]), ("2.7", "43.5"))
    def test_period_distinguishes_verified_zero_missing_fields_and_unsynced_time(self):
        first = midnight(date(2026, 1, 1))
        second = first + timedelta(days=1)
        third = second + timedelta(days=1)
        coverage = [{"start_at": first.isoformat(), "end_at": second.isoformat()}]
        self.assertEqual(period_summary([], first, second, coverage)["n"], 0)
        pending = period_summary([], first, third, coverage)
        self.assertEqual((pending["n"], pending["km"], pending["confirmed_until"]), (None, None, second.isoformat()))
        rows = [((first + timedelta(hours=7)).isoformat(), {"totalDistance": 5000, "tss": 20}),
                ((first + timedelta(hours=8)).isoformat(), {"totalTimeSeconds": 1800}),
                (second.isoformat(), {"totalDistance": 10000, "tss": 50})]
        partial = period_summary(rows, first, second, coverage)
        self.assertEqual((partial["n"], partial["km"], partial["tss"], partial["tss_count"]), (2, None, None, 1))

    def test_aurora_week_uses_observed_resources_and_preserves_missing_days(self):
        first, chosen, today = date(2026, 11, 16), date(2026, 11, 18), date(2026, 11, 19)
        coverage = [{"start_at": midnight(first).isoformat(), "end_at": midnight(chosen).isoformat()}]
        workouts = [("2026-11-18T19:00:00+08:00", {"totalDistance": 5200, "totalTimeSeconds": 1800, "tss": 38})]
        sleep = [("2026-11-18T08:00:00+08:00", {"entryData":{"Duration": 25200, "SleepId": 1}})]
        recovery = [("2026-11-18T07:00:00+08:00", {"entryData":{"Balance":0.76}}),
                    ("2026-11-18T16:00:00+08:00", {"entryData":{"Balance":0.60}})]
        days, charts = aurora_week(first, chosen, today, workouts, sleep, recovery, coverage, {})
        self.assertEqual((days[2]["resource"], days[2]["sleep"], days[2]["km"]), (76, 7, 5.2))
        self.assertEqual(days[2]["resource_label"], "晨间身体资源")
        self.assertEqual(days[1]["tss"], 0)
        self.assertIsNone(days[3]["tss"])
        self.assertIsNone(days[4]["resource"])
        self.assertTrue(days[4]["future"])
        self.assertEqual(charts[1][0]["columns"]["TSS"][:4], [0, 0, 38, None])

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
                back = "/training?period=week&sport=Running#training-charts"
                activity = browser.get("/activities", params={"sport": "Running", "return_to": back}).text
                self.assertIn('href="/training?period=week&amp;sport=Running#training-charts"', activity)
                detail_back = "/activities?sport=Running&page=2#activity-0"
                detail = browser.get("/workout?" + urlencode({"id": "a", "return_to": detail_back})).text
                self.assertIn('href="/activities?sport=Running&amp;page=2#activity-0"', detail)
                self.assertNotIn("返回复盘图表", browser.get("/activities?return_to=//external.example/").text)
                self.assertNotIn('id="route-map-link"', browser.get("/routes?watch=on").text)
                self.assertNotIn("Demo route", browser.get("/routes?watch=off").text)
                self.assertEqual(browser.post("/preferences", data={"hrv": "unobserved"}, follow_redirects=False).status_code, 422)
                result = browser.post("/preferences", data={"route_basemap": "true", "weekly_km": "",
                                      "step_goal": ""}, follow_redirects=False)
                self.assertEqual(result.status_code, 303)
                self.assertIn('id="route-map-link"', browser.get("/routes?watch=on&km=").text)
                browser.post("/settings", data={"minutes": 15})
                self.assertIn('id="route-map-link"', browser.get("/routes").text)

    def test_compare_retains_selected_activity_across_candidate_pages(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize()
            at = datetime(2026, 1, 1, tzinfo=TZ)
            run = store.start("test", at.isoformat())
            store.commit(run, "workouts", [
                (f"workout-{i}", (at + timedelta(hours=i)).isoformat(),
                 {"workoutId": f"workout-{i}", "activityName": "Running", "workoutName": f"Run {i}",
                  "totalDistance": 12000 if i == 0 else 3000})
                for i in range(55)])
            with TestClient(create_app(directory)) as browser:
                first = browser.get("/compare", params={"sport": "Running", "first": "workout-54"}).text
                self.assertIn("第 1 / 2 页", first)
                self.assertNotIn('value="workout-0"', first)
                second = browser.get("/compare", params={"sport": "Running", "first": "workout-54",
                                                        "second": "workout-0", "page": 2}).text
                self.assertIn('value="workout-54" selected', second)
                self.assertIn('value="workout-0" selected', second)
                self.assertIn("最远距离 12.0 km", second)
                self.assertIn("运动对比结果", second)

    def test_routes_page_filters_before_paginating(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize()
            at = "2026-01-02T00:00:00+08:00"
            run = store.start("test", at)
            store.commit(run, "routes", [
                (f"route-{i:02}", at, {"routeId": f"route-{i:02}", "description": f"骑行 {i:02}",
                                      "modified": at, "startPoint": {"latitude": 30, "longitude": 120}})
                for i in range(26)])
            with TestClient(create_app(directory)) as browser:
                page = browser.get("/routes", params={"q": "骑行", "updated_to": "2026-01-02", "page": 2}).text
                self.assertIn("共 26 条路线", page)
                self.assertIn("第 2 / 2 页", page)
                self.assertIn("骑行 00", page)
                self.assertNotIn("骑行 25", page)
                self.assertIn("最近更新", page)
                self.assertIn("updated_to=2026-01-02", page)
                self.assertIn("30, 120", page)
                self.assertNotIn("骑行 00", browser.get("/routes?updated_to=2026-01-01").text)

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
