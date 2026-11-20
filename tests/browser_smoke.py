"""Browser regression run with synthetic Suunto responses and a loopback proxy."""

import argparse
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from html import escape
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import ssl
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import expect, sync_playwright
import uvicorn

from suunto.store import Store
from suunto.sync import TZ
from suunto.web import create_app
from test_probe import ProbeTest


@contextmanager
def dashboard(client):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(client=client), log_level="warning",
                                               forwarded_allow_ips="127.0.0.1"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.02)
            if not server.started:
                raise RuntimeError("Fixture app did not start")
            yield port
        finally:
            server.should_exit = True
            thread.join(10)


@contextmanager
def https_proxy(port, openssl):
    class Handler(BaseHTTPRequestHandler):
        def forward(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            hop_headers = {"connection", "transfer-encoding", "keep-alive"}
            headers = {key: value for key, value in self.headers.items() if key.lower() not in hop_headers}
            headers["X-Forwarded-Proto"] = "https"
            upstream = HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                upstream.request(self.command, self.path, body, headers)
                reply = upstream.getresponse()
                payload = reply.read()
                self.send_response(reply.status)
                for name, value in reply.getheaders():
                    if name.lower() not in hop_headers | {"content-length"}:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            finally:
                upstream.close()

        do_GET = forward
        do_POST = forward

        def log_message(self, *args):
            pass

    with TemporaryDirectory() as directory, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as proxy:
        key, cert = (Path(directory) / name for name in ("key.pem", "cert.pem"))
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=dashboard-b.test", "-keyout", str(key), "-out", str(cert)],
                       capture_output=True, check=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        proxy.socket = tls.wrap_socket(proxy.socket, server_side=True)
        worker = Thread(target=proxy.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"https://dashboard-b.test:{proxy.server_port}"
        finally:
            proxy.shutdown()
            worker.join(5)


def exercise(browser, origin, fixture, width):
    fixture.redirect = origin + "/oauth/callback"
    with browser.new_context(ignore_https_errors=True, viewport={"width": width, "height": 950}) as context:
        page = context.new_page()
        page.set_default_timeout(9000)
        navigations = []
        page.on("response", lambda response: navigations.append(
            (response.status, urlsplit(response.url).hostname, urlsplit(response.url).path)
        ) if response.request.is_navigation_request() else None)

        def begin(route):
            address = urlsplit(origin)
            endpoint = f"{address.scheme}://127.0.0.1:{address.port}/oauth/start"
            # Chromium resolves the test domains through host rules. The test
            # runner's HTTP client goes directly to loopback with the same Host.
            response = route.fetch(url=endpoint, headers={**route.request.all_headers(), "host": address.netloc},
                                   max_redirects=0)
            assert response.status == 303
            upstream = urlsplit(response.headers["location"])
            assert (upstream.scheme, upstream.netloc, upstream.path) == ("https", "mcp.suunto.cn", "/authorize")
            # Intercept the first navigation and expose the provider as a new link.
            # Browser route handlers can be skipped for a redirect target.
            target = "https://provider.test/authorize?" + upstream.query
            headers = {name: value for name, value in response.headers.items()
                       if name not in {"location", "content-length", "content-encoding"}}
            route.fulfill(status=200, headers=headers, content_type="text/html; charset=utf-8",
                          body=f'<a id="mock-provider" href="{escape(target)}">Open test provider</a>')

        def authorize(route):
            args = parse_qs(urlsplit(route.request.url).query)
            assert args["redirect_uri"] == [fixture.redirect]
            assert args["code_challenge_method"] == ["S256"]
            target = fixture.redirect + "?" + urlencode({"code": "one-time-code", "state": args["state"][0]})
            route.fulfill(content_type="text/html; charset=utf-8",
                          body=f'<a id="mock-authorize" href="{escape(target)}">Confirm test authorization</a>')

        context.route(origin + "/oauth/start", begin)
        context.route("https://provider.test/authorize**", authorize)
        page.goto(origin + "/settings")
        try:
            page.get_by_role("button", name="连接 Suunto").click()
            page.locator("#mock-provider").click()
            page.locator("#mock-authorize").click()
            expect(page.get_by_text("Suunto 授权成功", exact=False)).to_be_visible()
        except Exception as exc:
            raise AssertionError(f"OAuth browser fixture failed on {origin}: {navigations}") from exc
        assert fixture.grants == ["authorization_code"]
        assert page.url.startswith(origin + "/settings?authorized=true")

        store = Store(fixture.mcp.path.parent)
        now = datetime.now(TZ)
        when = now - timedelta(days=1)
        run = store.start("browser_fixture", now.isoformat())
        store.commit(run, "workouts", [("sample-activity", when.isoformat(), {
            "workoutId": "sample-activity", "activityName": "Running", "workoutName": "Browser fixture",
            "totalDistance": 5000, "totalTimeSeconds": 1800, "tss": 28,
        })])
        # Exercise the interactive links as well as the manually submitted date filter.
        page.goto(origin + "/training?sport=Running")
        page.locator(".chart-values summary").first.press("Enter")
        day = when.date().isoformat()
        page.locator(".chart-values[open]").get_by_role("link", name=day, exact=True).click()
        expect(page.get_by_text("共 1 条活动", exact=True)).to_be_visible()
        page.locator("input[name=start]").fill(day)
        page.locator("input[name=end]").fill(day)
        page.get_by_role("button", name="筛选", exact=True).click()
        expect(page.get_by_text("共 1 条活动", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="返回复盘图表")).to_be_visible()
        if not page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"):
            raise AssertionError(f"Dashboard exceeds {width}px page width")
        exercise_amap(context, page, origin, store, when.isoformat(), now.isoformat(), width)


def exercise_amap(context, page, origin, store, older, newer, width):
    run = store.start("routes_fixture", newer)
    store.commit(run, "routes", [
        ("no-point", newer, {"routeId": "no-point", "name": "无坐标"}),
        ("first", newer, {"routeId": "first", "name": "公园环线", "description": "公园环线",
         "startPoint": {"latitude": 30.5, "longitude": 120.5},
         "endPoint": {"latitude": 30.6, "longitude": 120.6}}),
        ("second", older, {"routeId": "second", "name": "海边步道", "description": "海边步道",
         "centerPoint": {"latitude": 31.5, "longitude": 121.5}}),
    ])
    # Install a deterministic SDK stand-in without sending a real map request.
    requests = []
    offline = False
    map_failure = ""

    def mock_sdk(route):
        requests.append(route.request.url)
        if offline:
            route.abort()
            return
        callback = parse_qs(urlsplit(route.request.url).query)["callback"][0]
        route.fulfill(content_type="application/javascript", body=f"const fixtureMapFailure = {json.dumps(map_failure)};" + """
          window.mapCalls = {instances: 0, conversions: 0};
          window.AMap = {
            Map: class {
              constructor(container) {
                if (fixtureMapFailure === 'tiles') throw new Error('tiles failed');
                this.container = container; window.mapCalls.instances++;
              }
              on(event, callback) { setTimeout(callback, 20); }
              add(markers) { this.container.dataset.markers = markers.map(m => m.title).join(','); }
              remove() {} setFitView() {} setZoomAndCenter() {} destroy() {}
              zoomIn() { this.container.dataset.zoom = 'in'; }
            },
            Marker: class { constructor({title}) { this.title = title; } },
            convertFrom(points, type, complete) {
              if (type !== 'gps') throw new Error('unexpected coordinate system');
              window.mapCalls.conversions++;
              if (fixtureMapFailure === 'domain') return complete('error', {infocode: '10006'});
              if (fixtureMapFailure === 'conversion') return complete('error', {info: 'error'});
              complete('complete', {locations: points});
            },
          };
        """ + f"window.{callback}();")

    context.route("https://webapi.amap.com/maps?*", mock_sdk)
    page.goto(origin + "/routes")
    expect(page.locator("#route-map")).to_be_hidden()
    assert not requests
    page.goto(origin + "/settings")
    settings = page.locator("#amap-settings")
    settings.get_by_label("自动显示高德地图").check()
    settings.get_by_label("高德 Web 端 Key").fill("a" * 32)
    settings.get_by_label("安全密钥").fill("b" * 32)
    settings.get_by_label("Suunto 路线坐标系").select_option("gps")
    settings.get_by_role("button", name="保存地图设置").click()
    expect(settings.get_by_text("高德地图设置已保存。")).to_be_visible()
    page.goto(origin + "/routes?q=missing")
    expect(page.locator("#amap-preview")).to_have_count(0)
    assert not requests
    page.goto(origin + "/routes")
    preview = page.locator("#amap-preview")
    expect(preview).to_be_visible()
    first = page.locator('.route-select[aria-pressed="true"]').inner_text()
    other = page.locator('.route-select[aria-pressed="false"]')
    assert first == "公园环线"
    expect(preview).to_have_attribute("data-markers", "起点,终点")
    page.get_by_role("button", name="放大地图").click()
    expect(preview).to_have_attribute("data-zoom", "in")
    other.click()
    expect(preview).to_be_visible()
    page.get_by_role("button", name=first).click()
    expect(preview).to_be_visible()
    assert page.evaluate("window.mapCalls") == {"instances": 1, "conversions": 2}
    assert len(requests) == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"Map overflows at {width}px"

    offline = True
    page.reload()
    expect(page.locator("#amap-status")).to_contain_text("仍可查看和复制坐标")
    expect(page.locator("#route-coordinates")).not_to_have_value("")
    offline = False
    page.get_by_role("button", name="重试加载高德地图").click()
    expect(preview).to_be_visible()

    for map_failure, message in (("", "坐标转换和底图均已加载"), ("domain", "高德拒绝了当前域名"),
                                 ("conversion", "坐标转换失败"), ("tiles", "底图加载失败")):
        page.goto(origin + "/settings#amap-settings")
        page.get_by_role("button", name="测试连接").click()
        expect(page.locator("#amap-check-status")).to_contain_text(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-executable")
    parser.add_argument("--openssl", default="openssl")
    options = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=options.browser_executable, args=[
            "--no-proxy-server", "--host-resolver-rules=MAP dashboard-a.test 127.0.0.1, MAP dashboard-b.test 127.0.0.1",
            "--disable-background-networking",
        ])
        try:
            for host, width in (("127.0.0.1", 390), ("dashboard-a.test", 834), ("dashboard-b.test", 1440)):
                fixture = ProbeTest()
                fixture.setUp()
                try:
                    with dashboard(fixture.mcp) as port:
                        if host == "dashboard-b.test":
                            with https_proxy(port, options.openssl) as origin:
                                exercise(browser, origin, fixture, width)
                        else:
                            exercise(browser, f"http://{host}:{port}", fixture, width)
                    print(f"{host} · {width}px · OAuth, date filters and AMap preview passed")
                finally:
                    fixture.doCleanups()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
