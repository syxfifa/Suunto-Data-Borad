"""Consistent SQLite backups, explicit offline restores and local diagnostics."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import zipfile

from . import VERSION
from .mcp import save_json
from .store import SCHEMA_VERSION, Store
from .sync import TZ


COMPONENTS = {"database": "suunto.sqlite", "settings": "config.json", "token": "token.json"}
MANIFEST = "manifest.json"
BACKUP_FORMAT = 1
BACKUP_STATUS = "backup-status.json"
KNOWN_TABLES = {"records", "cursors", "sync_runs", "sync_errors", "coverage", "backfill",
                "sync_failures", "sync_windows", "sync_queue", "source_success",
                "workout_details", "removed_routes", "route_absences"}


@contextmanager
def volume_lock(directory):
    """Hold one exclusive lease through a server lifetime or offline restore."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".maintenance.lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("数据卷由另一个服务或恢复任务占用。请先停止 app。") from exc
        yield


def check_database(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if not 0 <= version <= SCHEMA_VERSION:
            raise ValueError(f"SQLite 结构版本 {version} 不受本版本支持。")
        objects = db.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        tables = {name for kind, name, _ in objects if kind == "table"}
        if ("records" not in tables or not tables <= KNOWN_TABLES
                or any(kind not in ("table", "index") or (sql and sql.startswith("CREATE VIRTUAL TABLE"))
                       for kind, _, sql in objects)):
            raise ValueError("备份数据库结构不属于 Suunto Data Borad。")
        columns = {column[1] for column in db.execute("PRAGMA table_info(records)")}
        if (columns != {"source", "record_key", "occurred_at", "payload"}
                or db.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                or db.execute("PRAGMA foreign_key_check").fetchone()):
            raise ValueError("备份数据库完整性检查失败。")
        return version


def copy_database(source, target):
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)) as src:
        with closing(sqlite3.connect(target, timeout=15)) as dst:
            src.backup(dst, pages=256, sleep=0.1)


def backup(directory, output, *, include_token=False):
    directory = Path(directory).resolve()
    with TemporaryDirectory() as temporary, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        snapshot = Path(temporary) / COMPONENTS["database"]
        copy_database(directory / COMPONENTS["database"], snapshot)
        schema_version = check_database(snapshot)
        files = {snapshot.name: file_metadata(snapshot)}
        archive.write(snapshot, COMPONENTS["database"])
        for name in ["settings"] + (["token"] if include_token else []):
            try:
                data = (directory / COMPONENTS[name]).read_bytes()
            except FileNotFoundError:
                continue
            validate_json(name, data)
            archive.writestr(COMPONENTS[name], data)
            files[COMPONENTS[name]] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        archive.writestr(MANIFEST, json.dumps({
            "format": BACKUP_FORMAT, "app_version": VERSION, "sqlite_user_version": schema_version,
            "created_at": datetime.now(timezone.utc).isoformat(), "files": files,
        }, sort_keys=True))


def file_metadata(path):
    with path.open("rb") as handle:
        return {"size": path.stat().st_size, "sha256": hashlib.file_digest(handle, "sha256").hexdigest()}


def validate_json(name, content):
    if len(content) > 1024 * 1024:
        raise ValueError("设置或令牌文件过大。")
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("设置或令牌格式不正确。")
    if name == "token" and not isinstance(data.get("access_token"), str):
        raise ValueError("备份内没有有效结构的 OAuth 令牌。")
    return data


def extract_checked(archive, temporary):
    names = archive.namelist()
    if (len(names) != len(set(names)) or not set(names) <= {*COMPONENTS.values(), MANIFEST}
            or COMPONENTS["database"] not in names):
        raise ValueError("备份内容不符合预期。")
    manifest = None
    if MANIFEST in names:
        if archive.getinfo(MANIFEST).file_size > 65536:
            raise ValueError("备份清单过大。")
        manifest = validate_json("manifest", archive.read(MANIFEST))
        if (type(manifest.get("format")) is not int or manifest["format"] != BACKUP_FORMAT
                or not isinstance(manifest.get("app_version"), str)
                or type(manifest.get("sqlite_user_version")) is not int
                or not isinstance(manifest.get("files"), dict)
                or set(manifest["files"]) != set(names) - {MANIFEST}):
            raise ValueError("备份清单格式或版本不受支持。")
        try:
            timestamp = datetime.fromisoformat(manifest.get("created_at"))
            if timestamp.utcoffset() != timezone.utc.utcoffset(None):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("备份清单缺少有效的 UTC 创建时间。") from exc
    for component, name in COMPONENTS.items():
        if name not in names:
            continue
        entry = archive.getinfo(name)
        if component == "database":
            if entry.file_size > 32 * 1024**3:
                raise ValueError("数据库备份超过 32 GiB。")
            with archive.open(name) as src, (temporary / name).open("wb") as dst:
                shutil.copyfileobj(src, dst)
            if manifest and manifest["files"][name] != file_metadata(temporary / name):
                raise ValueError(f"备份校验失败：{name} 与清单不一致。")
            schema_version = check_database(temporary / name)
            if manifest and schema_version != manifest["sqlite_user_version"]:
                raise ValueError("备份数据库版本与清单不一致。")
        else:
            if entry.file_size > 1024 * 1024:
                raise ValueError("设置或令牌文件过大。")
            data = archive.read(name)
            if manifest and manifest["files"][name] != {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}:
                raise ValueError(f"备份校验失败：{name} 与清单不一致。")
            validate_json(component, data)
    return [component for component, name in COMPONENTS.items() if name in names]


def verify(input_file, *, record_to=None):
    with zipfile.ZipFile(input_file) as archive, TemporaryDirectory() as temporary:
        components = extract_checked(archive, Path(temporary))
    if record_to is not None:
        save_json(Path(record_to) / BACKUP_STATUS, {"verified_at": datetime.now(TZ).isoformat()})
    return components


def last_verified_backup(directory):
    try:
        value = json.loads((Path(directory) / BACKUP_STATUS).read_bytes())["verified_at"]
        at = datetime.fromisoformat(value)
        return at.astimezone(TZ).isoformat() if at.tzinfo else None
    except (OSError, KeyError, TypeError, ValueError):
        return None


def restore(directory, input_file, component, *, replace=False, same_account=False):
    directory = Path(directory).resolve()
    filename = COMPONENTS[component]
    with volume_lock(directory), zipfile.ZipFile(input_file) as archive, TemporaryDirectory() as temporary:
        snapshot = Path(temporary)
        if component not in extract_checked(archive, snapshot):
            raise ValueError("备份内没有所选恢复项。")
        target = directory / filename
        if target.exists() and not replace:
            raise ValueError("恢复项已存在，请先确认已备份，并指定 --replace。")
        if (component == "token" or (component == "database" and (directory / "token.json").exists())) and not same_account:
            raise ValueError("此操作可能混入不同账号的记录。确认归档和数据卷属于同一 Suunto 账号后指定 --same-account；新账号请使用独立数据卷。")
        if component == "database":
            copy_database(snapshot / filename, target)
        else:
            data = validate_json(component, archive.read(filename))
            save_json(target, data)


def storage_size(directory):
    path = Path(directory) / COMPONENTS["database"]
    total = 0
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            total += candidate.stat().st_size
        except FileNotFoundError:
            pass
    return total
