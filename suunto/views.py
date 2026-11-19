"""Build tables and chart series using fields observed in the official MCP payloads."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
import csv
from io import StringIO
import json
import math


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


def workout_page(store, start, end, page, size=50, *, sport="", search="", minimums=None,
                 sort="date", order="desc"):
    where, parameters = record_filter("workouts", start, end)
    if sport:
        where += " AND json_extract(payload, '$.activityName')=?"
        parameters.append(sport)
    if search:
        where += " AND instr(lower(coalesce(json_extract(payload, '$.workoutName'), '') || ' ' || " \
                 "coalesce(json_extract(payload, '$.description'), '')), lower(?))>0"
        parameters.append(search)
    paths = {"distance": "$.totalDistance", "duration": "$.totalTimeSeconds", "tss": "$.tss"}
    for metric, value in (minimums or {}).items():
        if metric in paths and value is not None:
            where += f" AND json_type(payload, '{paths[metric]}') IN ('real','integer') AND json_extract(payload, '{paths[metric]}')>=?"
            parameters.append(value)
    count = store.query(f"SELECT count(*) AS n FROM records WHERE {where}", parameters)[0]["n"]
    pages = max(1, (count + size - 1) // size)
    page = min(page, pages)
    primary = SORTS.get(sort, "occurred_at")
    direction = "ASC" if order == "asc" else "DESC"
    rows = store.query(f"""
        SELECT record_key, occurred_at, payload,
               {", ".join(f"CASE WHEN json_type(payload, '{path}') IN ('real','integer') THEN json_extract(payload, '{path}') END AS {alias}" for alias, path in paths.items())}
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


def scaled(value, divisor=1, digits=1):
    return round(value / divisor, digits) if value is not None else None


def summary(row):
    value = row.get("summary")
    return value if isinstance(value, dict) else {}


def average_speed(row):
    distance, seconds = numeric(row.get("totalDistance")), numeric(row.get("totalTimeSeconds"))
    return distance / seconds if distance is not None and distance >= 0 and seconds is not None and seconds > 0 else None


def workout_metrics(row):
    speed = average_speed(row)
    sport = str(row.get("activityName") or "").lower()
    pace = round(1000 / speed) if speed and ("run" in sport or "跑" in sport) else None
    heart = row.get("hrAvgValue")
    details = summary(row)
    metrics = {
        "距离（km）": scaled(numeric(row.get("totalDistance")), 1000, 2),
        "用时（分钟）": scaled(numeric(row.get("totalTimeSeconds")), 60),
        ("平均配速（min/km）" if pace else "平均速度（km/h）"):
            f"{pace//60}:{pace%60:02d}" if pace else round(speed * 3.6, 2) if speed is not None else None,
        "爬升（m）": numeric(details.get("ascent")), "下降（m）": numeric(details.get("descent")),
        "平均心率（bpm）": numeric(heart), "最高心率（bpm）": numeric(row.get("hrMaxValue")),
        "最低心率（bpm）": numeric(row.get("hrMinValue")), "平均步频": numeric(details.get("avgCadence")),
        "最高步频": numeric(details.get("maxCadence")), "平均功率（W）": numeric(details.get("avgPower")),
        "TSS": numeric(row.get("tss")), "PTE": numeric(details.get("pte")),
        "峰值 EPOC": numeric(details.get("peakEpoc")),
        "恢复时间（Suunto 原值）": numeric(details.get("recoveryTime")),
        "能量（kcal）": numeric(row.get("energy")), "步数": numeric(row.get("stepCount")),
    }
    return {label: value for label, value in metrics.items() if value is not None}


def table_rows(entries, source):
    if source == "workouts":
        return [[stamp, item.get("activityName"), scaled(numeric(item.get("totalDistance")), 1000, 2),
                 scaled(numeric(item.get("totalTimeSeconds")), 60), numeric(item.get("hrAvgValue")),
                 numeric((item.get("summary") or {}).get("pte")) if isinstance(item.get("summary"), dict) else None]
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
        ("距离（公里）", "距离", lambda r: scaled(numeric(r.get("totalDistance")), 1000, 6), "sum"),
        ("时长（分钟）", "时长", lambda r: scaled(numeric(r.get("totalTimeSeconds")), 60, 6), "sum"),
    ]
    if extended:
        definitions += [
            ("活动次数", "活动次数", lambda r: 1, "sum"),
            ("累计爬升（m）", "爬升", lambda r: numeric(summary(r).get("ascent")), "sum"),
            ("训练负荷（TSS）", "TSS", lambda r: numeric(r.get("tss")), "sum"),
            ("TSS 记录覆盖（%）", "有 TSS 的活动", lambda r: numeric(r.get("tss")), "coverage"),
            ("平均训练效果（PTE）", "PTE", lambda r: numeric(summary(r).get("pte")), "mean"),
        ]
    totals = [[] for _ in definitions]
    ranges = []
    for offset in range(0, (last_day - first_label).days + 1, step):
        bucket = first_label + timedelta(days=offset)
        start = max(first_day, bucket)
        end = min(last_day + timedelta(days=1), bucket + timedelta(days=step))
        covered = covers(intervals, datetime.combine(start, time.min, tz), datetime.combine(end, time.min, tz))
        label = bucket.isoformat()
        labels.append(f"{label}（部分覆盖）" if weekly and not covered else label)
        ranges.append((start.isoformat(), (end-timedelta(days=1)).isoformat()))
        rows = grouped.get(label, [])
        for column, (_, _, metric, method) in zip(totals, definitions):
            values = [v for row in rows if (v := metric(row)) is not None]
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
        for series in result:
            series["ranges"] = ranges
    return result


def chart(title, labels, columns, kind="line"):
    return {"title": title, "labels": labels, "columns": columns, "kind": kind}


def csv_cells(store):
    buffer = StringIO()
    writer = csv.writer(buffer)

    def line(values):
        buffer.seek(0)
        buffer.truncate()
        # Prevent CSV metadata values from being interpreted as spreadsheet formulas.
        writer.writerow([("'" + item) if isinstance(item, str) and item.lstrip().startswith(
            ("=", "+", "-", "@")) else item for item in values])
        return buffer.getvalue()

    yield "\ufeff" + line(("source", "record_key", "occurred_at", "payload"))
    # StreamingResponse may advance this iterator on different worker threads.
    with store.connect(check_same_thread=False) as db:
        cursor = db.execute("""
            SELECT source, record_key, occurred_at, payload FROM records
            ORDER BY source, occurred_at, record_key
            """)
        for row in cursor:
            yield line(row)
