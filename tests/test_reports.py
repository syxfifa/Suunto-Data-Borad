from datetime import date, datetime, timedelta
from csv import DictReader
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from suunto.reports import (acwr_series, aurora_week, compare_metrics, daily_report, dates,
                            intensity_charts, midnight, period_summary, weekly_progress, wellness)
from suunto.store import Store
from suunto.sync import Synchronizer, TZ
from suunto.web import create_app
from suunto.views import csv_cells, table_rows, workout_metrics


class ReportsTest(unittest.TestCase):
    def test_wellness_daily_review_links_sleep_and_training_to_selected_date(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize()
            day = midnight(date(2025, 6, 12))
            previous = day - timedelta(hours=4)
            morning = day + timedelta(hours=8)
            run = store.start("test", morning.isoformat())
            store.commit(run, "sleep", [("sleep", morning.isoformat(), {"entryData": {"Duration": 25200}})])
            store.commit(run, "workouts", [
                ("previous", previous.isoformat(), {"workoutName": "昨日慢跑", "totalDistance": 4000}),
                ("current", morning.isoformat(), {"workoutName": "今日骑行", "totalDistance": 10000}),
            ])
            params = {"start": "2025-06-12", "end": "2025-06-13", "chart_view": "table", "review_day": "2025-06-12"}
            with TestClient(create_app(directory)) as browser:
                view = browser.get("/sleep-recovery", params=params)
                self.assertEqual(view.status_code, 200)
                self.assertIn("<strong>7.0</strong> 小时", view.text)
                self.assertIn("昨日慢跑", view.text)
                self.assertIn("今日骑行", view.text)
                self.assertIn('review_day=2025-06-13#wellness-review">2025-06-13</a>', view.text)
                back = "/sleep-recovery?" + urlencode(params) + "#wellness-review"
                detail = browser.get("/workout", params={"id": "current", "return_to": back}).text
                self.assertIn(back.replace("&", "&amp;"), detail)
                self.assertIn("返回单日复盘", detail)
                bad = browser.get("/sleep-recovery", params={**params, "review_day": "2025-06-11"})
                self.assertEqual(bad.status_code, 422)
                self.assertIn("已显示范围内最后一天", bad.text)

    def test_weekly_progress_uses_the_same_checked_cutoff_for_both_weeks(self):
        begin = midnight(date(2026, 1, 12))
        now = begin + timedelta(days=2, hours=12)
        cutoff = now - timedelta(hours=2)
        offset = timedelta(days=7)
        rows = [((cutoff - timedelta(hours=1)).isoformat(), {"totalDistance": 8000}),
                ((cutoff + timedelta(hours=1)).isoformat(), {"totalDistance": 3000})]
        earlier = [((cutoff - offset - timedelta(hours=1)).isoformat(), {"totalDistance": 5000})]
        interval = lambda start, end: {"start_at": start.isoformat(), "end_at": end.isoformat()}
        current = interval(begin, cutoff)
        previous = interval(begin - offset, cutoff - offset)
        result = weekly_progress(rows, earlier, begin, now, [current, previous])
        self.assertEqual(result["km"], 11)
        self.assertEqual(result["delta"], {"km": 3, "n": 0, "at": cutoff})
        self.assertIsNone(weekly_progress(rows, earlier, begin, now, [current])["delta"])

    def test_home_freshness_links_to_source_and_calendar_retains_options(self):
        with TemporaryDirectory() as directory, TestClient(create_app(directory)) as browser:
            store = Store(directory)
            recorded = "2026-01-14T08:00:00+08:00"
            checked = "2026-01-16T09:15:00+08:00"
            run = store.start("fixture", checked)
            store.commit(run, "workouts", [("fixture", recorded, {"workoutId": "fixture"})], checked_at=checked)
            store.failure("sleep", None, None, "fixture missing")
            text = browser.get("/?day=2026-01-14&overview=28&chart_view=table").text
            self.assertIn("运动已检查 · 睡眠检查异常", text)
            self.assertIn("2026-01-16 09:15", text)
            self.assertIn('href="/settings?source=sleep#source-status-sleep"', text)
            self.assertIn('id="sync-details" class="settings-details" open', browser.get("/settings?source=sleep").text)
            self.assertIn('name="overview" value="28"', text)
            self.assertIn('name="chart_view" value="table"', text)
            self.assertEqual(browser.get("/?day=not-a-date").status_code, 422)

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
        self.assertEqual(stats, {"days": 1, "missing": 1, "latest": "2026-01-02", "average": 3000, "goal_days": 0})

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
                primary = browser.get("/training?sport=Running&period=week").text
                self.assertIn("查看进阶分析", primary)
                self.assertNotIn("急慢性负荷比", primary)
                self.assertIn("急慢性负荷比", browser.get("/training?sport=Running&period=week&analysis=advanced").text)
                back = "/training?period=week&sport=Running#training-charts"
                activity = browser.get("/activities", params={"sport": "Running", "return_to": back}).text
                self.assertIn('href="/training?period=week&amp;sport=Running#training-charts"', activity)
                detail_back = "/activities?sport=Running&page=2#activity-0"
                detail = browser.get("/workout?" + urlencode({"id": "a", "return_to": detail_back})).text
                self.assertIn('href="/activities?sport=Running&amp;page=2#activity-0"', detail)
                self.assertNotIn("返回复盘图表", browser.get("/activities?return_to=//external.example/").text)
                self.assertIn('id="route-map-link"', browser.get("/routes?watch=on").text)
                settings = browser.get("/settings").text
                self.assertNotIn('name="route_basemap"', settings)
                self.assertNotIn('href="/export.csv"', settings)
                self.assertNotIn('action="/maintenance/retention"', browser.get("/maintenance").text)
                self.assertNotIn("Demo route", browser.get("/routes?watch=off").text)
                self.assertEqual(browser.post("/preferences", data={"hrv": "unobserved"}, follow_redirects=False).status_code, 422)
                result = browser.post("/preferences", data={"weekly_km": "",
                                      "step_goal": ""}, follow_redirects=False)
                self.assertEqual(result.status_code, 303)
                self.assertIn('id="route-map-link"', browser.get("/routes?watch=on&km=").text)
                browser.post("/settings", data={"minutes": 15})
                self.assertIn('id="route-map-link"', browser.get("/routes").text)

    def test_report_dates_and_advanced_data_follow_the_selected_range(self):
        with TemporaryDirectory() as directory, TestClient(create_app(directory)) as browser:
            store = Store(directory)
            run = store.start("fixture", "2024-02-29T00:00:00+08:00")
            sport = {"activityName": "Running", "totalDistance": 5000, "totalTimeSeconds": 1800}
            store.commit(run, "workouts", [("in", "2024-02-29T08:00:00+08:00", sport),
                                          ("out", "2024-03-01T00:00:00+08:00", sport)])
            page = browser.get("/training", params={"start": "2024-02-29", "end": "2024-02-29",
                                                   "sport": "Running", "chart_view": "table"})
            self.assertIn("2024-02-29", page.text)
            self.assertIn("start=2024-02-22&amp;end=2024-02-22", page.text)
            self.assertIn("sport=Running", page.text)
            self.assertIn("<dd>1</dd>", page.text)
            self.assertNotIn("急慢性负荷比", page.text)
            advanced = browser.get("/training?start=2024-02-29&end=2024-02-29&analysis=advanced&chart_view=table")
            self.assertIn("急慢性负荷比", advanced.text)
            invalid = browser.get("/training?start=2024-03-01&end=2024-02-29&sport=Running&chart_view=table")
            self.assertEqual(invalid.status_code, 422)
            self.assertIn('结束日期不能早于开始日期', invalid.text)
            self.assertIn('value="2024-03-01"', invalid.text)
            self.assertIn('name="chart_view" value="table"', invalid.text)
            self.assertIn('Running</option>', invalid.text)
            self.assertIn('value="wrong-date"', browser.get("/daily-activity?start=wrong-date&end=2024-02-29").text)
            self.assertEqual(browser.get("/sleep-recovery?start=2024-01-01&end=2024-07-01").status_code, 422)
            self.assertIn("当前按周", browser.get("/training?days=365").text)
            self.assertIn('value="day" selected', browser.get("/training?days=365&period=day").text)

    def test_health_preferences_load_only_when_requested_and_save_separately(self):
        with TemporaryDirectory() as directory, TestClient(create_app(directory)) as browser:
            from suunto.config import Config
            config = Config(Store(directory).path.parent)
            config.update({"weekly_km": 15, "wellness_fields": {"hrv": "observed"}, "sleep_stage_unit": "ms"})
            with patch.object(Store, "observed_entry_fields", return_value=[]) as observed:
                self.assertNotIn('name="hrv"', browser.get("/settings").text)
                browser.post("/preferences", data={"section": "goal", "weekly_km": "20"}, follow_redirects=False)
                observed.assert_not_called()
                self.assertIn('name="hrv"', browser.get("/settings?edit_preferences=true").text)
                self.assertEqual(observed.call_count, 2)
                self.assertEqual(config.read()["wellness_fields"], {"hrv": "observed"})
                self.assertEqual(config.read()["sleep_stage_unit"], "ms")
                browser.post("/preferences", data={"section": "fields", "sleep_stage_unit": "seconds"},
                             follow_redirects=False)
                self.assertEqual(config.read()["weekly_km"], 20)
                self.assertEqual(config.read()["wellness_fields"], {})

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
                search = browser.get("/compare", params={"sport": "Running", "first": "workout-54",
                                                         "q": "Run 0", "day": "2026-01-01"}).text
                self.assertIn('value="workout-54" selected', search)
                self.assertIn('value="workout-0"', search)
                self.assertNotIn('value="workout-1"', search)

    def test_missing_days_link_to_the_overlapping_failure(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize()
            start = midnight(date(2025, 6, 12))
            end = start + timedelta(days=1)
            run = store.start("test", start.isoformat())
            store.commit(run, "workouts", [], interval=((start - timedelta(days=1)).isoformat(), start.isoformat()))
            store.window_failure("workouts", start.isoformat(), end.isoformat(), "fixture remote error", end)
            with TestClient(create_app(directory)) as browser:
                empty = browser.get("/activities?start=2025-06-11&end=2025-06-11").text
                self.assertIn("已查询，所选日期暂无记录", empty)
                affected = browser.get("/activities?start=2025-06-12&end=2025-06-12").text
                self.assertIn("所选日期有同步失败的时段", affected)
                self.assertIn("#sync-window-target", affected)
                query = urlencode({"source": "workouts", "window_start": start.isoformat(), "window_end": end.isoformat()})
                self.assertIn('id="sync-window-target" tabindex="-1"', browser.get("/settings?" + query).text)
                future = browser.get("/activities?start=2099-01-01&end=2099-01-01").text
                self.assertIn("所选日期尚未到来", future)

    def test_activity_comparison_prefers_a_recent_run_at_similar_distance(self):
        with TemporaryDirectory() as directory:
            store = Store(directory)
            store.initialize()
            at = midnight(date(2025, 6, 12))
            run = store.start("test", at.isoformat())
            store.commit(run, "workouts", [
                (name, (at + timedelta(hours=hour)).isoformat(),
                 {"workoutName": name, "activityName": sport, "totalDistance": distance, "totalTimeSeconds": duration})
                for name, hour, sport, distance, duration in [
                    ("prior", 1, "Running", 5000, 1800), ("short", 2, "Running", 2500, 900),
                    ("cycle", 3, "Cycling", 5000, 900), ("current", 4, "Running", 5000, 1740)]
            ])
            with TestClient(create_app(directory)) as browser:
                detail = browser.get("/workout?id=current").text
                self.assertIn("与近期同类活动对比", detail)
                self.assertIn("first=prior&amp;second=current", detail)
                comparison = browser.get("/compare?sport=Running&first=prior&second=current").text
                self.assertIn("活动 B 每公里快 12 秒", comparison)
                compared = browser.get("/compare", params={"sport": "Running", "first": "prior", "second": "current",
                    "q": "current", "day": "2025-06-12", "return_to": "/workout?id=current"}).text
                self.assertIn("first=current&amp;second=prior&amp;return_to=%2Fworkout%3Fid%3Dcurrent"
                              "&amp;q=current&amp;day=2025-06-12&amp;page=1#comparison-result", compared)
                swapped = browser.get("/compare?sport=Running&first=current&second=prior").text
                self.assertIn("活动 B 每公里慢 12 秒", swapped)
                self.assertIn("缺少可对比数据", swapped)

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
