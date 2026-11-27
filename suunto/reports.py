"""Local training and wellness reports backed by captured Suunto fields."""

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from statistics import mean, median
from urllib.parse import urlencode

from .sync import TZ, timestamp
from .views import WORKOUT_DIVISORS, chart, covers, numeric, summary, workout_display_value, workout_speed, workout_value


# Optional metrics bind to observed entryData keys. The instance owner confirms
# their meaning and units before the charts convert units or group sleep sessions.
WELLNESS_FIELDS = {
    "bedtime": ("sleep", "入睡时间（ISO 日期时间）", "text"),
    "wake": ("sleep", "醒来时间（ISO 日期时间）", "text"),
    "sleep_kind": ("sleep", "睡眠类别", "scalar"),
    "deep": ("sleep", "深睡时长", "number"),
    "light": ("sleep", "浅睡时长", "number"),
    "rem": ("sleep", "REM 时长", "number"),
    "quality": ("sleep", "睡眠质量（原值）", "number"),
    "hrv": ("recovery", "HRV（毫秒）", "number"),
    "resting_hr": ("recovery", "静息心率（bpm）", "number"),
}


def dates(first, last):
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def midnight(day):
    return datetime.combine(day, time.min, TZ)


def sport_name(row):
    return row.get("activityName") or "未标注运动类型"


def sport_counts(rows):
    return Counter(sport_name(item) for _, item in rows).most_common()


def period_totals(rows, covered):
    vals = [workout_value(row, "tss") for _, row in rows]
    known = bool(rows) or covered
    return {
        "n": len(rows) if known else None,
        "km": observed_total([workout_value(row, "distance") for _, row in rows], WORKOUT_DIVISORS["distance"]) if known else None,
        "minutes": observed_total([workout_value(row, "duration") for _, row in rows], WORKOUT_DIVISORS["duration"]) if known else None,
        "climb": observed_total([workout_value(row, "ascent") for _, row in rows], 1) if known else None,
        "tss": observed_total(vals, 1) if known else None,
        "tss_count": sum(v is not None for v in vals),
        "covered": covered,
    }


def observed_total(values, divisor):
    return round(sum(values) / divisor, 2) if all(value is not None for value in values) else None


def period_summary(rows, begin, end, intervals):
    covered = covers(intervals, begin, end)
    start, finish = begin.isoformat(), end.isoformat()
    confirmed_until = max((min(finish, item["end_at"]) for item in intervals
                           if item["start_at"] < finish and item["end_at"] > start), default=None)
    included = [(at, row) for at, row in rows if start <= at < finish]
    return {**period_totals(included, covered), "confirmed_until": confirmed_until}


def weekly_progress(rows, previous_rows, begin, now, intervals):
    week = period_summary(rows, begin, now, intervals)
    delta = None
    offset = timedelta(days=7)
    # Use only the continuous coverage starting on Monday, and match the
    # cutoff to the same weekday and time last week.
    cutoff = max((min(now, datetime.fromisoformat(item["end_at"])) for item in intervals
                  if item["start_at"] <= begin.isoformat() < item["end_at"]), default=None)
    if cutoff and cutoff > begin and covers(intervals, begin - offset, cutoff - offset):
        current = period_summary(rows, begin, cutoff, intervals)
        previous = period_summary(previous_rows, begin - offset, cutoff - offset, intervals)
        delta = {"n": current["n"] - previous["n"], "at": cutoff,
                 "km": round(current["km"] - previous["km"], 2)
                 if current["km"] is not None and previous["km"] is not None else None}
    return {**week, "delta": delta}


