"""Overview, training reports and day-to-day display preferences."""

import asyncio
from datetime import date, datetime, time as clock_time, timedelta
import json
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from .reports import (acwr_series, aurora_week, daily_report, dates, grouped_totals,
                      intensity_charts, longest_streak, midnight, period_summary,
                      sport_counts, sport_name, wellness)
from .sync import TZ
from .views import (covers, local_return, lower_bound, numeric, records, summary, workout_chart,
                    workout_filter, workout_metrics, workout_page)
from .web_shared import HOME_HEADERS, NO_CACHE, SOURCE_LABELS, TEMPLATES


def activity_link(start, end, sport="", return_to=""):
    return "/activities?" + urlencode({"start": start, "end": end, "sport": sport, "return_to": return_to})


def add_review_links(series, sport, back, today):
    for item in series:
        item["links"] = [activity_link(start, end, sport, back) if start <= today.isoformat() else None
                         for start, end in item["ranges"]]


def report_date_links(request, current, choices, **filters):
    if request.query_params.get("chart_view") == "table":
        filters["chart_view"] = "table"
    return [(f"近 {days} 天", request.url.path + "?" + urlencode({**filters, "days": days}), days == current)
            for days in choices]


def report_window(request, days, start, end, *, maximum, filters):
    """Resolve a rolling preset or an inclusive Beijing calendar range."""
    now = datetime.now(TZ)
    today = now.date()
    earliest = date(1900, 1, 1)
    custom = start is not None or end is not None
    if custom:
        if not start or not end:
            raise HTTPException(422, "请填写开始日期和结束日期。")
        if start > end:
            raise HTTPException(422, "结束日期不能早于开始日期。")
        if start < earliest or end > today:
            raise HTTPException(422, "请选择 1900 年 1 月 1 日至今天之间的日期。")
        if (end - start).days >= maximum:
            raise HTTPException(422, f"一次最多查看 {maximum} 天，请缩短日期范围。")
    else:
        start, end = today - timedelta(days=days - 1), today
    controls = dict(filters)
    if request.query_params.get("chart_view") == "table":
        controls["chart_view"] = "table"
    links = []
    for label, delta in (("上一周", -min(7, (start - earliest).days)),
                         ("下一周", min(7, (today - end).days))):
        if delta:
            shifted = timedelta(days=delta)
            href = request.url.path + "?" + urlencode({**controls, "start": start + shifted, "end": end + shifted})
            links.append((label, href, False))
    return {"first": start, "last": end, "end_at": min(now, midnight(end + timedelta(days=1))),
            "today": today, "length": (end - start).days + 1, "links": links, "controls": controls,
            "bounds": {"start": start, "end": end} if custom else {"days": days}}


def date_form_window(request, days, start, end, *, maximum, filters):
    parsed, malformed = {}, []
    for name, value in (("start", start), ("end", end)):
        try:
            parsed[name] = date.fromisoformat(value) if value else None
        except ValueError:
            malformed.append(name)
    try:
        if malformed:
            raise HTTPException(422, "请输入有效日期（YYYY-MM-DD）。")
        return report_window(request, days, parsed["start"], parsed["end"], maximum=maximum, filters=filters)
    except HTTPException as exc:
        return {"first": start, "last": end, "today": datetime.now(TZ).date(), "links": [],
                "controls": filters, "malformed": malformed, "error": exc.detail}


