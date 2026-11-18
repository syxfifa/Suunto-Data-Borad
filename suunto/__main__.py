"""Probe through the running app so one process owns OAuth token refreshes."""

import argparse
import json
import sys

import httpx


def main():
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("probe", help="Run tools/list against Suunto")
    actions.add_parser("sync", help="Run an incremental synchronization")
    sample = actions.add_parser("sample", help="Report actual returned fields from a read-only tool")
    sample.add_argument("tool")
    sample.add_argument("arguments", help="Tool arguments as a JSON object")
    args = parser.parse_args()
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
        response = httpx.post("http://127.0.0.1:8000/api/" + path, json=body, timeout=900, trust_env=False)
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Cannot reach the running app: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if response.is_success and result.get("status", "ready") in ("ready", "ok", "partial") else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main())
