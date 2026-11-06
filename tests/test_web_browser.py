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
        self.ads_import_requests = []
        self.credential_requests = []
        self.delete_requests = []
        self.ads_profiles_response = {
            "ok": True,
            "base_url": "http://localhost:50325",
            "items": [
                {
                    "profile_id": "k1aaa",
                    "profile_no": "301",
                    "name": "1号会员：aaa123 过期12-05",
                    "account": "aaa123",
                    "group_name": "测试",
                    "proxy_host": "163.227.3.10",
                    "proxy_port": "443",
                    "expired": False,
                    "stopped": False,
                    "bound_account": None,
                },
                {
                    "profile_id": "k1bbb",
                    "profile_no": "302",
                    "name": "2号会员：bbb456 过期12-04",
                    "account": "bbb456",
                    "group_name": "",
                    "proxy_host": "163.227.3.11",
                    "proxy_port": "443",
                    "expired": False,
                    "stopped": False,
                    "bound_account": {
                        "id": "acc-2",
                        "name": "已绑定账号",
                        "platform": "crown_member",
                    },
                },
            ],
            "summary": {"total": 2, "bound": 1, "expired": 0, "available": 1},
        }
        self.open_path_requests = []
        self.pool_response = None
        self.copy_groups_response = None
        self.monitor_response = {
            "ok": True,
            "endpoints": [
                {
                    "url": "https://hga038.com",
                    "host": "hga038.com",
                    "beats": [
                        {"at": "2026-11-04T10:00:00+00:00", "ok": True, "latency": 280, "error": ""}
                    ],
                    "uptime": 100.0,
                    "samples": 1,
                    "status": "online",
                    "latency": 280,
                    "last_checked": "2026-11-04T10:00:00+00:00",
                    "error": "",
                },
                {
                    "url": "https://hga050.com",
                    "host": "hga050.com",
                    "beats": [
                        {
                            "at": "2026-11-04T10:00:00+00:00",
                            "ok": False,
                            "latency": 190,
                            "error": "TLS: SSLEOFError",
                        }
                    ],
                    "uptime": 0.0,
                    "samples": 1,
                    "status": "down",
                    "latency": 190,
                    "last_checked": "2026-11-04T10:00:00+00:00",
                    "error": "TLS: SSLEOFError",
                },
            ],
            "summary": {"total": 2, "online": 1, "offline": 1, "interval": 30},
        }
        self.merge_response_sequence = None
        self.merge_request_count = 0
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
        if parsed.path == "/api/monitor":
            route.fulfill(json=self.monitor_response)
            return
        if parsed.path == "/api/monitor/refresh":
            route.fulfill(json={**self.monitor_response, "started": True})
            return
        if parsed.path == "/api/pool" and self.pool_response is not None:
            route.fulfill(json=self.pool_response)
            return
        if parsed.path == "/api/adspower/profiles":
            route.fulfill(json=self.ads_profiles_response)
            return
        if parsed.path == "/api/pool/import-envs":
            self.ads_import_requests.append(route.request.post_data_json or {})
            route.fulfill(
                json={
                    "ok": True,
                    "created": [{"id": "new-1", "env_id": "k1aaa"}],
                    "skipped": [],
                    "message": "已导入 1 个账号",
                }
            )
            return
        if parsed.path == "/api/pool/credentials":
            self.credential_requests.append(route.request.post_data_json or {})
            route.fulfill(json={"ok": True, "message": "账号凭据已保存"})
            return
        if parsed.path == "/api/pool/delete":
            self.delete_requests.append(route.request.post_data_json or {})
            route.fulfill(json={"ok": True, "message": "账号已删除"})
            return
        if parsed.path == "/api/merge":
            self.merge_request_count += 1
            if self.merge_response_sequence is not None:
                payload = self.merge_response_sequence[0]
                if len(self.merge_response_sequence) > 1:
                    payload = self.merge_response_sequence.pop(0)
                route.fulfill(json=payload)
                return
        if parsed.path == "/api/my-events/copy-groups" and self.copy_groups_response is not None:
            route.fulfill(json=self.copy_groups_response)
            return
        if parsed.path == "/api/actions/open-path":
            self.open_path_requests.append(route.request.post_data_json)
            route.fulfill(json={"ok": True, "message": "已在文件管理器中打开 orders.sqlite3"})
            return
        responses = {
            "/api/notify": {
                "ok": True,
                "enabled": False,
                "configured": False,
                "has_token": False,
                "token_hint": "",
                "chat_id": "",
                "proxy": "",
                "push_result": True,
                "bot_username": "",
                "last_error": "",
                "last_sent_at": "",
                "sent_count": 0,
            },
            "/api/status": {
                "ok": True,
                "version": "0.0.0-test",
                "session": {"status": "online"},
                "backfill": {},
                "db": {"path": "/tmp/ps38-test/orders.sqlite3", "ready": True},
                "config": {
                    "host": "127.0.0.1",
                    "port": 8899,
                    "db_path": "/tmp/ps38-test/orders.sqlite3",
                    "follow_db_path": "/tmp/ps38-test/follow.sqlite3",
                    "merge_db_path": "/tmp/ps38-test/fixture_merge.sqlite3",
                    "pool_db_path": "/tmp/ps38-test/accounts.sqlite3",
                    "pool_dir": "/tmp/ps38-test/accounts",
                    "has_credentials": False,
                },
                "counts": {"total": 0, "open": 0, "settled": 0},
            },
            "/api/my-events": {"ok": True, "ready": True, "rows": [], "total": 0, "agents": []},
            "/api/my-events/copy-groups": {
                "ok": True,
                "ready": True,
                "rows": [],
                "total": 0,
                "agents": [],
                "window_ms": 20000,
            },
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
        self.expect(self.page.locator("#notifySave")).to_be_enabled()
        self.expect(self.page.locator("#notifyPanelTag")).to_have_text("未配置")
        self.expect(self.page.locator("#notifyBotStatus")).to_have_text("未认证")
        self.expect(self.page.locator("#copySplitCount")).to_have_value("1")
        self.expect(self.page.locator("#copyMatchLimit")).to_have_value("0")
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

    def test_settings_paths_open_in_file_manager(self):
        self.page.goto(f"{self.origin}/#/settings")
        self.expect(self.page.locator(".settings-row-path")).to_have_count(5)
        self.page.locator('[data-open-kind="orders_db"]').click()
        self.expect(self.page.locator("#toast")).to_contain_text(
            "已在文件管理器中打开 orders.sqlite3"
        )
        self.assertEqual(self.open_path_requests, [{"kind": "orders_db"}])

    def test_pool_filters_and_search_can_be_cleared(self):
        self.page.goto(f"{self.origin}/#/pool")
        cards = self.page.locator("#poolCards .pool-card:not(.pool-monitor-card)")
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

    def test_ads_import_dialog_selects_and_imports(self):
        self.page.goto(f"{self.origin}/#/pool")
        self.page.locator("#poolImportAds").click()
        dialog = self.page.locator("#poolAdsDialog")
        self.expect(dialog).to_be_visible()
        rows = dialog.locator(".pool-ads-row")
        self.expect(rows).to_have_count(2)
        self.expect(rows.nth(0)).to_contain_text("aaa123")
        self.expect(rows.nth(1)).to_contain_text("已绑定")
        self.expect(rows.nth(1).locator(".pool-ads-check")).to_be_disabled()

        rows.nth(0).locator(".pool-ads-check").check()
        self.expect(dialog.locator("#poolAdsImport")).to_be_enabled()
        self.expect(dialog.locator("#poolAdsImport")).to_contain_text("导入所选（1）")
        dialog.locator("#poolAdsImport").click()

        self.expect(dialog).not_to_be_visible()
        self.expect(self.page.locator("#toast")).to_contain_text("已导入 1 个账号")
        self.assertEqual(len(self.ads_import_requests), 1)
        payload = self.ads_import_requests[0]
        self.assertEqual(payload["env_ids"], ["k1aaa"])
        self.assertEqual(payload["platform"], "auto")
        self.assertEqual(payload["usernames"]["k1aaa"], "aaa123")

    def test_credentials_dialog_saves_totp(self):
        self.page.goto(f"{self.origin}/#/pool")
        self.page.locator('[data-pool-action="credentials"]').first.click()
        dialog = self.page.locator("#poolCredDialog")
        self.expect(dialog).to_be_visible()
        self.expect(dialog.locator("#poolCredUsername")).to_have_value("fixture-online")
        dialog.locator("#poolCredPassword").fill("secret-pass")
        dialog.locator("#poolCredTotp").fill("JBSWY3DPEHPK3PXP")
        dialog.locator('button[type="submit"]').click()

        self.expect(dialog).not_to_be_visible()
        self.expect(self.page.locator("#toast")).to_contain_text("账号凭据已保存")
        self.assertEqual(len(self.credential_requests), 1)
        payload = self.credential_requests[0]
        self.assertEqual(payload["id"], "online-fixture")
        self.assertEqual(payload["username"], "fixture-online")
        self.assertEqual(payload["password"], "secret-pass")
        self.assertEqual(payload["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_pool_delete_requires_second_click_without_native_confirm(self):
        self.page.goto(f"{self.origin}/#/pool")
        button = self.page.locator('[data-pool-action="delete"]').first
        button.click()
        self.expect(self.page.locator("#toast")).to_contain_text("再点一次")
        self.assertEqual(self.delete_requests, [])

        button.click()
        self.expect(self.page.locator("#toast")).to_contain_text("账号已删除")
        self.assertEqual(len(self.delete_requests), 1)
        self.assertEqual(self.delete_requests[0]["id"], "online-fixture")

    def test_member_balances_show_zero_unknown_and_refresh_from_snapshot(self):
        common = {
            "platform": "crown_member",
            "platform_label": "皇冠会员",
            "status": "online",
            "has_session": True,
            "enabled": True,
            "balance_currency": "CNY",
            "balance_checked_at": "2026-11-04T03:40:00+08:00",
        }
        self.pool_response = {
            "ok": True,
            "accounts": [
                {**common, "id": "funded", "name": "余额测试", "balance": "2500.25"},
                {**common, "id": "zero", "name": "零余额测试", "balance": "0"},
                {
                    **common,
                    "id": "unknown",
                    "name": "未知余额测试",
                    "balance": None,
                    "balance_checked_at": "",
                },
            ],
            "summary": {"total": 3, "online": 3, "pending": 0, "error": 0},
            "logs": [],
            "check_interval": 60,
        }
        self.page.goto(f"{self.origin}/#/pool")
        funded = self.page.locator('#poolCards .pool-card[data-pool-id="funded"]')
        zero = self.page.locator('#poolCards .pool-card[data-pool-id="zero"]')
        unknown = self.page.locator('#poolCards .pool-card[data-pool-id="unknown"]')
        self.expect(funded).to_contain_text("2,500.25 CNY")
        self.expect(funded).not_to_contain_text("尚无记录")
        self.expect(zero).to_contain_text("0 CNY")
        self.expect(zero).not_to_contain_text("未知")
        self.expect(unknown).to_contain_text("未知")
        self.expect(unknown).to_contain_text("尚无记录")

        self.pool_response["accounts"][2].update(
            balance="350.50", balance_checked_at="2026-11-04T03:41:00+08:00"
        )
        self.expect(unknown).to_contain_text("350.5 CNY", timeout=8000)
        self.expect(unknown).not_to_contain_text("尚无记录")

    def test_merge_cold_saved_mappings_are_visible_before_ready_data(self):
        mapping = {
            "ps38Id": "pin:old",
            "crownId": "crown:old",
            "updatedAt": "2026-11-01T12:00:00+08:00",
            "ps38Snapshot": {
                "id": "pin:old",
                "league": "旧平博联赛",
                "name": "主队 vs 客队",
                "kickoffLocal": "11-01 12:00",
            },
            "crownSnapshot": {
                "id": "crown:old",
                "league": "旧皇冠联赛",
                "name": "主队 vs 客队",
                "kickoffLocal": "11-01 12:00",
                "homeId": "crown-home-108179",
                "awayId": "crown-away-108180",
            },
        }
        loading = {
            "ok": True,
            "ready": False,
            "refreshing": True,
            "sources": {
                "ps38": {"ok": False, "count": 0, "leagueCount": 0},
                "crown": {"ok": False, "count": 0, "leagueCount": 0},
            },
            "counts": {"leagueMappings": 0, "fixtureMappings": 1},
            "ps38Events": [],
            "crownEvents": [],
            "ps38Leagues": [],
            "crownLeagues": [],
            "leagueMappings": [],
            "mappings": [mapping],
            "leagueSuggestions": [],
            "suggestions": [],
        }
        ready = {
            **loading,
            "ready": True,
            "refreshing": False,
            "generatedAt": "2026-11-04T12:00:00+08:00",
            "sources": {
                "ps38": {"ok": True, "count": 1, "leagueCount": 1},
                "crown": {"ok": True, "count": 1, "leagueCount": 1},
            },
            "counts": {"leagueMappings": 0, "fixtureMappings": 1},
        }
        self.merge_response_sequence = [loading]
        self.page.goto(f"{self.origin}/#/merge")
        fixtures = self.page.locator("#mergeFixtures")
        merged_tab = self.page.locator('[data-merge-view="merged"]')
        self.expect(merged_tab).to_have_text("已合并 1")
        self.expect(self.page.locator("#mergeUpdated")).to_have_text("后台正在准备赛事数据…")
        self.expect(fixtures).to_contain_text("旧皇冠联赛")
        self.expect(fixtures).to_contain_text("旧平博联赛")
        self.expect(fixtures).to_contain_text("主队 vs 客队")
        self.expect(fixtures).to_contain_text(
            "主队 ID crown-home-108179 · 客队 ID crown-away-108180"
        )
        self.expect(self.page.locator("#mergeAuto")).to_be_disabled()

        self.merge_response_sequence = [ready]
        self.expect(self.page.locator("#mergeUpdated")).to_have_text("更新 12:00:00")
        self.assertGreaterEqual(self.merge_request_count, 2)
        self.expect(self.page.locator("#mergeAuto")).to_be_enabled()

    def test_merge_stops_cold_start_polling_after_leaving_page(self):
        self.merge_response_sequence = [{"ok": True, "ready": False, "refreshing": True}]
        self.page.goto(f"{self.origin}/#/merge")
        self.expect(self.page.locator("#mergeFixtures")).to_contain_text("正在准备赛事匹配数据…")
        self.page.locator('[data-page-link="home"]').click()
        self.page.wait_for_timeout(650)
        self.assertEqual(self.merge_request_count, 1)

    def test_saved_fixture_mappings_remain_visible_without_crown_session(self):
        self.merge_response_sequence = [
            {
                "ok": True,
                "ready": True,
                "refreshing": False,
                "generatedAt": "2026-11-04T12:00:00+08:00",
                "sources": {
                    "ps38": {"ok": True, "count": 0, "leagueCount": 0},
                    "crown": {
                        "ok": False,
                        "count": 0,
                        "leagueCount": 0,
                        "error": "皇冠会员会话不可用",
                    },
                },
                "counts": {"leagueMappings": 1, "fixtureMappings": 1},
                "ps38Events": [],
                "crownEvents": [],
                "ps38Leagues": [],
                "crownLeagues": [],
                "leagueMappings": [],
                "mappings": [
                    {
                        "ps38Id": "pin:old",
                        "crownId": "crown:old",
                        "updatedAt": "2026-11-01T12:00:00+08:00",
                        "ps38Snapshot": {
                            "id": "pin:old",
                            "league": "旧平博联赛",
                            "name": "主队 vs 客队",
                            "kickoffLocal": "11-01 12:00",
                        },
                        "crownSnapshot": {
                            "id": "crown:old",
                            "league": "旧皇冠联赛",
                            "name": "主队 vs 客队",
                            "kickoffLocal": "11-01 12:00",
                        },
                    }
                ],
                "leagueSuggestions": [],
                "suggestions": [],
                "crownError": "皇冠会员会话不可用",
            }
        ]
        self.page.goto(f"{self.origin}/#/merge")
        self.expect(self.page.locator("#mergeNotice")).to_contain_text("皇冠会员会话不可用")
        merged_tab = self.page.locator('[data-merge-view="merged"]')
        self.expect(merged_tab).to_have_text("已合并 1")
        merged_tab.click()
        self.expect(self.page.locator("#mergeFixtures")).to_contain_text("旧皇冠联赛")
        self.expect(self.page.locator("#mergeFixtures")).to_contain_text("旧平博联赛")
        self.expect(self.page.locator("#mergeFixtures")).to_contain_text("主队 vs 客队")
        self.expect(self.page.locator("#mergeFixtures")).to_contain_text("主队 ID — · 客队 ID —")

    def test_merge_uses_current_crown_ids_for_legacy_fixture_snapshot(self):
        self.merge_response_sequence = [
            {
                "ok": True,
                "ready": True,
                "refreshing": False,
                "generatedAt": "2026-11-04T12:00:00+08:00",
                "sources": {
                    "ps38": {"ok": True, "count": 0, "leagueCount": 0},
                    "crown": {"ok": True, "count": 2, "leagueCount": 1},
                },
                "counts": {"leagueMappings": 1, "fixtureMappings": 1},
                "ps38Events": [],
                "crownEvents": [
                    {"id": "crown:other", "homeId": "wrong-home", "awayId": "wrong-away"},
                    {"id": "crown:old", "homeId": "108179", "awayId": "108180"},
                ],
                "ps38Leagues": [],
                "crownLeagues": [],
                "leagueMappings": [],
                "mappings": [
                    {
                        "ps38Id": "pin:old",
                        "crownId": "crown:old",
                        "updatedAt": "2026-11-01T12:00:00+08:00",
                        "ps38Snapshot": {
                            "id": "pin:old",
                            "league": "旧平博联赛",
                            "name": "主队 vs 客队",
                            "kickoffLocal": "11-01 12:00",
                        },
                        "crownSnapshot": {
                            "id": "crown:old",
                            "league": "旧皇冠联赛",
                            "name": "主队 vs 客队",
                            "kickoffLocal": "11-01 12:00",
                        },
                    }
                ],
                "leagueSuggestions": [],
                "suggestions": [],
            }
        ]
        self.page.goto(f"{self.origin}/#/merge")
        self.page.locator('[data-merge-view="merged"]').click()
        team_ids = self.page.locator("#mergeFixtures .merge-crown-team-ids")
        self.expect(team_ids).to_have_text("主队 ID 108179 · 客队 ID 108180")
        self.expect(team_ids).not_to_contain_text("wrong-")

    def test_pool_monitor_is_a_full_height_column_left_of_the_cards(self):
        self.page.goto(f"{self.origin}/#/pool")
        monitor = self.page.locator("#poolMonitorCard")
        self.expect(monitor).to_be_visible()
        self.expect(monitor.locator("h3")).to_have_text("接口监控")
        self.expect(monitor.locator(".pool-monitor-row")).to_have_count(2)
        self.expect(monitor.locator(".pool-monitor-row").first).to_contain_text("hga038.com")
        self.expect(monitor.locator(".pool-monitor-row").first).to_contain_text("100%")
        self.expect(monitor.locator(".pool-monitor-row").nth(1)).to_contain_text("0%")
        # 心跳条沿用会员卡片的 .pool-beat 样式：绿=通、红=断
        self.expect(monitor.locator(".pool-monitor-bars .pool-beat.is-online")).to_have_count(1)
        self.expect(monitor.locator(".pool-monitor-bars .pool-beat.is-error")).to_have_count(1)
        self.expect(monitor.locator(".pool-monitor-bars .pool-beat")).to_have_count(24)
        # 账号卡片在右侧，且监控列比账号卡高（纵向拉满）
        cards = self.page.locator("#poolCards .pool-card")
        self.expect(cards).to_have_count(2)
        monitor_box = monitor.bounding_box()
        cards_box = cards.first.bounding_box()
        self.assertLess(monitor_box["x"], cards_box["x"])
        # 监控列与卡片列同为一行高度（网格拉伸），只要不矮于卡片即视为拉满
        self.assertGreaterEqual(monitor_box["height"], cards_box["height"])
        self.assertGreater(monitor_box["height"], 300)

    def test_failed_pool_save_shows_error_and_keeps_the_page_usable(self):
        self.fail_save = True
        self.page.goto(f"{self.origin}/#/pool")
        self.expect(
            self.page.locator("#poolCards .pool-card:not(.pool-monitor-card)")
        ).to_have_count(2)
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

    def test_copy_view_shows_order_number_and_expands_source_rows(self):
        self.copy_groups_response = {
            "ok": True,
            "ready": True,
            "total": 1,
            "agents": [],
            "window_ms": 20000,
            "scanned": 2,
            "rows": [
                {
                    "group_key": "k1",
                    "order_no": "12345678",
                    "league": "哥伦比亚 - 甲级联赛",
                    "teams": "佩雷拉-vs-托利马体育",
                    "market": "让球 0.50",
                    "selection": "主",
                    "handicap": "0.50",
                    "period_type": 0,
                    "units": "常规赛",
                    "corner_event_unit": 0,
                    "score_first": "0-1",
                    "score_last": "0-1",
                    "start_ms": 1793737800000,
                    "end_ms": 1793737809000,
                    "duration_ms": 9000,
                    "orders": 2,
                    "open_orders": 2,
                    "settled_orders": 0,
                    "members": 2,
                    "member_sample": ["TLH0R030C1", "TLH0R040M1"],
                    "stake": 4571.0,
                    "odds_min": 0.93,
                    "odds_max": 0.95,
                    "sources": [
                        {
                            "order_id": "3835726359",
                            "league": "哥伦比亚 - 甲级联赛",
                            "teams": "佩雷拉-vs-托利马体育",
                            "score": "0-1",
                            "market": "让球 0.50",
                            "selection": "主",
                            "handicap": "0.50",
                            "period_type": 0,
                            "units": "常规赛",
                            "corner_event_unit": 0,
                            "odds": 0.93,
                            "stake": 2672.0,
                            "agent": "TLH0R03",
                            "member": "TLH0R030C1",
                            "created_ms": 1793737800000,
                            "status": "OPEN",
                        },
                        {
                            "order_id": "3835728899",
                            "league": "哥伦比亚 - 甲级联赛",
                            "teams": "佩雷拉-vs-托利马体育",
                            "score": "0-1",
                            "market": "让球 0.50",
                            "selection": "主",
                            "handicap": "0.50",
                            "period_type": 0,
                            "units": "常规赛",
                            "corner_event_unit": 0,
                            "odds": 0.95,
                            "stake": 1899.0,
                            "agent": "TLH0R04",
                            "member": "TLH0R040M1",
                            "created_ms": 1793737809000,
                            "status": "OPEN",
                        },
                    ],
                }
            ],
        }
        self.page.goto(f"{self.origin}/#/home")
        self.expect(self.page.locator("#myEventsCopyWrap")).to_be_visible()
        rows = self.page.locator("#myEventsCopyBody tr.my-events-copy-row")
        self.expect(rows).to_have_count(1)
        self.expect(rows.first).to_contain_text("12345678")
        self.expect(rows.first.locator("td.teams")).to_have_text("佩雷拉 vs 托利马体育")
        self.expect(rows.first.locator("td.teams .inplay")).to_have_count(0)
        # 明细默认收起，点行才展开源单。
        self.expect(self.page.locator("#myEventsCopyBody .follow-source-table")).to_have_count(0)
        rows.first.click()
        table = self.page.locator("#myEventsCopyBody .follow-source-table")
        self.expect(table).to_have_count(1)
        self.expect(table.locator("thead")).to_contain_text("下单时间")
        self.expect(table).to_contain_text("16:30:00")
        self.expect(table).to_contain_text("16:30:09")
        self.expect(table).to_contain_text("3835726359")
        self.expect(table).to_contain_text("3835728899")
        self.expect(table).to_contain_text("TLH0R03")
        rows.first.click()
        self.expect(self.page.locator("#myEventsCopyBody .follow-source-table")).to_be_hidden()


if __name__ == "__main__":
    unittest.main()
