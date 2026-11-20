"""Probe through the running app so one process owns OAuth token refreshes."""

import argparse
from contextlib import contextmanager
from getpass import getpass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryFile
from zipfile import BadZipFile

import httpx

from .access import USERNAME
from .maintenance import COMPONENTS, backup, restore, verify, volume_lock


@contextmanager
def archive_input(path):
    if path == "-":
        with TemporaryFile() as handle:
            shutil.copyfileobj(sys.stdin.buffer, handle)
            handle.seek(0)
            yield handle
    else:
        yield path


def maintain(args):
    try:
        if args.action == "backup":
            if args.archive == "-":
                # Pipes can report unreliable offsets on Windows. Write and
                # finalize the ZIP in a seekable file before sending it.
                with TemporaryFile() as output:
                    backup(args.data_dir, output, include_token=args.include_token)
                    output.seek(0)
                    shutil.copyfileobj(output, sys.stdout.buffer)
            else:
                target = Path(args.archive)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                try:
                    with os.fdopen(fd, "wb") as output:
                        backup(args.data_dir, output, include_token=args.include_token)
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
        elif args.action == "access-reset":
            from .access import Access
            with volume_lock(args.data_dir):
                Access(args.data_dir).clear()
            print("Local dashboard password cleared.")
        else:
            with archive_input(args.archive) as archive:
                if args.action == "verify":
                    print(json.dumps({"status": "verified", "components": verify(archive)}))
                else:
                    restore(args.data_dir, archive, args.component, replace=args.replace, same_account=args.same_account)
                    print(f"Restored {args.component}.")
        return 0
    except (OSError, ValueError, sqlite3.Error, BadZipFile) as exc:
        print(f"Maintenance failed: {exc}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/data", help="Volume directory for maintenance")
    parser.add_argument("--authenticate", action="store_true", help="Prompt for the dashboard password")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("probe", help="Run tools/list against Suunto")
    actions.add_parser("sync", help="Run an incremental synchronization")
    sample = actions.add_parser("sample", help="Report actual returned fields from a read-only tool")
    sample.add_argument("tool")
    sample.add_argument("arguments", help="Tool arguments as a JSON object")
    archive = actions.add_parser("backup", help="Create a consistent backup (default: binary stdout)")
    archive.add_argument("archive", nargs="?", default="-")
    archive.add_argument("--include-token", action="store_true")
    check = actions.add_parser("verify", help="Verify an archive from a file or stdin (-)")
    check.add_argument("archive", nargs="?", default="-")
    restore_cmd = actions.add_parser("restore", help="Restore one component while the app is stopped")
    restore_cmd.add_argument("archive", nargs="?", default="-")
    restore_cmd.add_argument("--component", choices=COMPONENTS, default="database")
    restore_cmd.add_argument("--replace", action="store_true")
    restore_cmd.add_argument("--same-account", action="store_true")
    actions.add_parser("access-reset", help="Clear the dashboard password while the app is stopped")
    args = parser.parse_args()
    if args.action in ("backup", "verify", "restore", "access-reset"):
        return maintain(args)
    body = None
    path = args.action
    if args.action == "sample":
        try:
            arguments = json.loads(args.arguments)
            if not isinstance(arguments, dict):
                raise ValueError()
        except ValueError:
            parser.error("arguments must be a JSON object")
        body = {"tool": args.tool, "arguments": arguments}
        path = "probe/sample"
    try:
        credentials = (USERNAME, getpass("Dashboard password: ")) if args.authenticate else None
        response = httpx.post("http://127.0.0.1:8000/api/" + path, json=body, timeout=900, trust_env=False,
                              auth=credentials)
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Cannot reach the running app: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if response.is_success and result.get("status", "ready") in ("ready", "ok", "partial") else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main())
