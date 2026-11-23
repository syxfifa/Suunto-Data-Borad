"""Build tables and chart series using fields observed in the official MCP payloads."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
import csv
from io import StringIO
import json
import math
import re
from urllib.parse import urlsplit


def local_return(value, *allowed_paths):
    if not value or re.search(r"[\\\x00-\x20\x7f]", value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if not (parsed.scheme or parsed.netloc or value.startswith("//")) and parsed.path in allowed_paths else None


def record_filter(source, start=None, end=None):
    filters, parameters = ["source=?"], [source]
    if source == "routes":
        filters.append("record_key NOT IN (SELECT record_key FROM removed_routes)")
    if start is not None:
        filters.append("occurred_at>=?")
        parameters.append(start.isoformat())
    if end is not None:
        filters.append("occurred_at<?")
        parameters.append(end.isoformat())
    return " AND ".join(filters), parameters


def records(store, source, start=None, end=None):
    where, parameters = record_filter(source, start, end)
    rows = store.query(f"SELECT occurred_at, payload FROM records WHERE {where} ORDER BY occurred_at, record_key",
                       parameters)
    return [(row["occurred_at"], json.loads(row["payload"])) for row in rows]


SORTS = {"date": "occurred_at", "distance": "distance", "duration": "duration", "tss": "tss"}
WORKOUT_FIELDS = {
    "distance": ("totalDistance",),
    "duration": ("totalTimeSeconds",),
    "ascent": ("summary", "ascent"),
    "heart_rate": ("hrAvgValue",),
    "tss": ("tss",),
    "pte": ("summary", "pte"),
}
WORKOUT_FILTERS = ("distance", "duration", "tss")
WORKOUT_DIVISORS = {"distance": 1000, "duration": 60}


def workout_value(row, metric):
    for key in WORKOUT_FIELDS[metric]:
        row = row.get(key) if isinstance(row, dict) else None
    return numeric(row)


def workout_display_value(row, metric, digits=2):
    value = workout_value(row, metric)
    return scaled(value, WORKOUT_DIVISORS[metric], digits) if metric in WORKOUT_DIVISORS else value


def workout_sql(metric):
    path = "$." + ".".join(WORKOUT_FIELDS[metric])
    value = f"json_extract(payload, '{path}')"
    return (f"CASE WHEN json_type(payload, '{path}') IN ('real','integer') "
            f"AND abs(CAST({value} AS REAL))<=1.7976931348623157e308 THEN {value} END")


def workout_filter(where, parameters, sport="", search="", minimums=None):
    if sport == "未标注运动类型":
        where += " AND coalesce(json_extract(payload, '$.activityName'), '')=''"
    elif sport:
        where += " AND json_extract(payload, '$.activityName')=?"
        parameters.append(sport)
    if search:
        where += " AND instr(lower(coalesce(json_extract(payload, '$.workoutName'), '') || ' ' || " \
                 "coalesce(json_extract(payload, '$.description'), '')), lower(?))>0"
        parameters.append(search)
    for metric, value in (minimums or {}).items():
        if metric in WORKOUT_FILTERS and value is not None:
            where += f" AND ({workout_sql(metric)})>=?"
            parameters.append(value)
    return where, parameters


def workout_page(store, start, end, page, size=50, *, sport="", search="", minimums=None,
                 sort="date", order="desc"):
    where, parameters = workout_filter(*record_filter("workouts", start, end), sport, search, minimums)
    count = store.query(f"SELECT count(*) AS n FROM records WHERE {where}", parameters)[0]["n"]
    pages = max(1, (count + size - 1) // size)
    page = min(page, pages)
    primary = SORTS.get(sort, "occurred_at")
    direction = "ASC" if order == "asc" else "DESC"
    rows = store.query(f"""
        SELECT record_key, occurred_at, payload,
               {", ".join(f"{workout_sql(metric)} AS {metric}" for metric in WORKOUT_FILTERS)}
        FROM records WHERE {where}
        ORDER BY {primary} IS NULL, {primary} {direction}, occurred_at DESC, record_key DESC LIMIT ? OFFSET ?
        """, [*parameters, size, (page - 1) * size])
    displayed = table_rows([(row["occurred_at"], json.loads(row["payload"])) for row in reversed(rows)], "workouts")
    return count, page, pages, displayed, [row["record_key"] for row in rows]


def covers(intervals, begin, end):
    start, finish = begin.isoformat(), end.isoformat()
    return any(row["start_at"] <= start and row["end_at"] >= finish for row in intervals)


def numeric(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def lower_bound(value, maximum):
    if not value.strip():
        return None
    result = float(value)
    if not 0 <= result <= maximum:
        raise ValueError
    return result


def scaled(value, divisor=1, digits=1):
    return round(value / divisor, digits) if value is not None else None


def summary(row):
    value = row.get("summary")
    return value if isinstance(value, dict) else {}


def average_speed(row):
    distance, seconds = workout_value(row, "distance"), workout_value(row, "duration")
    return distance / seconds if distance is not None and distance >= 0 and seconds is not None and seconds > 0 else None


def workout_speed(row):
    speed = average_speed(row)
    sport = str(row.get("activityName") or "").lower()
    pace = round(1000 / speed) if speed and ("run" in sport or "跑" in sport) else None
    if pace:
        return "min/km", f"{pace//60}:{pace%60:02d}"
    return "km/h", scaled(speed, 1 / 3.6, 2)


def workout_metrics(row):
    unit, speed = workout_speed(row)
    details = summary(row)
    metrics = {
        "距离（km）": workout_display_value(row, "distance"),
        "时长（分钟）": workout_display_value(row, "duration"),
        ("平均配速（min/km）" if unit == "min/km" else "平均速度（km/h）"): speed,
        "爬升（m）": workout_value(row, "ascent"), "下降（m）": numeric(details.get("descent")),
        "平均心率（bpm）": workout_value(row, "heart_rate"), "最高心率（bpm）": numeric(row.get("hrMaxValue")),
        "最低心率（bpm）": numeric(row.get("hrMinValue")), "平均步频": numeric(details.get("avgCadence")),
        "最高步频": numeric(details.get("maxCadence")), "平均功率（W）": numeric(details.get("avgPower")),
        "TSS": workout_value(row, "tss"), "PTE": workout_value(row, "pte"),
        "峰值 EPOC": numeric(details.get("peakEpoc")),
        "恢复时间（Suunto 原值）": numeric(details.get("recoveryTime")),
        "能量（kcal）": numeric(row.get("energy")), "步数": numeric(row.get("stepCount")),
    }
    return {label: value for label, value in metrics.items() if value is not None}


def table_rows(entries, source):
    if source == "workouts":
        return [[stamp, item.get("activityName"), workout_display_value(item, "distance"),
                 workout_display_value(item, "duration"), workout_value(item, "heart_rate"),
                 workout_value(item, "pte")]
                for stamp, item in reversed(entries)]
    return [[item.get("description"), scaled(numeric(item.get("totalDistance")), 1000, 2),
             "、".join(name for name in item["activityNames"] if isinstance(name, str))
             if isinstance(item.get("activityNames"), list) else None]
            for _, item in reversed(entries)]


def route_map_points(entries):
    points_by_route = []
    for _, route in reversed(entries):
        points = []
        for field, label in (("startPoint", "起点"), ("centerPoint", "参考中心"), ("endPoint", "终点")):
            point = route.get(field)
            if not isinstance(point, dict):
                continue
            lat, lon = numeric(point.get("latitude")), numeric(point.get("longitude"))
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                points.append({"label": label, "lat": lat, "lon": lon, "alt": numeric(point.get("altitude"))})
        points_by_route.append(points)
    return points_by_route


def workout_chart(entries, weekly, first_day, last_day, intervals, tz, *, extended=False):
    grouped = defaultdict(list)
    for at, row in entries:
        day = date.fromisoformat(at[:10])
        if weekly:
            day -= timedelta(days=day.weekday())
        grouped[day.isoformat()].append(row)
    step = 7 if weekly else 1
    first_label = first_day
    if weekly:
        first_label -= timedelta(days=first_day.weekday())
    labels = []
    definitions = [
        ("距离（公里）", "距离", lambda r: workout_display_value(r, "distance", 9), "sum"),
        ("时长（分钟）", "时长", lambda r: workout_display_value(r, "duration", 9), "sum"),
    ]
    if extended:
        definitions += [
            ("活动次数", "活动次数", lambda r: 1, "sum"),
            ("累计爬升（m）", "爬升", lambda r: workout_value(r, "ascent"), "sum"),
            ("训练负荷（TSS）", "TSS", lambda r: workout_value(r, "tss"), "sum"),
            ("TSS 记录覆盖（%）", "有 TSS 的活动", lambda r: workout_value(r, "tss"), "coverage"),
            ("平均训练效果（PTE）", "PTE", lambda r: workout_value(r, "pte"), "mean"),
        ]
    totals = [[] for _ in definitions]
    missing = [[] for _ in definitions]
    ranges = []
    coverage = []
    counts = []
    for offset in range(0, (last_day - first_label).days + 1, step):
        bucket = first_label + timedelta(days=offset)
        start = max(first_day, bucket)
        end = min(last_day + timedelta(days=1), bucket + timedelta(days=step))
        covered = covers(intervals, datetime.combine(start, time.min, tz), datetime.combine(end, time.min, tz))
        coverage.append(covered)
        label = bucket.isoformat()
        labels.append(f"{label}（部分覆盖）" if weekly and not covered else label)
        ranges.append((start.isoformat(), (end-timedelta(days=1)).isoformat()))
        rows = grouped.get(label, [])
        counts.append(len(rows))
        for column, gaps, (_, _, metric, method) in zip(totals, missing, definitions):
            values = [v for row in rows if (v := metric(row)) is not None]
            gaps.append(len(values) < len(rows))
            if method == "coverage":
                value = 100 * len(values) / len(rows) if rows else None
            elif method == "mean":
                value = sum(values) / len(values) if values else None
            else:
                value = sum(values) if (rows or covered) and len(values) == len(rows) else None
            column.append(round(value, 2) if value is not None else None)
    result = [chart(title, labels, {label: values}, "bar")
              for (title, label, _, _), values in zip(definitions, totals)]
    if extended:
        for series, gaps in zip(result, missing):
            series["missing"] = gaps
            series["ranges"] = ranges
            series["covered"] = coverage
            series["counts"] = counts
    return result


def chart(title, labels, columns, kind="line"):
    return {"title": title, "labels": labels, "columns": columns, "kind": kind}


def csv_cells(store, source=None, start=None, end=None, *, format="raw", share=False,
              sport="", search="", minimums=None, hrv_field=None):
    buffer = StringIO()
    writer = csv.writer(buffer)

    def line(values):
        buffer.seek(0)
        buffer.truncate()
        # Prevent CSV metadata values from being interpreted as spreadsheet formulas.
        writer.writerow([("'" + item) if isinstance(item, str) and item.lstrip().startswith(
            ("=", "+", "-", "@")) else item for item in values])
        return buffer.getvalue()

    if source:
        # The archive includes historical route entries removed remotely.
        where = ["source=?"]
        parameters = [source]
    else:
        where, parameters = ["1=1"], []
    for value, condition in ((start, "occurred_at>=?"), (end, "occurred_at<?")):
        if value is not None:
            where.append(condition)
            parameters.append(value.isoformat())
    clause = " AND ".join(where)
    if source == "workouts":
        clause, parameters = workout_filter(clause, parameters, sport, search, minimums)
    raw = format == "raw" and not share
    fields = ("source", "date") if share else ("source", "record_key", "occurred_at")
    columns = ("activity_type", "distance_km", "duration_min", "avg_hr_bpm", "tss", "elevation_gain_m",
               "sleep_hours", "body_resources_percent", "stress_state", "steps", "energy_kcal")
    yield "\ufeff" + line(fields + (("payload",) if raw else columns + (() if share else ("name", "provider")) + ("pte", "hrv_ms")))
    # StreamingResponse may advance this iterator on different worker threads.
    with store.connect(check_same_thread=False) as db:
        cursor = db.execute(f"""
            SELECT source, record_key, occurred_at, payload FROM records WHERE {clause}
            ORDER BY source, occurred_at, record_key
            """, parameters)
        for row in cursor:
            if raw:
                yield line(row)
                continue
            name, key, at, raw_data = row
            prefix = (name, at[:10] if at else None) if share else (name, key, at)
            payload = json.loads(raw_data)
            detail = payload.get("entryData")
            data = detail if isinstance(detail, dict) else {}
            route_sports = payload.get("activityNames")
            sport_name = payload.get("activityName") if name == "workouts" else (
                " / ".join(s for s in route_sports if isinstance(s, str)) if isinstance(route_sports, list) else None)
            step = None
            energy = numeric(payload.get("energy"))
            if name == "daily_activity" and payload.get("Aggregation") == "sum":
                sample = numeric(payload.get("Sample", {}).get("Value"))
                if payload.get("Name") == "stepcount":
                    step = sample
                if payload.get("Name") == "energyconsumption":
                    energy = scaled(sample, 4184, 2)
            balance = numeric(data.get("Balance"))
            metric_values = (sport_name, workout_display_value(payload, "distance"),
                             workout_display_value(payload, "duration"),
                             workout_value(payload, "heart_rate"), workout_value(payload, "tss"),
                             workout_value(payload, "ascent"), scaled(numeric(data.get("Duration")), 3600, 2),
                             round(balance * 100, 2) if balance is not None and 0 <= balance <= 1 else None,
                             numeric(data.get("StressState")), step, energy)
            private = () if share else (payload.get("workoutName") or payload.get("description"), payload.get("Source"))
            hrv = numeric(data.get(hrv_field)) if name == "recovery" and hrv_field else None
            yield line(prefix + metric_values + private + (workout_value(payload, "pte"), hrv))
