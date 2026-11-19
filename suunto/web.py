"""First-stage web authorization and real MCP discovery."""

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, time as clock_time, timedelta
from ipaddress import ip_address
import json
import logging
from pathlib import Path
import re
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .mcp import AUTHORIZATION_TIMEOUT, Client, MCPError, NeedsAuthorization, READ_TOOLS
from .config import Config
from .store import Store
from .sync import Synchronizer, TZ
from .views import csv_cells, daily_charts, records, route_map_points, summary_chart, table_rows, workout_chart


TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name("templates"))
LOG = logging.getLogger(__name__)
NO_CACHE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# Browsers can send Origin: null on a form POST from a no-referrer page.
HOME_HEADERS = {**NO_CACHE, "Referrer-Policy": "same-origin"}
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
    discovery = {"status": "probing", "tools": [], "checked_at": None, "message": None}

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
        # Serve the authorization page even while the first network probe waits.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        store.initialize()
        startup = asyncio.create_task(asyncio.to_thread(probe))
        schedule_changed = asyncio.Event()
        app.state.schedule_changed = schedule_changed
        stopping = False

        async def schedule():
            await startup
            while not stopping:
                try:
                    await asyncio.wait_for(schedule_changed.wait(), timeout=config.load() * 60)
                    schedule_changed.clear()
                except asyncio.TimeoutError:
                    try:
                        await asyncio.to_thread(sync.run, "scheduled")
                    except Exception:
                        LOG.exception("Scheduled synchronization failed")

        scheduler = asyncio.create_task(schedule())
        try:
            yield
        finally:
            stopping = True
            schedule_changed.set()
            await scheduler
            mcp.http.close()

    app = FastAPI(title="Suunto 内网看板", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")

    @app.get("/")
    def index(request: Request):
        status = store.status()
        today = datetime.now(TZ)
        overview = []
        if "workouts" in status["counts"]:
            for days in (7, 28):
                start = (today - timedelta(days=days)).isoformat(timespec="seconds")
                row = store.query("""
                    SELECT count(*) AS n,
                           sum(CASE WHEN json_type(payload,'$.totalDistance') IN ('real','integer')
                                    THEN json_extract(payload,'$.totalDistance') END) / 1000.0 AS km,
                           sum(CASE WHEN json_type(payload,'$.totalTimeSeconds') IN ('real','integer')
                                    THEN json_extract(payload,'$.totalTimeSeconds') END) / 3600.0 AS hours
                    FROM records WHERE source='workouts' AND occurred_at>=?
                    """, (start,))[0]
                overview.append({"days": days, **row})
        return TEMPLATES.TemplateResponse(
            request=request, name="discovery.html",
            context={"overview": overview},
            headers=NO_CACHE,
        )

    @app.get("/healthz")
    def health():
        store.query("SELECT 1 FROM records LIMIT 1")
        return {"status": "ok"}

    @app.post("/oauth/start")
    def authorize(request: Request):
        try:
            redirect_uri = dashboard_callback(request)
            origin = request.headers.get("origin")
            if origin and origin != redirect_uri.removesuffix("/oauth/callback"):
                raise MCPError("请从看板页面发起授权。")
            url, state = mcp.begin(redirect_uri)
        except MCPError as exc:
            LOG.warning("OAuth registration or discovery failed: %s", exc)
            return settings_page(request, oauth_error=str(exc))
        result = RedirectResponse(url, status_code=303, headers=NO_CACHE)
        result.set_cookie("suunto_oauth_state", state, max_age=AUTHORIZATION_TIMEOUT, httponly=True,
                          samesite="lax", secure=request.url.scheme == "https")
        return result

    def sync_after_authorization():
        probe()
        try:
            sync.run("oauth")
        except Exception:
            LOG.exception("Synchronization after OAuth failed")

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
    def browser_probe():
        probe()
        return RedirectResponse("/settings", status_code=303, headers=NO_CACHE)

    @app.post("/api/probe")
    def api_probe():
        # Return schemas from the wire and flag which known tools this app may call.
        result = probe()
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
        return {"running": sync.lock.locked(), **store.status()}

    @app.post("/api/sync")
    def api_sync():
        result = sync.run("cli")
        if result is None:
            raise HTTPException(409, "已有同步任务在运行。")
        return result

    @app.post("/sync")
    def browser_sync(request: Request):
        result = sync.run("web")
        if result is None:
            return settings_page(request, message="已有同步任务在运行。")
        return RedirectResponse("/settings", status_code=303, headers=NO_CACHE)

    def dashboard(request, title, **context):
        return TEMPLATES.TemplateResponse(request=request, name="dashboard.html",
                                         context={"title": title, **context}, headers=NO_CACHE)

    def recent(source, days=28):
        today = datetime.now(TZ).date()
        return records(store, source, datetime.combine(today - timedelta(days=days - 1), clock_time.min, TZ))

    @app.get("/training")
    def training(request: Request, period: str = "day"):
        weekly = period == "week"
        today = datetime.now(TZ).date()
        first_day = today - timedelta(days=89)
        return dashboard(request, "训练趋势", show_period=True, weekly=weekly,
                         charts=workout_chart(recent("workouts", 90), weekly, first_day, today))

    @app.get("/activities")
    def workouts(request: Request, start: str = "", end: str = ""):
        activity_span = store.query(
            "SELECT min(occurred_at) AS first, max(occurred_at) AS last FROM records WHERE source='workouts'"
        )[0]
        available_days = store.query("""
            SELECT DISTINCT substr(occurred_at, 1, 10) AS day FROM records
            WHERE source='workouts' AND occurred_at IS NOT NULL ORDER BY day DESC LIMIT 90
        """)
        filters = {"filters": True, "activity_span": activity_span, "available_days": available_days}
        start, end = start.strip(), end.strip()
        try:
            first = date.fromisoformat(start.replace("/", "-")) if start else None
            last = date.fromisoformat(end.replace("/", "-")) if end else None
            if last == date.max or (first and last and last < first):
                raise ValueError
        except ValueError:
            return dashboard(request, "运动活动", **filters, start=start, end=end,
                             message="请选择有效的日期（YYYY-MM-DD），结束日期应等于或晚于开始日期。")
        if first is None and last is None and not {"start", "end"}.intersection(request.query_params):
            first = datetime.now(TZ).date() - timedelta(days=27)
        begin = datetime.combine(first, clock_time.min, TZ) if first else None
        finish = datetime.combine(last + timedelta(days=1), clock_time.min, TZ) if last else None
        return dashboard(request, "运动活动", **filters, start=first, end=last,
                         headings=("北京时间", "运动类型", "距离（km）", "时长（分钟）", "平均心率", "训练效果 PTE"),
                         rows=table_rows(records(store, "workouts", begin, finish), "workouts"))

    @app.get("/sleep-recovery")
    def sleep_recovery(request: Request):
        return dashboard(request, "睡眠与恢复", charts=[
            summary_chart("睡眠时长（小时）", recent("sleep"), "Duration", lambda values: sum(values) / 3600),
            summary_chart("日均身体资源（%）", recent("recovery"), "Balance",
                          lambda values: 100 * sum(values) / len(values)),
        ])

    @app.get("/daily-activity")
    def daily_activity(request: Request):
        return dashboard(request, "日常活动", charts=daily_charts(recent("daily_activity")))

    @app.get("/routes")
    def routes(request: Request):
        entries = records(store, "routes")
        return dashboard(request, "保存路线", headings=("路线名称或描述", "距离（km）", "运动类型", "地图"),
                         rows=table_rows(entries, "routes"), route_maps=route_map_points(entries))

    @app.get("/sync-status")
    def sync_page():
        return RedirectResponse("/settings#sync", status_code=303, headers=NO_CACHE)

    def settings_page(request, *, saved=False, message=None, oauth_error=None):
        return TEMPLATES.TemplateResponse(
            request=request, name="dashboard.html",
            context={"title": "设置", "saved": saved, "message": message, "oauth_error": oauth_error,
                     "settings_minutes": config.load(), "discovery": discovery, "sync_status": sync_status()},
            headers=NO_CACHE if request.url.path == "/oauth/callback" else HOME_HEADERS,
        )

    @app.get("/settings")
    def settings(request: Request, saved: bool = False, authorized: bool = False, refresh_required: bool = False):
        message = None
        if authorized and mcp.credential().get("access_token"):
            message = "Suunto 授权成功，已安排同步。"
            if refresh_required:
                message += " 本次授权未返回刷新令牌，到期后需重新授权。"
        return settings_page(request, saved=saved, message=message)

    @app.post("/settings")
    async def save_settings(minutes: Annotated[int, Form(ge=1, le=1440)]):
        await asyncio.to_thread(config.save, minutes)
        app.state.schedule_changed.set()
        return RedirectResponse("/settings?saved=true", status_code=303, headers=NO_CACHE)

    @app.get("/export.csv")
    def export_csv():
        return StreamingResponse(csv_cells(store), media_type="text/csv",
                                 headers={**NO_CACHE, "Content-Disposition": 'attachment; filename="suunto.csv"'})

    return app