def report_router(store, config, dashboard, mcp_state, retry_after, account_has_data):
    router = APIRouter()

    @router.get("/")
    def index(request: Request, day: date | None = None, overview: str = "week"):
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
        day = today.date()
        week_start = day - timedelta(days=day.weekday())
        periods = {"week": ("本周", week_start),
                   "month": ("本月", day.replace(day=1)),
                   "7": ("近 7 天", day - timedelta(days=6)),
                   "28": ("近 28 天", day - timedelta(days=27))}
        overview = overview if overview in periods else "week"
        label, summary_start = periods[overview]
        view_args = {"chart_view": "table"} if request.query_params.get("chart_view") == "table" else {}
        review_query = urlencode({"overview": overview, **view_args})
        back = "/?" + urlencode({"day": chosen, "overview": overview, **view_args}) + "#aurora-review"
        add_review_links(review_charts[1], "", back, today.date())
        selected = snapshots[(chosen - review_start).days]
        selected_end = min(today, midnight(chosen + timedelta(days=1)))
        selected["confirmed_until"] = period_summary([], midnight(chosen), selected_end, intervals)["confirmed_until"]
        data = records(store, "workouts", midnight(summary_start), today)
        period = period_summary(data, midnight(summary_start), today, intervals)
        prior = None
        if overview == "week":
            previous_start = midnight(week_start - timedelta(days=7))
            previous_end = today - timedelta(days=7)
            previous_rows = records(store, "workouts", previous_start, previous_end)
            prior = period_summary(previous_rows, previous_start, previous_end, intervals)
        period_links = [(name, "/?" + urlencode({"day": chosen, "overview": key, **view_args}) + "#overview-summary", key == overview)
                        for key, (name, _) in periods.items()]
        stats = store.home_sync_status()
        connection = mcp_state()
        retry = retry_after()
        needs_connection = connection == "authorization_required"
        existing_data = account_has_data() if needs_connection else False
        returning_user = needs_connection and (existing_data or preferences.get("suunto_connected_before"))
        if needs_connection:
            warning = None
        elif retry:
            warning = f"Suunto 请求受限，请等待 {retry} 秒后再试。"
        elif connection == "error":
            warning = "Suunto 连接异常。看板仍可查看本地记录。"
        else:
            warning = {"partial": "最近一轮同步只更新了部分数据。", "failed": "最近一轮同步失败。"}.get(stats["latest_status"])
        return TEMPLATES.TemplateResponse(
            request=request, name="discovery.html",
            context={"overview": overview, "period": period, "period_label": label, "period_start": summary_start,
                     "period_end": today.isoformat(timespec="minutes"), "period_links": period_links,
                     "prior": prior, "review_query": review_query, "chosen_day": chosen,
                     "selected_link": activity_link(chosen, chosen, return_to=back), "today": day,
                     "summary_link": "/training?" + urlencode({"start": summary_start, "end": day, "period": "week", **view_args}),
                     "week_chart_links": {
                         path: path + "?" + urlencode({"start": review_start, "end": min(review_end, day), **view_args})
                         for path in ("/training", "/sleep-recovery")
                     },
                     "needs_connection": needs_connection, "existing_data": existing_data, "returning_user": returning_user,
                     "sports": sport_counts(data), "last_sync": stats["last_success_at"],
                     "sync_warning": warning, "source_success": stats["source_success"], "source_labels": SOURCE_LABELS,
                     "weekly_goal": preferences.get("weekly_km"), "review_days": snapshots,
                     "selected": selected, "week_from": review_start,
                     "week_to": review_end, "review_charts": review_charts,
                     "previous_week": chosen - timedelta(days=7) if chosen - timedelta(days=7) >= earliest else None,
                     "next_week": min(today.date(), chosen + timedelta(days=7)) if review_end < today.date() else None},
            headers=HOME_HEADERS,
        )

    @router.get("/training")
    def training(request: Request, period: str = "", sport: str = "",
                 days: Annotated[int, Query(ge=7, le=366)] = 90, report: str = "week",
                 start: str = "", end: str = "", analysis: str = ""):
        report = "month" if report == "month" else "week"
        advanced = analysis == "advanced"
        filters = {"sport": sport, "report": report}
        if period in ("week", "day"):
            filters["period"] = period
        if advanced:
            filters["analysis"] = "advanced"
        window = date_form_window(request, days, start, end, maximum=366, filters=filters)
        if window.get("error"):
            return dashboard(request, "训练趋势", status_code=422, show_period=True, report_window=window,
                             report_ranges=report_date_links(request, None, (7, 28, 90, 365), **filters),
                             report=report, weekly=period == "week", sport=sport, sports=[sport] if sport else [])
        first_day, last_day = window["first"], window["last"]
        weekly = period == "week" or (period != "day" and window["length"] >= 365)
        coverage = store.coverage("workouts")
        first = first_day.isoformat()
        context_start = first_day - timedelta(days=27) if advanced else first_day
        rows = records(store, "workouts", midnight(context_start), window["end_at"])
        sports = {sport_name(row) for _, row in rows}
        if sport and sport not in sports:
            where, values = workout_filter("source='workouts'", [], sport=sport)
            if store.query(f"SELECT 1 FROM records WHERE {where} LIMIT 1", values):
                sports.add(sport)
            else:
                sport = ""
                window["controls"]["sport"] = ""
                filters["sport"] = ""
        history = [(at, row) for at, row in rows if not sport or sport_name(row) == sport]
        current = [(at, row) for at, row in history if at[:10] >= first]
        series = workout_chart(current, weekly, first_day, last_day, coverage, TZ, extended=True,
                               primary_only=not advanced)
        params = {**window["controls"], **window["bounds"]}
        back = "/training?" + urlencode(params)
        add_review_links(series, sport, back + "#training-charts", last_day)
        totals = grouped_totals(current, first_day, last_day, coverage, monthly=report == "month")
        for item in totals:
            item["link"] = activity_link(item["start"], item["end"], sport, back + "#training-reports")
        report_links = {key: "/training?" + urlencode({**params, "report": key}) + "#training-reports"
                        for key in ("week", "month")}
        acwr = acwr_series(history, first_day, last_day, coverage) if advanced else None
        report_ranges = report_date_links(request, None if start else days, (7, 28, 90, 365), **filters)
        analysis_args = {key: value for key, value in params.items() if key != "analysis"}
        if not advanced:
            analysis_args["analysis"] = "advanced"
        return dashboard(request, "训练趋势", show_period=True, weekly=weekly, report_ranges=report_ranges,
                         report_window=window, charts=series[:3], sport=sport, sports=sorted(sports), days=window["length"],
                         sport_links={name: "/training?" + urlencode({**params, "sport": name}) for name in sports},
                         advanced_charts=series[3:] + [acwr] if advanced else [],
                         analysis_link="/training?" + urlencode(analysis_args) + "#training-analysis",
                         acwr_days=sum(value is not None for value in acwr["columns"]["ACWR"]) if advanced else 0,
                         report=report, training_totals=totals, report_links=report_links,
                         range_summary=period_summary(current, midnight(first_day), window["end_at"], coverage),
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
        presets = [(f"近 {days} 天", today - timedelta(days=days-1)) for days in (7, 28, 90)]
        presets.extend((("本月", today.replace(day=1)), ("全部日期", None)))
        activity_types = store.query("""
            SELECT DISTINCT coalesce(nullif(json_extract(payload, '$.activityName'), ''), '未标注运动类型') AS sport
            FROM records WHERE source='workouts' ORDER BY sport
        """)
        search_state = {"sport": sport, "q": q, "km": km, "minutes": minutes, "tss": tss, "sort": sort, "order": order}
        review_return = local_return(return_to, "/", "/training")
        range_filters = {**search_state, "return_to": review_return or ""}
        filters = {"filters": True, "activity_span": activity_span, "activity_types": activity_types,
                   "search": search_state, "review_return": review_return,
                   "activity_ranges": [(name, "/activities?" + urlencode({**range_filters, "start": first or "",
                                       "end": today if first else ""}), False) for name, first in presets],
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
        default_range = first is None and last is None and not {"start", "end"}.intersection(request.query_params)
        if default_range:
            first = today - timedelta(days=27)
        if default_range or last == today or first is None and last is None:
            active_label = next((name for name, preset in presets if preset == first), None)
            filters["activity_ranges"] = [(name, href, name == active_label)
                                          for name, href, _ in filters["activity_ranges"]]
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
    def sleep_recovery(request: Request, days: Annotated[int, Query(ge=7, le=90)] = 28,
                       start: str = "", end: str = ""):
        window = date_form_window(request, days, start, end, maximum=90, filters={})
        if window.get("error"):
            return dashboard(request, "睡眠与恢复", status_code=422, wellness_days=days, report_window=window,
                             report_ranges=report_date_links(request, None, (7, 28, 90)))
        span = dates(window["first"], window["last"])
        preferences = config.read()
        fields = preferences.get("wellness_fields", {})
        unit = preferences.get("sleep_stage_unit", "seconds")
        sleep = records(store, "sleep", midnight(span[0] - timedelta(days=1)), window["end_at"])
        recovery = records(store, "recovery", midnight(span[0]), window["end_at"])
        charts, sleep_rows, count, latest_metrics = wellness(sleep, recovery,
            records(store, "workouts", midnight(span[0]), window["end_at"]),
            span, fields, unit, training_coverage=store.coverage("workouts"))
        first = span[0].isoformat()
        latest_balance = next(((at, round(value * 100, 2)) for at, row in reversed(recovery)
                               if (value := numeric(row["entryData"].get("Balance"))) is not None
                               and 0 <= value <= 1), None)
        metric_states = []
        observed = store.observed_entry_fields("recovery", midnight(window["today"] - timedelta(days=89)).isoformat())
        candidates = {row["name"] for row in observed
                      if row["type"] in ("integer", "real") and row["name"] not in {"Balance", "StressState"}}
        readings = {item[0]: item for item in latest_metrics}
        for key, name, title in (("hrv", "HRV", "HRV（ms）"), ("resting_hr", "静息心率", "静息心率（bpm）")):
            field = fields.get(key)
            if not field:
                message = "尚未配置字段" if candidates else "尚未采到可供配置的数值字段"
            elif field not in candidates and not any(field in row["entryData"] for _, row in recovery):
                message = "近期未采到已配置字段"
            elif title not in readings:
                message = "所选时段没有有效样本"
            else:
                graph = next(item for item in charts if item["title"] == title)
                baseline = graph["columns"]["14 日个人基线"]
                reading_day = readings[title][2][:10]
                message = "基线样本不足" if baseline[graph["labels"].index(reading_day)] is None else ""
            metric_states.append((name, readings.get(title), message))
        groups = [(key, title, [item for item in charts if item["group"] == key])
                  for key, title in (("sleep", "睡眠"), ("recovery", "恢复"), ("metrics", "HRV 与静息心率"))]
        report_ranges = report_date_links(request, None if start else days, (7, 28, 90))
        return dashboard(request, "睡眠与恢复", charts=charts, wellness_groups=groups, report_ranges=report_ranges,
                         report_window=window, wellness_days=len(span), sleep_rows=sleep_rows, metric_states=metric_states,
                         latest_balance=latest_balance, recovery_days=len({at[:10] for at, _ in recovery}),
                         recovery_latest=recovery[-1][0][:10] if recovery else None,
                         sleep_count=count, sleep_samples=sum(at[:10] >= first for at, _ in sleep),
                         latest_sleep=next((row for row in sleep_rows if row[-1] is not None), None))

    @router.get("/daily-activity")
    def daily_activity(request: Request, source: str = "", period: str = "day",
                       days: Annotated[int, Query(ge=7, le=366)] = 28, start: str = "", end: str = ""):
        preferences = config.read()
        goal = preferences.get("step_goal")
        period = period if period in ("day", "week", "month") else "day"
        providers = store.daily_providers()
        preferred = source if "source" in request.query_params else preferences.get("primary_source")
        selected_source = preferred if preferred in providers else ""
        filters = {"period": period, "source": selected_source}
        window = date_form_window(request, days, start, end, maximum=366, filters=filters)
        if window.get("error"):
            return dashboard(request, "日常活动", status_code=422, daily_stats=None, providers=providers,
                             report_window=window, report_ranges=report_date_links(request, None, (7, 28, 90, 365), **filters),
                             daily_period=period, daily_source=selected_source)
        charts, _, selected, metrics = daily_report(
            records(store, "daily_activity", midnight(window["first"]), window["end_at"]),
            dates(window["first"], window["last"]), selected_source, goal, period)
        report_ranges = report_date_links(request, None if start else days, (7, 28, 90, 365), **filters)
        return dashboard(request, "日常活动", charts=charts, daily_stats=metrics, providers=providers,
                         report_ranges=report_ranges, report_window=window,
                         provider=selected, daily_period=period, step_goal=goal,
                         daily_source=selected_source,
                         daily_saved={"source": "已保存常用来源。", "goal": "已保存每日步数目标。"}
                         .get(request.query_params.get("saved")))

    @router.post("/daily-activity/preferences")
    async def daily_preferences(request: Request, action: Annotated[str, Form()], days: Annotated[int, Form(ge=7, le=366)] = 28,
                                period: Annotated[str, Form()] = "day", source: Annotated[str, Form()] = "",
                                step_goal: Annotated[str, Form()] = "", chart_view: Annotated[str, Form()] = "",
                                start: Annotated[str, Form()] = "", end: Annotated[str, Form()] = ""):
        if period not in ("day", "week", "month") or source and source not in store.daily_providers():
            raise HTTPException(422, "请选择有效的日常活动时间范围或数据来源。")
        window = date_form_window(request, days, start, end, maximum=366, filters={})
        if window.get("error"):
            params = {"days": days, "period": period, "source": source, "start": start, "end": end}
            if chart_view == "table":
                params["chart_view"] = "table"
            return RedirectResponse("/daily-activity?" + urlencode(params), status_code=303, headers=NO_CACHE)
        if action == "source":
            changes = {"primary_source": source}
        elif action == "goal":
            try:
                goal = int(step_goal) if step_goal.strip() else None
                if goal is not None and not 1 <= goal <= 1000000:
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(422, "请填写 1 至 1000000 步，或留空清除目标。") from exc
            changes = {"step_goal": goal}
        else:
            raise HTTPException(422, "未知的偏好操作。")
        await asyncio.to_thread(config.update, changes)
        params = {**window["bounds"], "period": period, "source": source, "saved": action}
        if chart_view == "table":
            params["chart_view"] = "table"
        query = urlencode(params)
        return RedirectResponse("/daily-activity?" + query, status_code=303, headers=NO_CACHE)

    return router
