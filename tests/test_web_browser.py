"""Opt-in Chromium smoke tests against real static files and mocked local APIs.

PS38_TEST_BROWSER=1 PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
    .venv/bin/python -m unittest tests.test_web_browser -v
"""

import mimetypes
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


WEB_DIR = Path(__file__).resolve().parents[1] / "web"


class StaticWebHandler(BaseHTTPRequestHandler):
    """Expose only the checked-in web directory, without the application server."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        relative = path.removeprefix("/static/") if path.startswith("/static/") else "index.html"
        target = (WEB_DIR / relative).resolve()
        try:
            target.relative_to(WEB_DIR)
        except ValueError:
            self.send_error(404)
            return
        if not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(
    os.environ.get("PS38_TEST_BROWSER") == "1", "set PS38_TEST_BROWSER=1 to launch Chromium"
)
class WebBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StaticWebHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.stop_server)
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    @classmethod
    def stop_server(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        from playwright.sync_api import expect

        self.expect = expect
        self.page_errors = []
        self.blocked_requests = []
        self.unexpected_api_requests = []
        self.fail_save = False
        self.context = self.browser.new_context(service_workers="block", locale="zh-CN")
        self.addCleanup(self.context.close)
        self.context.route("**/*", self.route_request)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))

    def tearDown(self):
        self.assertEqual(self.page_errors, [], "页面出现未处理的 JavaScript 异常")
        self.assertEqual(self.blocked_requests, [], "页面尝试访问本机测试服务以外的地址")
        self.assertEqual(self.unexpected_api_requests, [], "请显式补充新增 API 的模拟数据")

    def route_request(self, route):
        parsed = urlsplit(route.request.url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            self.blocked_requests.append(route.request.url)
            route.abort("blockedbyclient")
            return
        if not parsed.path.startswith("/api/"):
            route.continue_()
            return
        if parsed.path == "/api/pool/save" and self.fail_save:
            route.fulfill(status=503, json={"ok": False, "error": "模拟保存失败，请稍后重试"})
            return
        responses = {
            "/api/status": {"ok": True, "session": {"status": "online"}, "backfill": {}},
            "/api/my-events": {"ok": True, "ready": True, "rows": [], "total": 0, "agents": []},
            "/api/my-events/summary": {"ok": True, "ready": False},
            "/api/my-events/events": {"ok": True, "ready": True, "events": [], "cursor": 0},
            "/api/merge": {"ok": True, "sources": {}, "counts": {}},
            "/api/follow": {
                "ok": True,
                "service": {"enabled": False, "running": True, "scope": "live"},
                "rule": {"enabled": False, "direction": "forward", "amount_mode": "fixed"},
                "summary": {},
                "rows": [],
                "total": 0,
                "accounts": [],
            },
            "/api/pool": {
                "ok": True,
                "accounts": [
                    {
                        "id": "online-fixture",
                        "name": "本地在线账号",
                        "status": "online",
                        "username": "fixture-online",
                        "base_url": "https://online.example.invalid",
                        "platform": "ps38_backend",
                        "platform_label": "PS38 后台",
                        "has_session": True,
                    },
                    {
                        "id": "expired-fixture",
                        "name": "本地失效账号",
                        "status": "expired",
                        "username": "fixture-expired",
                        "base_url": "https://expired.example.invalid",
                        "platform": "crown_member",
                        "platform_label": "皇冠会员",
                        "has_session": True,
                    },
                ],
                "summary": {"total": 2, "online": 1, "pending": 0, "error": 1},
                "logs": [],
                "check_interval": 60,
            },
        }
        payload = responses.get(parsed.path)
        if payload is None:
            self.unexpected_api_requests.append(parsed.path)
            route.fulfill(status=404, json={"ok": False, "error": "未设置此测试接口"})
        else:
            route.fulfill(json=payload)

    def test_deep_link_and_navigation_keep_the_visible_panel_in_sync(self):
        self.page.goto(f"{self.origin}/#/settings")
        self.expect(self.page.locator('[data-page-panel="settings"]')).to_be_visible()
        self.expect(self.page.locator("#appPageTitle")).to_have_text("设置")
        self.expect(self.page.locator('[data-page-link="settings"]')).to_have_attribute(
            "aria-current", "page"
        )
        self.expect(self.page.locator(".settings-panel")).to_have_count(3)
        self.expect(self.page.locator("#copyRuleForm")).to_be_visible()
        self.expect(self.page.locator("#settingsState")).to_have_text("采集在线")
        self.expect(self.page.locator(".app-statusbar")).to_be_visible()
        self.expect(self.page.locator("#statusbarHealth")).to_contain_text("系统安全运行中")

        self.page.locator('[data-page-link="merge"]').click()
        self.expect(self.page).to_have_url(f"{self.origin}/#/merge")
        self.expect(self.page.locator('[data-page-panel="merge"]')).to_be_visible()
        self.expect(self.page.locator('[data-page-panel="settings"]')).to_be_hidden()
        self.page.go_back()
        self.expect(self.page.locator('[data-page-panel="settings"]')).to_be_visible()
        self.expect(self.page.locator("#appPageTitle")).to_have_text("设置")

    def test_pool_filters_and_search_can_be_cleared(self):
        self.page.goto(f"{self.origin}/#/pool")
        cards = self.page.locator("#poolCards .pool-card")
        self.expect(cards).to_have_count(2)
        self.page.locator('[data-pool-filter="online"]').click()
        self.expect(cards).to_have_count(1)
        self.expect(cards.first).to_contain_text("本地在线账号")
        self.expect(self.page.locator("#poolFilterCount")).to_have_text("1 / 2 个账号")

        self.page.locator("#poolSearch").fill("不存在的账号")
        self.expect(self.page.locator("#poolEmptyTitle")).to_have_text("没有匹配的账号")
        self.expect(self.page.locator("#poolEmpty")).to_be_visible()
        self.page.locator("#poolEmptyAdd").click()
        self.expect(cards).to_have_count(2)
        self.expect(self.page.locator("#poolSearch")).to_have_value("")
        self.expect(self.page.locator('[data-pool-filter="all"]')).to_have_attribute(
            "aria-pressed", "true"
        )

    def test_failed_pool_save_shows_error_and_keeps_the_page_usable(self):
        self.fail_save = True
        self.page.goto(f"{self.origin}/#/pool")
        self.expect(self.page.locator("#poolCards .pool-card")).to_have_count(2)
        self.page.locator("#poolAdd").click()
        self.expect(self.page.locator("#poolDialog")).to_be_visible()
        self.page.locator("#poolUsername").fill("local-fixture")
        self.page.locator('#poolForm button[type="submit"]').click()

        self.expect(self.page.locator("#poolFormError")).to_have_text("模拟保存失败，请稍后重试")
        self.expect(self.page.locator("#poolDialog")).to_be_visible()
        self.expect(self.page.locator("#poolUsername")).to_have_value("local-fixture")
        self.page.locator("#poolDialog [data-pool-dialog-close]").first.click()
        self.expect(self.page.locator("#poolDialog")).to_be_hidden()
        self.page.locator('[data-page-link="home"]').click()
        self.expect(self.page.locator('[data-page-panel="home"]')).to_be_visible()


if __name__ == "__main__":
    unittest.main()