def aurora_week(first, selected, today, workouts, sleep, recovery, intervals, fields):
    """Summarize Beijing calendar days without equating missing samples to zero."""
    days = dates(first, first + timedelta(days=6))
    grouped = defaultdict(list)
    for stamp, row in workouts:
        grouped[stamp[:10]].append((stamp, row))
    durations = defaultdict(list)
    sleep_days = set()
    for item in sleep_sessions(sleep, fields):
        sleep_days.add(item["day"])
        duration = item["duration"]
        if duration is not None and duration >= 0:
            durations[item["day"]].append(duration)
    balance, morning = defaultdict(list), {}
    recovery_days = set()
    for stamp, row in recovery:
        recovery_days.add(stamp[:10])
        data = row.get("entryData")
        value = numeric(data.get("Balance")) if isinstance(data, dict) else None
        if value is not None and 0 <= value <= 1:
            day = stamp[:10]
            balance[day].append(value)
            if "05:00" <= stamp[11:16] < "10:00":
                morning.setdefault(day, value)

    snapshots = []
    for day in days:
        key = day.isoformat()
        rows = grouped[key]
        covered = day < today and covers(intervals, midnight(day), midnight(day + timedelta(days=1)))
        totals = period_totals(rows, covered)
        sleep_hours = round(sum(durations[key]) / 3600, 1) if durations[key] else None
        resource = morning.get(key)
        label = "晨间身体资源"
        if resource is None and balance[key]:
            resource = mean(balance[key])
            label = "日均身体资源"
        snapshots.append({
            "date": day, "n": totals["n"], "covered": covered,
            "future": day > today, "picked": day == selected, "sleep": sleep_hours,
            "resource": round(resource * 100) if resource is not None else None,
            "sleep_recorded": key in sleep_days, "recovery_recorded": key in recovery_days,
            "resource_label": label, "km": totals["km"], "minutes": totals["minutes"], "tss": totals["tss"],
        })
    labels = [day.strftime("%m/%d") for day in days]
    def make_series(name, field, unit, *, activity=False):
        result = chart(name, labels, {unit: [item[field] for item in snapshots]}, "bar")
        result["aurora"] = True
        result["selected"] = (selected - first).days
        if activity:
            result["ranges"] = [(day.isoformat(), day.isoformat()) for day in days]
            result["covered"] = [item["covered"] for item in snapshots]
            result["counts"] = [item["n"] for item in snapshots]
        return result

    return snapshots, [
        [make_series("睡眠时长", "sleep", "小时"), make_series("身体资源", "resource", "%")],
        [make_series("训练负荷", "tss", "TSS", activity=True), make_series("训练距离", "km", "km", activity=True)],
    ]


def grouped_totals(rows, first, last, coverage, monthly=False):
    grouped = defaultdict(list)
    for at, row in rows:
        day = date.fromisoformat(at[:10])
        label = day.strftime("%Y-%m") if monthly else (day - timedelta(days=day.weekday())).isoformat()
        grouped[label].append((at, row))
    periods = []
    start = first
    while start <= last:
        if monthly:
            label = start.strftime("%Y-%m")
            boundary = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            label = (start - timedelta(days=start.weekday())).isoformat()
            boundary = start + timedelta(days=7 - start.weekday())
        end = min(boundary, last + timedelta(days=1))
        entries = grouped[label]
        totals = period_summary(entries, midnight(start), midnight(end), coverage)
        by_sport = defaultdict(list)
        if not monthly:
            for entry in entries:
                by_sport[sport_name(entry[1])].append(entry)
        periods.append({"period": label, "start": start.isoformat(), "end": (end - timedelta(days=1)).isoformat(),
                        **totals, "sports": [
                            {"name": name, **period_totals(items, totals["covered"])}
                            for name, items in sorted(by_sport.items(), key=lambda pair: (-len(pair[1]), pair[0]))
                        ]})
        start = end
    return list(reversed(periods))


