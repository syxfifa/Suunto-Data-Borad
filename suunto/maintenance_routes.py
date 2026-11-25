"""Dashboard export, maintenance and access settings."""

import asyncio
from contextlib import closing
from datetime import date, timedelta
from itertools import islice
from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from . import VERSION
from .maintenance import backup_status, storage_size
from .reports import midnight
from .sync import SOURCES
from .views import csv_cells, lower_bound
from .web_shared import HOME_HEADERS, NO_CACHE, SOURCE_LABELS, TEMPLATES, local_transport


def csv_chunks(rows):
    # Send several CSV records per worker-thread handoff, with bounded buffering.
    with closing(rows):
        while chunk := "".join(islice(rows, 128)):
            yield chunk


def maintenance_router(store, config, access, mcp_state, prune_logs):
    router = APIRouter()
    data_dir = store.path.parent

    @router.get("/export.csv")
    def export_csv(source: str = "", start: str = "", end: str = "", format: str = "raw", share: bool = False,
                   sport: str = "", q: str = "", km: str = "", minutes: str = "", tss: str = "",
                   purpose: Literal["share", "archive", "columns"] | None = None):
        if purpose is not None:
            share = purpose == "share"
            format = "raw" if purpose == "archive" else "columns"
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
                         hrv_field=config.read().get("wellness_fields", {}).get("hrv"),
                         minimums={"distance": distance * 1000 if distance is not None else None,
                                   "duration": duration * 60 if duration is not None else None, "tss": stress})
        name = "suunto-share.csv" if share else "suunto.csv"
        return StreamingResponse(csv_chunks(rows), media_type="text/csv",
                                 headers={**NO_CACHE, "Content-Disposition": f'attachment; filename="{name}"'})

    @router.get("/export")
    def export_options(request: Request):
        return TEMPLATES.TemplateResponse(request=request, name="maintenance.html",
            context={"title": "导出数据", "sources": {name: SOURCE_LABELS[name] for name in SOURCES}},
            headers=HOME_HEADERS)

    @router.get("/maintenance")
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
                     "last_sync": status["last_success_at"],
                     "backup_verified": backup_status(data_dir), "restore_drill": backup_status(data_dir, drill=True),
                     "protected": access.enabled(), "secure": local_transport(request)},
            headers=HOME_HEADERS)

    @router.get("/diagnostics.json")
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

    @router.post("/maintenance/retention")
    async def save_retention(days: Annotated[int, Form(ge=7, le=365)]):
        await asyncio.to_thread(config.update, {"log_retention_days": days})
        # Honor a shorter window immediately without removing any active run.
        await asyncio.to_thread(prune_logs)
        return RedirectResponse("/maintenance?notice=retention", status_code=303, headers=NO_CACHE)

    @router.post("/maintenance/access")
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

    return router
