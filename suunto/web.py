"""First-stage web authorization and real MCP discovery."""

import asyncio
from contextlib import ExitStack, asynccontextmanager
from datetime import date, datetime, time as clock_time, timedelta
from ipaddress import ip_address
import json
import logging
from math import ceil
from pathlib import Path
import re
from time import monotonic, time
from sqlite3 import Error as SQLiteError
from typing import Annotated
from urllib.parse import urlencode, urlsplit

from fastapi import Cookie, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .mcp import AUTHORIZATION_TIMEOUT, Client, MCPError, NeedsAuthorization, READ_TOOLS
from .access import Access
from .amap import amap_router
from . import VERSION
from .library import library_router
from .maintenance import storage_size, volume_lock
from .config import Config
from .reports import (WELLNESS_FIELDS, acwr_series, aurora_week, daily_report, dates, grouped_totals,
                      intensity_charts, longest_streak, mapping_fields, midnight, period_summary,
                      sport_counts, sport_name, wellness)
from .store import Store
from .sync import SOURCES, Synchronizer, TZ
from .views import covers, csv_cells, lower_bound, records, summary, workout_chart, workout_metrics, workout_page


TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name("templates"))
LOG = logging.getLogger(__name__)
NO_CACHE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# Browsers can send Origin: null on a form POST from a no-referrer page.
HOME_HEADERS = {**NO_CACHE, "Referrer-Policy": "same-origin"}
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
SOURCE_LABELS = dict(workouts="运动活动", sleep="睡眠", recovery="恢复", daily_activity="日常活动",
                     routes="保存路线", workout_details="运动详情")


def local_return(value, *allowed_paths):
    if not value or re.search(r"[\\\x00-\x20\x7f]", value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if not (parsed.scheme or parsed.netloc or value.startswith("//")) and parsed.path in allowed_paths else None


def activity_link(start, end, sport="", return_to=""):
    return "/activities?" + urlencode({"start": start, "end": end, "sport": sport, "return_to": return_to})


def add_review_links(series, sport, back, today):
    for item in series:
        item["links"] = [activity_link(start, end, sport, back) if start <= today.isoformat() else None
                         for start, end in item["ranges"]]


def local_transport(request):
    if request.url.scheme == "https":
        return True
    try:
        return (ip_address(request.client.host).is_loopback
                and (request.url.hostname == "localhost" or ip_address(request.url.hostname).is_loopback))
    except (ValueError, AttributeError):
        return False


def dashboard_callback(request):
    """Use the same origin that holds the browser's OAuth state cookie."""
    try:
        origin = urlsplit(str(request.base_url))
        host = origin.hostname
        port = origin.port
    except ValueError as exc:
        raise MCPError("看板访问地址无效。") from exc
    if origin.scheme not in ("http", "https") or not host or origin.username is not None:
        raise MCPError("看板访问地址无效。")
    try:
        valid_host = not ip_address(host).is_unspecified
    except ValueError:
        name = host.removesuffix(".")
        valid_host = len(name) <= 253 and all(DNS_LABEL.fullmatch(label) for label in name.split("."))
    if not valid_host:
        raise MCPError("看板访问地址无效，请使用服务器 IP 或域名打开看板。")
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != {"http": 80, "https": 443}[origin.scheme]:
        authority += f":{port}"
    return f"{origin.scheme}://{authority}/oauth/callback"


class SampleRequest(BaseModel):
    tool: str
    arguments: dict


def field_types(result):
    """Report observed types from JSON and JSON text blocks, with values omitted."""
    fields = {}

    def walk(value, path, depth=0):
        if depth >= 12 or len(fields) >= 1000:
            return
        kind = {str: "string", int: "integer", float: "number", bool: "boolean",
                type(None): "null", dict: "object", list: "array"}.get(type(value), "unknown")
        fields.setdefault(path, set()).add(kind)
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}/{key.replace('~', '~0').replace('/', '~1')}", depth + 1)
        elif isinstance(value, list):
            for child in value:
                walk(child, path + "[]", depth + 1)

    if "structuredContent" in result:
        walk(result["structuredContent"], "structuredContent")
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                walk(json.loads(item.get("text", "")), "content[]")
            except (TypeError, ValueError):
                walk(item.get("text"), "content[]")
    return {key: sorted(kinds) for key, kinds in sorted(fields.items())}


