"""Dashboard export, maintenance and access settings."""

import asyncio
from contextlib import closing
from datetime import date, datetime, timedelta
from itertools import islice
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from . import VERSION
from .maintenance import backup_status, storage_size
from .reports import midnight
from .sync import SOURCES, TZ
from .views import csv_cells, export_filter, lower_bound
from .web_shared import HOME_HEADERS, NO_CACHE, SOURCE_LABELS, TEMPLATES, local_transport


def csv_chunks(rows):
    # Send several CSV records per worker-thread handoff, with bounded buffering.
    with closing(rows):
        while chunk := "".join(islice(rows, 128)):
            yield chunk


def export_bounds(source, start, end, km, minutes, tss):
    if source and source not in SOURCES:
        raise ValueError("请选择有效的数据来源。")
    try:
        first = date.fromisoformat(start) if start else None
        last = date.fromisoformat(end) if end else None
        if last == date.max or (first and last and first > last):
            raise ValueError
        distance, duration, stress = lower_bound(km, 100000), lower_bound(minutes, 600000), lower_bound(tss, 100000)
    except ValueError as exc:
        raise ValueError("请输入有效的筛选条件，日期使用 YYYY-MM-DD，结束日期须等于或晚于开始日期。") from exc
    return (midnight(first) if first else None, midnight(last + timedelta(days=1)) if last else None,
            {"distance": distance * 1000 if distance is not None else None,
             "duration": duration * 60 if duration is not None else None, "tss": stress})


def backup_overview(data_dir, last_sync):
    verified = backup_status(data_dir)
    drill = backup_status(data_dir, drill=True)
    age = None
    newer_sync = False
    created_at = verified["created_at"] if verified else None
    if created_at:
        created = datetime.fromisoformat(created_at)
        elapsed = datetime.now(TZ) - created
        if elapsed < timedelta():
            age = "归档生成时间晚于当前系统时间，请检查 NAS 时钟。"
        else:
            hours, minutes = divmod(int(elapsed.total_seconds() // 60), 60)
            days, hours = divmod(hours, 24)
            duration = f"{days} 天 {hours} 小时" if days else f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"
            age = f"最近验证的归档生成于 {duration}前。" if elapsed >= timedelta(minutes=1) else "最近验证的归档生成于刚刚。"
        if last_sync:
            synced = datetime.fromisoformat(last_sync)
            newer_sync = bool(synced.tzinfo and synced > created)
    if not verified or not drill:
        archive_match = "记录齐全后可核对验证和演练使用的归档。"
    elif not verified["archive_sha256"] or not drill["archive_sha256"]:
        archive_match = "旧结果缺少归档指纹。可用同一份 ZIP 重新记录验证和演练。"
    elif verified["archive_sha256"] == drill["archive_sha256"]:
        archive_match = "最近的验证和恢复演练使用了同一份 ZIP。"
    else:
        archive_match = "最近的验证和恢复演练使用了不同的 ZIP。可用最近验证的归档再做一次演练。"
    return {"verified": verified, "drill": drill, "age": age, "newer_sync": newer_sync, "archive_match": archive_match}


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
        if format not in ("raw", "columns"):
            raise HTTPException(422, "导出格式无效。")
        try:
            begin, finish, minimums = export_bounds(source, start, end, km, minutes, tss)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        rows = csv_cells(store, source, begin, finish, format=format, share=share, sport=sport, search=q.strip(),
                         hrv_field=config.read().get("wellness_fields", {}).get("hrv"),
                         minimums=minimums)
        name = "suunto-share.csv" if share else "suunto.csv"
        return StreamingResponse(csv_chunks(rows), media_type="text/csv",
                                 headers={**NO_CACHE, "Content-Disposition": f'attachment; filename="{name}"'})

    @router.get("/export")
    def export_options(request: Request, source: str = "", start: str = "", end: str = "",
                       sport: str = "", q: str = "", km: str = "", minutes: str = "", tss: str = ""):
        filters = {"source": source, "start": start.strip(), "end": end.strip()}
        criteria = {key: value for key, value in (("sport", sport), ("q", q.strip()), ("km", km),
                    ("minutes", minutes), ("tss", tss)) if value.strip()} if source == "workouts" else {}
        count, message = None, None
        try:
            begin, finish, minimums = export_bounds(source, filters["start"], filters["end"], km, minutes, tss)
            filters["start"] = begin.date().isoformat() if begin else ""
            filters["end"] = (finish.date() - timedelta(days=1)).isoformat() if finish else ""
            clause, parameters = export_filter(source, begin, finish, sport=sport, search=q.strip(), minimums=minimums)
            count = store.query(f"SELECT count(*) AS n FROM records WHERE {clause}", parameters)[0]["n"]
        except ValueError as exc:
            message = str(exc)
        activities_link = None
        if source == "workouts" and count:
            activities_link = "/activities?" + urlencode({"start": filters["start"], "end": filters["end"], **criteria})
        return TEMPLATES.TemplateResponse(request=request, name="maintenance.html",
            context={"title": "导出数据", "sources": {name: SOURCE_LABELS[name] for name in SOURCES},
                     "filters": filters, "criteria": criteria, "count": count, "message": message,
                     "activities_link": activities_link},
            headers=HOME_HEADERS, status_code=422 if message else 200)

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
                     "backup_overview": backup_overview(data_dir, status["last_success_at"]),
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
