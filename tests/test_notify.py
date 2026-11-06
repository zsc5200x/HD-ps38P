"""Telegram 推送：消息模板、配置存储与回调解析。"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ps38app.follow.store import FollowStore
from ps38app.notify import (
    NotifyError,
    NotifyService,
    NotifyStore,
    TelegramAPI,
    emoji_theme,
    keyboard,
    new_order_text,
    result_text,
)

GROUP_ID = "a" * 32
TOKEN = "123456789:AAAAAAAAbbbbbbbbCCCCCCCC"
NOW = "2026-11-04T00:03:25+08:00"


def make_group(**extra):
    group = {
        "id": GROUP_ID,
        "base_key": "base-1",
        "start_ms": 1,
        "close_ms": 2,
        "status": "SUCCEEDED",
        "account_id": "crown-1",
        "created_at": NOW,
        "updated_at": NOW,
        "league": "日本 J3 联赛",
        "home": "大阪",
        "away": "熊本深红",
        "crown_home": "大阪",
        "crown_away": "熊本深红",
        "market_type": "HANDICAP",
        "period": "FT",
        "direction": "reverse",
        "source_side": "HOME",
        "source_line": "-0.75",
        "target_side": "HOME",
        "target_line": "-0.75",
        "source_stake": "30000",
        "target_stake": "100",
        "accepted_stake": "150",
        "accepted_odds": "0.91",
        "odds": "0.95",
        "currency": "CNY",
        "receipt_id": "25650723094",
        "message": "平台已确认成功",
        "source_created_at": NOW,
        "member_count": 1,
        "order_count": 1,
    }
    group.update(extra)
    return group


MEMBER = {
    "source": "ps38ag",
    "member_identity": "code:LuB000222",
    "order_id": "25650723094",
    "stake": "30000",
    "currency": "THB",
    "reported_currency": "THB",
    "selection": "大",
    "created_at": NOW,
}


class MessageTemplateTests(unittest.TestCase):
    def test_new_order_text_matches_push_style(self):
        text = new_order_text(make_group(), [MEMBER])
        for expected in (
            "🤣新注单提醒",
            "🤣日本 J3 联赛",
            "🤣大阪 v 熊本深红",
            "📍 状态：滚球",
            "玩法：让球",
            "时段：全场",
            "选择：主队 -0.5/1 @0.91",
            "💰 原注金额：30,000",
            "💲 跟单金额：100",
            "🐦‍⬛跟单成功：150",
            "来源后台：LuB000222",
            "🧠单号：25650723094",
            "🗓时间：11-04-2026 00:03:25",
        ):
            self.assertIn(expected, text)

    def test_new_order_text_shows_score_source_odds_and_batch(self):
        members = [
            {
                **MEMBER,
                "side": "AWAY",
                "line": "1.25",
                "odds": "1.09",
            }
        ]
        text = new_order_text(make_group(score="0:1", source_batch_no="79093251-2"), members)
        self.assertIn("大阪 v 熊本深红（0-1）", text)
        self.assertIn("平博：客队 +1/1.5 @1.09", text)
        self.assertIn("单号：79093251-2", text)
        self.assertIn("@0.91", text)

    def test_result_text_success_and_rejected(self):
        success = result_text(make_group())
        self.assertIn("✅ 跟单成功：150", success)
        self.assertIn("🧾 票号：25650723094", success)
        partial = result_text(make_group(status="PARTIAL", accepted_stake="500"))
        self.assertIn("部分成功", partial)
        self.assertIn("500", partial)
        rejected = result_text(
            make_group(status="REJECTED", accepted_stake=None, message="皇冠请求失败")
        )
        self.assertIn("❌ 跟单结果：已拒绝", rejected)
        self.assertIn("皇冠请求失败", rejected)

    def test_prematch_card_escapes_team_names(self):
        text = new_order_text(
            make_group(crown_home="主队 & <客队>", intent={"is_live": False}), [MEMBER]
        )
        self.assertIn("状态：赛前", text)
        self.assertIn("主队 &amp; &lt;客队&gt;", text)

    def test_keyboard_callback_data_fits_telegram_limit(self):
        markup = keyboard(GROUP_ID)
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        self.assertEqual(
            [button["text"] for button in buttons],
            [
                "🎯 跟单结果",
                "🧾 跟单日志",
                "📉 输赢结果",
                "📊 跟单报表",
            ],
        )
        for button in buttons:
            self.assertLessEqual(len(button["callback_data"].encode("utf-8")), 64)
            self.assertIn(GROUP_ID, button["callback_data"])


class CustomEmojiTests(unittest.TestCase):
    def test_theme_uses_telegram_custom_emoji(self):
        theme = emoji_theme({"custom_emoji": True})
        text = new_order_text(make_group(), [MEMBER], theme)
        self.assertIn('<tg-emoji emoji-id="5935873621749668287">🤣</tg-emoji>新注单提醒', text)
        self.assertIn('<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji>', text)
        self.assertNotIn("<tg-emoji", new_order_text(make_group(), [MEMBER]))

        markup = keyboard(GROUP_ID, theme)
        first = markup["inline_keyboard"][0][0]
        self.assertEqual(first["icon_custom_emoji_id"], "5936176756246452306")
        self.assertEqual(first["text"], "跟单结果")
        plain = keyboard(GROUP_ID)["inline_keyboard"][0][0]
        self.assertNotIn("icon_custom_emoji_id", plain)

    def test_theme_is_none_when_disabled(self):
        self.assertIsNone(emoji_theme({"custom_emoji": False}))


class EmojiFallbackTests(unittest.TestCase):
    def test_sync_falls_back_to_plain_emoji(self):
        with tempfile.TemporaryDirectory() as tmp:
            follow_db = Path(tmp) / "follow.sqlite3"
            orders_db = Path(tmp) / "orders.sqlite3"
            store = FollowStore(follow_db)
            group = make_group(status="READY", accepted_stake=None, receipt_id=None)
            with store.connect(write=True) as connection:
                store.save_group(connection, group)
                store.add_member(connection, GROUP_ID, MEMBER)

            service = NotifyService({"follow_db_path": str(follow_db), "db_path": str(orders_db)})
            service.store.save_settings(
                {"enabled": True, "bot_token": TOKEN, "chat_id": "-100", "since": ""}
            )

            opener = SequenceOpener(
                [
                    {
                        "ok": False,
                        "description": "Bad Request: can't parse entities: unsupported custom emoji",
                    },
                    {"ok": True, "result": {"message_id": 21}},
                ]
            )
            with mock.patch("ps38app.notify.build_opener", return_value=opener):
                service.sync()

            self.assertEqual(len(opener.calls), 2)
            self.assertIn("<tg-emoji", opener.calls[0][1]["text"])
            self.assertNotIn("<tg-emoji", opener.calls[1][1]["text"])
            self.assertIn("新注单提醒", opener.calls[1][1]["text"])
            self.assertIn("custom emoji", service.status()["custom_emoji_error"].lower())
            self.assertIsNotNone(service.store.delivery(GROUP_ID))


class NotifyStoreTests(unittest.TestCase):
    def test_settings_and_delivery_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notify.json"
            store = NotifyStore(path)
            settings = store.save_settings(
                {
                    "enabled": True,
                    "bot_token": TOKEN,
                    "chat_id": "-100123",
                    "proxy": "http://127.0.0.1:7890",
                }
            )
            self.assertTrue(settings["enabled"])
            self.assertTrue(settings["since"])
            store.record(GROUP_ID, "-100123", 88, "SUCCEEDED")

            reloaded = NotifyStore(path)
            self.assertEqual(reloaded.settings()["chat_id"], "-100123")
            delivery = reloaded.delivery(GROUP_ID)
            self.assertEqual(delivery["message_id"], 88)
            self.assertEqual(delivery["stage"], "SUCCEEDED")

            reloaded.save_settings({"enabled": False})
            self.assertFalse(reloaded.settings()["enabled"])


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class SequenceOpener:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request.full_url, json.loads(request.data.decode("utf-8"))))
        return FakeResponse(self.payloads.pop(0))


class FakeOpener:
    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {"ok": True, "result": {"message_id": 11}}

    def open(self, request, timeout=None):
        self.calls.append((request.full_url, json.loads(request.data.decode("utf-8"))))
        return FakeResponse(self.payload)


class TelegramAPITests(unittest.TestCase):
    def test_callback_alert_falls_back_when_telegram_rejects_alert(self):
        opener = SequenceOpener(
            [
                {"ok": False, "description": "Bad Request: alert rejected"},
                {"ok": True, "result": True},
            ]
        )
        with mock.patch("ps38app.notify.build_opener", return_value=opener):
            self.assertTrue(TelegramAPI(TOKEN).answer_callback("callback-1", "摘要", True))
        self.assertEqual(len(opener.calls), 2)
        self.assertTrue(opener.calls[0][1]["show_alert"])
        self.assertFalse(opener.calls[1][1]["show_alert"])

    def test_callback_failure_returns_false_without_stopping_polling(self):
        opener = FakeOpener({"ok": False, "description": "Bad Request: query expired"})
        with mock.patch("ps38app.notify.build_opener", return_value=opener):
            self.assertFalse(TelegramAPI(TOKEN).answer_callback("expired", "摘要", True))
        self.assertEqual(len(opener.calls), 2)

    def test_send_and_edit_payloads(self):
        opener = FakeOpener()
        with mock.patch("ps38app.notify.build_opener", return_value=opener):
            api = TelegramAPI(TOKEN)
            result = api.send_message("-100123", "<b>hi</b>", keyboard(GROUP_ID))
            self.assertEqual(result["message_id"], 11)
            api.edit_message("-100123", 11, "<b>hi</b>", keyboard(GROUP_ID))

        send_url, send_payload = opener.calls[0]
        self.assertTrue(send_url.endswith(f"/bot{TOKEN}/sendMessage"))
        self.assertEqual(send_payload["chat_id"], "-100123")
        self.assertEqual(send_payload["parse_mode"], "HTML")
        self.assertEqual(
            send_payload["reply_markup"]["inline_keyboard"][0][0]["text"], "🎯 跟单结果"
        )
        edit_url, edit_payload = opener.calls[1]
        self.assertTrue(edit_url.endswith(f"/bot{TOKEN}/editMessageText"))
        self.assertEqual(edit_payload["message_id"], 11)

    def test_telegram_error_is_reported(self):
        opener = FakeOpener({"ok": False, "description": "Bad Request: chat not found"})
        with mock.patch("ps38app.notify.build_opener", return_value=opener):
            with self.assertRaises(NotifyError) as context:
                TelegramAPI(TOKEN).send_message("-100", "hi")
        self.assertIn("chat not found", str(context.exception))


class CallbackTests(unittest.TestCase):
    def test_missing_split_settlement_does_not_report_all_tickets_settled(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = NotifyService(
                {
                    "follow_db_path": str(Path(tmp) / "follow.sqlite3"),
                    "db_path": str(Path(tmp) / "orders.sqlite3"),
                }
            )
            service._crown_settlement = lambda _account, ticket: (
                {"win_gold": "10", "team_h_show": "主队", "team_c_show": "客队"}
                if ticket == "A"
                else None
            )
            detail = {
                "group": make_group(
                    split_tickets=["A", "B"],
                    receipt_id="A",
                    market_type="TOTAL",
                    target_side="OVER",
                    target_line="2.5",
                )
            }
            text = service._alert_pnl(detail)
            self.assertIn("部分结算（1/2 单）", text)
            self.assertIn("盈利：+10.00", text)
            self.assertIn("玩法：大 2.5", text)
            self.assertNotIn("注单结算成功", text)

    def test_callback_text_reads_follow_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            follow_db = Path(tmp) / "follow.sqlite3"
            orders_db = Path(tmp) / "orders.sqlite3"
            store = FollowStore(follow_db)
            with store.connect(write=True) as connection:
                store.save_group(connection, make_group())
                store.add_member(connection, GROUP_ID, MEMBER)

            service = NotifyService({"follow_db_path": str(follow_db), "db_path": str(orders_db)})
            self.assertIn("🎯 跟单结果", service.callback_text(f"g:{GROUP_ID}:res"))
            self.assertIn("🧾 跟单日志", service.callback_text(f"g:{GROUP_ID}:log"))
            self.assertIn("📊 跟单报表", service.callback_text(f"g:{GROUP_ID}:amt"))
            self.assertIn("📉 输赢结果", service.callback_text(f"g:{GROUP_ID}:pnl"))
            self.assertIn("不存在或已清理", service.callback_text(f"g:{'b' * 32}:res"))
            self.assertIn("按钮已过期", service.callback_text("bad-data"))

            for action, marker in (
                ("res", "📋 跟单详细"),
                ("log", "🧾 跟单日志"),
                ("pnl", "📉 输赢结果"),
                ("amt", "📈 输赢报表"),
            ):
                with self.subTest(action=action):
                    alert = service.alert_text(f"g:{GROUP_ID}:{action}")
                    self.assertIn(marker, alert)
                    self.assertLessEqual(len(alert), 200)
                    self.assertNotIn("<", alert)

            service._crown_settlement = lambda account, ticket: {
                "league": "智利杯",
                "team_h_show": "智利大学",
                "team_c_show": "安图法加斯塔",
                "ioratio": "0.91",
                "win_gold": "112.7",
                "result_data": "4 - 0",
            }
            settlement = service.alert_text(f"g:{GROUP_ID}:pnl")
            self.assertIn("注单结算成功", settlement)
            self.assertIn("智利杯", settlement)
            self.assertIn("+112.70", settlement)
            self.assertLessEqual(len(settlement), 200)

            split_calls = {"A": "100", "B": "50"}
            service._detail = lambda _gid: {
                "group": {
                    "split_tickets": ["A", "B"],
                    "account_id": "acct",
                    "target_side": "HOME",
                    "target_line": "-1",
                    "crown_home": "主队",
                    "crown_away": "客队",
                    "market_type": "HANDICAP",
                    "period": "FT",
                },
                "members": [],
            }
            service._crown_settlement = lambda _account, ticket: {
                "league": "测试杯",
                "team_h_show": "主队",
                "team_c_show": "客队",
                "ioratio": "0.9",
                "win_gold": split_calls.get(ticket, ""),
                "result_data": "2 - 0",
            }
            split_text = service.alert_text(f"g:{GROUP_ID}:pnl")
            self.assertIn("+150.00", split_text)
            self.assertNotIn("回执", split_text)
            self.assertLessEqual(len(split_text), 200)


class ServiceSyncTests(unittest.TestCase):
    def test_sync_sends_once_and_edits_on_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            follow_db = Path(tmp) / "follow.sqlite3"
            orders_db = Path(tmp) / "orders.sqlite3"
            store = FollowStore(follow_db)
            group = make_group(status="READY", accepted_stake=None, receipt_id=None)
            with store.connect(write=True) as connection:
                store.save_group(connection, group)
                store.add_member(connection, GROUP_ID, MEMBER)

            service = NotifyService({"follow_db_path": str(follow_db), "db_path": str(orders_db)})
            service.store.save_settings(
                {
                    "enabled": True,
                    "bot_token": TOKEN,
                    "chat_id": "-100",
                    "custom_emoji": False,
                    "since": "",
                }
            )

            opener = FakeOpener()
            with mock.patch("ps38app.notify.build_opener", return_value=opener):
                service.sync()
                self.assertEqual(len(opener.calls), 1)
                url, payload = opener.calls[0]
                self.assertTrue(url.endswith("/sendMessage"))
                self.assertIn("🤣新注单提醒", payload["text"])
                self.assertIn("跟单金额：100", payload["text"])

                service.sync()
                self.assertEqual(len(opener.calls), 1)

                with store.connect(write=True) as connection:
                    current = store.get_group(connection, GROUP_ID)
                    current.update(
                        status="SUCCEEDED",
                        accepted_stake="150",
                        receipt_id="R-1",
                        message="平台已确认成功",
                        updated_at=NOW,
                    )
                    store.save_group(connection, current)

                opener.calls.clear()
                service.sync()
                self.assertEqual(len(opener.calls), 1)
                url, payload = opener.calls[0]
                self.assertTrue(url.endswith("/editMessageText"))
                self.assertIn("跟单成功：150", payload["text"])
                self.assertNotIn("票号", payload["text"])


class NotifyServiceLifecycleTests(unittest.TestCase):
    def test_stop_timeout_keeps_worker_until_it_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = NotifyService(
                {
                    "follow_db_path": str(Path(tmp) / "follow.sqlite3"),
                    "db_path": str(Path(tmp) / "orders.sqlite3"),
                }
            )
            entered, release = threading.Event(), threading.Event()

            def blocked_loop():
                entered.set()
                release.wait(5)

            with mock.patch.object(service, "_loop", side_effect=blocked_loop) as loop:
                service.start()
                thread = service._thread
                try:
                    self.assertTrue(entered.wait(2))
                    with mock.patch.object(thread, "join"):
                        service.stop()
                    service.start()
                    self.assertIs(service._thread, thread)
                    self.assertTrue(service._stop.is_set())
                finally:
                    release.set()
                    thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(loop.call_count, 1)
                service.stop()
                self.assertIsNone(service._thread)


class NotifyServiceSettingsTests(unittest.TestCase):
    def test_invalid_token_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = NotifyService(
                {
                    "follow_db_path": str(Path(tmp) / "follow.sqlite3"),
                    "db_path": str(Path(tmp) / "orders.sqlite3"),
                }
            )
            with self.assertRaises(NotifyError):
                service.save_settings({"bot_token": "not-a-token"})

    def test_enable_requires_token_and_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = NotifyService(
                {
                    "follow_db_path": str(Path(tmp) / "follow.sqlite3"),
                    "db_path": str(Path(tmp) / "orders.sqlite3"),
                }
            )
            with self.assertRaises(NotifyError):
                service.save_settings({"enabled": True})

    def test_socks_proxy_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = NotifyService(
                {
                    "follow_db_path": str(Path(tmp) / "follow.sqlite3"),
                    "db_path": str(Path(tmp) / "orders.sqlite3"),
                }
            )
            with self.assertRaises(NotifyError):
                service.save_settings({"proxy": "socks5://127.0.0.1:7890"})


if __name__ == "__main__":
    unittest.main()
