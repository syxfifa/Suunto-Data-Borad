"""Limit and validate HTTP requests before routing or parsing a form."""

import asyncio
from ipaddress import IPv6Address
import re

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse


MAX_BODY = 64 * 1024
AUTHORITY = re.compile(r"(\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::([0-9]{1,5}))?")
MEDIA_TYPES = {"application/x-www-form-urlencoded", "application/json"}


def valid_authority(value):
    match = AUTHORITY.fullmatch(value) if len(value) <= 260 else None
    if not match:
        return False
    host, port = match.groups()
    if host.startswith("["):
        try:
            IPv6Address(host[1:-1])
        except ValueError:
            return False
    return port is None or int(port) <= 65535


class RequestSecurity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def secured_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
            await send(message)

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status, headers={"Cache-Control": "no-store"})(scope, receive, secured_send)

        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        if len(hosts) != 1 or not valid_authority(hosts[0]):
            return await reject(400, "请求的访问地址无效。")
        if scope["method"] not in ("POST", "PUT", "PATCH", "DELETE"):
            return await self.app(scope, receive, secured_send)

        content_type = headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type and content_type not in MEDIA_TYPES:
            return await reject(415, "此接口只接受网页表单或 JSON。")
        length = headers.get("content-length")
        if length is not None:
            if not re.fullmatch(r"[0-9]+", length):
                return await reject(400, "请求长度无效。")
            if len(length) > 5 or int(length) > MAX_BODY:
                return await reject(413, "请求内容过大。")

        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > MAX_BODY:
                        return await reject(413, "请求内容过大。")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "读取请求超时。")

        async def replay():
            nonlocal body
            if body is not None:
                buffered = bytes(body)
                body = None
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        await self.app(scope, replay, secured_send)
