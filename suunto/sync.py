"""Read-only incremental ingestion of live Suunto response formats."""

from datetime import datetime, time, timedelta
import json
from hashlib import sha256
import logging
from threading import Event, Lock
from time import monotonic
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
ROUTE_POINT_FIELDS = ("startPoint", "centerPoint", "endPoint")


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
    if result.get("isError") is True:
        raise MCPError("MCP 工具返回错误。")
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
        self.stopping = Event()
        self.pause_history = Event()
        self.yield_history = Event()
        self.current_source = None

    def run(self, trigger, *, stop=None, history=None, detail_key=None):
        if not self.lock.acquire(blocking=False):
            return None
        run_id = None
        status = "failed"
        try:
            stop = stop or now()
            run_id = self.store.start(trigger, stop.isoformat())
            if history is not None:
                self.store.prepare_backfill(
                    (source for source, (_, chunk) in SOURCES.items() if chunk is not None),
                    *(bound.isoformat() for bound in history),
                )
            names = {tool["name"] for tool in self.mcp.tools()}
            if trigger == "backfill":
                status = self.backfill(run_id, names)
                return {"run_id": run_id, "status": status}
            if trigger == "details":
                failures = self.fill_details(run_id, names, detail_key)
                status = "interrupted" if self.stopping.is_set() else "partial" if failures else "ok"
                return {"run_id": run_id, "status": status}
            if trigger == "route_details":
                self.fill_route_detail(run_id, names, detail_key)
                status = "ok"
                return {"run_id": run_id, "status": status}
            failures = 0
            for source, (tool, chunk) in SOURCES.items():
                self.current_source = source
                if self.stopping.is_set():
                    status = "interrupted"
                    return {"run_id": run_id, "status": status}
                if tool not in names:
                    self.store.failure(source, None, None, "MCP 未公开此工具。")
                    self.log_error(run_id, source, f"未探测到 {tool}，已跳过。")
                    failures += 1
                    continue
                try:
                    if source == "routes":
                        if self.routes(run_id, tool, stop, names):
                            self.store.clear_failure(source)
                    else:
                        self.windows(run_id, source, tool, chunk, stop, names)
                except NeedsAuthorization:
                    raise
                except (MCPError, ValueError) as exc:
                    if source == "routes":
                        self.store.failure(source, None, None, str(exc))
                    self.log_error(run_id, source, str(exc))
                    failures += 1
            if not self.stopping.is_set():
                self.repair_windows(run_id, names)
            if "get_workout_detail" in names and not self.stopping.is_set():
                self.fill_details(run_id, names)
            if self.stopping.is_set():
                status = "interrupted"
            elif self.store.query("SELECT 1 FROM sync_errors WHERE run_id=? LIMIT 1", (run_id,)):
                status = "failed" if failures == len(SOURCES) else "partial"
            else:
                status = "ok"
        except InterruptedError:
            status = "interrupted"
            if trigger == "backfill" and not self.stopping.is_set():
                status = "paused" if self.pause_history.is_set() else "yielded"
        except MCPError as exc:
            self.log_error(run_id, "mcp", str(exc))
            status = "authorization_required" if isinstance(exc, NeedsAuthorization) else "failed"
        finally:
            try:
                if run_id is not None:
                    self.store.finish(run_id, now().isoformat(), status)
            finally:
                self.current_source = None
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
        while start < stop and not self.stopping.is_set():
            end = min(start + chunk, stop)
            try:
                clean = self.window(run_id, source, tool, start, end, names)
            except NeedsAuthorization:
                raise
            except (MCPError, ValueError) as exc:
                self.store.window_failure(source, start.isoformat(), end.isoformat(), str(exc), now(), retry=False)
                raise
            if clean:
                self.store.clear_failure(source)
            else:
                self.store.window_failure(source, start.isoformat(), end.isoformat(),
                                          "部分记录无法解析，此窗口未确认覆盖。", now(), retry=False)
            start = end

    def repair_windows(self, run_id, names):
        supported = [source for source, (tool, chunk) in SOURCES.items() if chunk and tool in names]
        if not supported:
            return
        for failure in self.store.windows_due(now().isoformat(), supported, 4):
            if monotonic() < getattr(self.mcp, "cooldown", 0):
                break
            source = failure["source"]
            tool, _ = SOURCES[source]
            self.current_source = source
            start, end = failure["start_at"], failure["end_at"]
            try:
                if not self.window(run_id, source, tool, datetime.fromisoformat(start),
                                   datetime.fromisoformat(end), names, repair=True):
                    raise MCPError("重试仍有部分记录无法解析。")
            except NeedsAuthorization:
                raise
            except (MCPError, ValueError) as exc:
                self.store.window_failure(source, start, end, str(exc), now())
                self.log_error(run_id, source, f"修复历史缺口失败。{exc}")

    def backfill(self, run_id, names):
        failures = 0
        jobs = self.store.query("SELECT * FROM backfill WHERE next_at<end_at ORDER BY rowid")
        for job in jobs:
            source = job["source"]
            self.current_source = source
            tool, chunk = SOURCES[source]
            start = datetime.fromisoformat(job["next_at"])
            stop = datetime.fromisoformat(job["end_at"])
            try:
                if tool not in names:
                    raise MCPError(f"MCP 未公开 {tool}。")
                while start < stop:
                    end = min(start + chunk, stop)
                    try:
                        if not self.window(run_id, source, tool, start, end, names, historical=True):
                            raise MCPError("部分记录无法解析，补采保留在待重试窗口。")
                    except NeedsAuthorization:
                        raise
                    except (MCPError, ValueError) as exc:
                        self.store.window_failure(source, start.isoformat(), end.isoformat(), str(exc), now(),
                                                  retry=False)
                        raise
                    start = end
            except (MCPError, ValueError) as exc:
                self.store.backfill_error(source, str(exc))
                self.log_error(run_id, source, str(exc))
                failures += 1
                if isinstance(exc, NeedsAuthorization):
                    raise
            if self.stopping.is_set():
                return "interrupted"
        return "partial" if failures or self.store.query(
            "SELECT 1 FROM sync_errors WHERE run_id=? LIMIT 1", (run_id,)
        ) else "ok"

    def window(self, run_id, source, tool, start, stop, names, *, historical=False, repair=False):
        if self.stopping.is_set() or (historical and (self.pause_history.is_set() or self.yield_history.is_set())):
            raise InterruptedError
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
            left = self.window(run_id, source, tool, start, middle, names, historical=historical, repair=repair)
            if historical and not left:
                return False
            right = self.window(run_id, source, tool, middle, stop, names, historical=historical, repair=repair)
            if left and right:
                self.store.clear_windows(source, start.isoformat(), stop.isoformat())
            return left and right
        errors = self.store.query("SELECT max(id) AS id FROM sync_errors WHERE run_id=?", (run_id,))[0]["id"]
        if source == "daily_activity":
            rows = list(self.daily_rows(run_id, records))
        else:
            rows = list(self.rows(run_id, source, records))
        clean = not self.store.query(
            "SELECT 1 FROM sync_errors WHERE id>? AND run_id=? LIMIT 1", (errors or 0, run_id)
        )
        details = []
        if listed:
            rows, details = self.merge_workout_list(rows)
        self.store.commit(run_id, source, rows, until=None if historical or repair else args["endDate"],
                          history_until=args["endDate"] if historical and clean else None,
                          interval=(args["startDate"], args["endDate"]) if clean else None,
                          details=details, checked_at=now().isoformat() if clean else None)
        return clean

    def rows(self, run_id, source, records):
        for position, item in enumerate(records, 1):
            try:
                key, date, data = entry_row(source, item)
                encode(data)
            except (ValueError, TypeError, OverflowError):
                self.log_error(run_id, source, f"第 {position} 条记录无效，已跳过。")
                continue
            yield key, date, data

    def merge_workout_list(self, rows):
        details = []
        merged = []
        states = self.store.detail_states(key for key, _, _ in rows)
        for key, at, data in rows:
            stored = self.store.record("workouts", key) or {}
            fingerprint = sha256(encode(data).encode()).hexdigest()
            state = states.get(key)
            if not state or state["fingerprint"] != fingerprint:
                unchanged_legacy = (not state and isinstance(stored.get("summary"), dict)
                                    and all(stored.get(field) == value for field, value in data.items()))
                details.append((key, fingerprint, "ready" if unchanged_legacy else "pending", None))
            merged.append((key, at, {**stored, **data}))
        return merged, details

    def fill_details(self, run_id, names, key=None):
        self.current_source = "workout_details"
        if "get_workout_detail" not in names:
            self.log_error(run_id, "workout_details", "MCP 未公开运动详情工具。")
            return True
        if key is None:
            rows = self.store.details_due(now().isoformat(), 25)
        else:
            rows = self.store.query("""
                SELECT r.record_key, r.occurred_at, r.payload, d.fingerprint
                FROM records r LEFT JOIN workout_details d USING(record_key)
                WHERE r.source='workouts' AND r.record_key=?
                """, (key,))
        failed = False
        for row in rows:
            if self.stopping.is_set():
                raise InterruptedError
            key, fingerprint = row["record_key"], row["fingerprint"]
            try:
                detail = unpack(self.mcp.sample("get_workout_detail", {"workoutId": key}, names), dict)
                if detail.get("workoutId") != key:
                    raise MCPError("详情返回的 workoutId 不匹配。")
                at = timestamp(detail["startTime"]) if detail.get("startTime") else row["occurred_at"]
                data = {**json.loads(row["payload"]), **detail}
                self.store.commit(run_id, "workouts", [(key, at, data)], details=[(key, fingerprint, "ready", None)])
            except NeedsAuthorization:
                raise
            except (MCPError, ValueError, TypeError, OverflowError) as exc:
                failed = True
                self.store.detail_state(key, fingerprint, "pending", (now() + timedelta(minutes=30)).isoformat())
                self.log_error(run_id, "workout_details", f"详情未能读取，已保留列表字段。{exc}")
        return failed

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
        active = set()
        complete = True
        cursor = self.store.cursor("routes")
        full_at = cursor["full_scan_at"] if cursor else None
        full = not full_at or stop - datetime.fromisoformat(full_at) >= timedelta(days=1)
        args = {"size": 100, "sortDirection": "ASC"}
        if not full:
            args["sinceDate"] = (datetime.fromisoformat(cursor["scanned_until"]) - OVERLAP).isoformat()
        page = 0
        while not self.stopping.is_set():
            items = unpack(self.mcp.sample(tool, {**args, "page": page}, names), list)
            rows = list(self.rows(run_id, "routes", items))
            identifiers = tuple(key for key, _, _ in rows)
            if active.intersection(identifiers) or len(set(identifiers)) != len(identifiers):
                raise MCPError("路线分页返回重复数据，游标未推进。")
            active.update(identifiers)
            complete = complete and len(rows) == len(items)
            rows = [(key, at, {**data, **self.preserved_route_points(key, data)}) for key, at, data in rows]
            last_page = len(items) < args["size"]
            reconciled = last_page and full and complete
            self.store.commit(run_id, "routes", rows, until=stop.isoformat() if last_page and complete else None,
                              full_scan_at=stop.isoformat() if reconciled else None,
                              active_routes=active if reconciled else None,
                              checked_at=now().isoformat() if last_page and complete else None)
            if last_page:
                if not complete:
                    self.store.failure("routes", None, None, "部分路线无法解析，已保留以前的路线。")
                return complete
            page += 1

    def preserved_route_points(self, key, data):
        if all(isinstance(data.get(field), dict) for field in ROUTE_POINT_FIELDS):
            return {}
        stored = self.store.record("routes", key) or {}
        if stored.get("modified") != data.get("modified"):
            return {}
        return {field: stored[field] for field in ROUTE_POINT_FIELDS
                if isinstance(stored.get(field), dict) and not isinstance(data.get(field), dict)}

    def fill_route_detail(self, run_id, names, key):
        if "get_route_summary" not in names:
            raise MCPError("MCP 未公开线路详情工具。")
        stored = self.store.record("routes", key)
        if not stored:
            raise MCPError("路线不在本地记录中。")
        detail = unpack(self.mcp.sample("get_route_summary", {"routeId": key}, names), dict)
        if detail.get("routeId") != key:
            raise MCPError("详情返回的 routeId 不匹配。")
        merged = {**stored, **detail, **self.preserved_route_points(key, {**stored, **detail})}
        self.store.commit(run_id, "routes", [entry_row("routes", merged)])
