"""OAuth PKCE and a read-only Suunto MCP Streamable HTTP client."""

import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import secrets
from tempfile import NamedTemporaryFile
from threading import RLock
import time
from urllib.parse import urlencode, urlsplit

import httpx


MCP_URL = "https://mcp.suunto.cn/open/mcp"
ISSUER = "https://mcp.suunto.cn"
REDIRECT = "http://127.0.0.1:8000/oauth/callback"
SCOPE = "mcp:tools"
READ_TOOLS = frozenset({
    "list_user_workouts", "get_workout_detail", "get_sleep_summary", "get_recovery_summary",
    "get_daily_activity_summary", "list_user_routes", "get_route_summary",
})
LOG = logging.getLogger(__name__)


class MCPError(RuntimeError):
    pass


class NeedsAuthorization(MCPError):
    pass


class ReadTimeout(MCPError):
    pass


class Reconnect(MCPError):
    def __init__(self, status):
        self.status = status


def retry_delay(value, attempt):
    delay = 2 ** attempt
    if value:
        try:
            return max(delay, int(value))
        except ValueError:
            try:
                return max(delay, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return delay


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with NamedTemporaryFile(dir=path.parent, delete=False) as output:
        staged = Path(output.name)
    try:
        with staged.open("w", encoding="utf-8") as output:
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


class Client:
    def __init__(self, data_dir, http=None):
        self.path = Path(data_dir) / "token.json"
        self.http = http or httpx.Client(timeout=15, follow_redirects=False)
        self.lock = RLock()
        self.pending = None
        self.headers = None
        self.cooldown = 0.0

    def credential(self):
        try:
            value = json.loads(self.path.read_bytes())
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as exc:
            raise MCPError("无法读取 token.json。") from exc

    def send(self, method, url, *, retry=True, **kwargs):
        for attempt in range(3):
            if time.monotonic() < self.cooldown:
                raise MCPError("Suunto HTTP 429，仍处于 Retry-After 等待期。")
            try:
                response = self.http.send(self.http.build_request(method, url, **kwargs), stream=True)
            except httpx.RequestError as exc:
                if not retry or attempt == 2:
                    raise MCPError("连接 Suunto 超时或网络不可用。") from exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 429:
                delay = retry_delay(response.headers.get("Retry-After"), attempt)
                response.close()
                LOG.warning("Suunto HTTP 429, Retry-After %.0f seconds", delay)
                if delay > 15 or attempt == 2:
                    self.cooldown = time.monotonic() + delay
                    raise MCPError(f"Suunto HTTP 429，请等待 {delay:.0f} 秒。")
                time.sleep(delay)
                continue
            if retry and response.status_code in (502, 503, 504) and attempt < 2:
                response.close()
                time.sleep(2 ** attempt)
                continue
            return response
        raise MCPError("请求未完成。")

    def oauth_json(self, method, url, **options):
        response = self.send(method, url, retry=method == "GET", **options)
        try:
            response.read()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            if data.get("error") in ("invalid_grant", "invalid_client"):
                raise NeedsAuthorization("授权码或刷新凭据无效，请重新授权。")
            if not response.is_success:
                raise MCPError(f"OAuth HTTP {response.status_code}。")
            return data
        except (ValueError, httpx.RequestError) as exc:
            raise MCPError(f"OAuth HTTP {response.status_code} 响应无效或读取超时。") from exc
        finally:
            response.close()

    def metadata(self):
        data = self.oauth_json("GET", ISSUER + "/.well-known/oauth-authorization-server")
        if data.get("issuer") != ISSUER or "S256" not in data.get("code_challenge_methods_supported", []):
            raise MCPError("OAuth issuer 或 PKCE 元数据不匹配。")
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            endpoint = urlsplit(data.get(key, ""))
            if f"{endpoint.scheme}://{endpoint.netloc}" != ISSUER:
                raise MCPError(f"OAuth 端点无效（{key}）。")
        return data

    def begin(self):
        with self.lock:
            metadata = self.metadata()
            credential = self.credential()
            client_id = credential.get("client_id")
            if not client_id:
                data = self.oauth_json("POST", metadata["registration_endpoint"], json={
                    "client_name": "Suunto LAN Dashboard", "application_type": "native",
                    "redirect_uris": [REDIRECT], "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"], "token_endpoint_auth_method": "none", "scope": SCOPE,
                })
                client_id = data.get("client_id")
                if not isinstance(client_id, str) or not client_id:
                    raise MCPError("动态注册未返回有效的 client_id。")
                save_json(self.path, {"client_id": client_id})
            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(48)
            digest = hashlib.sha256(verifier.encode()).digest()
            challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
            self.pending = (state, verifier, time.monotonic() + 300)
            url = metadata["authorization_endpoint"] + "?" + urlencode({
                "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "resource": MCP_URL,
                "scope": SCOPE, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            })
            return url, state

    def complete(self, code, browser_state, returned_state=None, issuer=None):
        with self.lock:
            if not self.pending or not code:
                raise NeedsAuthorization("请先创建授权链接。")
            expected, verifier, deadline = self.pending
            if (time.monotonic() > deadline or
                    not secrets.compare_digest(expected.encode(), browser_state.encode()) or
                    (returned_state is not None and not secrets.compare_digest(expected.encode(), returned_state.encode())) or
                    issuer not in (None, ISSUER)):
                raise NeedsAuthorization("授权会话已过期或者 state/issuer 无效。")
            self.pending = None
            return self.exchange(self.credential(), grant_type="authorization_code",
                                 code=code, code_verifier=verifier, redirect_uri=REDIRECT)

    def exchange(self, credential, **grant):
        started = time.time()
        result = self.oauth_json("POST", self.metadata()["token_endpoint"], data={
            "client_id": credential["client_id"], "resource": MCP_URL, **grant,
        })
        expiry = result.get("expires_in")
        access = result.get("access_token")
        if not access or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= 0:
            raise MCPError("Suunto OAuth 响应缺少 access_token 或有效期。")
        refresh = result.get("refresh_token")
        if not refresh and grant["grant_type"] == "refresh_token":
            refresh = credential.get("refresh_token")
        new = {**credential, "access_token": access, "refresh_token": refresh,
               "refresh_at": started + expiry - min(60, expiry / 2)}
        save_json(self.path, new)
        self.headers = None
        return new

    def refresh(self, credential):
        refresh_token = credential.get("refresh_token")
        if not refresh_token:
            raise NeedsAuthorization("Suunto 未提供刷新 token，请重新授权。")
        return self.exchange(credential, grant_type="refresh_token", refresh_token=refresh_token)

    def rpc(self, headers, method, params=None, *, notification=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = secrets.token_hex(16)
        response = self.send("POST", MCP_URL, headers=headers, json=payload)
        try:
            if response.status_code in (401, 404):
                raise Reconnect(response.status_code)
            if not response.is_success:
                raise MCPError(f"MCP {method} HTTP {response.status_code}。")
            if notification:
                return {}, response.headers
            if response.headers.get("content-type", "").startswith("text/event-stream"):
                lines = []
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        lines.append(line[5:].lstrip(" "))
                    elif not line and lines:
                        message = json.loads("\n".join(lines))
                        lines.clear()
                        if isinstance(message, dict) and message.get("id") == payload["id"]:
                            break
                else:
                    raise MCPError(f"MCP {method} 事件流未返回匹配的响应。")
            else:
                response.read()
                message = response.json()
            if not isinstance(message, dict) or message.get("id") != payload["id"]:
                raise MCPError(f"MCP {method} 响应 ID 无效。")
            result = message.get("result")
            if not isinstance(result, dict) or result.get("isError"):
                raise MCPError(f"MCP {method} 调用失败。")
            return result, response.headers
        except httpx.RequestError as exc:
            raise ReadTimeout(f"MCP {method} 读取超时或连接中断。") from exc
        except ValueError as exc:
            raise MCPError(f"MCP {method} 响应无效。") from exc
        finally:
            response.close()

    def connect(self, token):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
        result, response_headers = self.rpc(headers, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "suunto-lan-dashboard", "version": "0.1.0"},
        })
        version = result.get("protocolVersion")
        if version not in ("2025-03-26", "2025-06-18", "2025-11-25"):
            raise MCPError("MCP 协议版本不受支持。")
        headers["MCP-Protocol-Version"] = version
        if response_headers.get("Mcp-Session-Id"):
            headers["Mcp-Session-Id"] = response_headers["Mcp-Session-Id"]
        self.rpc(headers, "notifications/initialized", notification=True)
        self.headers = headers

    def call(self, method, params=None):
        with self.lock:
            credential = self.credential()
            if not credential.get("access_token"):
                raise NeedsAuthorization("等待颂拓账号授权。")
            refreshed = time.time() >= credential.get("refresh_at", 0)
            if refreshed:
                credential = self.refresh(credential)
            reconnected = False
            retried_read = False
            while True:
                try:
                    if self.headers is None:
                        self.connect(credential["access_token"])
                    return self.rpc(self.headers, method, params)[0]
                except Reconnect as exc:
                    self.headers = None
                    if exc.status == 401 and not refreshed:
                        credential = self.refresh(credential)
                        refreshed = True
                    elif exc.status == 404 and not reconnected:
                        reconnected = True
                    elif exc.status == 401:
                        raise NeedsAuthorization("Suunto HTTP 401，请重新授权。") from exc
                    else:
                        raise MCPError("Suunto MCP 会话无法恢复。") from exc
                except ReadTimeout:
                    if retried_read:
                        raise
                    retried_read = True
                    self.headers = None
                    time.sleep(1)

    def tools(self):
        result, seen, params = [], set(), {}
        while True:
            page = self.call("tools/list", params)
            tools = page.get("tools")
            if not isinstance(tools, list) or any(
                    not isinstance(tool, dict) or not isinstance(tool.get("name"), str) for tool in tools):
                raise MCPError("tools/list 未返回有效工具列表。")
            result.extend(tools)
            cursor = page.get("nextCursor")
            if not cursor:
                return result
            if not isinstance(cursor, str) or cursor in seen:
                raise MCPError("tools/list 分页游标无效。")
            seen.add(cursor)
            params = {"cursor": cursor}

    def sample(self, name, arguments, discovered):
        if name not in READ_TOOLS or name not in discovered:
            raise MCPError("该工具不在本项目已探测的只读范围内。")
        return self.call("tools/call", {"name": name, "arguments": arguments})
