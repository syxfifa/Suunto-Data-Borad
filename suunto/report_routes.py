"""Read-only overview and training report routes."""

from datetime import date, datetime, time as clock_time, timedelta
import json
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request

from .reports import (acwr_series, aurora_week, daily_report, dates, grouped_totals,
                      intensity_charts, longest_streak, midnight, period_summary,
                      sport_counts, sport_name, wellness)
from .sync import TZ
from .views import covers, local_return, lower_bound, records, summary, workout_chart, workout_metrics, workout_page
from .web_shared import HOME_HEADERS, SOURCE_LABELS, TEMPLATES


def activity_link(start, end, sport="", return_to=""):
    return "/activities?" + urlencode({"start": start, "end": end, "sport": sport, "return_to": return_to})


def add_review_links(series, sport, back, today):
    for item in series:
        item["links"] = [activity_link(start, end, sport, back) if start <= today.isoformat() else None
                         for start, end in item["ranges"]]


def report_router(store, config, dashboard, mcp_state, retry_after):
    router = APIRouter()

    @router.get("/")
    def index(request: Request, day: date | None = None):
        today = datetime.now(TZ)
        chosen = day or today.date()
        earliest = date(1900, 1, 1)
        if not earliest <= chosen <= today.date():
            raise HTTPException(422, "请选择有效的历史日期。")
        review_start = chosen - timedelta(days=chosen.weekday())
        review_end = review_start + timedelta(days=6)
        query_end = min(today, midnight(review_end + timedelta(days=1)))
        preferences = config.read()
        intervals = store.coverage("workouts")
        snapshots, review_charts = aurora_week(
            review_start, chosen, today.date(),
            records(store, "workouts", midnight(review_start), query_end),
            records(store, "sleep", midnight(review_start - timedelta(days=1)), today if review_end >= today.date()
                    else midnight(review_end + timedelta(days=2))),
            records(store, "recovery", midnight(review_start), query_end),
            intervals, preferences.get("wellness_fields", {}),
        )
        back = "/?" + urlencode({"day": chosen}) + "#aurora-review"
        add_review_links(review_charts[1], "", back, today.date())
        selected = snapshots[(chosen - review_start).days]
        selected_end = min(today, midnight(chosen + timedelta(days=1)))
        selected["confirmed_until"] = period_summary([], midnight(chosen), selected_end, intervals)["confirmed_until"]
        day = today.date()
        week_start = day - timedelta(days=day.weekday())
        month_start = day.replace(day=1)
        previous_start = week_start - timedelta(days=7)
        data = records(store, "workouts", midnight(min(month_start, day - timedelta(days=27))), today)
        week_rows = [(at, row) for at, row in data if at[:10] >= week_start.isoformat()]
        overview = [{"days": days, **period_summary(data, midnight(day - timedelta(days=days-1)), today, intervals)}
                    for days in (7, 28)]
        stats = store.home_sync_status()
        connection = mcp_state()
        retry = retry_after()
        if connection == "authorization_required":
            warning = "尚未连接 Suunto 或授权已失效。请在设置中连接。"
        elif retry:
            warning = f"Suunto 请求受限，请等待 {retry} 秒后再试。"
        elif connection == "error":
            warning = "Suunto 连接异常。看板仍可查看本地记录。"
        else:
            warning = {"partial": "最近一轮同步只更新了部分数据。", "failed": "最近一轮同步失败。"}.get(stats["latest_status"])
        return TEMPLATES.TemplateResponse(
            request=request, name="discovery.html",
            context={"overview": overview, "week": period_summary(data, midnight(week_start), today, intervals),
                     "prior": period_summary(data, midnight(previous_start), today - timedelta(days=7), intervals),
                     "month": period_summary(data, midnight(month_start), today, intervals),
                     "selected_link": activity_link(chosen, chosen, return_to=back), "today": day,
                     "sports": sport_counts(week_rows), "last_sync": stats["last_success_at"],
                     "sync_warning": warning, "source_success": stats["source_success"], "source_labels": SOURCE_LABELS,
                     "weekly_goal": preferences.get("weekly_km"), "review_days": snapshots,
                     "selected": selected, "week_from": review_start,
                     "week_to": review_end, "review_charts": review_charts,
                     "previous_week": chosen - timedelta(days=7) if chosen - timedelta(days=7) >= earliest else None,
                     "next_week": min(today.date(), chosen + timedelta(days=7)) if review_end < today.date() else None},
            headers=HOME_HEADERS,
        )

    def recent(source, days=28):
        current = datetime.now(TZ)
        return records(store, source, midnight(current.date() - timedelta(days=days - 1)), current)

    @router.get("/training")
    def training(request: Request, period: str = "day", sport: str = "",
                 days: Annotated[int, Query(ge=7, le=366)] = 90, report: str = "week"):
        weekly = period == "week"
        report = "month" if report == "month" else "week"
        current_time = datetime.now(TZ)
        today = current_time.date()
        first_day = today - timedelta(days=days-1)
        coverage = store.coverage("workouts")
        context_rows = records(store, "workouts", midnight(first_day - timedelta(days=27)),
                               current_time)
        sports = sorted({sport_name(row) for _, row in context_rows})
        selected = sport if sport in sports else ""
        history = [(at, row) for at, row in context_rows if not selected or sport_name(row) == selected]
        current = [(at, row) for at, row in history if at[:10] >= first_day.isoformat()]
        series = workout_chart(current, weekly, first_day, today, coverage, TZ, extended=True)
        back = "/training?" + urlencode({"period": "week" if weekly else "day", "sport": selected, "days": days, "report": report})
        add_review_links(series, selected, back + "#training-charts", today)
        totals = grouped_totals(current, first_day, today, coverage, monthly=report == "month")
        for item in totals:
            item["link"] = activity_link(item["start"], item["end"], selected, back + "#training-reports")
        report_links = {key: "/training?" + urlencode({"period": period, "days": days,
                        "sport": selected, "report": key}) + "#training-reports" for key in ("week", "month")}
        acwr = acwr_series(history, first_day, today, coverage)
        return dashboard(request, "训练趋势", show_period=True, weekly=weekly,
                         charts=series[:2] + [series[4]], sport=selected, sports=sports, days=days,
                         advanced_charts=series[2:4] + series[5:] + [acwr],
                         acwr_days=sum(value is not None for value in acwr["columns"]["ACWR"]),
                         report=report, training_totals=totals, report_links=report_links,
                         range_summary=period_summary(current, midnight(first_day), current_time, coverage),
                         streak=longest_streak(current), training_mix=sport_counts(current))

    @router.get("/activities")
    def workouts(request: Request, start: str = "", end: str = "",
                 page: Annotated[int, Query(ge=1)] = 1, sport: str = "", q: str = "",
                 km: str = "", minutes: str = "", tss: str = "",
                 sort: str = "date", order: str = "desc", return_to: str = ""):
        today = datetime.now(TZ).date()
        activity_span = store.query(
            "SELECT min(occurred_at) AS first, max(occurred_at) AS last FROM records WHERE source='workouts'"
        )[0]
        presets = [(f"近 {days} 天", (today - timedelta(days=days-1)).isoformat()) for days in (7, 28, 90)]
        presets.append(("本月", today.replace(day=1).isoformat()))
        activity_types = store.query("""
            SELECT DISTINCT coalesce(nullif(json_extract(payload, '$.activityName'), ''), '未标注运动类型') AS sport
            FROM records WHERE source='workouts' ORDER BY sport
        """)
        search_state = {"sport": sport, "q": q, "km": km, "minutes": minutes, "tss": tss, "sort": sort, "order": order}
        filters = {"filters": True, "activity_span": activity_span,
                   "presets": presets, "today": today, "activity_types": activity_types, "search": search_state,
                   "review_return": local_return(return_to, "/", "/training"),
                   "search_query": urlencode({key: value for key, value in search_state.items() if value not in ("", None)})}
        start, end = start.strip(), end.strip()
        try:
            first = date.fromisoformat(start.replace("/", "-")) if start else None
            last = date.fromisoformat(end.replace("/", "-")) if end else None
            if last == date.max or (first and last and last < first):
                raise ValueError
        except ValueError:
            return dashboard(request, "运动活动", **filters, start=start, end=end,
                             message="请选择有效的日期（YYYY-MM-DD），结束日期应等于或晚于开始日期。")
        try:
            km, minutes, tss = lower_bound(km, 100000), lower_bound(minutes, 600000), lower_bound(tss, 100000)
        except ValueError:
            return dashboard(request, "运动活动", **filters, start=start, end=end, message="请输入有效的筛选数值。")
        if first is None and last is None and not {"start", "end"}.intersection(request.query_params):
            first = today - timedelta(days=27)
        begin = datetime.combine(first, clock_time.min, TZ) if first else None
        finish = datetime.combine(last + timedelta(days=1), clock_time.min, TZ) if last else None
        count, page, pages, rows, keys = workout_page(
            store, begin, finish, page, sport=sport, search=q.strip(), sort=sort, order=order,
            minimums={"distance": km * 1000 if km is not None else None,
                      "duration": minutes * 60 if minutes is not None else None, "tss": tss})
        summary = empty_action = None
        if count == 0:
            start_at = begin.isoformat() if begin else None
            end_at = finish.isoformat() if finish else None
            match = store.query("""
                SELECT 1 FROM sync_failures WHERE source='workouts'
                UNION ALL
                SELECT 1 FROM sync_windows WHERE source='workouts'
                    AND (? IS NULL OR start_at<?) AND (? IS NULL OR end_at>?)
                LIMIT 1
                """, (end_at, end_at, start_at, start_at))
            if match:
                summary = "这段日期的运动同步遇到问题，可以查看同步状态。"
                empty_action = ("/settings#sync-details", "查看同步详情")
            elif begin and covers(store.coverage("workouts"), begin, finish or datetime.now(TZ)):
                summary = "这段已查询范围内没有符合筛选条件的活动。"
                empty_action = ("/activities?start=&end=", "清除筛选")
            else:
                summary = "本地没有匹配记录，所选范围尚未确认完整。可以补采历史。"
                empty_action = ("/settings#backfill", "查看历史补采")
        export_query = urlencode({"source": "workouts", "start": first or "", "end": last or "",
                                  **{key: search_state[key] for key in ("sport", "q", "km", "minutes", "tss")}})
        current_page = "/activities?" + urlencode({"start": first or "", "end": last or "", "page": page,
                                                 **search_state, "return_to": filters["review_return"] or ""})
        filter_values = {"start": first or "", "end": last or "", **search_state,
                         "return_to": filters["review_return"] or ""}
        labels = {"start": "起始", "end": "截止", "sport": "运动类型", "q": "关键词",
                  "km": "最短距离 km", "minutes": "最短时长 分钟", "tss": "最低 TSS",
                  "sort": "排序", "order": "方向"}
        sort_labels = {"distance": "距离", "duration": "时长", "tss": "TSS", "asc": "从小到大"}
        active_filters = []
        for key, label in labels.items():
            value = filter_values[key]
            if value and value not in {"sort": ("date",), "order": ("desc",)}.get(key, ()):
                cleared = {**filter_values, key: ""}
                if key in ("sort", "order"):
                    cleared.pop(key)
                shown = sort_labels.get(value, value) if key in ("sort", "order") else value
                active_filters.append((label, shown, "/activities?" + urlencode(cleared)))
        details = ["/workout?" + urlencode({"id": key, "return_to": current_page + f"#activity-{index}"})
                   for index, key in enumerate(keys)]
        return dashboard(request, "运动活动", **filters, start=first, end=last, detail_links=details,
                         headings=("北京时间", "运动类型", "距离（km）", "时长（分钟）", "平均心率", "训练效果 PTE"),
                         rows=rows, activity_keys=keys, result_count=count, page=page, pages=pages,
                         empty_message=summary, empty_action=empty_action, active_filters=active_filters,
                         export_query=export_query)

    @router.get("/workout")
    def workout(request: Request, id: str, return_to: str = ""):
        found = store.query("SELECT occurred_at, payload FROM records WHERE source='workouts' AND record_key=?", (id,))
        if not found:
            raise HTTPException(404, "找不到该活动。")
        row = json.loads(found[0]["payload"])
        detail = summary(row)
        labels = {"avgTemperature": "平均温度", "humidity": "湿度", "windSpeed": "风速",
                  "windDirection": "风向", "feeling": "主观感受"}
        featured = {"avgSpeed", "ascent", "descent", "avgCadence", "maxCadence",
                    "avgPower", "pte", "peakEpoc", "recoveryTime", "zones"}
        optional = {labels.get(key, key): value for key, value in detail.items()
                    if key not in featured and value is not None}
        back = local_return(return_to, "/activities", "/compare")
        detail_url = "/workout?" + urlencode({"id": id, "return_to": back or ""})
        compare_url = "/compare?" + urlencode({"first": id, "sport": sport_name(row), "return_to": detail_url})
        metrics = workout_metrics(row)
        key_labels = ("距离（km）", "时长（分钟）", "平均配速（min/km）", "平均速度（km/h）", "平均心率（bpm）", "TSS")
        return dashboard(request, row.get("workoutName") or sport_name(row), workout=row, workout_id=id,
                         workout_at=found[0]["occurred_at"],
                         metrics={key: metrics[key] for key in key_labels if key in metrics},
                         extra_metrics={key: value for key, value in metrics.items() if key not in key_labels},
                         workout_return=back, compare_url=compare_url,
                         charts=intensity_charts(row), optional_metrics=optional)

    @router.get("/sleep-recovery")
    def sleep_recovery(request: Request, days: Annotated[int, Query(ge=7, le=90)] = 28):
        today = datetime.now(TZ).date()
        span = dates(today - timedelta(days=days-1), today)
        preferences = config.read()
        fields = preferences.get("wellness_fields", {})
        unit = preferences.get("sleep_stage_unit", "seconds")
        sleep = recent("sleep", days+1)
        charts, sleep_rows, count, latest_metrics = wellness(sleep, recent("recovery", days),
            recent("workouts", days), span, fields, unit, training_coverage=store.coverage("workouts"))
        first = span[0].isoformat()
        return dashboard(request, "睡眠与恢复", charts=charts, wellness_days=days, sleep_rows=sleep_rows,
                         sleep_count=count, sleep_samples=sum(at[:10] >= first for at, _ in sleep),
                         latest_sleep=next((row[0] for row in sleep_rows if row[-1] is not None), None),
                         latest_metrics=latest_metrics)

    @router.get("/daily-activity")
    def daily_activity(request: Request, source: str = "", period: str = "day",
                       days: Annotated[int, Query(ge=7, le=366)] = 28):
        today = datetime.now(TZ).date()
        preferences = config.read()
        goal = preferences.get("step_goal")
        period = period if period in ("day", "week", "month") else "day"
        charts, providers, selected, metrics = daily_report(recent("daily_activity", days),
            dates(today - timedelta(days=days-1), today), source or preferences.get("primary_source"), goal, period)
        return dashboard(request, "日常活动", charts=charts, daily_stats=metrics, providers=providers,
                         provider=selected, daily_period=period, daily_days=days, step_goal=goal)

    return router
