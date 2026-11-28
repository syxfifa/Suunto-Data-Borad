"""Shared dashboard templates, response headers and transport policy."""

from ipaddress import ip_address
from pathlib import Path

from fastapi.templating import Jinja2Templates


TEMPLATES = Jinja2Templates(directory=Path(__file__).with_name("templates"))
NO_CACHE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# Browsers can send Origin: null on a form POST from a no-referrer page.
HOME_HEADERS = {**NO_CACHE, "Referrer-Policy": "same-origin"}
SOURCE_LABELS = dict(workouts="运动活动", sleep="睡眠", recovery="恢复", daily_activity="日常活动",
                     routes="保存路线", workout_details="运动详情")


def local_transport(request, *, allow_http=False):
    if request.url.scheme == "https":
        return True
    if allow_http and request.url.scheme == "http":
        return True
    try:
        return (ip_address(request.client.host).is_loopback
                and (request.url.hostname == "localhost" or ip_address(request.url.hostname).is_loopback))
    except (ValueError, AttributeError):
        return False
