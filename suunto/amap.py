"""Keep the AMap security code server-side and proxy only the map's required services."""

import json
import re
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
import httpx


SERVICES = {
    "v3/assistant/coordinate/convert": "https://restapi.amap.com/v3/assistant/coordinate/convert",
    "v4/map/styles": "https://webapi.amap.com/v4/map/styles",
}
CREDENTIAL = re.compile(r"[a-fA-F0-9]{32}")
CALLBACK = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
PRIVATE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def amap_ready(settings):
    return bool(settings.get("enabled") and settings.get("key") and settings.get("secret")
                and settings.get("coordinates") in ("gps", "gaode"))


def amap_router(config):
    router = APIRouter()

    @router.post("/settings/amap")
    def save_amap(enabled: Annotated[bool, Form()] = False,
                  key: Annotated[str, Form()] = "",
                  secret: Annotated[str, Form()] = "",
                  coordinates: Annotated[str, Form()] = "",
                  clear: Annotated[bool, Form()] = False):
        if clear:
            config.update({"enabled": False, "key": "", "secret": "", "coordinates": ""})
        else:
            old = config.read()
            key = key.strip() or old.get("key", "")
            secret = secret.strip() or old.get("secret", "")
            if (key and not CREDENTIAL.fullmatch(key)) or (secret and not CREDENTIAL.fullmatch(secret)):
                raise HTTPException(422, "请输入高德 Web 端 32 位 Key 和安全密钥。")
            if coordinates not in ("", "gps", "gaode"):
                raise HTTPException(422, "请选择路线坐标系。")
            values = dict(enabled=enabled, key=key, secret=secret, coordinates=coordinates)
            if enabled and not amap_ready(values):
                raise HTTPException(422, "启用预览需要高德 Key、安全密钥和路线坐标系。")
            config.update(values)
        return RedirectResponse("/settings?amap_saved=true#amap-settings", status_code=303, headers=PRIVATE)

    @router.get("/_AMapService/{service:path}")
    def proxy(request: Request, service: str):
        endpoint = SERVICES.get(service)
        settings = config.read()
        if not endpoint or not amap_ready(settings):
            raise HTTPException(404)
        if request.headers.get("sec-fetch-site") not in (None, "same-origin"):
            raise HTTPException(403)
        if len(request.url.query) > 4096:
            raise HTTPException(400)

        callback = request.query_params.get("callback", "")
        if callback and (len(callback) > 128 or not CALLBACK.fullmatch(callback)):
            raise HTTPException(400)
        params = [(name, value) for name, value in request.query_params.multi_items()
                  if name.lower() not in {"key", "jscode", "sig", "callback"}]
        if callback:
            params.append(("callback", callback))
        params.extend((("key", settings["key"]), ("jscode", settings["secret"])))
        # Forward only the page's origin for AMap domain validation.
        address = urlsplit(str(request.base_url))
        headers = {"Referer": f"{address.scheme}://{address.netloc}/"}
        try:
            reply = httpx.get(endpoint, params=params, headers=headers, timeout=5, follow_redirects=False)
        except httpx.HTTPError:
            raise HTTPException(502, "高德地图请求失败。") from None
        if reply.status_code == 429:
            raise HTTPException(429, "高德地图请求受限，请稍后重试。")
        if reply.status_code != 200 or settings["secret"].encode() in reply.content:
            raise HTTPException(502, "高德地图请求失败。")
        try:
            payload = reply.text.removeprefix("/**/").strip().removesuffix(";")
            if callback and payload.startswith(callback + "(") and payload.endswith(")"):
                payload = payload[len(callback) + 1:-1]
            data = json.loads(payload)
            if callback:
                return Response(f"{callback}({json.dumps(data, allow_nan=False)});", media_type="application/javascript", headers=PRIVATE)
            return JSONResponse(data, headers=PRIVATE)
        except ValueError:
            raise HTTPException(502, "高德地图响应格式异常。") from None

    return router
