"""Activity comparison and saved-route browsing."""

from datetime import date, timedelta
import json
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request

from .amap import amap_ready
from .reports import compare_metrics
from .views import local_return, lower_bound, record_filter, route_map_points, table_rows, workout_filter, workout_speed, workout_sql


ROUTE_SPORTS = """CASE WHEN json_type(payload, '$.activityNames')='array'
                        THEN json_extract(payload, '$.activityNames') ELSE '[]' END"""


def library_router(store, amap_config, discovery, render):
    router = APIRouter()

    @router.get("/compare")
    def compare(request: Request, sport: str = "", first: str = "", second: str = "",
                page: Annotated[int, Query(ge=1)] = 1, return_to: str = ""):
        size = 50
        with store.connect() as db:
            db.execute("BEGIN")
            sports = [row["sport"] for row in db.execute("""
                SELECT DISTINCT coalesce(nullif(json_extract(payload, '$.activityName'), ''), '未标注运动类型') AS sport
                FROM records WHERE source='workouts' ORDER BY sport
                """)]
            chosen = sport if sport in sports else next(iter(sports), "")
            where, params = workout_filter("source='workouts'", [], sport=chosen)
            stats = db.execute(f"""
                SELECT count(*) AS total, max({workout_sql("distance")}) AS distance,
                       max({workout_sql("ascent")}) AS climb FROM records WHERE {where}
                """, params).fetchone()
            pages = max(1, (stats["total"] + size - 1) // size)
            page = min(page, pages)
            options = [dict(row) for row in db.execute(f"""
                SELECT record_key AS id, occurred_at AS at,
                       coalesce(nullif(json_extract(payload, '$.workoutName'), ''),
                                json_extract(payload, '$.activityName')) AS label
                FROM records WHERE {where} ORDER BY occurred_at DESC, record_key DESC LIMIT ? OFFSET ?
                """, [*params, size, (page - 1) * size])]
            picked = {}
            if first or second:
                for row in db.execute(f"""
                    SELECT record_key, occurred_at, payload FROM records
                    WHERE {where} AND record_key IN (?, ?)
                    """, [*params, first, second]):
                    body = json.loads(row["payload"])
                    key = row["record_key"]
                    picked[key] = body
                    if not any(item["id"] == key for item in options):
                        options.append({"id": key, "at": row["occurred_at"],
                                        "label": body.get("workoutName") or body.get("activityName")})
        rows = [compare_metrics(picked[key]) for key in (first, second)] if (
            first != second and first in picked and second in picked) else []
        back = local_return(return_to, "/workout", "/activities")
        deltas = []
        detail_links = []
        if rows:
            comparison_url = "/compare?" + urlencode({"sport": chosen, "first": first, "second": second,
                                                       "page": page, "return_to": back or ""})
            for key in (first, second):
                detail_links.append((picked[key].get("workoutName") or chosen,
                                     "/workout?" + urlencode({"id": key, "return_to": comparison_url})))
            for index, label, unit in ((0, "距离", "km"), (1, "时长", "分钟"),
                                       (3, "爬升", "m"), (4, "平均心率", "bpm")):
                a, b = rows[0][index], rows[1][index]
                deltas.append((label, f"{b - a:+.2f} {unit}" if a is not None and b is not None else None))
            unit_a, speed_a = workout_speed(picked[first])
            unit_b, speed_b = workout_speed(picked[second])
            pace = unit_a == unit_b == "min/km"
            difference = None
            if unit_a == unit_b and speed_a is not None and speed_b is not None:
                if pace:
                    a, b = (sum(int(value) * factor for value, factor in zip(speed.split(":"), (60, 1)))
                            for speed in (speed_a, speed_b))
                    difference = f"{b - a:+d} 秒/km"
                else:
                    difference = f"{speed_b - speed_a:+.2f} km/h"
            deltas.insert(2, ("平均配速" if pace else "平均配速或速度", difference))
        return render(request, "运动对比", comparison=True, sports=sports, sport=chosen, options=options,
                      comparison_return=back, comparison_deltas=deltas, comparison_links=detail_links,
                      first=first, second=second,
                      best_km=stats["distance"] / 1000 if stats["distance"] is not None else None,
                      best_climb=stats["climb"], comparison_rows=rows, comparison_page=page,
                      comparison_pages=pages, comparison_query=urlencode(
                          {"sport": chosen, "first": first, "second": second, "return_to": back or ""}))

    @router.get("/routes")
    def routes(request: Request, q: str = "", sport: str = "", watch: str = "", visibility: str = "",
               km: str = "", updated: str = "", updated_to: str = "",
               page: Annotated[int, Query(ge=1)] = 1):
        try:
            first = date.fromisoformat(updated) if updated else None
            last = date.fromisoformat(updated_to) if updated_to else None
            if first and last and first > last:
                raise ValueError
        except ValueError:
            return render(request, "保存路线", message="请输入有效的更新日期范围，结束日期不得早于开始日期。")
        try:
            km = lower_bound(km, 100000)
        except ValueError:
            return render(request, "保存路线", message="请输入有效的距离。")
        base, base_params = record_filter("routes")
        where, params = [base], list(base_params)
        if q.strip():
            where.append("instr(casefold(CAST(coalesce(json_extract(payload, '$.description'), '') AS TEXT)), ?) > 0")
            params.append(q.strip().casefold())
        if sport:
            where.append(f"EXISTS (SELECT 1 FROM json_each({ROUTE_SPORTS}) AS activity "
                         "WHERE activity.type='text' AND activity.value=?)")
            params.append(sport)
        if watch:
            where.append("json_type(payload, '$.watchEnabled')=?")
            params.append({"on": "true", "off": "false"}.get(watch))
        if visibility:
            where.append("json_extract(payload, '$.visibility')=?")
            params.append(visibility)
        if km is not None:
            where.append(f"({workout_sql('distance')})>=?")
            params.append(km * 1000)
        if first or last:
            where.append("json_type(payload, '$.modified')='text' AND json_extract(payload, '$.modified')<>''")
        if first:
            where.append("occurred_at>=?")
            params.append(first.isoformat())
        if last and last != date.max:
            where.append("occurred_at<?")
            params.append((last + timedelta(days=1)).isoformat())
        clause = " AND ".join(where)
        size = 25
        with store.connect() as db:
            db.create_function("casefold", 1, str.casefold, deterministic=True)
            db.execute("BEGIN")
            stats = db.execute(f"""
                WITH matched AS MATERIALIZED (
                    SELECT record_key, {ROUTE_SPORTS} AS sport_list FROM records WHERE {clause}
                )
                SELECT NULL AS sport, count(*) AS total FROM matched
                UNION ALL
                SELECT activity.value, count(DISTINCT record_key)
                FROM matched, json_each(matched.sport_list) AS activity WHERE activity.type='text'
                GROUP BY activity.value
                """, params).fetchall()
            count = stats[0]["total"]
            distribution = sorted((tuple(row) for row in stats[1:]), key=lambda item: (-item[1], item[0]))
            pages = max(1, (count + size - 1) // size)
            page = min(page, pages)
            matches = db.execute(f"""
                SELECT occurred_at, payload FROM records WHERE {clause}
                ORDER BY occurred_at DESC, record_key DESC LIMIT ? OFFSET ?
                """, [*params, size, (page - 1) * size]).fetchall()
            all_matched = len(where) == 1 or count == db.execute(
                f"SELECT count(*) FROM records WHERE {base}", base_params).fetchone()[0]
            sports = sorted(name for name, _ in distribution) if all_matched else [
                row[0] for row in db.execute(f"""
                    SELECT DISTINCT activity.value FROM records, json_each({ROUTE_SPORTS}) AS activity
                    WHERE {base} AND activity.type='text' ORDER BY activity.value
                    """, base_params)]
        entries = [(row["occurred_at"], json.loads(row["payload"])) for row in reversed(matches)]
        metadata = [{"id": row["routeId"], "modified": at if row.get("modified") else None,
                     "watch": row.get("watchEnabled"), "visibility": row.get("visibility")}
                    for at, row in reversed(entries)]
        route_filter = {"q": q, "sport": sport, "watch": watch, "visibility": visibility,
                        "km": km, "updated": first or "", "updated_to": last or ""}
        query = urlencode({key: value for key, value in route_filter.items() if value not in ("", None)})
        amap = amap_config.read()
        preview = {"key": amap["key"], "coordinates": amap["coordinates"]} if amap_ready(amap) else None
        return render(request, "保存路线", headings=("路线名称或描述", "距离（km）", "运动类型", "地图"),
                      rows=table_rows(entries, "routes"), route_maps=route_map_points(entries), route_meta=metadata,
                      route_sports=sports, route_summary=distribution, route_filter=route_filter, route_query=query,
                      route_page=page, route_pages=pages, route_count=count,
                      amap_preview=preview,
                      route_detail_available=any(t["name"] == "get_route_summary" for t in discovery["tools"]))

    return router
