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
from threading import Lock
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .mcp import AUTHORIZATION_TIMEOUT, Client, MCPError, NeedsAuthorization, READ_TOOLS
from .access import Access, AccessBusy
from .amap import amap_router
from . import VERSION
from .library import library_router
from .maintenance import volume_lock
from .maintenance_routes import maintenance_router
from .config import Config
from .reports import WELLNESS_FIELDS, mapping_fields, midnight
from .report_routes import report_router
from .store import Store
from .sync import SOURCES, Synchronizer, TZ
from .views import lower_bound
from .web_shared import HOME_HEADERS, NO_CACHE, SOURCE_LABELS, TEMPLATES, local_transport
from .web_security import RequestSecurity


LOG = logging.getLogger(__name__)
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


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
    discovery_lock = Lock()
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
                result = await asyncio.to_thread(sync.run, trigger, discover=discover_tools, **options)
                if result and result["status"] == "authorization_required":
                    discovery.update(status="authorization_required", tools=[], message="Suunto 需要重新授权。",
                                     checked_at=datetime.now(TZ).isoformat())
                return result
            except Exception:
                LOG.exception("Synchronization task failed")
                return {"status": "failed"}
            finally:
                status = result["status"] if result else "failed"
                keep = status in {"yielded", "interrupted"} or (
                    trigger == "backfill" and status == "authorization_required") or bool(retry_after())
                if ticket and not keep and not sync.stopping.is_set():
                    store.dequeue(ticket["kind"], ticket["revision"])
                wake.set()

        task_name = trigger
        task = asyncio.create_task(work())
        return task

    def discover_tools():
        with discovery_lock:
            try:
                tools = mcp.tools()
            except MCPError as exc:
                status = "authorization_required" if isinstance(exc, NeedsAuthorization) else "error"
                discovery.update(status=status, tools=[], message=str(exc), checked_at=datetime.now(TZ).isoformat())
                raise
            discovery.update(status="ready", tools=tools, message=None, checked_at=datetime.now(TZ).isoformat())
            return tools

    def probe():
        try:
            discover_tools()
        except NeedsAuthorization:
            pass
        except MCPError as exc:
            LOG.warning("MCP discovery failed: %s", exc)
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
                wait = max(0, deadline - monotonic())
                if not stopping and not (task and not task.done()):
                    queued = store.queued()
                    ticket = queued.get("incremental")
                    if ticket is None and discovery["status"] != "authorization_required":
                        ticket = queued.get("backfill")
                    cooldown = retry_after()
                    if ticket and cooldown:
                        wait = min(wait, cooldown)
                    elif ticket:
                        submit(ticket["trigger"], ticket=ticket)
                try:
                    await asyncio.wait_for(wake.wait(), timeout=wait)
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
        path = request.scope["path"]
        if path == "/healthz" or path.startswith("/static/"):
            return await call_next(request)
        if access.enabled():
            if not local_transport(request):
                return JSONResponse({"detail": "已启用访问控制，请通过 HTTPS 地址访问。"}, 403, headers=NO_CACHE)
            try:
                accepted = await asyncio.to_thread(access.accepts, request.headers.get("authorization", ""))
            except AccessBusy:
                return JSONResponse({"detail": "密码验证繁忙，请稍后重试。"}, 503,
                                    headers={**NO_CACHE, "Retry-After": "1"})
            if not accepted:
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
            if request.headers.get("sec-fetch-site") in ("cross-site", "same-site") or not same:
                return JSONResponse({"detail": "请从看板页面发起操作。"}, 403, headers=NO_CACHE)
        return await call_next(request)

    app.add_middleware(RequestSecurity)

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
            if (request.headers.get("sec-fetch-site") in ("cross-site", "same-site")
                    or origin and origin != redirect_uri.removesuffix("/oauth/callback")):
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
                "retry_after": retry_after(), **store.status()}

    def retry_after():
        return max(0, ceil(getattr(mcp, "cooldown", 0) - monotonic()))

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

    @app.post("/sync/window")
    async def retry_sync_window(source: Annotated[str, Form()], start: Annotated[str, Form()],
                                end: Annotated[str, Form()]):
        key = (source, start, end)
        if source not in SOURCES or SOURCES[source][1] is None or not store.window_record(*key):
            return RedirectResponse("/settings?job=window_missing#sync", status_code=303, headers=NO_CACHE)
        if mcp_state() == "authorization_required":
            return RedirectResponse("/settings?job=window_auth#sync", status_code=303, headers=NO_CACHE)
        if retry_after():
            return RedirectResponse("/settings?job=window_limited#sync", status_code=303, headers=NO_CACHE)
        job = submit("window_retry", window_key=key)
        return sync_redirect(job)

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

    app.include_router(amap_router(amap_config, TEMPLATES))
    app.include_router(library_router(store, amap_config, discovery, dashboard))

    @app.get("/sync-status")
    def sync_page():
        return RedirectResponse("/settings#sync", status_code=303, headers=NO_CACHE)

    def settings_page(request, *, saved=False, preferences_saved=False, amap_saved=False, message=None, oauth_error=None):
        status = sync_status()
        status["backfill_pending"] = any(row["next_at"] < row["end_at"] for row in status["backfill"])
        for row in status["backfill"]:
            duration = datetime.fromisoformat(row["end_at"]) - datetime.fromisoformat(row["next_at"])
            row["remaining"] = max(0, ceil(duration / SOURCES[row["source"]][1]))
        retry_times = {}
        for row in status["window_failures"]:
            source = row["source"]
            retry_times[source] = min(row["retry_at"], retry_times.get(source, row["retry_at"]))
        spans = {row["source"]: row for row in status["sources"]}
        sources = [{"name": name, "label": SOURCE_LABELS[name], "span": spans.get(name),
                    "coverage": store.coverage(name), "cursor": store.cursor(name),
                    "checked_at": status["source_success"].get(name),
                    "next_retry": retry_times.get(name),
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
                     "access_protected": access.enabled(), "connection": mcp_state(),
                     "daily_sources": daily_sources(), "account_has_data": account_has_data()},
            headers=NO_CACHE if request.url.path == "/oauth/callback" else HOME_HEADERS,
        )

    @app.get("/settings")
    def settings(request: Request, saved: bool = False, authorized: bool = False, refresh_required: bool = False,
                 job: str = "", preferences_saved: bool = False, amap_saved: bool = False):
        message = {"started": "任务已提交。", "busy": "已有同步任务运行中，请稍后重试。",
                   "pause": "已请求暂停，当前请求结束后会保留补采进度。",
                   "window_missing": "这个窗口已处理或已移除，请刷新后查看。",
                   "window_auth": "请先连接 Suunto，再重试此窗口。",
                   "window_limited": "Suunto 正在限流，窗口暂不能重试。"}.get(job)
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
                                "sleep_stage_unit": sleep_stage_unit,
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

    app.include_router(report_router(store, config, dashboard, mcp_state, retry_after))
    app.include_router(maintenance_router(store, config, access, mcp_state, prune_logs))
    return app