def create_app(data_dir=None, *, client=None):
    data_dir = Path(data_dir) if data_dir is not None else (client.path.parent if client else Path("/data"))
    mcp = client or Client(data_dir)
    store = Store(data_dir)
    sync = Synchronizer(mcp, store)
    config = Config(data_dir)
    amap_config = Config(data_dir, "amap.json")
    access = Access(data_dir)
    discovery = {"status": "probing", "tools": [], "checked_at": None, "message": None}
    task = None
    task_name = None
    wake = asyncio.Event()

    def queue_increment(trigger):
        store.enqueue("incremental", trigger)
        sync.yield_history.set()
        wake.set()

    def submit(trigger, *, ticket=None, **options):
        nonlocal task, task_name
        if sync.stopping.is_set() or sync.lock.locked() or (task and not task.done()):
            return None
        if trigger == "backfill":
            sync.pause_history.clear()
            sync.yield_history.clear()

        async def work():
            result = None
            try:
                result = await asyncio.to_thread(sync.run, trigger, **options)
                if result and result["status"] == "authorization_required":
                    discovery.update(status="authorization_required", tools=[], message="Suunto 需要重新授权。")
                return result
            except Exception:
                LOG.exception("Synchronization task failed")
                return {"status": "failed"}
            finally:
                status = result["status"] if result else "failed"
                keep = status in {"yielded", "interrupted"} or (
                    trigger == "backfill" and status == "authorization_required")
                if ticket and not keep and not sync.stopping.is_set():
                    store.dequeue(ticket["kind"], ticket["revision"])
                wake.set()

        task_name = trigger
        task = asyncio.create_task(work())
        return task

    def probe():
        try:
            tools = mcp.tools()
            discovery.update(status="ready", tools=tools, message=None)
        except NeedsAuthorization as exc:
            discovery.update(status="authorization_required", tools=[], message=str(exc))
        except MCPError as exc:
            LOG.warning("MCP discovery failed: %s", exc)
            discovery.update(status="error", tools=[], message=str(exc))
        discovery["checked_at"] = datetime.now(TZ).isoformat()
        return dict(discovery)

    @asynccontextmanager
    async def lifespan(_):
        with ExitStack() as maintenance:
            maintenance.enter_context(volume_lock(data_dir))
            async with app_runtime():
                yield

    @asynccontextmanager
    async def app_runtime():
        # Serve the authorization page even while the first network probe waits.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        sync.stopping.clear()
        sync.pause_history.clear()
        sync.yield_history.clear()
        store.initialize()
        prune_logs()
        startup = asyncio.create_task(asyncio.to_thread(probe))
        schedule_changed = asyncio.Event()
        app.state.schedule_changed = schedule_changed
        stopping = False

        async def schedule():
            await startup
            deadline = monotonic() + config.load() * 60
            while not stopping:
                wake.clear()
                if schedule_changed.is_set():
                    schedule_changed.clear()
                    deadline = monotonic() + config.load() * 60
                if monotonic() >= deadline:
                    deadline = monotonic() + config.load() * 60
                    if discovery["status"] != "authorization_required":
                        queue_increment("scheduled")
                    try:
                        await asyncio.to_thread(prune_logs)
                    except SQLiteError:
                        LOG.exception("Could not prune old sync logs")
                if not stopping and not (task and not task.done()):
                    queued = store.queued()
                    ticket = queued.get("incremental")
                    if ticket is None and discovery["status"] != "authorization_required":
                        ticket = queued.get("backfill")
                    if ticket:
                        submit(ticket["trigger"], ticket=ticket)
                try:
                    await asyncio.wait_for(wake.wait(), timeout=max(0, deadline - monotonic()))
                except asyncio.TimeoutError:
                    pass

        scheduler = asyncio.create_task(schedule())
        try:
            yield
        finally:
            stopping = True
            sync.stopping.set()
            wake.set()
            await scheduler
            if task is not None:
                await task
            mcp.http.close()

    def prune_logs():
        store.prune_logs((datetime.now(TZ) - timedelta(days=config.retention_days())).isoformat())

    app = FastAPI(title="Suunto 内网看板", version=VERSION, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")

    @app.middleware("http")
    async def local_access(request, call_next):
        path = request.url.path
        if path == "/healthz" or path.startswith("/static/"):
            return await call_next(request)
        if access.enabled():
            if not local_transport(request):
                return JSONResponse({"detail": "已启用访问控制，请通过 HTTPS 地址访问。"}, 403, headers=NO_CACHE)
            if not await asyncio.to_thread(access.accepts, request.headers.get("authorization", "")):
                return JSONResponse({"detail": "请输入本地看板用户名和密码。"}, 401,
                                    headers={**NO_CACHE, "WWW-Authenticate": 'Basic realm="Suunto dashboard", charset="UTF-8"'})
        if request.method == "POST" and path != "/oauth/start":
            origin = request.headers.get("origin")
            same = True
            if origin is not None:
                try:
                    same = origin == dashboard_callback(request).removesuffix("/oauth/callback")
                except MCPError:
                    same = False
            if request.headers.get("sec-fetch-site") == "cross-site" or not same:
                return JSONResponse({"detail": "请从看板页面发起操作。"}, 403, headers=NO_CACHE)
        return await call_next(request)

    @app.get("/")
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
        stats = store.status()
        return TEMPLATES.TemplateResponse(
            request=request, name="discovery.html",
            context={"overview": overview, "week": period_summary(data, midnight(week_start), today, intervals),
                     "prior": period_summary(data, midnight(previous_start), today - timedelta(days=7), intervals),
                     "month": period_summary(data, midnight(month_start), today, intervals),
                     "selected_link": activity_link(chosen, chosen, return_to=back), "today": day,
                     "sports": sport_counts(week_rows), "last_sync": stats["last_success_at"],
                     "weekly_goal": preferences.get("weekly_km"), "review_days": snapshots,
                     "selected": selected, "week_from": review_start,
                     "week_to": review_end, "review_charts": review_charts,
                     "previous_week": chosen - timedelta(days=7) if chosen - timedelta(days=7) >= earliest else None,
                     "next_week": min(today.date(), chosen + timedelta(days=7)) if review_end < today.date() else None},
            headers=HOME_HEADERS,
        )

    @app.get("/healthz")
    def health():
        store.query("SELECT 1 FROM records LIMIT 1")
        return {"status": "ok"}

    def mcp_state():
        try:
            credential = mcp.credential()
        except MCPError:
            return "error"
        if not credential.get("access_token"):
            return "authorization_required"
        expires = credential.get("expires_at", credential.get("refresh_at"))
        if not credential.get("refresh_token") and type(expires) in (int, float) and expires <= time():
            return "authorization_required"
        state = discovery["status"]
        return state if state in ("ready", "authorization_required", "error") else "probing"

    @app.get("/readyz")
    def readiness():
        state = mcp_state()
        return JSONResponse({"status": state, "version": VERSION}, status_code=200 if state == "ready" else 503,
                            headers=NO_CACHE)

    def account_has_data():
        return bool(store.query("SELECT 1 FROM records LIMIT 1"))

    @app.post("/disconnect")
    async def disconnect():
        if (task and not task.done()) or not sync.lock.acquire(blocking=False):
            return RedirectResponse("/maintenance?notice=busy", status_code=303, headers=NO_CACHE)
        try:
            await asyncio.to_thread(mcp.disconnect)
            store.dequeue("incremental")
            discovery.update(status="authorization_required", tools=[], message="已从此看板断开 Suunto。")
        finally:
            sync.lock.release()
            wake.set()
        return RedirectResponse("/maintenance?notice=disconnected", status_code=303, headers=NO_CACHE)

    @app.post("/oauth/start")
    async def authorize(request: Request, same_account: Annotated[bool, Form()] = False):
        try:
            redirect_uri = dashboard_callback(request)
            origin = request.headers.get("origin")
            if origin and origin != redirect_uri.removesuffix("/oauth/callback"):
                raise MCPError("请从看板页面发起授权。")
            if not same_account and account_has_data():
                raise MCPError("此数据卷已有个人记录，请确认使用原 Suunto 账号；新账号请使用独立数据卷。")
            url, state = await asyncio.to_thread(mcp.begin, redirect_uri)
        except MCPError as exc:
            LOG.warning("OAuth registration or discovery failed: %s", exc)
            return settings_page(request, oauth_error=str(exc))
        result = RedirectResponse(url, status_code=303, headers=NO_CACHE)
        result.set_cookie("suunto_oauth_state", state, max_age=AUTHORIZATION_TIMEOUT, httponly=True,
                          samesite="lax", secure=request.url.scheme == "https")
        return result

    async def sync_after_authorization():
        # Persist the work before probing again so an interrupted startup can recover it.
        queue_increment("oauth")
        job = submit("oauth", ticket=store.queued()["incremental"])
        await asyncio.to_thread(probe)
        wake.set()
        if job is not None:
            await asyncio.shield(job)

    def complete(request, code, browser_state, returned_state, issuer, callback_uri):
        try:
            credentials = mcp.complete(code, browser_state, returned_state, issuer, callback_uri=callback_uri)
        except MCPError as exc:
            LOG.warning("OAuth completion failed: %s", exc)
            return settings_page(request, message=str(exc))
        discovery.update(status="probing", tools=[], message=None)
        target = "/settings?authorized=true"
        if not credentials.get("refresh_token"):
            target += "&refresh_required=true"
        response = RedirectResponse(target, status_code=303, headers=NO_CACHE,
                                    background=BackgroundTask(sync_after_authorization))
        response.delete_cookie("suunto_oauth_state")
        return response

    @app.get("/oauth/callback")
    def callback(request: Request, code: str = "", state: str = "", iss: str | None = None, error: str = "",
                 suunto_oauth_state: Annotated[str, Cookie()] = ""):
        if not suunto_oauth_state:
            return settings_page(request, message="授权会话已失效，请在当前浏览器重新连接 Suunto。")
        if error:
            return settings_page(request, message="Suunto 未完成授权，请返回设置重新发起。")
        if not code or not state:
            return settings_page(request, message="回调未收到授权码和 state，请重新授权。")
        try:
            redirect_uri = dashboard_callback(request)
        except MCPError as exc:
            return settings_page(request, message=str(exc))
        return complete(request, code, suunto_oauth_state, state, iss, redirect_uri)

    @app.post("/probe")
    async def browser_probe():
        await asyncio.to_thread(probe)
        wake.set()
        return RedirectResponse("/settings", status_code=303, headers=NO_CACHE)

    @app.post("/api/probe")
    async def api_probe():
        # Return schemas from the wire and flag which known tools this app may call.
        result = await asyncio.to_thread(probe)
        wake.set()
        result["tools"] = [{**item, "allowed_read": item["name"] in READ_TOOLS} for item in result["tools"]]
        return result

    @app.post("/api/probe/sample")
    def api_sample(sample: SampleRequest):
        try:
            result = mcp.sample(sample.tool, sample.arguments, {item["name"] for item in discovery["tools"]})
        except NeedsAuthorization as exc:
            raise HTTPException(401, str(exc)) from exc
        except MCPError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"tool": sample.tool, "observed_fields": field_types(result)}

    @app.get("/api/status")
    def sync_status():
        active = bool(task and not task.done())
        queued = store.queued()
        return {"running": active or sync.lock.locked(), "task": task_name if active else None,
                "incremental_pending": "incremental" in queued, "backfill_active": "backfill" in queued,
                "current_source": sync.current_source,
                "retry_after": max(0, ceil(getattr(mcp, "cooldown", 0) - monotonic())), **store.status()}

    @app.post("/api/sync")
    async def api_sync():
        job = submit("cli")
        if job is None:
            raise HTTPException(409, "已有同步任务在运行。")
        return await asyncio.shield(job)

    @app.post("/sync")
    async def browser_sync():
        queue_increment("web")
        return sync_redirect(True)

    def sync_redirect(job):
        target = "/settings?job=started#sync" if job else "/settings?job=busy#sync"
        return RedirectResponse(target, status_code=303, headers=NO_CACHE)

    @app.post("/backfill")
    async def backfill(request: Request, mode: Annotated[str, Form()] = "",
                       start: Annotated[str, Form()] = "", end: Annotated[str, Form()] = ""):
        if mode == "pause":
            store.dequeue("backfill")
            if task_name == "backfill" and task and not task.done():
                sync.pause_history.set()
            return RedirectResponse("/settings?job=pause#sync", status_code=303, headers=NO_CACHE)
        pending = bool(store.query("SELECT 1 FROM backfill WHERE next_at<end_at LIMIT 1"))
        if mode == "resume":
            if not pending:
                return settings_page(request, message="当前没有待继续的历史补采。")
            if not store.queued().get("backfill"):
                store.enqueue("backfill", "backfill")
                wake.set()
            return sync_redirect(True)
        if pending and mode != "replace":
            return settings_page(request, message="已有未完成的历史补采，请继续或明确替换。")
        today = datetime.now(TZ).date()
        try:
            if mode in ("90", "365"):
                first, last = today - timedelta(days=int(mode) - 1), today
            elif mode in ("custom", "replace"):
                first, last = date.fromisoformat(start.strip()), date.fromisoformat(end.strip())
            else:
                raise ValueError
            if not first <= last <= today:
                raise ValueError
        except ValueError:
            return settings_page(request, message="请输入有效的补采范围，结束日期不能晚于今天。")
        begin = datetime.combine(first, clock_time.min, TZ)
        finish = min(datetime.now(TZ).replace(microsecond=0),
                     datetime.combine(last + timedelta(days=1), clock_time.min, TZ))
        if begin >= finish:
            return settings_page(request, message="所选时间范围尚无可查询时段。")
        if task_name == "backfill" and task and not task.done():
            return sync_redirect(False)
        store.prepare_backfill((source for source, (_, chunk) in SOURCES.items() if chunk is not None),
                               begin.isoformat(), finish.isoformat(), activate=True)
        wake.set()
        return sync_redirect(True)

    @app.post("/workout-details")
    async def refresh_details(key: Annotated[str, Form()] = ""):
        if key and store.record("workouts", key) is None:
            raise HTTPException(404, "该活动不在本地数据库中。")
        return sync_redirect(submit("details", detail_key=key or None))

    @app.post("/route-details")
    async def refresh_route(key: Annotated[str, Form()]):
        if not store.query("""
            SELECT 1 FROM records WHERE source='routes' AND record_key=?
            AND record_key NOT IN (SELECT record_key FROM removed_routes)
        """, (key,)):
            raise HTTPException(404, "路线不在本地路线库中。")
        return sync_redirect(submit("route_details", detail_key=key))

    def dashboard(request, title, **context):
        return TEMPLATES.TemplateResponse(request=request, name="dashboard.html",
                                         context={"title": title, **context}, headers=HOME_HEADERS)

    def recent(source, days=28):
        current = datetime.now(TZ)
        return records(store, source, midnight(current.date() - timedelta(days=days - 1)), current)

    @app.get("/training")
    def training(request: Request, period: str = "day", sport: str = "",
                 days: Annotated[int, Query(ge=7, le=366)] = 90):
        weekly = period == "week"
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
        back = "/training?" + urlencode({"period": "week" if weekly else "day", "sport": selected, "days": days})
        add_review_links(series, selected, back + "#training-charts", today)
        months = grouped_totals(current, first_day, today, coverage, monthly=True)
        weeks = grouped_totals(current, first_day, today, coverage)
        for item in months + weeks:
            item["link"] = activity_link(item["start"], item["end"], selected, back + "#training-reports")
        return dashboard(request, "训练趋势", show_period=True, weekly=weekly,
                         charts=series[:2] + [series[4]], sport=selected, sports=sports, days=days,
                         advanced_charts=series[2:4] + series[5:] + [acwr_series(history, first_day, today, coverage)],
                         monthly_totals=months, weeks=weeks,
                         streak=longest_streak(current), training_mix=sport_counts(current))

    @app.get("/activities")
    def workouts(request: Request, start: str = "", end: str = "",
                 page: Annotated[int, Query(ge=1)] = 1, sport: str = "", q: str = "",
                 km: str = "", minutes: str = "", tss: str = "",
                 sort: str = "date", order: str = "desc", return_to: str = ""):
        today = datetime.now(TZ).date()
        activity_span = store.query(
            "SELECT min(occurred_at) AS first, max(occurred_at) AS last FROM records WHERE source='workouts'"
        )[0]
        available_days = store.query("""
            SELECT DISTINCT substr(occurred_at, 1, 10) AS day FROM records
            WHERE source='workouts' AND occurred_at IS NOT NULL ORDER BY day DESC LIMIT 90
        """)
        presets = [(f"近 {days} 天", (today - timedelta(days=days-1)).isoformat()) for days in (7, 28, 90)]
        presets.append(("本月", today.replace(day=1).isoformat()))
        activity_types = store.query("""
            SELECT DISTINCT coalesce(nullif(json_extract(payload, '$.activityName'), ''), '未标注运动类型') AS sport
            FROM records WHERE source='workouts' ORDER BY sport
        """)
        search_state = {"sport": sport, "q": q, "km": km, "minutes": minutes, "tss": tss, "sort": sort, "order": order}
        filters = {"filters": True, "activity_span": activity_span, "available_days": available_days,
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
        summary = None
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
                summary = "这段日期的运动同步遇到问题，请查看设置中的同步状态。"
            elif begin and covers(store.coverage("workouts"), begin, finish or datetime.now(TZ)):
                summary = "这段已查询范围内没有符合筛选条件的活动。"
            else:
                summary = "本地没有匹配记录，所选范围尚未确认完整。可到设置发起历史补采。"
        export_query = urlencode({"source": "workouts", "start": first or "", "end": last or "",
                                  **{key: search_state[key] for key in ("sport", "q", "km", "minutes", "tss")}})
        current_page = "/activities?" + urlencode({"start": first or "", "end": last or "", "page": page,
                                                 **search_state, "return_to": filters["review_return"] or ""})
        details = ["/workout?" + urlencode({"id": key, "return_to": current_page + f"#activity-{index}"})
                   for index, key in enumerate(keys)]
        return dashboard(request, "运动活动", **filters, start=first, end=last, detail_links=details,
                         headings=("北京时间", "运动类型", "距离（km）", "时长（分钟）", "平均心率", "训练效果 PTE"),
                         rows=rows, activity_keys=keys, result_count=count, page=page, pages=pages,
                         empty_message=summary, export_query=export_query)

    @app.get("/workout")
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
        return dashboard(request, row.get("workoutName") or sport_name(row), workout=row, workout_id=id,
                         workout_at=found[0]["occurred_at"], metrics=workout_metrics(row),
                         workout_return=local_return(return_to, "/activities"),
                         charts=intensity_charts(row), optional_metrics=optional)

    @app.get("/sleep-recovery")
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

    @app.get("/daily-activity")
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

    app.include_router(amap_router(amap_config))
    app.include_router(library_router(store, config, amap_config, discovery, dashboard))

    @app.get("/sync-status")
    def sync_page():
        return RedirectResponse("/settings#sync", status_code=303, headers=NO_CACHE)

    def settings_page(request, *, saved=False, preferences_saved=False, amap_saved=False, message=None, oauth_error=None):
        status = sync_status()
        status["backfill_pending"] = any(row["next_at"] < row["end_at"] for row in status["backfill"])
        spans = {row["source"]: row for row in status["sources"]}
        sources = [{"name": name, "label": SOURCE_LABELS[name], "span": spans.get(name),
                    "coverage": store.coverage(name), "cursor": store.cursor(name),
                    "checked_at": status["source_success"].get(name),
                    "failure": status["failures"].get(name),
                    "available": any(tool["name"] == meta[0] for tool in discovery["tools"])}
                   for name, meta in SOURCES.items()]
        prefs = config.read()
        map_settings = amap_config.read()
        field_sets = wellness_fields()
        configured = prefs.get("wellness_fields", {})
        cutoff = midnight(datetime.now(TZ).date() - timedelta(days=89)).isoformat()
        samples = {source: store.query(
            "SELECT count(*) AS n FROM records WHERE source=? AND occurred_at>=?", (source, cutoff)
        )[0]["n"] for source in field_sets}
        stage_unit = {"seconds": "秒", "minutes": "分钟", "ms": "毫秒"}[prefs.get("sleep_stage_unit", "seconds")]
        bindings = []
        for key, (source, label, kind) in WELLNESS_FIELDS.items():
            options = mapping_fields(field_sets[source], kind)
            selected = configured.get(key)
            stage = key in {"deep", "light", "rem"}
            counter = "nonnegative" if stage else "timestamps" if kind == "text" else "samples"
            unit = stage_unit if stage else {"hrv": "毫秒", "resting_hr": "bpm"}.get(
                key, "ISO 日期时间" if kind == "text" else "Suunto 原值")
            valid = sum(row[counter] for row in field_sets[source] if row["name"] == selected
                        and (kind != "number" or row["type"] in {"integer", "real"}))
            bindings.append({"key": key, "label": label, "options": options, "selected": selected,
                             "unit": unit, "valid": valid, "samples": samples[source]})
        return TEMPLATES.TemplateResponse(
            request=request, name="dashboard.html",
            context={"title": "设置", "saved": saved, "preferences_saved": preferences_saved,
                     "amap_saved": amap_saved, "amap_enabled": map_settings.get("enabled", False),
                     "amap_coordinates": map_settings.get("coordinates"),
                     "amap_has_key": bool(map_settings.get("key")), "amap_has_secret": bool(map_settings.get("secret")),
                     "message": message, "oauth_error": oauth_error,
                     "settings_minutes": config.load(), "discovery": discovery, "sync_status": status,
                     "sources": sources, "source_labels": SOURCE_LABELS, "prefs": prefs, "bindings": bindings,
                     "access_protected": access.enabled(),
                     "daily_sources": daily_sources(), "account_has_data": account_has_data()},
            headers=NO_CACHE if request.url.path == "/oauth/callback" else HOME_HEADERS,
        )

    @app.get("/settings")
    def settings(request: Request, saved: bool = False, authorized: bool = False, refresh_required: bool = False,
                 job: str = "", preferences_saved: bool = False, amap_saved: bool = False):
        message = {"started": "任务已提交。", "busy": "已有同步任务运行中，请稍后重试。",
                   "pause": "已请求暂停，当前请求结束后会保留补采进度。"}.get(job)
        if authorized and mcp.credential().get("access_token"):
            message = "Suunto 授权成功，已安排同步。"
            if refresh_required:
                message += " 本次授权未返回刷新令牌，到期后需重新授权。"
        return settings_page(request, saved=saved, preferences_saved=preferences_saved, amap_saved=amap_saved, message=message)

    @app.post("/settings")
    async def save_settings(minutes: Annotated[int, Form(ge=1, le=1440)]):
        await asyncio.to_thread(config.save, minutes)
        app.state.schedule_changed.set()
        wake.set()
        return RedirectResponse("/settings?saved=true", status_code=303, headers=NO_CACHE)

    @app.post("/preferences")
    async def save_preferences(request: Request,
                               weekly_km: Annotated[str, Form()] = "",
                               step_goal: Annotated[str, Form()] = "",
                               primary_source: Annotated[str, Form()] = "",
                               route_basemap: Annotated[bool, Form()] = False,
                               sleep_stage_unit: Annotated[str, Form()] = "seconds"):
        if sleep_stage_unit not in ("seconds", "minutes", "ms"):
            raise HTTPException(422, "睡眠阶段单位无效。")
        if primary_source and primary_source not in daily_sources():
            raise HTTPException(422, "请选择已有的日常活动来源。")
        try:
            distance_goal = lower_bound(weekly_km, 10000)
            steps = int(step_goal) if step_goal.strip() else None
            if distance_goal == 0 or steps is not None and not 0 < steps <= 1000000:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "训练目标或步数目标无效。")
        form = await request.form()
        stored = config.read().get("wellness_fields", {})
        data = wellness_fields()
        fields = {}
        for key, (source, _, kind) in WELLNESS_FIELDS.items():
            picked = form.get(key)
            if picked and picked != stored.get(key) and picked not in mapping_fields(data[source], kind):
                raise HTTPException(422, "只能选择本地记录里实际出现的字段。")
            if picked:
                fields[key] = picked
        stages = [fields[key] for key in ("deep", "light", "rem") if key in fields]
        if len(stages) != len(set(stages)) or fields.get("bedtime") and fields.get("bedtime") == fields.get("wake"):
            raise HTTPException(422, "睡眠阶段及入睡、醒来时间必须使用各自不同的字段。")
        await asyncio.to_thread(config.update, {"weekly_km": distance_goal, "step_goal": steps,
                                "primary_source": primary_source,
                                "route_basemap": route_basemap, "sleep_stage_unit": sleep_stage_unit,
                                "wellness_fields": fields})
        return RedirectResponse("/settings?preferences_saved=true#preferences", status_code=303, headers=NO_CACHE)

    def wellness_fields():
        cutoff = midnight(datetime.now(TZ).date() - timedelta(days=89)).isoformat()
        return {source: store.observed_entry_fields(source, cutoff) for source in ("sleep", "recovery")}

    def daily_sources():
        return [row["provider"] for row in store.query("""
            SELECT DISTINCT json_extract(payload, '$.Source') AS provider FROM records
            WHERE source='daily_activity' AND json_type(payload, '$.Source')='text' ORDER BY provider
        """)]

    @app.get("/export.csv")
    def export_csv(source: str = "", start: str = "", end: str = "", format: str = "raw", share: bool = False,
                   sport: str = "", q: str = "", km: str = "", minutes: str = "", tss: str = ""):
        if (source and source not in SOURCES) or format not in ("raw", "columns"):
            raise HTTPException(422, "导出数据源或格式无效。")
        try:
            first = date.fromisoformat(start) if start else None
            last = date.fromisoformat(end) if end else None
            if last == date.max or (first and last and first > last):
                raise ValueError
            distance, duration, stress = lower_bound(km, 100000), lower_bound(minutes, 600000), lower_bound(tss, 100000)
        except ValueError as exc:
            raise HTTPException(422, "导出日期或筛选条件无效。") from exc
        begin = midnight(first) if first else None
        finish = midnight(last + timedelta(days=1)) if last else None
        rows = csv_cells(store, source, begin, finish, format=format, share=share, sport=sport, search=q.strip(),
                         minimums={"distance": distance * 1000 if distance is not None else None,
                                   "duration": duration * 60 if duration is not None else None, "tss": stress})
        name = "suunto-share.csv" if share else "suunto.csv"
        return StreamingResponse(rows, media_type="text/csv",
                                 headers={**NO_CACHE, "Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/export")
    def export_options(request: Request):
        return TEMPLATES.TemplateResponse(request=request, name="maintenance.html",
            context={"title": "导出数据", "sources": {name: SOURCE_LABELS[name] for name in SOURCES}},
            headers=HOME_HEADERS)

    @app.get("/maintenance")
    def maintenance_page(request: Request, notice: str = ""):
        message = {"disconnected": "已断开 Suunto 授权，本地历史仍可查看。",
                   "busy": "同步正在进行，请完成或暂停后重试。",
                   "retention": "已保存日志保留天数。",
                   "enabled": "已保存本地看板密码。", "disabled": "已关闭本地看板密码。"}.get(notice)
        status = store.status()
        return TEMPLATES.TemplateResponse(request=request, name="maintenance.html",
            context={"title": "维护", "maintenance": True, "message": message, "version": VERSION,
                     "counts": {name: status["counts"].get(name, 0) for name in SOURCES}, "source_labels": SOURCE_LABELS,
                     "size": round(storage_size(data_dir)/1024**2, 2), "mcp_status": mcp_state(),
                     "last_sync": status["last_success_at"], "log_days": config.retention_days(),
                     "protected": access.enabled(), "secure": local_transport(request)},
            headers=HOME_HEADERS)

    @app.get("/diagnostics.json")
    def diagnostics():
        status = store.status()
        # Build the report from explicitly allowed aggregates, never from diagnostic log bodies.
        states = {"ok", "failed", "partial", "running", "interrupted", "paused", "yielded", "authorization_required"}
        report = {
            "version": VERSION, "database_bytes": storage_size(data_dir),
            "authorization": mcp_state(), "access_control": access.enabled(),
            "retention_days": config.retention_days(),
            "record_counts": {name: status["counts"].get(name, 0) for name in SOURCES},
            "recent_sync_statuses": [item["status"] for item in status["runs"] if item["status"] in states],
        }
        return JSONResponse(report, headers={**NO_CACHE, "Content-Disposition": 'attachment; filename="suunto-diagnostics.json"'})

    @app.post("/maintenance/retention")
    async def save_retention(days: Annotated[int, Form(ge=7, le=365)]):
        await asyncio.to_thread(config.update, {"log_retention_days": days})
        # Honor a shorter window immediately without removing any active run.
        await asyncio.to_thread(prune_logs)
        return RedirectResponse("/maintenance?notice=retention", status_code=303, headers=NO_CACHE)

    @app.post("/maintenance/access")
    async def save_access(request: Request, mode: Annotated[str, Form()], password: Annotated[str, Form()] = ""):
        if not local_transport(request):
            raise HTTPException(403, "请先配置可信 HTTPS 反向代理。")
        try:
            if mode == "enable":
                await asyncio.to_thread(access.set_password, password)
                notice = "enabled"
            elif mode == "disable":
                access.clear()
                notice = "disabled"
            else:
                raise ValueError("访问控制操作无效。")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return RedirectResponse(f"/maintenance?notice={notice}#access", status_code=303, headers=NO_CACHE)

    return app
