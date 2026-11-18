"""Read-only incremental ingestion of live Suunto response formats."""

from datetime import datetime, time, timedelta
import json
import logging
from threading import Lock
from zoneinfo import ZoneInfo

from .mcp import MCPError, NeedsAuthorization
from .store import encode


TZ = ZoneInfo("Asia/Shanghai")
LOG = logging.getLogger(__name__)
OVERLAP = timedelta(days=2)
SOURCES = {
    "workouts": ("list_user_workouts", timedelta(days=27)),
    "sleep": ("get_sleep_summary", timedelta(days=27)),
    "recovery": ("get_recovery_summary", timedelta(days=1)),
    "daily_activity": ("get_daily_activity_summary", timedelta(days=27)),
    "routes": ("list_user_routes", None),
}


def now():
    return datetime.now(TZ).replace(microsecond=0)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("缺少时间字段")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        # The official Suunto MCP date contract uses UTC when no offset is supplied.
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(TZ).isoformat()


def nonempty(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少记录标识")
    return value


def unpack(result, expected):
    """The live MCP returns JSON inside a text block. Accept structured JSON as well."""
    candidates = [result.get("structuredContent")]
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                candidates.append(json.loads(block["text"]))
            except (ValueError, TypeError, KeyError):
                continue
    for value in candidates:
        if isinstance(value, expected):
            return value
    raise MCPError("MCP 响应结构与已验证的格式不一致。")


def as_list(value):
    if not isinstance(value, list):
        raise ValueError("响应列表字段缺失")
    return value


def entry_row(source, item):
    if not isinstance(item, dict):
        raise ValueError("记录不是对象")
    if source == "workouts":
        return nonempty(item.get("workoutId")), timestamp(item.get("startTime")), item
    if source == "routes":
        date = item.get("modified") or item.get("created")
        return nonempty(item.get("routeId")), timestamp(date) if date else None, item
    observed = timestamp(item.get("timestamp"))
    data = item.get("entryData")
    if not isinstance(data, dict):
        raise ValueError("缺少 entryData")
    if source == "sleep":
        # Live samples show SleepId is reused. Include the sample timestamp.
        return encode([data.get("SleepId"), observed]), observed, item
    return observed, observed, item


class Synchronizer:
    def __init__(self, mcp, store):
        self.mcp = mcp
        self.store = store
        self.lock = Lock()

    def run(self, trigger, *, stop=None):
        if not self.lock.acquire(blocking=False):
            return None
        run_id = None
        status = "failed"
        try:
            stop = stop or now()
            run_id = self.store.start(trigger, stop.isoformat())
            names = {tool["name"] for tool in self.mcp.tools()}
            failures = 0
            for source, (tool, chunk) in SOURCES.items():
                if tool not in names:
                    self.log_error(run_id, source, f"未探测到 {tool}，已跳过。")
                    failures += 1
                    continue
                try:
                    if source == "routes":
                        self.routes(run_id, tool, stop, names)
                    else:
                        self.windows(run_id, source, tool, chunk, stop, names)
                except NeedsAuthorization:
                    raise
                except (MCPError, ValueError) as exc:
                    self.log_error(run_id, source, str(exc))
                    failures += 1
            if self.store.query("SELECT 1 FROM sync_errors WHERE run_id=? LIMIT 1", (run_id,)):
                status = "failed" if failures == len(SOURCES) else "partial"
            else:
                status = "ok"
        except MCPError as exc:
            self.log_error(run_id, "mcp", str(exc))
            status = "authorization_required" if isinstance(exc, NeedsAuthorization) else "failed"
        finally:
            try:
                if run_id is not None:
                    self.store.finish(run_id, now().isoformat(), status)
            finally:
                self.lock.release()
        return {"run_id": run_id, "status": status}

    def log_error(self, run_id, source, message):
        LOG.warning("sync run=%s source=%s: %s", run_id, source, message)
        self.store.error(run_id, source, message)

    def windows(self, run_id, source, tool, chunk, stop, names):
        cursor = self.store.cursor(source)
        start = (datetime.fromisoformat(cursor["scanned_until"]) - OVERLAP if cursor
                 else stop - timedelta(days=28))
        if source == "daily_activity":
            start = datetime.combine(start.date(), time.min, TZ)
        while start < stop:
            end = min(start + chunk, stop)
            self.window(run_id, source, tool, start, end, names)
            start = end

    def window(self, run_id, source, tool, start, stop, names):
        args = {"startDate": start.isoformat(), "endDate": stop.isoformat()}
        if source == "workouts":
            args["limit"] = 100
        listed = source == "workouts"
        value = unpack(self.mcp.sample(tool, args, names), list if listed else dict)
        records = value if listed else as_list(value.get("data"))
        if listed and len(records) >= args["limit"]:
            span = int((stop - start).total_seconds())
            if span <= 1:
                raise MCPError("运动查询在一秒窗口仍达到 100 条上限。")
            middle = start + timedelta(seconds=span // 2)
            self.window(run_id, source, tool, start, middle, names)
            self.window(run_id, source, tool, middle, stop, names)
            return
        if source == "daily_activity":
            rows = list(self.daily_rows(run_id, records))
        else:
            rows = list(self.rows(run_id, source, records, names))
        self.store.commit(run_id, source, rows, until=stop.isoformat())

    def rows(self, run_id, source, records, names):
        for position, item in enumerate(records, 1):
            try:
                key, date, data = entry_row(source, item)
                encode(data)
            except (ValueError, TypeError, OverflowError):
                self.log_error(run_id, source, f"第 {position} 条记录无效，已跳过。")
                continue
            if source == "workouts":
                data = self.workout_detail(run_id, names, key, data)
            yield key, date, data

    def workout_detail(self, run_id, names, key, data):
        if "get_workout_detail" not in names:
            return data
        stored = self.store.record("workouts", key) or {}
        if isinstance(stored.get("summary"), dict):
            return {**stored, **data}
        try:
            detail = unpack(self.mcp.sample("get_workout_detail", {"workoutId": key}, names), dict)
            if detail.get("workoutId") != key:
                raise MCPError("详情返回的 workoutId 不匹配。")
            encode(detail)
            return {**data, **detail}
        except NeedsAuthorization:
            raise
        except (MCPError, ValueError, TypeError):
            self.log_error(run_id, "workouts", "单条运动详情未能读取，已保留列表字段。")
            return data

    def daily_rows(self, run_id, series):
        for stream in series:
            try:
                name = nonempty(stream["Name"])
                aggregation = nonempty(stream["Aggregation"])
                sources = as_list(stream["Sources"])
            except (KeyError, TypeError, ValueError):
                self.log_error(run_id, "daily_activity", "日常活动指标结构无效。")
                continue
            for source in sources:
                try:
                    provider = nonempty(source["Name"])
                    samples = as_list(source["Samples"])
                except (KeyError, TypeError, ValueError):
                    self.log_error(run_id, "daily_activity", "日常活动来源结构无效。")
                    continue
                for item in samples:
                    try:
                        at = timestamp(item["TimeISO8601"])
                        key = encode([name, aggregation, provider, at])
                        payload = {"Name": name, "Aggregation": aggregation, "Source": provider, "Sample": item}
                        encode(payload)
                        yield key, at, payload
                    except (KeyError, TypeError, ValueError, OverflowError):
                        self.log_error(run_id, "daily_activity", "日常活动采样无效，已跳过。")

    def routes(self, run_id, tool, stop, names):
        seen = set()
        cursor = self.store.cursor("routes")
        full_at = cursor["full_scan_at"] if cursor else None
        full = not full_at or stop - datetime.fromisoformat(full_at) >= timedelta(days=1)
        args = {"size": 100, "sortDirection": "ASC"}
        if not full:
            args["sinceDate"] = (datetime.fromisoformat(cursor["scanned_until"]) - OVERLAP).isoformat()
        page = 0
        while True:
            items = unpack(self.mcp.sample(tool, {**args, "page": page}, names), list)
            rows = list(self.rows(run_id, "routes", items, names))
            identifiers = tuple(key for key, _, _ in rows)
            if identifiers and identifiers in seen:
                raise MCPError("路线分页返回重复数据，游标未推进。")
            seen.add(identifiers)
            last_page = len(items) < args["size"]
            self.store.commit(run_id, "routes", rows, until=stop.isoformat() if last_page else None,
                              full_scan_at=stop.isoformat() if last_page and full else None)
            if last_page:
                return
            page += 1
