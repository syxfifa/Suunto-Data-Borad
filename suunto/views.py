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


def workout_page(store, start, end, page, size=50):
    where, parameters = record_filter("workouts", start, end)
    count = store.query(f"SELECT count(*) AS n FROM records WHERE {where}", parameters)[0]["n"]
    pages = max(1, (count + size - 1) // size)
    page = min(page, pages)
    rows = store.query(f"""
        SELECT record_key, occurred_at, payload FROM records WHERE {where}
        ORDER BY occurred_at DESC, record_key DESC LIMIT ? OFFSET ?
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
                points.append({"label": label, "lat": lat, "lon": lon})
        points_by_route.append(points)
    return points_by_route


def workout_chart(entries, weekly, first_day, last_day, intervals, tz):
    grouped = defaultdict(lambda: [[], []])
    for at, row in entries:
        day = date.fromisoformat(at[:10])
        if weekly:
            day -= timedelta(days=day.weekday())
        metrics = grouped[day.isoformat()]
        for target, field, divisor in zip(metrics, ("totalDistance", "totalTimeSeconds"), (1000, 60)):
            value = numeric(row.get(field))
            target.append(value / divisor if value is not None else None)
    step = 7 if weekly else 1
    first_label = first_day
    if weekly:
        first_label -= timedelta(days=first_day.weekday())
    labels = []
    totals = [[], []]
    for offset in range(0, (last_day - first_label).days + 1, step):
        bucket = first_label + timedelta(days=offset)
        start = max(first_day, bucket)
        end = min(last_day + timedelta(days=1), bucket + timedelta(days=step))
        covered = covers(intervals, datetime.combine(start, time.min, tz), datetime.combine(end, time.min, tz))
        label = bucket.isoformat()
        labels.append(f"{label}（部分覆盖）" if weekly and not covered else label)
        metrics = grouped.get(label)
        for index, column in enumerate(totals):
            if not metrics:
                column.append(0 if covered else None)
            else:
                values = metrics[index]
                column.append(None if any(value is None for value in values) else round(sum(values), 2))
    return [chart("距离（公里）", labels, {"距离": totals[0]}, "bar"),
            chart("时长（分钟）", labels, {"时长": totals[1]}, "bar")]


def chart(title, labels, columns, kind="line"):
    return {"title": title, "labels": labels, "columns": columns, "kind": kind}


def summary_chart(title, entries, field, combine):
    values = defaultdict(list)
    for at, row in entries:
        value = numeric(row["entryData"].get(field))
        if value is not None:
            values[at[:10]].append(value)
    labels = sorted(values)
    return chart(title, labels, {title: [round(combine(values[day]), 2) for day in labels]})


def daily_charts(entries):
    kinds = {
        "stepcount": ("步数", 1),
        "energyconsumption": ("日常能量消耗（千卡）", 4184),
    }
    values = defaultdict(lambda: defaultdict(dict))
    for stamp, row in entries:
        name = row["Name"]
        value = numeric(row["Sample"].get("Value"))
        if name in kinds and row["Aggregation"] == "sum" and value is not None:
            values[name][row["Source"]][stamp[:10]] = value / kinds[name][1]
    charts = []
    for name, (title, _) in kinds.items():
        providers = values[name]
        labels = sorted({day for samples in providers.values() for day in samples})
        columns = {f"来源 {index}": [scaled(samples.get(day)) for day in labels]
                   for index, (_, samples) in enumerate(sorted(providers.items()), start=1)}
        charts.append(chart(title, labels, columns))
    return charts


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