def acwr_series(rows, first, last, coverage):
    daily = defaultdict(list)
    for at, row in rows:
        daily[date.fromisoformat(at[:10])].append(workout_value(row, "tss"))
    labels, ratio = [], []
    for day in dates(first, last):
        start = day - timedelta(days=27)
        labels.append(day.isoformat())
        if not covers(coverage, midnight(start), midnight(day + timedelta(days=1))):
            ratio.append(None)
            continue
        values = [[*daily.get(d, [])] for d in dates(start, day)]
        if any(v is None or v < 0 for group in values for v in group):
            ratio.append(None)
            continue
        total = sum(map(sum, values))
        ratio.append(round(4 * sum(map(sum, values[-7:])) / total, 2) if total else None)
    return chart("急慢性负荷比（ACWR）", labels, {"ACWR": ratio})


def longest_streak(rows):
    days = sorted({date.fromisoformat(at[:10]) for at, _ in rows})
    best = current = 0
    previous = None
    for day in days:
        current = current + 1 if previous and day - previous == timedelta(days=1) else 1
        best = max(best, current)
        previous = day
    return best


def compare_metrics(row):
    unit, speed = workout_speed(row)
    return [workout_display_value(row, "distance"), workout_display_value(row, "duration"),
            f"{speed} {unit}" if speed is not None else None,
            workout_value(row, "ascent"), workout_value(row, "heart_rate"), workout_value(row, "tss")]


def intensity_charts(row):
    zones = summary(row).get("zones")
    if not isinstance(zones, dict):
        return []
    charts = []
    for key, label in (("heartRate", "心率"), ("power", "功率"), ("speed", "速度"), ("pace", "配速")):
        group = zones.get(key)
        if not isinstance(group, dict):
            continue
        values = [numeric(zone.get("totalTime")) if isinstance(zone := group.get(f"zone{i}"), dict) else None
                  for i in range(1, 6)]
        seconds = numeric(row.get("totalTimeSeconds"))
        if any(v is not None for v in values) and all(v is None or v >= 0 for v in values):
            known = sum(v or 0 for v in values)
            if seconds is None or known <= seconds * 1.1 + 1:
                charts.append(chart(f"{label}区间（分钟）", [f"Z{i}" for i in range(1, 6)],
                                    {label: [round(v/60, 2) if v is not None else None for v in values]}, "bar"))
    return charts


def mapping_fields(fields, kind):
    valid = {"number": {"integer", "real"}, "text": {"text"}, "scalar": {"integer", "real", "text"}}
    return sorted({field["name"] for field in fields if field["type"] in valid[kind]})


