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
        page.goto(origin + "/")
        try:
            page.get_by_role("button", name="连接 Suunto").click()
            page.locator("#mock-provider").click()
            page.locator("#mock-authorize").click()
            expect(page.get_by_text("Suunto 授权成功", exact=False)).to_be_visible()
            expect(page.get_by_role("button", name="立即同步")).to_be_visible()
            expect(page.get_by_role("button", name="连接 Suunto")).to_have_count(0)
            expect(page.locator("#sync-progress")).to_contain_text("新增或更新 1 条", timeout=15000)
        except Exception as exc:
            raise AssertionError(f"OAuth browser fixture failed on {origin}: {navigations}") from exc
        assert fixture.grants == ["authorization_code"]
        assert page.url.startswith(origin + "/settings?authorized=true")
        expect(page.get_by_role("link", name="查看已同步活动")).to_be_hidden()

        store = Store(fixture.mcp.path.parent)
        now = datetime.now(TZ)
        when = now - timedelta(days=1)
        run = store.start("browser_fixture", now.isoformat())
        store.commit(run, "workouts", [("sample-activity", when.isoformat(), {
            "workoutId": "sample-activity", "activityName": "Running", "workoutName": "Browser fixture",
            "totalDistance": 5000, "totalTimeSeconds": 1800, "tss": 28,
            "summary": {"ascent": 48},
        })])
        page.reload()
        expect(page.get_by_role("link", name="查看已同步活动")).to_be_visible()
        # Exercise the interactive links as well as the manually submitted date filter.
        page.goto(origin + "/training?sport=Running")
        page.get_by_role("navigation", name="快捷日期").get_by_role("link", name="近 7 天").click()
        expect(page.get_by_role("navigation", name="快捷日期").get_by_role("link", name="近 7 天")).to_have_attribute("aria-current", "page")
        assert parse_qs(urlsplit(page.url).query)["sport"] == ["Running"]
        chart_values = page.locator(".chart-values").first
        summary = chart_values.locator("summary")
        expect(summary).to_be_visible()
        summary.press("Enter")
        day = when.date().isoformat()
        chart_values.get_by_role("link", name=day, exact=True).click()
        expect(page.get_by_text("共 1 条活动", exact=True)).to_be_visible()
        page.locator("input[name=start]").fill(day)
        page.locator("input[data-date-field=end]").fill(day)
        expect(page.locator("input[name=end]")).to_have_value(day)
        page.get_by_role("button", name="应用筛选", exact=True).click()
        expect(page.get_by_text("共 1 条活动", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="返回复盘图表")).to_be_visible()
        if not page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"):
            raise AssertionError(f"Dashboard exceeds {width}px page width")
        compare_nav = page.get_by_role("navigation", name="主导航").locator('a[href="/compare"]')
        expect(page.get_by_role("link", name="打开运动对比")).to_be_visible()
        if width < 624:
            expect(compare_nav).to_be_hidden()
        else:
            expect(compare_nav).to_be_visible()
        filtered = urlsplit(page.url)
        page.get_by_role("link", name="查看详情").click()
        expect(page.get_by_text("48", exact=True)).to_be_hidden()
        page.get_by_text("完整运动指标").click()
        expect(page.get_by_text("48", exact=True)).to_be_visible()
        page.get_by_role("link", name="加入同类运动对比").click()
        page.get_by_role("button", name="应用选择").click()
        page.get_by_role("link", name="返回之前的活动页面").click()
        page.get_by_role("link", name="返回筛选后的活动列表").click()
        updated = urlsplit(page.url)
        expected_filter = parse_qs(filtered.query)
        assert all(parse_qs(updated.query).get(key) == value for key, value in expected_filter.items())
        assert parse_qs(updated.query)["page"] == ["1"] and updated.fragment == "activity-0"
        # Fill the first page without changing the filtered fixture above.
        compare_day = when - timedelta(days=2)
        rows = [(f"fixture-{i}", (compare_day + timedelta(minutes=i)).isoformat(), {
            "activityName": "Running", "workoutName": f"对比样本 {i}",
            "totalDistance": 7000, "totalTimeSeconds": 2100,
        }) for i in range(50)]
        covered_start = datetime.combine(when.date() - timedelta(days=1), datetime.min.time(), TZ)
        covered_end = datetime.combine(when.date(), datetime.min.time(), TZ)
        store.commit(run, "workouts", rows, interval=(covered_start.isoformat(), covered_end.isoformat()))
        page.goto(origin + "/activities?start=&end=&sport=Running")
        page.locator("[data-compare-id=sample-activity]").check()
        page.get_by_role("link", name="下一页").click()
        expect(page.get_by_text("已选 1 / 2 条", exact=False)).to_be_visible()
        page.locator("[data-compare-id=fixture-0]").check()
        page.get_by_role("link", name="对比所选活动").click()
        expect(page.get_by_role("heading", name="差值（活动 B 减活动 A）")).to_be_visible()
        expect(page.get_by_text("+2.00 km", exact=True)).to_be_visible()
        expect(page.get_by_text("+5.00 分钟", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.get_by_role("link", name="B · 对比样本 0").click()
        page.get_by_role("link", name="返回运动对比").click()
        page.get_by_role("link", name="返回之前的活动页面").click()
        assert parse_qs(urlsplit(page.url).query)["page"] == ["2"]
        assert urlsplit(page.url).fragment == "activity-0"
        expect(page.locator("[data-compare-id=fixture-0]")).to_be_checked()
        page.goto(origin + "/training?sport=Running")
        expect(page.get_by_label("图表数据状态").first).to_contain_text("同步未确认完整")
        expect(page.get_by_label("图表数据状态").first).to_contain_text("已确认无活动")
        expect(page.locator(".primary-charts .chart-coverage").last).to_contain_text("活动缺少指标")
        page.get_by_role("link", name="月报", exact=True).click()
        expect(page.get_by_role("heading", name="月报", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="周报", exact=True)).to_have_count(0)
        expect(page.get_by_role("heading", name="急慢性负荷比（ACWR）")).to_have_count(0)
        page.get_by_role("link", name="查看进阶分析").click()
        expect(page.get_by_role("heading", name="急慢性负荷比（ACWR）")).to_be_visible()
        page.get_by_role("link", name="收起进阶分析").click()
        page.get_by_label("开始日期").fill(day)
        page.get_by_label("结束日期").fill(day)
        page.get_by_role("button", name="应用筛选").click()
        expect(page.get_by_label("运动类型")).to_have_value("Running")
        assert parse_qs(urlsplit(page.url).query)["end"] == [day]
        page.get_by_role("link", name="上一周").click()
        expect(page.get_by_label("运动类型")).to_have_value("Running")
        page.get_by_role("link", name="下一周").click()
        expect(page.get_by_label("结束日期")).to_have_value(day)
        page.get_by_label("开始日期").fill(now.date().isoformat())
        current_url = page.url
        page.get_by_role("button", name="应用筛选").click()
        expect(page.locator("#report-date-error")).to_have_text("结束日期不能早于开始日期。")
        assert page.url == current_url
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        # Distinguish empty data from script failure, and verify the server-rendered fallback.
        page.goto(origin + "/daily-activity")
        expect(page.locator(".chart-empty").first).to_contain_text("没有可用的图表数据")
        expect(page.locator(".chart-failure").first).to_be_hidden()
        for script in ("**/static/chart.umd.min.js", "**/static/charts.js?*"):
            context.route(script, lambda route: route.abort())
            try:
                page.goto(origin + "/training?sport=Running&days=7")
                failure = page.locator(".chart-failure").first
                expect(failure).to_be_visible()
                expect(page.locator(".chart-empty").first).to_be_hidden()
                if script.endswith("chart.umd.min.js"):
                    expect(page.locator(".chart-values").first.get_by_role("table")).to_be_visible()
                failure.get_by_role("link", name="打开数据表").click()
                assert parse_qs(urlsplit(page.url).query)["sport"] == ["Running"]
                assert parse_qs(urlsplit(page.url).query)["days"] == ["7"]
                expect(page.locator(".chart-panel").first.get_by_role("table")).to_be_visible()
                expect(page.locator(".chart-panel").first.get_by_role("link", name=day, exact=True)).to_be_visible()
                expect(page.locator(".chart-panel").first.get_by_text("已确认无活动")).to_be_visible()
                page.get_by_role("link", name="返回图表").click()
                assert "chart_view" not in parse_qs(urlsplit(page.url).query)
            finally:
                context.unroute(script)
        page.goto(origin + "/?chart_view=table")
        expect(page.locator(".aurora-chart").last.get_by_role("table").first).to_be_visible()
        page.goto(origin + "/?day=" + day)
        expect(page.get_by_role("heading", name=f"复盘日期 {day}")).to_be_visible()
        page.get_by_role("link", name="近 28 天", exact=True).click()
        expect(page.get_by_role("heading", name="活动汇总 · 近 28 天")).to_be_visible()
        assert parse_qs(urlsplit(page.url).query)["day"] == [day]
        with page.expect_navigation():
            page.get_by_role("link", name="查看这 28 天的训练趋势").click()
        expect(page.get_by_label("开始日期")).to_have_value((now.date() - timedelta(days=27)).isoformat())
        expect(page.get_by_label("结束日期")).to_have_value(now.date().isoformat())
        page.go_back()
        page.get_by_role("link", name="回到今天").click()
        expect(page.get_by_role("heading", name=f"复盘日期 {now.date()}")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

        store.commit(run, "sleep", [("sleep-test", when.isoformat(), {
            "entryData": {"SleepId": 1, "Duration": 25200},
        })])
        store.commit(run, "recovery", [("recovery-test", when.isoformat(), {
            "entryData": {"Balance": .75, "StressState": 4, "HRV": 52},
        })])
        page.goto(origin + "/sleep-recovery")
        readings = page.locator(".wellness-latest")
        expect(readings).to_contain_text("7.0 小时")
        expect(readings).to_contain_text("尚未配置字段")
        expect(page.get_by_role("heading", name="HRV 与静息心率")).to_be_visible()
        page.get_by_role("link", name="配置已采到的睡眠与恢复字段").click()
        page.get_by_label("HRV（毫秒）").select_option("HRV")
        page.get_by_role("button", name="保存偏好").click()
        page.goto(origin + "/sleep-recovery")
        expect(readings).to_contain_text("52 ms")
        expect(readings).to_contain_text("基线样本不足")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

        store.commit(run, "daily_activity", [("daily-primary", when.isoformat(), {
            "Name": "stepcount", "Aggregation": "sum", "Source": "手表", "Sample": {"Value": 8900},
        })])
        page.goto(origin + "/daily-activity")
        expect(page.get_by_label("数据来源")).to_have_count(0)
        expect(page.get_by_text("当前来源：手表。")).to_be_visible()
        store.commit(run, "daily_activity", [("daily-secondary", when.isoformat(), {
            "Name": "stepcount", "Aggregation": "sum", "Source": "手机", "Sample": {"Value": 7200},
        })])
        page.reload()
        page.get_by_label("数据来源").select_option("手机")
        page.get_by_label("查看尺度").select_option("week")
        page.get_by_role("button", name="记住来源设置").click()
        expect(page.get_by_text("已保存常用来源。")).to_be_visible()
        expect(page.get_by_text("标签括号为有效天数", exact=False).first).to_be_visible()
        page.get_by_text("设置每日步数目标", exact=True).click()
        page.get_by_label("每日目标（步，留空清除）").fill("7000")
        page.get_by_role("button", name="保存目标").click()
        expect(page.get_by_text("每日目标 7000 步，达标 1 / 1 个有效日。")).to_be_visible()
        page.goto(origin + "/daily-activity")
        expect(page.get_by_label("数据来源")).to_have_value("手机")
        expect(page.get_by_text("每日目标 7000 步", exact=False)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
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
    page.get_by_role("button", name="公园环线").click()
    expect(page.locator("#route-coordinates")).not_to_have_value("")
    expect(page.get_by_role("link", name="在 OpenStreetMap 打开底图")).to_be_visible()
    assert not requests
    page.get_by_role("button", name="返回路线列表").click()
    expect(page.locator("#route-map")).to_be_hidden()
    page.goto(origin + "/settings")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"Settings overflow at {width}px"
    expect(page.locator("#sync-details")).not_to_have_attribute("open", "")
    page.locator("#sync-details > summary").press("Enter")
    expect(page.get_by_role("heading", name="数据覆盖")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), f"Sync details overflow at {width}px"
    settings = page.locator("#amap-settings")
    settings.get_by_text("启用或配置高德地图", exact=True).click()
    settings.get_by_label("自动显示高德地图").check()
    settings.get_by_label("高德 Web 端 Key").fill("a" * 32)
    settings.get_by_label("安全密钥").fill("b" * 32)
    settings.get_by_label("Suunto 路线坐标系").select_option("gps")
    settings.get_by_role("button", name="保存地图设置").click()
    expect(settings.get_by_text("高德地图设置已保存。")).to_be_visible()
    page.goto(origin + "/routes?q=missing")
    expect(page.locator("#amap-preview")).to_have_count(0)
    expect(page.get_by_text("没有符合筛选条件的路线。", exact=False)).to_be_visible()
    expect(page.get_by_role("link", name="清除筛选")).to_have_attribute("href", "/routes")
    assert not requests
    page.goto(origin + "/routes")
    preview = page.locator("#amap-preview")
    expect(preview).to_be_visible()
    first = page.locator('.route-select[aria-pressed="true"]').inner_text()
    other = page.locator('.route-select[aria-pressed="false"]')
    assert first == "公园环线"
    expect(preview).to_have_attribute("data-markers", "起点,终点")
    if width < 960:
        expect(page.locator(".route-inline-map #amap-preview")).to_be_visible()
    else:
        expect(page.locator("#route-map-home #amap-preview")).to_be_visible()
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
                    print(f"{host} · {width}px · OAuth, P0/P1/P2 dashboard flows and AMap preview passed")
                finally:
                    fixture.doCleanups()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
