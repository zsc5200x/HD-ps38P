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
            },
            {
                "id": "crown-invalid-session",
                "label": "会话失效账号",
                "username": "expired-fixture",
                "platform": "crown_member",
                "status": "online",
                "enabled": True,
                "follow_eligible": False,
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
            "/api/my-events": {"ok": True, "ready": True, "rows": [], "total": 0, "agents": []},
            "/api/my-events/summary": {"ok": True, "ready": False},
            "/api/my-events/events": {"ok": True, "ready": True, "events": [], "cursor": 0},
            "/api/merge": {"ok": True, "sources": {}, "counts": {}},
            "/api/pool": {"ok": True, "accounts": [], "summary": {}, "logs": []},
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
        self.page.locator('[data-copy-filter="UNKNOWN"]').click()
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
            {
                "at": "2026-11-03T08:00:25+08:00",
                "method": "automatic",
                "status": "UNKNOWN",
                "message": "尚未唯一匹配",
            }
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
        self.expect(self.page.locator("#copyRows")).to_contain_text("下次动作")
        self.expect(self.page.locator("#copyRows")).to_contain_text("查盘 2 · 重试 1 · 核对 3")
        self.expect(self.page.locator("#copyRows")).to_contain_text("MARKET_UNAVAILABLE")
        # 左侧看板与会员池使用同一次 /api/follow 响应，不额外请求接口。
        self.expect(self.page.locator("#copyOnlineMembers")).to_have_text("2")
        self.expect(self.page.locator("#copyPoolList")).to_contain_text("皇冠测试账号")
        self.expect(self.page.locator("#copyPoolList")).to_contain_text("自动分配候选")
        self.expect(self.page.locator("#copyPoolList")).to_contain_text("会话校验未通过")
        self.expect(self.page.locator("#copyLogBody [data-copy-group]")).to_have_count(3)
        self.expect(self.page.locator("#copyLogBody")).to_contain_text("等待最新报价")
        self.page.locator('[data-copy-detail="retry-1"]').click()
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("状态历史")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text(
            "核验盘口中 → 等待重试"
        )
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("核对审计")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("自动核对")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("2 / 1 / 3")
        self.expect(self.page.locator("#copyResolutionForm")).to_have_count(0)

    def test_submission_attempt_detail_shows_timing_platform_error_and_request_state(self):
        self.attempts["success-1"] = [
            {
                "started_at": "2026-11-03T08:00:20+08:00",
                "finished_at": "2026-11-03T08:00:23+08:00",
                "account_id": "crown-local",
                "status": "REJECTED",
                "receipt_id": None,
                "error_code": "1X001",
                "request_sent": True,
                "category": "INSUFFICIENT_FUNDS",
                "message": "余额不足，平台拒绝提交",
            }
        ]
        self.open_page()
        self.page.locator('[data-copy-detail="success-1"]').click()
        attempts = self.page.locator(".copy-detail-section").filter(has=self.page.get_by_role("heading", name="提交尝试"))
        self.expect(attempts).to_contain_text("开始时间")
        self.expect(attempts).to_contain_text("结束时间")
        self.expect(attempts).to_contain_text("耗时")
        self.expect(attempts).to_contain_text("08:00:20")
        self.expect(attempts).to_contain_text("08:00:23")
        self.expect(attempts).to_contain_text("3 秒")
        self.expect(attempts).to_contain_text("1X001")
        self.expect(attempts).to_contain_text("已发送")
        self.expect(attempts).to_contain_text("INSUFFICIENT_FUNDS")
        self.expect(attempts).to_contain_text("余额不足，平台拒绝提交")

    def test_read_only_reconcile_has_audit_and_updates_status_without_resubmit(self):
        self.open_unknown_detail()
        self.fail_reconcile = True
        self.page.locator("#copyReconcileRequest").click()
        self.expect(self.page.locator("#copyDetailActionError")).to_have_text("模拟只读核对失败")
        self.expect(self.page.locator("#copyReconcileRequest")).to_be_enabled()
        self.fail_reconcile = False
        self.hold_action = "reconcile"
        self.page.locator("#copyReconcileRequest").click()
        self.expect(self.page.locator("#copyReconcileRequest")).to_be_disabled()
        self.expect(self.page.locator("#copyResolutionSubmit")).to_be_disabled()
        self.expect(self.page.locator("#copyResolution-outcome")).to_be_disabled()
        self.reconcile_outcome = "SUCCEEDED"
        self.fulfill_action(self.pending_action_route, self.records[1], "reconcile")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("QUERY-123")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("平台注单只读核对")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("手动触发只读核对")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("待核对 → 跟单成功")
        self.expect(self.page.locator("#copyReconcileRequest")).to_have_count(0)
        self.expect(self.page.locator("#copyResolutionForm")).to_have_count(0)
        self.assertEqual(self.resolutions, [])
        self.assertTrue(all(path.endswith("/reconcile") for path in self.action_routes))

    def test_manual_success_validates_preserves_failed_input_and_blocks_duplicate_post(self):
        self.open_unknown_detail()
        self.page.locator("#copyResolutionSubmit").click()
        self.expect(self.page.locator("#copyDetailActionError")).to_contain_text("请选择")
        self.page.locator("#copyResolution-outcome").select_option("SUCCEEDED")
        self.page.locator("#copyResolution-note").fill("已核实")
        self.page.locator("#copyResolutionSubmit").click()
        self.expect(self.page.locator("#copyDetailActionError")).to_contain_text("有效的平台票号")
        self.assertEqual(self.resolutions, [])
        self.page.locator("#copyResolution-receipt_id").fill("MANUAL-123")
        note = '<img src=x onerror="window.bad=true"> 已核实平台票号'
        self.page.locator("#copyResolution-note").fill(note)
        self.page.locator("#copyResolution-accepted_stake").fill("0")
        self.page.locator("#copyResolutionSubmit").click()
        self.expect(self.page.locator("#copyDetailActionError")).to_contain_text("必须大于零")
        self.assertEqual(self.resolutions, [])
        self.page.locator("#copyResolution-accepted_stake").fill("59.5")
        self.page.locator("#copyResolution-accepted_odds").fill("0.93")
        self.fail_resolution = True
        self.page.locator("#copyResolutionSubmit").click()
        self.expect(self.page.locator("#copyDetailActionError")).to_have_text("模拟核对登记失败")
        self.expect(self.page.locator("#copyResolution-receipt_id")).to_have_value("MANUAL-123")
        self.expect(self.page.locator("#copyResolution-note")).to_have_value(note)
        self.expect(self.page.locator("#copyResolution-accepted_stake")).to_have_value("59.5")
        self.page.evaluate(
            "() => import('/static/modules/copy/page.js').then(m => m.refreshCopyRecords({force: true}))"
        )
        self.expect(self.page.locator("#copyResolution-note")).to_have_value(note)
        self.page.locator("#copyDetailRefresh").click()
        self.expect(self.page.locator("#copyResolution-note")).to_have_value(note)
        self.fail_resolution = False
        self.hold_action = "resolution"
        self.page.locator("#copyResolutionSubmit").click()
        self.expect(self.page.locator("#copyResolutionSubmit")).to_be_disabled()
        self.expect(self.page.locator("#copyResolution-note")).to_be_disabled()
        self.page.locator("#copyResolutionForm").evaluate(
            "form => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}))"
        )
        self.assertEqual(len(self.action_routes), 2)
        self.fulfill_action(self.pending_action_route, self.records[1], "resolution")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("人工核对成功")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("人工核对登记")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("MANUAL-123")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("59.5 CNY")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("0.93")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text(note)
        self.expect(self.page.locator("#copyDetailContent img")).to_have_count(0)
        self.assertFalse(self.page.evaluate("Boolean(window.bad)"))
        self.expect(self.page.locator("#copyResolutionForm")).to_have_count(0)
        self.assertEqual(len(self.resolutions), 2)
        self.assertEqual(self.resolutions[-1]["accepted_stake"], "59.5")
        self.assertEqual(self.reconcile_requests, [])
        self.page.locator("#copyDetailClose").click()
        self.page.locator('[data-copy-filter="SUCCEEDED"]').click()
        self.expect(self.page.locator("#copyRows")).to_contain_text("人工核对成功")

    def test_manual_rejected_ends_group_and_optional_amounts_stay_unknown(self):
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.open_unknown_detail()
        self.page.locator("#copyResolution-outcome").select_option("SUCCEEDED")
        self.page.locator("#copyResolution-receipt_id").fill("DRAFT-123")
        self.page.locator("#copyResolution-accepted_stake").fill("60")
        self.page.locator("#copyResolution-outcome").select_option("REJECTED")
        self.expect(self.page.locator("#copyResolution-receipt_id")).to_be_disabled()
        self.expect(self.page.locator("#copyResolution-accepted_stake")).to_be_disabled()
        self.page.locator("#copyResolution-note").fill("平台核实该请求未成交")
        self.page.locator("#copyResolutionSubmit").click()
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("人工确认未成交")
        self.expect(self.page.locator("#copyResolutionForm")).to_have_count(0)
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("人工登记")
        self.assertEqual(self.resolutions[-1]["outcome"], "REJECTED")
        self.assertEqual(self.resolutions[-1]["receipt_id"], "")
        self.assertNotIn("accepted_stake", self.resolutions[-1])
        self.assertNotIn("accepted_odds", self.resolutions[-1])
        self.assertEqual(self.records[1]["accepted_stake"], None)
        self.assertLessEqual(
            self.page.locator("#copyDetailDialog").evaluate("el => el.scrollWidth"),
            self.page.locator("#copyDetailDialog").evaluate("el => el.clientWidth"),
        )

    def test_manual_success_without_actual_amount_does_not_invent_receipt_values(self):
        self.open_unknown_detail()
        self.page.locator("#copyResolution-outcome").select_option("SUCCEEDED")
        self.page.locator("#copyResolution-receipt_id").fill("MANUAL-EMPTY")
        self.page.locator("#copyResolution-note").fill("已核实票号，平台未显示实际金额和水位")
        self.page.locator("#copyResolutionSubmit").click()
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("人工核对成功")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("未登记实际金额")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("未登记实际水位")
        self.assertNotIn("accepted_stake", self.resolutions[-1])
        self.assertNotIn("accepted_odds", self.resolutions[-1])

    def test_pending_action_close_reopen_and_late_response_preserve_current_view(self):
        self.records.append({**self.records[1], "id": "unknown-2", "crown_home": "丙队"})
        self.open_page()
        self.page.locator('[data-copy-filter="UNKNOWN"]').click()
        self.page.locator('[data-copy-detail="unknown-1"]').click()
        self.hold_action = "reconcile"
        self.page.locator("#copyReconcileRequest").click()
        self.expect(self.page.locator("#copyReconcileRequest")).to_be_disabled()
        first_route = self.pending_action_route
        self.page.locator("#copyDetailClose").click()
        self.page.locator('[data-copy-detail="unknown-1"]').click()
        self.expect(self.page.locator("#copyResolutionForm")).to_be_visible()
        self.expect(self.page.locator("#copyReconcileRequest")).to_be_disabled()
        self.expect(self.page.locator("#copyResolutionSubmit")).to_be_disabled()
        self.fail_reconcile = True
        self.fulfill_action(first_route, self.records[1], "reconcile")
        self.expect(self.page.locator("#copyReconcileRequest")).to_be_enabled()
        self.expect(self.page.locator("#copyDetailActionError")).to_be_hidden()
        self.fail_reconcile = False
        self.page.locator("#copyReconcileRequest").click()
        self.expect(self.page.locator("#copyReconcileRequest")).to_be_disabled()
        second_route = self.pending_action_route
        self.page.locator("#copyDetailClose").click()
        self.page.locator('[data-copy-detail="unknown-2"]').click()
        self.expect(self.page.locator("#copyDetailTitle")).to_contain_text("丙队")
        self.expect(self.page.locator("#copyReconcileRequest")).to_be_enabled()
        self.page.locator("#copyResolution-outcome").select_option("SUCCEEDED")
        self.page.locator("#copyResolution-receipt_id").fill("OTHER-DRAFT")
        self.page.locator("#copyResolution-note").fill("另一组核对草稿")
        self.reconcile_outcome = "SUCCEEDED"
        self.fulfill_action(second_route, self.records[1], "reconcile")
        self.page.evaluate(
            "() => import('/static/modules/copy/page.js').then(m => m.refreshCopyRecords({force: true}))"
        )
        self.expect(self.page.locator("#copyDetailTitle")).to_contain_text("丙队")
        self.expect(self.page.locator("#copyResolution-receipt_id")).to_have_value("OTHER-DRAFT")
        self.expect(self.page.locator("#copyResolution-note")).to_have_value("另一组核对草稿")
        self.expect(self.page.locator("#copyDetailActionError")).to_be_hidden()
        self.expect(self.page.locator("#copyDetailContent")).not_to_contain_text("QUERY-123")

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

    def test_success_receipts_default_and_unknown_are_separate(self):
        self.open_page()
        self.expect(self.page.locator("#copyRows [data-copy-group]")).to_have_count(1)
        self.expect(self.page.locator("#copyRows")).to_contain_text("R-123")
        self.expect(self.page.locator("#copyPrevious")).to_be_hidden()
        self.expect(self.page.locator("#copyMore")).to_be_hidden()
        self.expect(self.page.locator("#copyRows")).to_contain_text("提交金额 · 回执未回显")
        self.assertEqual(self.queries[0]["status"], ["SUCCEEDED"])
        self.page.locator('[data-copy-filter="UNKNOWN"]').click()
        self.expect(self.page.locator("#copyRows [data-copy-group]")).to_have_count(1)
        self.expect(self.page.locator("#copyRows")).to_contain_text("待核对")
        self.expect(self.page.locator("#copyRows")).not_to_contain_text("跟单成功")
        self.page.locator("#copyRows [data-copy-detail]").click()
        self.expect(self.page.locator("#copyDetailDialog")).to_be_visible()
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("source-1")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("后台币种标签")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("THB")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("100 CNY")
        self.expect(self.page.locator("#copyDetailContent")).to_contain_text("尚无明确成交结果")
        self.page.keyboard.press("Escape")
        self.expect(self.page.locator("#copyDetailDialog")).to_be_hidden()

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
        self.expect(self.page.locator("#copyRuleSummary")).to_contain_text("已暂停")
        self.page.locator("#copyEnableRule").click()
        self.expect(self.page.locator("#copyRuleFeedback")).to_have_text("规则已保存，跟单已启用。")
        self.assertTrue(self.saves[-1]["enabled"])
        self.assertNotIn("account_id", self.saves[-1])
        self.expect(self.page.locator("#copyRuleSummary")).to_contain_text("已启用")
        self.page.locator("#copyPause").click()
        self.expect(self.page.locator("#copyRuleSummary")).to_contain_text("已暂停")

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
        self.expect(self.page.locator("#copyRows")).to_contain_text("R-123")
        self.page.locator('[data-copy-filter="UNKNOWN"]').click()
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