def parsed_stamp(value):
    if value is not None:
        try:
            return timestamp(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def sleep_sessions(entries, fields):
    sessions = {}
    for at, row in entries:
        data = row["entryData"]
        duration = numeric(data.get("Duration"))
        start = parsed_stamp(data.get(fields.get("bedtime")))
        end = parsed_stamp(data.get(fields.get("wake")))
        if start and end and start >= end:
            start = end = None
        wake_day = (end or at)[:10]
        # SleepId can recur on different nights, so include wake day; explicit
        # distinct session windows take precedence when the tool supplies both.
        sleep_id = data.get("SleepId")
        key = (start, end) if start and end else (wake_day, sleep_id if isinstance(sleep_id, (int, str)) else at)
        item = {"day": wake_day, "stamp": at, "start": start, "end": end, "duration": duration, "data": data}
        existing = sessions.get(key)
        if existing is None or (start and end and at >= existing["stamp"]) or (
            not (start and end) and (duration or 0, at) >= (existing["duration"] or 0, existing["stamp"])
        ):
            sessions[key] = item
    return sorted(sessions.values(), key=lambda item: (item["day"], item["stamp"]))


def values_by_day(entries, field):
    output = defaultdict(list)
    for at, row in entries:
        value = numeric(row["entryData"].get(field))
        if value is not None:
            output[at[:10]].append(value)
    return output


def line(title, labels, groups, aggregate=mean):
    return chart(title, labels, {label: [round(aggregate(source[day]), 2) if source.get(day) else None
                                         for day in labels] for label, source in groups.items()})


def wellness(sleep, recovery, training, days, fields, stage_unit, *, training_coverage=()):
    labels = [day.isoformat() for day in days]
    sessions = [item for item in sleep_sessions(sleep, fields) if item["day"] in labels]
    totals, stage_values = defaultdict(list), {}
    for item in sessions:
        if item["duration"] is not None and item["duration"] >= 0:
            totals[item["day"]].append(item["duration"] / 3600)
    for key, label in (("deep", "深睡"), ("light", "浅睡"), ("rem", "REM")):
        sums = defaultdict(list)
        for item in sessions:
            value = numeric(item["data"].get(fields.get(key)))
            if value is not None and value >= 0:
                sums[item["day"]].append(value / {"seconds": 60, "minutes": 1, "ms": 60000}[stage_unit])
        if sums:
            stage_values[label] = sums
    sleep_week = {}
    for index, day in enumerate(labels):
        previous = [sum(totals[d]) for d in labels[max(0, index-6):index+1] if d in totals]
        if previous:
            sleep_week[day] = [mean(previous)]
    charts = [line("睡眠时长与 7 日均值（小时）", labels, {"时长": totals, "7 日均值": sleep_week}, sum)]
    if stage_values:
        stages = line("睡眠阶段（分钟）", labels, stage_values, sum)
        charts.append({**stages, "kind": "bar", "stacked": True})
    quality = defaultdict(list)
    for item in sessions:
        value = numeric(item["data"].get(fields.get("quality")))
        if value is not None:
            quality[item["day"]].append(value)
    if quality:
        charts.append(line("睡眠质量（原值）", labels, {"质量": quality}))
    bedtime, wake = defaultdict(list), defaultdict(list)
    for item in sessions:
        if item["start"]:
            start = datetime.fromisoformat(item["start"])
            hour = start.hour + start.minute / 60
            bedtime[item["day"]].append(hour + 24 if hour < 12 else hour)
        if item["end"]:
            end = datetime.fromisoformat(item["end"])
            wake[item["day"]].append(end.hour + end.minute / 60)
    if bedtime:
        charts.append(line("入睡时刻（凌晨记作次日小时）", labels, {"入睡": bedtime}, median))
    if wake:
        charts.append(line("醒来时刻（小时）", labels, {"醒来": wake}, median))
    for item in charts:
        item["group"] = "sleep"
    balance, morning = defaultdict(list), defaultdict(list)
    same_day = defaultdict(dict)
    stress = {label: defaultdict(list) for label in ("休息或恢复", "活动", "静止", "压力")}
    by_day = defaultdict(list)
    for at, row in recovery:
        data = row["entryData"]
        value = numeric(data.get("Balance"))
        if value is not None and 0 <= value <= 1:
            balance[at[:10]].append(value * 100)
            same_day[at[:10]][at[11:19]] = round(value * 100, 2)
            if "05:00" <= at[11:16] < "10:00" and not morning[at[:10]]:
                morning[at[:10]].append(value * 100)
        state = numeric(data.get("StressState"))
        if state in (1, 2, 3, 4):
            by_day[at[:10]].append(int(state))
    for day, states in by_day.items():
        for i, group in enumerate(stress.values(), 1):
            group[day] = [states.count(i) / len(states) * 100]
    charts.append(line("身体资源（%）", labels, {"日均": balance, "晨间首读": morning}))
    charts.append({**line("压力状态（有效样本占比 %）", labels, stress), "kind": "bar", "stacked": True})
    charts[-2].update(group="recovery", hint="晨间首读取北京时间 05:00–10:00 的首条有效读数，日均使用当天所有有效读数。")
    charts[-1].update(group="recovery", hint="按有效样本计算各状态占比。")
    latest_metrics = []
    for key, title in (("hrv", "HRV（ms）"), ("resting_hr", "静息心率（bpm）")):
        values = values_by_day(recovery, fields.get(key))
        baseline = {}
        for i, day in enumerate(labels):
            prior = [median(values[d]) for d in labels[max(0, i-14):i] if d in values]
            if len(prior) >= 3:
                baseline[day] = [median(prior)]
        if values:
            charts.append(line(title, labels, {"日中位数": values, "14 日个人基线": baseline}, median))
            charts[-1].update(group="metrics", hint="按日取中位数。14 日个人基线至少需要此前 3 天有效读数。")
            for at, row in reversed(recovery):
                value = numeric(row["entryData"].get(fields.get(key)))
                if at[:10] in labels and value is not None:
                    in_range = [values[day] for day in labels if day in values]
                    latest_metrics.append((title, value, at, len(in_range), sum(map(len, in_range))))
                    break
    tss = defaultdict(list)
    for at, row in training:
        tss[at[:10]].append(workout_value(row, "tss"))
    known_tss = {d: values for d, values in tss.items() if all(v is not None for v in values)}
    for day in days:
        label = day.isoformat()
        if label not in tss and covers(training_coverage, midnight(day), midnight(day + timedelta(days=1))):
            known_tss[label] = [0]
    charts.append(line("同期训练负荷（TSS）", labels, {"TSS": known_tss}, sum))
    charts[-1]["group"] = "recovery"
    latest = max((d for d in same_day if d in labels), default=None)
    if latest:
        points = same_day[latest]
        charts.append(chart(f"{latest} 日内身体资源（%）", sorted(points),
                            {"身体资源": [points[k] for k in sorted(points)]}))
        charts[-1]["group"] = "recovery"
    rows = [[item["day"], item["start"], item["end"],
             item["data"].get(fields.get("sleep_kind")),
             round(item["duration"]/3600, 2) if item["duration"] is not None and item["duration"] >= 0 else None]
            for item in reversed(sessions)]
    return charts, rows, len(totals), latest_metrics


def daily_report(entries, days, source, goal, period):
    labels = [day.isoformat() for day in days]
    actual = defaultdict(lambda: defaultdict(dict))
    for stamp, row in entries:
        value = numeric(row["Sample"].get("Value"))
        if (row["Name"] in ("stepcount", "energyconsumption") and row["Aggregation"] == "sum"
                and value is not None and value >= 0):
            actual[row["Source"]][row["Name"]][stamp[:10]] = value
    providers = sorted(actual)
    source = source or max(providers, key=lambda key: (
        len(actual[key]["stepcount"]), len(actual[key]["energyconsumption"])), default="")
    steps = actual[source]["stepcount"] if source else {}
    groups = defaultdict(list)
    for day in days:
        group = ((day-timedelta(days=day.weekday())).isoformat() if period == "week" else
                 day.strftime("%Y-%m") if period == "month" else day.isoformat())
        groups[group].append(day.isoformat())
    charts = []
    for metric, title, divisor in (("stepcount", "步数", 1), ("energyconsumption", "日常能量（kcal）", 4184)):
        values = actual[source][metric] if source else {}
        series = [round(sum(values[d] for d in group if d in values) / divisor, 1)
                  if any(d in values for d in group) else None for group in groups.values()]
        name = source or "来源"
        display_labels = [f"{key} ({sum(d in values for d in group)}/{len(group)})" if period != "day" else key
                          for key, group in groups.items()]
        charts.append(chart(title, display_labels, {name: series}, "bar"))
        if period != "day":
            charts[-1]["hint"] = "标签括号为有效天数 / 这一柱所涵盖的天数。数值只汇总有记录的日期。"
    valid = [steps[day] for day in labels if day in steps]
    activity_days = steps.keys() | actual[source]["energyconsumption"].keys() if source else set()
    return charts, providers, source, {"days": len(valid), "missing": len(labels)-len(valid),
                                        "latest": max(activity_days, default=None),
                                        "average": round(mean(valid)) if valid else None,
                                        "goal_days": sum(v >= goal for v in valid) if goal else None}
