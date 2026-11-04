"""跟单页的真实静态页面与本地模拟接口回归，不调用真实平台。"""

import os
import threading
import unittest
from copy import deepcopy
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from tests.test_web_browser import StaticWebHandler


@unittest.skipUnless(
    os.environ.get("PS38_TEST_BROWSER") == "1", "set PS38_TEST_BROWSER=1 to launch Chromium"
)
class FollowBrowserTests(unittest.TestCase):
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
        self.unexpected = []
        self.queries = []
        self.saves = []
        self.fail_save = False
        self.fail_read = False
        self.fail_reconcile = False
        self.fail_resolution = False
        self.reconcile_outcome = "UNKNOWN"
        self.reconcile_requests = []
        self.resolutions = []
        self.action_routes = []
        self.hold_action = None
        self.reconciliations = {}
        self.history = {}
        self.attempts = {}
        self.rule = {
            "enabled": False,
            "direction": "reverse",
            "amount_mode": "ratio",
            "amount_value": "10",
            "currency": "CNY",
            "max_age_seconds": 120,
            "min_odds": None,
            "max_odds": None,
            "member_codes": [],
            "scope": "live",
        }
        self.accounts = [
            {
                "id": "crown-local",
                "label": "皇冠测试账号",
                "username": "fixture",
                "platform": "crown_member",
                "status": "online",
                "enabled": True,
                "follow_eligible": True,
                "latency_ms": 420,
                "balance": "1700",
                "balance_currency": "CNY",
            },
            {
                "id": "crown-invalid-session",
                "label": "会话失效账号",
                "username": "expired-fixture",
                "platform": "crown_member",
                "status": "online",
                "enabled": True,
                "follow_eligible": False,
                "latency_ms": 860,
                "balance": "700",
                "balance_currency": "CNY",
            }
        ]
        common = {
            "created_at": "2026-11-03T08:00:00+08:00",
            "updated_at": "2026-11-03T08:00:25+08:00",
            "league": "本地联赛",
            "home": "甲队",
            "away": "乙队",
            "crown_home": "甲队",
            "crown_away": "乙队",
            "period": "FT",
            "market_type": "TOTAL",
            "source_side": "OVER",
            "source_line": "2.5",
            "target_side": "UNDER",
            "target_line": "2.5",
            "direction": "reverse",
            "source_stake": "600",
            "target_stake": "60",
            "accepted_stake": None,
            "accepted_odds": None,
            "odds": "0.95",
            "currency": "CNY",
            "order_count": 3,
            "member_count": 2,
            "account_id": "crown-local",
            "message": "",
            "prepare_count": 2,
            "retry_count": 1,
            "reconcile_count": 0,
            "next_action_at": None,
            "waiting_reason": "",
            "error_category": None,
        }
        self.records = [
            {**common, "id": "success-1", "status": "SUCCEEDED", "receipt_id": "R-123"},
            {
                **common,
                "id": "unknown-1",
                "status": "UNKNOWN",
                "receipt_id": None,
                "message": "提交结果未知，等待核对",
            },
        ]
        self.context = self.browser.new_context(service_workers="block", locale="zh-CN")
        self.addCleanup(self.context.close)
        self.context.route("**/*", self.route_request)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))

    def tearDown(self):
        self.assertEqual(self.page_errors, [], "页面出现未处理的 JavaScript 异常")
        self.assertEqual(self.blocked_requests, [], "页面尝试访问测试服务以外的地址")
        self.assertEqual(self.unexpected, [], "新增接口应有明确的模拟响应")

    def route_request(self, route):
        parsed = urlsplit(route.request.url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            self.blocked_requests.append(route.request.url)
            route.abort("blockedbyclient")
            return
        if not parsed.path.startswith("/api/"):
            route.continue_()
            return
        if parsed.path == "/api/follow":
            query = parse_qs(parsed.query)
            self.queries.append(query)
            if self.fail_read:
                route.fulfill(status=503, json={"ok": False, "error": "模拟读取失败"})
                return
            selected = query.get("status", ["ALL"])[0]
            rows = deepcopy(self.records)
            if selected != "ALL":
                states = (
                    {
                        "GROUPING",
                        "WAIT_MAPPING",
                        "READY",
                        "WAIT_ACCOUNT",
                        "PREPARING",
                        "WAIT_MARKET",
                        "RETRY_WAIT",
                        "SUBMITTING",
                    }
                    if selected == "PROCESSING"
                    else {"REJECTED", "EXPIRED", "SKIPPED"}
                    if selected == "FAILED"
                    else {selected}
                )
                rows = [row for row in rows if row["status"] in states]
            search = query.get("search", [""])[0]
            if search:
                rows = [row for row in rows if search in str(row)]
            total = len(rows)
            offset = int(query.get("offset", [0])[0])
            limit = int(query.get("limit", [50])[0])
            route.fulfill(
                json={
                    "ok": True,
                    "service": {
                        "enabled": self.rule["enabled"],
                        "running": True,
                        "scope": "live",
                        "active_tasks": 1,
                        "queued_tasks": 3,
                    },
                    "rule": self.rule,
                    "summary": {"succeeded": 1, "unknown": 1, "total": len(self.records)},
                    "rows": rows[offset : offset + limit],
                    "total": total,
                    "accounts": self.accounts,
                }
            )
            return
        if parsed.path.startswith("/api/follow/groups/"):
            group_id = parsed.path.split("/")[4]
            row = next(item for item in self.records if item["id"] == group_id)
            action = parsed.path.rsplit("/", 1)[-1]
            if route.request.method == "POST":
                if action not in {"reconcile", "resolution"}:
                    self.unexpected.append(parsed.path)
                    route.fulfill(status=404, json={"ok": False})
                    return
                self.action_routes.append(parsed.path)
                if self.hold_action == action:
                    self.pending_action_route = route
                    return
                self.fulfill_action(route, row, action)
                return
            route.fulfill(
                json={
                    "ok": True,
                    "group": row,
                    "members": [
                        {
                            "order_id": "source-1",
                            "member_identity": "local-member",
                            "stake": "100",
                            "currency": "CNY",
                            "reported_currency": "THB",
                            "selection": "大",
                            "created_at": "2026-11-03T08:00:00+08:00",
                        }
                    ],
                    "attempts": self.attempts.get(group_id, []),
                    "history": self.history.get(group_id, []),
                    "reconciliations": self.reconciliations.get(group_id, []),
                }
            )
            return
        if parsed.path == "/api/follow/rule":
            self.saves.append(route.request.post_data_json)
            if self.fail_save:
                route.fulfill(status=400, json={"ok": False, "error": "模拟规则保存失败"})
            else:
                self.rule = route.request.post_data_json
                route.fulfill(json={"ok": True, "rule": self.rule})
            return
        if parsed.path == "/api/follow/pause":
            self.rule["enabled"] = False
            route.fulfill(json={"ok": True})
            return
        responses = {
            "/api/status": {"ok": True, "session": {"status": "online"}, "backfill": {}},
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
            "/api/pool": {"ok": True, "accounts": [], "summary": {}, "logs": []},
            "/api/monitor": {
                "ok": True,
                "endpoints": [],
                "summary": {"total": 0, "online": 0, "offline": 0, "interval": 30},
            },
        }
        if parsed.path in responses:
            route.fulfill(json=responses[parsed.path])
        else:
            self.unexpected.append(parsed.path)
            route.fulfill(status=404, json={"ok": False})

    def fulfill_action(self, route, row, action):
        group_id = row["id"]
        if action == "reconcile":
            self.reconcile_requests.append(group_id)
            if self.fail_reconcile:
                route.fulfill(status=400, json={"ok": False, "error": "模拟只读核对失败"})
                return
            row["reconcile_count"] += 1
            row["status"] = self.reconcile_outcome
            if row["status"] == "SUCCEEDED":
                row.update(receipt_id="QUERY-123", receipt_source="platform_query")
            method, note = "requested", "本地只读核对结果"
        else:
            body = route.request.post_data_json
            self.resolutions.append(body)
            if self.fail_resolution:
                route.fulfill(status=400, json={"ok": False, "error": "模拟核对登记失败"})
                return
            row.update(
                status=body["outcome"],
                receipt_id=body["receipt_id"] if body["outcome"] == "SUCCEEDED" else None,
                receipt_source="manual",
                accepted_stake=body.get("accepted_stake"),
                accepted_odds=body.get("accepted_odds"),
            )
            method, note = "manual", body["note"]
        self.reconciliations.setdefault(group_id, []).append(
            {
                "at": "2026-11-03T08:01:00+08:00",
                "method": method,
                "status": row["status"],
                "receipt_id": row["receipt_id"],
                "note": note,
            }
        )
        self.history.setdefault(group_id, []).append(
            {
                "at": "2026-11-03T08:01:00+08:00",
                "from_status": "UNKNOWN",
                "to_status": row["status"],
                "kind": "RECONCILE",
                "message": note,
            }
        )
        route.fulfill(
            json={
                "ok": True,
                "message": "已安排只读核对" if action == "reconcile" else "人工核对结果已登记",
            }
        )

    def open_page(self):
        self.page.goto(f"{self.origin}/#/copy")
        # 顶部状态条已移除，等首轮 /api/follow 返回即可。
        self.expect(self.page.locator("#copyRecordCount")).to_contain_text("组")

    def open_unknown_detail(self):
        self.open_page()
        self.page.locator('[data-copy-filter="FAILED"]').click()
        self.page.locator("#copyRows [data-copy-detail]").click()
        self.expect(self.page.locator("#copyResolutionForm")).to_be_visible()
        self.expect(self.page.get_by_role("button", name="重新提交", exact=True)).to_have_count(0)

    def test_queue_states_waiting_reasons_and_detail_audits(self):
        self.records.extend(
            [
                {
                    **self.records[1],
                    "id": "preparing-1",
                    "status": "PREPARING",
                    "waiting_reason": "正在核验皇冠盘口",
                },
                {
                    **self.records[1],
                    "id": "retry-1",
                    "status": "RETRY_WAIT",
                    "waiting_reason": "等待最新报价",
                    "error_category": "MARKET_UNAVAILABLE",
                    "next_action_at": "2026-11-03T08:00:30+08:00",
                    "reconcile_count": 3,
                },
                {
                    **self.records[1],
                    "id": "wait-account-1",
                    "status": "WAIT_ACCOUNT",
                    "waiting_reason": "可用皇冠会员正在执行或有未知结果待核对",
                    "error_category": "ACCOUNT_BUSY",
                },
            ]
        )
        self.history["retry-1"] = [
            {
                "at": "2026-11-03T08:00:25+08:00",
                "from_status": "PREPARING",
                "to_status": "RETRY_WAIT",
                "kind": "MARKET_UNAVAILABLE",
                "message": "等待最新报价",
            }
        ]
        self.reconciliations["retry-1"] = [
        ]
        self.open_page()
        self.expect(self.page.locator("#copyActiveTasks")).to_have_text("1")
        self.expect(self.page.locator("#copyQueuedTasks")).to_have_text("3")
        self.page.locator('[data-copy-filter="PROCESSING"]').click()
        self.expect(self.page.locator("#copyRows [data-copy-group]")).to_have_count(3)
        self.expect(self.page.locator("#copyRows")).to_contain_text("核验盘口中")
        self.expect(self.page.locator("#copyRows")).to_contain_text("等待重试")
        self.expect(self.page.locator("#copyRows")).to_contain_text("等待可用会员")
        self.expect(self.page.locator("#copyRows")).to_contain_text("等待最新报价")
        # 卡面只保留主状态；下次动作、查盘/重试次数、错误分类放在明细弹窗里。
        self.expect(self.page.locator("#copyRows")).not_to_contain_text("MARKET_UNAVAILABLE")
        # 左侧看板与会员池使用同一次 /api/follow 响应，不额外请求接口。
        self.expect(self.page.locator("#copyOnlineMembers")).to_have_text("2")
        self.expect(self.page.locator("#copyPoolList")).to_contain_text("皇冠测试账号")
        pool = self.page.locator("#copyPoolList")
        self.expect(pool).to_contain_text("延迟")
        self.expect(pool).to_contain_text("420 ms")
        self.expect(pool).to_contain_text("余额")
        self.expect(pool).to_contain_text("1,700 CNY")
        self.expect(pool).to_contain_text("860 ms")
        self.expect(pool).not_to_contain_text("自动分配候选")
        self.expect(pool).not_to_contain_text("不参与自动分配")
        self.expect(pool).not_to_contain_text("本页组数")
        self.expect(self.page.locator("#copyLogBody [data-copy-group]")).to_have_count(3)
        self.expect(self.page.locator("#copyLogBody")).to_contain_text("等待最新报价")
        self.page.locator('[data-copy-detail="retry-1"]').click()
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("状态历史")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text(
            "核验盘口中 → 等待重试"
        )
        self.expect(self.page.locator("#copyDetailContent")).not_to_contain_text("核对审计")
        self.expect(self.page.locator("#copyDetailContent")).not_to_contain_text("查盘 / 重试次数")
        self.expect(self.page.locator("#copyResolutionForm")).to_have_count(0)

    def test_submission_attempts_are_no_longer_rendered_in_the_detail(self):
        self.attempts["success-1"] = [
            {
                "started_at": "2026-11-03T08:00:20+08:00",
                "finished_at": "2026-11-03T08:00:23+08:00",
                "account_id": "fixture-crown",
                "status": "SUCCEEDED",
                "receipt_id": "25650324839",
                "error_code": "1X001",
                "request_sent": True,
                "category": "INSUFFICIENT_FUNDS",
                "message": "余额不足，平台拒绝提交",
            }
        ]
        self.open_page()
        self.page.locator('[data-copy-detail="success-1"]').click()
        content = self.page.locator("#copyDetailContent")
        self.expect(content).not_to_contain_text("提交尝试")
        self.expect(content).not_to_contain_text("1X001")
        self.expect(content).to_contain_text("状态历史")

    def test_success_detail_shows_receipt_summary_in_match_and_sources_first(self):
        self.records[0].update(
            receipt_id="25650324839",
            target_stake="100",
            accepted_stake="100",
            accepted_odds="0.96",
        )
        self.open_page()
        self.page.locator('[data-copy-detail="success-1"]').click()

        match = self.page.locator(".copy-detail-match")
        self.expect(match).to_contain_text("跟单成功")
        outcome = match.locator(".copy-detail-outcome")
        self.expect(outcome).to_contain_text("金额")
        self.expect(outcome).to_contain_text("100 CNY")
        self.expect(outcome).to_contain_text("水位")
        self.expect(outcome).to_contain_text("@0.96")
        self.expect(outcome).to_contain_text("回执号")
        self.expect(outcome).to_contain_text("25650324839")
        self.expect(outcome).to_contain_text("皇冠账号")
        self.expect(outcome).to_contain_text("皇冠测试账号")
        self.expect(self.page.locator("#copyDetailSources")).to_contain_text("查看来源订单")
        self.expect(self.page.locator("#copyDetailSourcesTable")).to_have_count(0)
        self.page.locator("#copyDetailSources").click()
        self.expect(self.page.locator("#copyDetailSourcesTable")).to_contain_text("来源订单 · 1 笔")
        self.expect(self.page.locator("#copyDetailContent")).not_to_contain_text("结果来源")
        self.expect(self.page.locator("#copyDetailContent")).not_to_contain_text("查盘 / 重试次数")
        self.expect(self.page.locator("#copyDetailContent")).not_to_contain_text("计划金额")

        for viewport in ({"width": 375, "height": 812}, {"width": 812, "height": 375}):
            self.page.set_viewport_size(viewport)
            dimensions = self.page.locator("#copyDetailDialog").evaluate(
                "element => ({width: element.clientWidth, scrollWidth: element.scrollWidth})"
            )
            self.assertLessEqual(dimensions["scrollWidth"], dimensions["width"])

    def test_shell_keeps_the_page_inside_the_viewport(self):
        self.records.extend(
            {**self.records[0], "id": f"bulk-{index}", "receipt_id": f"B-{index}"}
            for index in range(20)
        )
        self.open_page()
        self.page.locator('[data-copy-filter="ALL"]').click()
        self.expect(self.page.locator("#copyRows [data-copy-group]")).to_have_count(len(self.records))
        metrics = self.page.evaluate(
            """() => {
              const cards = document.querySelector('.copy-cards');
              return {
                scrollHeight: document.documentElement.scrollHeight,
                viewport: window.innerHeight,
                cardsClient: cards.clientHeight,
                cardsScroll: cards.scrollHeight,
                statusbar: Boolean(document.querySelector('.app-statusbar')),
              };
            }"""
        )
        self.assertLessEqual(metrics["scrollHeight"], metrics["viewport"])
        self.assertGreater(metrics["cardsScroll"], metrics["cardsClient"])
        self.assertTrue(metrics["statusbar"])

    def test_poll_does_not_overwrite_edits_and_save_is_explicit(self):
        self.open_page()
        self.assertEqual(self.saves, [])
        # 跟单规则已移到设置页，跟单页按钮只负责跳转。
        self.page.locator("#copyRulesToggle").click()
        self.expect(self.page.locator('[data-page-panel="settings"]')).to_be_visible()
        self.expect(self.page.locator("#copyAccount")).to_have_count(0)
        self.expect(self.page.locator('#copyRuleForm [name="account_id"]')).to_have_count(0)
        self.expect(self.page.locator("#cfgAccountPoolSummary")).to_contain_text("1 个自动分配候选")
        self.expect(self.page.locator("#copyParamList")).to_contain_text("号池自动分配 · 1 个候选")
        self.expect(self.page.locator("#copyRuleForm [name=currency]")).to_have_count(0)
        self.expect(self.page.locator("#copyRuleForm")).to_contain_text("不做汇率换算")
        self.page.locator('[data-rule-mode="fixed"]').click()
        self.page.locator("#copyAmountValue").fill("321")
        self.page.evaluate(
            "() => import('/static/modules/copy/page.js')"
            ".then(m => m.refreshCopyRecords({ force: true }))"
        )
        self.page.wait_for_timeout(300)
        self.expect(self.page.locator("#copyAmountValue")).to_have_value("321")
        self.expect(self.page.locator("#copyRuleDirty")).to_be_visible()
        self.page.locator("#copySaveRule").click()
        self.expect(self.page.locator("#copyRuleFeedback")).to_have_text("规则已保存。")
        self.assertFalse(self.saves[-1]["enabled"])
        self.assertNotIn("currency", self.saves[-1])
        self.assertNotIn("account_id", self.saves[-1])
        self.expect(self.page.locator("#copyRuleSummary")).to_contain_text("跟单未开启")
        self.page.locator("#copyEnableRule").click()
        self.expect(self.page.locator("#copyRuleFeedback")).to_have_text("规则已保存，跟单已启用。")
        self.assertTrue(self.saves[-1]["enabled"])
        self.assertNotIn("account_id", self.saves[-1])
        self.expect(self.page.locator("#copyRuleSummary")).to_contain_text("已启用")
        self.page.locator("#copyPause").click()
        self.expect(self.page.locator("#copyRuleSummary")).to_contain_text("跟单未开启")

    def test_errors_preserve_form_and_navigation_remains_usable(self):
        self.open_page()
        self.fail_save = True
        self.page.locator("#copyRulesToggle").click()
        self.expect(self.page.locator('[data-page-panel="settings"]')).to_be_visible()
        self.page.locator("#copyAmountValue").fill("15")
        self.page.locator("#copySaveRule").click()
        self.expect(self.page.locator("#copyFormError")).to_have_text("模拟规则保存失败")
        self.expect(self.page.locator("#copyAmountValue")).to_have_value("15")
        self.expect(self.page.locator("#copySaveRule")).to_be_enabled()
        self.fail_read = True
        self.page.locator('[data-page-link="copy"]').click()
        self.expect(self.page.locator("#copyLoadError")).to_contain_text("模拟读取失败")
        self.expect(self.page.locator("#copyRows")).to_contain_text("跟单成功")
        self.page.locator('[data-copy-filter="FAILED"]').click()
        self.expect(self.page.locator("#copyRecordCount")).to_have_text("记录读取失败")
        self.expect(self.page.locator("#copyRows [data-copy-group]")).to_have_count(0)
        self.page.locator('[data-page-link="settings"]').click()
        self.expect(self.page.locator('[data-page-panel="settings"]')).to_be_visible()

    def test_search_and_pagination_use_server_query(self):
        self.records = [
            {**self.records[0], "id": f"s-{index}", "receipt_id": f"R-{index}"}
            for index in range(55)
        ]
        self.open_page()
        self.expect(self.page.locator("#copyRows [data-copy-group]")).to_have_count(50)
        self.page.locator("#copyMore").click()
        self.expect(self.page.locator("#copyRows [data-copy-group]")).to_have_count(5)
        self.expect(self.page.locator("#copyPrevious")).to_be_visible()
        self.page.locator("#copySearch").fill("不存在")
        self.expect(self.page.locator("#copyEmptyTitle")).to_have_text("没有找到匹配的记录")
        self.assertEqual(self.queries[-1].get("offset"), ["0"])
        self.page.locator("#copyEmptyAll").click()
        self.expect(self.page.locator("#copyRows [data-copy-group]")).to_have_count(50)
        self.expect(self.page.locator("#copySearch")).to_have_value("")

    def test_remote_text_is_rendered_as_text_and_mobile_fits(self):
        self.records[0]["crown_home"] = '<img src=x onerror="window.bad=true">'
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.open_page()
        self.expect(self.page.locator("#copyRows img")).to_have_count(0)
        self.assertFalse(self.page.evaluate("Boolean(window.bad)"))
        self.assertLessEqual(
            self.page.evaluate("document.documentElement.scrollWidth"),
            self.page.evaluate("window.innerWidth"),
        )


if __name__ == "__main__":
    unittest.main()
