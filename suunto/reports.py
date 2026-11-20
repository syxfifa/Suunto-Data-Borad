"""Local training and wellness reports backed by captured Suunto fields."""

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from statistics import mean, median
from urllib.parse import urlencode

from .sync import TZ, timestamp
from .views import average_speed, chart, covers, numeric, summary


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


def period_totals(rows):
    vals = [numeric(row.get("tss")) for _, row in rows]
    return {
        "n": len(rows),
        "km": observed_total([numeric(row.get("totalDistance")) for _, row in rows], 1000),
        "minutes": observed_total([numeric(row.get("totalTimeSeconds")) for _, row in rows], 60),
        "climb": observed_total([numeric(summary(row).get("ascent")) for _, row in rows], 1),
        "tss": round(sum(v for v in vals if v is not None), 1) if any(v is not None for v in vals) else None,
        "tss_count": sum(v is not None for v in vals),
    }


def observed_total(values, divisor):
    return round(sum(values) / divisor, 2) if all(value is not None for value in values) else None


def grouped_totals(rows, monthly=False):
    buckets = defaultdict(list)
    for at, row in rows:
        day = date.fromisoformat(at[:10])
        key = day.strftime("%Y-%m") if monthly else (day - timedelta(days=day.weekday())).isoformat()
        buckets[key].append((at, row))
    return [{"period": key, **period_totals(bucket)} for key, bucket in sorted(buckets.items(), reverse=True)]


def acwr_series(rows, first, last, coverage):
    daily = defaultdict(list)
    for at, row in rows:
        daily[date.fromisoformat(at[:10])].append(numeric(row.get("tss")))
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
    speed = average_speed(row)
    sport = str(sport_name(row)).lower()
    pace = round(1000 / speed) if speed and ("run" in sport or "跑" in sport) else None
    distance = numeric(row.get("totalDistance"))
    return [round(distance/1000, 2) if distance is not None else None,
            round(row["totalTimeSeconds"] / 60, 1) if numeric(row.get("totalTimeSeconds")) is not None else None,
            f"{pace//60}:{pace%60:02d} min/km" if pace else f"{speed * 3.6:.2f} km/h" if speed is not None else None,
            numeric(summary(row).get("ascent")), numeric(row.get("hrAvgValue")), numeric(row.get("tss"))]


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


def wellness(sleep, recovery, training, days, fields, stage_unit):
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
            for at, row in reversed(recovery):
                value = numeric(row["entryData"].get(fields.get(key)))
                if at[:10] in labels and value is not None:
                    latest_metrics.append((title, value, at, len(set(values).intersection(labels))))
                    break
    tss = defaultdict(list)
    for at, row in training:
        tss[at[:10]].append(numeric(row.get("tss")))
    known_tss = {d: values for d, values in tss.items() if all(v is not None for v in values)}
    charts.append(line("同期训练负荷（TSS）", labels, {"TSS": known_tss}, sum))
    latest = max((d for d in same_day if d in labels), default=None)
    if latest:
        points = same_day[latest]
        charts.append(chart(f"{latest} 日内身体资源（%）", sorted(points),
                            {"身体资源": [points[k] for k in sorted(points)]}))
    rows = [[item["day"], item["start"], item["end"],
             item["data"].get(fields.get("sleep_kind")),
             round(item["duration"]/3600, 2) if item["duration"] is not None else None]
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
    source = source if source in actual else max(providers, key=lambda key: (
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
    valid = [steps[day] for day in labels if day in steps]
    return charts, providers, source, {"days": len(valid), "missing": len(labels)-len(valid),
                                        "average": round(mean(valid)) if valid else None,
                                        "goal_days": sum(v >= goal for v in valid) if goal else None}
