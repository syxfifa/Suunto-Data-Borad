"""First-stage web authorization and real MCP discovery."""

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, time as clock_time, timedelta
import json
import logging
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlsplit

from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .mcp import Client, MCPError, NeedsAuthorization, READ_TOOLS, REDIRECT
from .config import Config
from .store import Store
from .sync import Synchronizer, TZ
from .views import csv_cells, daily_charts, records, summary_chart, table_rows, workout_chart


TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name("templates"))
LOG = logging.getLogger(__name__)
NO_CACHE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


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

    def page(request, *, message=None, url=None):
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
            context={"discovery": discovery, "message": message, "authorization_url": url,
                     "sync_status": status, "overview": overview},
            headers=NO_CACHE,
        )

    @app.get("/")
    def index(request: Request):
        return page(request)

    @app.get("/healthz")
    def health():
        store.query("SELECT 1 FROM records LIMIT 1")
        return {"status": "ok"}

    @app.post("/oauth/start")
    def authorize(request: Request):
        try:
            url, state = mcp.begin()
        except MCPError as exc:
            LOG.warning("OAuth registration or discovery failed: %s", exc)
            return page(request, message=str(exc))
        result = page(request, url=url)
        result.set_cookie("suunto_oauth_state", state, max_age=300, httponly=True, samesite="lax")
        return result

    def complete(request, code, browser_state, returned_state=None, issuer=None):
        try:
            credentials = mcp.complete(code, browser_state, returned_state, issuer)
        except MCPError as exc:
            LOG.warning("OAuth completion failed: %s", exc)
            return page(request, message=str(exc))
        probe()
        if not credentials.get("refresh_token"):
            response = page(request, message="本次授权未返回 refresh token，访问令牌到期后需重新授权。")
        else:
            response = RedirectResponse("/", status_code=303, headers=NO_CACHE)
        response.delete_cookie("suunto_oauth_state")
        return response

    @app.get("/oauth/callback")
    def callback(request: Request, code: str = "", state: str = "", iss: str | None = None,
                 suunto_oauth_state: Annotated[str, Cookie()] = ""):
        if not suunto_oauth_state:
            return page(request, message="请复制地址栏中的完整回调地址，回到原页面手动提交。")
        if not code or not state:
            return page(request, message="回调未收到授权码和 state，请重新授权。")
        return complete(request, code, suunto_oauth_state, state, iss)

    @app.post("/oauth/manual")
    def paste(request: Request, authorization: Annotated[str, Form(max_length=8192)],
              suunto_oauth_state: Annotated[str, Cookie()] = ""):
        value = authorization.strip()
        if "://" in value:
            try:
                target = urlsplit(value)
                query = parse_qs(target.query)
                if target._replace(query="", fragment="").geturl() != REDIRECT:
                    raise ValueError()
                code, state = query["code"][0], query["state"][0]
            except (KeyError, ValueError):
                return page(request, message="请粘贴包含 code 和 state 的完整回调地址。")
            return complete(request, code, suunto_oauth_state, state, query.get("iss", [None])[0])
        return complete(request, value, suunto_oauth_state)

    @app.post("/probe")
    def browser_probe():
        probe()
        return RedirectResponse("/", status_code=303, headers=NO_CACHE)

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
            return page(request, message="已有同步任务在运行。")
        return RedirectResponse("/", status_code=303, headers=NO_CACHE)

    def dashboard(request, title, **context):
        return TEMPLATES.TemplateResponse(request=request, name="dashboard.html",
                                         context={"title": title, **context}, headers=NO_CACHE)

    def recent(source, days=28):
        today = datetime.now(TZ).date()
        return records(store, source, datetime.combine(today - timedelta(days=days - 1), clock_time.min, TZ))

    @app.get("/training")
    def training(request: Request, period: str = "day"):
        weekly = period == "week"
        return dashboard(request, "训练趋势", show_period=True, weekly=weekly,
                         charts=workout_chart(recent("workouts", 90), weekly))

    @app.get("/activities")
    def workouts(request: Request, start: date | None = None, end: date | None = None):
        if start and end and end < start:
            return dashboard(request, "运动活动", filters=True, start=start, end=end, message="结束日期早于开始日期。")
        if end == date.max:
            raise HTTPException(422, "结束日期不在支持的范围内。")
        if start is None and end is None:
            start = datetime.now(TZ).date() - timedelta(days=27)
        begin = datetime.combine(start, clock_time.min, TZ) if start else None
        finish = datetime.combine(end + timedelta(days=1), clock_time.min, TZ) if end else None
        return dashboard(request, "运动活动", filters=True, start=start, end=end,
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
        return dashboard(request, "保存路线", headings=("路线名称或描述", "距离（km）", "运动类型"),
                         rows=table_rows(records(store, "routes"), "routes"))

    @app.get("/sync-status")
    def sync_page(request: Request):
        return dashboard(request, "同步状态", sync_status=sync_status())

    @app.get("/settings")
    def settings(request: Request, saved: bool = False):
        return dashboard(request, "设置", settings_minutes=config.load(), saved=saved)

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
