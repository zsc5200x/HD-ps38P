"""Telegram 推送：新注单提醒、跟单结果更新与按钮回调。

实现只依赖标准库：通过 Telegram Bot HTTP API 发送消息，用 getUpdates
轮询按钮回调。配置和已送达消息保存在 ``data/notify.json``，机器人停用或
未配置时不会发起任何网络请求。
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .follow.store import FollowStore

BEIJING = timezone(timedelta(hours=8))
DIVIDER = "——————————"
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
MASK = "••••••••"

DEFAULT_SETTINGS = {
    "enabled": False,
    "bot_token": "",
    "chat_id": "",
    "proxy": "",
    "push_result": True,
    "custom_emoji": True,
    "since": "",
}

# 旧系统使用的 Telegram 自定义表情（Premium emoji）ID。
DEFAULT_TEXT_EMOJI = {
    "title": "5935873621749668287",
    "title_suffix": "5936235940895792238",
    "league": "5861874741148653288",
    "match": "5933996313019423929",
    "status": "5936176756246452306",
    "play": "",
    "stake": "5224257782013769471",
    "follow_stake": "5298682684715840924",
    "follow_success": "5298731965170597296",
    "follow_success_suffix": "5933605531125029360",
    "source": "",
    "order_id": "6034918793115868355",
    "time": "5929427322514971930",
}
DEFAULT_BUTTON_EMOJI = {
    "res": "5936176756246452306",
    "log": "5861874741148653288",
    "pnl": "5936235940895792238",
    "amt": "5224257782013769471",
}
DEFAULT_DIVIDER_EMOJI = [
    "5391112257826467406",
    "5391074908790860063",
    "5390871726772987721",
    "5390965687772525012",
    "5393367042642424178",
    "5391165051564469669",
    "5395659850803852003",
    "5339336414188285500",
    "5285308684837072423",
    "5321007327719992618",
]

STATUS_LABELS = {
    "SUCCEEDED": "跟单成功",
    "REJECTED": "已拒绝",
    "SKIPPED": "已跳过",
    "EXPIRED": "已过期",
    "UNKNOWN": "待核对",
    "PARTIAL": "部分成功",
    "FAILED": "失败",
    "GROUPING": "合组中",
    "WAIT_MAPPING": "等待赛事匹配",
    "READY": "等待执行",
    "WAIT_ACCOUNT": "等待账号",
    "WAIT_MARKET": "等待盘口",
    "PREPARING": "核验盘口中",
    "PREPARED": "已匹配盘口",
    "RETRY_WAIT": "等待重试",
    "SUBMITTING": "提交中",
    "LATE_ORDER": "迟到待核对",
}
TERMINAL = {"SUCCEEDED", "REJECTED", "SKIPPED", "EXPIRED", "FAILED", "UNKNOWN", "PARTIAL"}
MARKETS = {"HANDICAP": "让球", "TOTAL": "大小"}
PERIODS = {"FT": "全场", "HT": "上半场"}
SIDES = {"HOME": "主队", "AWAY": "客队", "OVER": "大", "UNDER": "小"}


class NotifyError(RuntimeError):
    """推送配置或 Telegram 调用失败。"""


def _now_iso() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed


def _display_time(value: Any) -> str:
    parsed = _parse_time(value)
    return parsed.strftime("%m-%d %H:%M:%S") if parsed else "—"


def _amount(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number == int(number):
        return f"{int(number):,}"
    return f"{number:,.2f}"


def _line_text(value: Any) -> str:
    try:
        number = abs(float(value))
    except (TypeError, ValueError):
        return ""
    whole = int(number)
    fraction = round(number - whole, 2)
    if fraction == 0:
        return str(whole)
    if fraction == 0.5:
        return f"{whole}.5"
    if fraction == 0.25:
        return f"{whole}/{whole}.5"
    if fraction == 0.75:
        return f"{whole}.5/{whole + 1}"
    return str(number)


def _signed_line(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    text = _line_text(number)
    if number > 0:
        return f"+{text}"
    if number < 0:
        return f"-{text}"
    return "0"


def _selection(row: Dict[str, Any]) -> str:
    side = str(row.get("target_side") or row.get("source_side") or "")
    line = row.get("target_line") if row.get("target_line") is not None else row.get("source_line")
    market = str(row.get("market_type") or "")
    if not side:
        return "待匹配盘口"
    if market == "TOTAL":
        return f"{SIDES.get(side, side)} {_line_text(line)}"
    return f"{SIDES.get(side, side)} {_signed_line(line)}"


def _teams(row: Dict[str, Any]) -> str:
    home = row.get("crown_home") or row.get("home") or "待匹配主队"
    away = row.get("crown_away") or row.get("away") or "待匹配客队"
    return f"{home} v {away}"


def _agent_and_order(members: List[Dict[str, Any]]) -> Tuple[str, str]:
    agent, order_id = "—", "—"
    if members:
        member = members[0]
        order_id = str(member.get("order_id") or "—")
        identity = str(member.get("member_identity") or "")
        code = re.search(r"([A-Z]{2,}\d+[A-Z0-9]*)", identity)
        if code:
            raw = code.group(1)
            agent = raw[:7] if len(raw) > 7 else raw
        elif identity:
            agent = identity.split(":")[-1][:12]
    return agent, order_id


def emoji_theme(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """返回自定义表情主题；关闭时返回 None，走普通 emoji。"""
    if not settings.get("custom_emoji", True):
        return None
    return {
        "text": dict(DEFAULT_TEXT_EMOJI),
        "buttons": dict(DEFAULT_BUTTON_EMOJI),
        "divider": list(DEFAULT_DIVIDER_EMOJI),
    }


def _tag(emoji_id: Any, fallback: str) -> str:
    emoji_id = str(emoji_id or "").strip()
    if not emoji_id:
        return ""
    return f'<tg-emoji emoji-id="{html.escape(emoji_id, quote=True)}">{fallback}</tg-emoji>'


def _icon(theme: Optional[Dict[str, Any]], key: str, fallback: str) -> str:
    if not theme:
        return fallback
    emoji_id = (theme.get("text") or {}).get(key)
    if not emoji_id:
        return ""
    return _tag(emoji_id, fallback)


def _divider(theme: Optional[Dict[str, Any]]) -> str:
    if not theme:
        return DIVIDER
    tags = [_tag(emoji_id, "🏁") for emoji_id in (theme.get("divider") or [])]
    return "".join(tags) or DIVIDER


def _line(icon_html: str, text: str) -> str:
    return f"{icon_html} {text}" if icon_html else text


def new_order_text(
    row: Dict[str, Any],
    members: List[Dict[str, Any]],
    theme: Optional[Dict[str, Any]] = None,
) -> str:
    """新注单提醒，版式与旧系统推送保持一致。"""
    agent, order_id = _agent_and_order(members)
    success = row.get("accepted_stake")

    title = f"{_icon(theme, 'title', '🎯')} 新注单提醒"
    suffix = _icon(theme, "title_suffix", "🎯")
    if suffix:
        title = f"{title} {suffix}"

    success_icon = _icon(theme, "follow_success", "✅")
    success_line = f"{success_icon} 跟单成功：{_amount(success) if success is not None else '0'}"
    success_suffix = _icon(theme, "follow_success_suffix", "🎉") if theme else ""
    if success_suffix:
        success_line = f"{success_line} {success_suffix}"

    lines = [
        f"<b>{title}</b>",
        _divider(theme),
        _line(_icon(theme, "league", "🏆"), html.escape(str(row.get("league") or "未知联赛"))),
        _line(_icon(theme, "match", "⚽"), html.escape(_teams(row))),
        "",
        _line(_icon(theme, "status", "📍"), "状态：滚球"),
        _line(
            _icon(theme, "play", "🎯"),
            f"玩法：{MARKETS.get(str(row.get('market_type')), str(row.get('market_type') or '—'))}",
        ),
        _line(
            _icon(theme, "play", "⏱"),
            f"时段：{PERIODS.get(str(row.get('period')), str(row.get('period') or '—'))}",
        ),
        _line(_icon(theme, "play", "🎲"), f"选择：{html.escape(_selection(row))}"),
        "",
        _line(_icon(theme, "stake", "💰"), f"原注金额：{_amount(row.get('source_stake'))}"),
        _line(_icon(theme, "follow_stake", "💵"), f"跟单金额：{_amount(row.get('target_stake'))}"),
        success_line,
        "",
        _line(_icon(theme, "source", "🏦"), f"来源后台：{html.escape(agent)}"),
        _line(_icon(theme, "order_id", "🧠"), f"单号：{html.escape(order_id)}"),
        _line(
            _icon(theme, "time", "🗓"),
            f"时间：{_display_time(row.get('source_created_at') or row.get('created_at'))}",
        ),
    ]
    return "\n".join(lines)


def result_text(row: Dict[str, Any], theme: Optional[Dict[str, Any]] = None) -> str:
    status = str(row.get("status") or "")
    if status == "SUCCEEDED":
        lines = [
            _line(
                _icon(theme, "follow_success", "✅"),
                "跟单成功：" + _amount(row.get("accepted_stake")),
            )
        ]
        if row.get("accepted_odds") is not None:
            lines.append(f"🎚 成交水位：{row.get('accepted_odds')}")
        if row.get("receipt_id"):
            lines.append(f"🧾 票号：{html.escape(str(row.get('receipt_id')))}")
        return "\n".join(lines)
    label = STATUS_LABELS.get(status, status or "未知")
    if status == "PARTIAL":
        return f"⚠️ 跟单结果：部分成功（成交 {_amount(row.get('accepted_stake'))}）"
    lines = [f"❌ 跟单结果：{label}"]
    if row.get("message"):
        lines.append(f"📝 {html.escape(str(row.get('message'))[:120])}")
    return "\n".join(lines)


def keyboard(group_id: str, theme: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    specs = [
        ("res", "🎯 跟单结果"),
        ("log", "🧾 跟单日志"),
        ("pnl", "📉 输赢结果"),
        ("amt", "📊 跟单报表"),
    ]
    buttons: List[Dict[str, str]] = []
    for action, text in specs:
        button = {"text": text, "callback_data": f"g:{group_id}:{action}"}
        emoji_id = ((theme or {}).get("buttons") or {}).get(action)
        if emoji_id:
            button["icon_custom_emoji_id"] = str(emoji_id)
        buttons.append(button)
    return {"inline_keyboard": [buttons[0:2], buttons[2:4]]}


class NotifyStore:
    """``data/notify.json`` 的读写；配置与已送达消息共用一把锁。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            raw = {}
        settings = dict(DEFAULT_SETTINGS)
        if isinstance(raw.get("settings"), dict):
            settings.update(
                {key: raw["settings"][key] for key in DEFAULT_SETTINGS if key in raw["settings"]}
            )
        deliveries = raw.get("deliveries") if isinstance(raw.get("deliveries"), dict) else {}
        return {"settings": settings, "deliveries": deliveries}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def settings(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data["settings"])

    def save_settings(self, values: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            current = self._data["settings"]
            if values.get("enabled") and not current.get("enabled"):
                current["since"] = _now_iso()
            for key in DEFAULT_SETTINGS:
                if key in values:
                    current[key] = values[key]
            self._write()
            return dict(current)

    def delivery(self, group_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            value = self._data["deliveries"].get(group_id)
            return dict(value) if isinstance(value, dict) else None

    def record(self, group_id: str, chat_id: str, message_id: int, stage: str) -> None:
        with self._lock:
            self._data["deliveries"][group_id] = {
                "chat_id": str(chat_id),
                "message_id": int(message_id),
                "stage": stage,
                "at": _now_iso(),
            }
            if len(self._data["deliveries"]) > 500:
                ordered = sorted(
                    self._data["deliveries"].items(),
                    key=lambda item: str(item[1].get("at") or ""),
                )
                for key, _ in ordered[: len(ordered) - 500]:
                    self._data["deliveries"].pop(key, None)
            self._write()


class TelegramAPI:
    def __init__(self, token: str, proxy: str = "", timeout: int = 15):
        self.token = token
        self.timeout = timeout
        handlers = [ProxyHandler({"http": proxy, "https": proxy})] if proxy else [ProxyHandler({})]
        self._opener = build_opener(*handlers)

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8"))
                message = str(detail.get("description") or f"HTTP {exc.code}")
            except Exception:  # noqa: BLE001 - 尽量保留原始错误
                message = f"Telegram HTTP {exc.code}"
            raise NotifyError(message) from exc
        except (URLError, OSError) as exc:
            raise NotifyError(f"网络错误：{exc}") from exc
        except ValueError as exc:
            raise NotifyError("Telegram 返回了无法解析的响应") from exc
        if not result.get("ok"):
            raise NotifyError(str(result.get("description") or "Telegram 调用失败"))
        return result.get("result")

    def send_message(
        self, chat_id: str, text: str, markup: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if markup:
            payload["reply_markup"] = markup
        return self.call("sendMessage", payload)

    def edit_message(
        self, chat_id: str, message_id: int, text: str, markup: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if markup:
            payload["reply_markup"] = markup
        return self.call("editMessageText", payload)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
        except NotifyError:
            pass

    def get_updates(self, offset: int) -> List[Dict[str, Any]]:
        result = self.call(
            "getUpdates",
            {"offset": offset, "timeout": 0, "limit": 20, "allowed_updates": ["callback_query"]},
        )
        return result if isinstance(result, list) else []

    def get_me(self) -> Dict[str, Any]:
        result = self.call("getMe", {})
        return result if isinstance(result, dict) else {}


class NotifyService:
    """后台推送服务：跟随跟单库轮询，发送/更新消息并处理按钮回调。"""

    def __init__(self, config: Dict[str, Any]):
        self.follow_db = Path(config["follow_db_path"])
        self.orders_db = Path(config["db_path"])
        self.store = NotifyStore(self.follow_db.with_name("notify.json"))
        self.follow = FollowStore(self.follow_db)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._api: Optional[TelegramAPI] = None
        self._api_key: Tuple[str, str] = ("", "")
        self._offset = 0
        self._state: Dict[str, Any] = {
            "bot_username": "",
            "last_error": "",
            "last_sent_at": "",
            "sent_count": 0,
            "custom_emoji_error": "",
        }

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="notify", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lock:
            if self._thread is thread and thread and not thread.is_alive():
                self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                settings = self.store.settings()
                if self._ready(settings):
                    self.sync()
                    self.poll_callbacks()
                else:
                    self._api = None
            except Exception as exc:  # noqa: BLE001 - 推送失败不影响跟单
                self._set_error(str(exc))
            self._stop.wait(2.5)

    # ── settings / status ────────────────────────────────────────────────
    @staticmethod
    def _ready(settings: Dict[str, Any]) -> bool:
        return bool(
            settings.get("enabled")
            and TOKEN_RE.match(str(settings.get("bot_token") or ""))
            and str(settings.get("chat_id") or "").strip()
        )

    def status(self) -> Dict[str, Any]:
        settings = self.store.settings()
        with self._lock:
            state = dict(self._state)
        token = str(settings.get("bot_token") or "")
        configured = bool(TOKEN_RE.match(token) and str(settings.get("chat_id") or "").strip())
        return {
            "enabled": bool(settings.get("enabled")),
            "configured": configured,
            "has_token": bool(token),
            "token_hint": f"{MASK}{token[-4:]}" if token else "",
            "chat_id": str(settings.get("chat_id") or ""),
            "proxy": str(settings.get("proxy") or ""),
            "push_result": bool(settings.get("push_result", True)),
            "bot_username": state.get("bot_username", ""),
            "last_error": state.get("last_error", ""),
            "last_sent_at": state.get("last_sent_at", ""),
            "sent_count": state.get("sent_count", 0),
            "custom_emoji": bool(settings.get("custom_emoji", True)),
            "custom_emoji_error": state.get("custom_emoji_error", ""),
        }

    def save_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        values: Dict[str, Any] = {}
        if "enabled" in payload:
            values["enabled"] = bool(payload["enabled"])
        if "push_result" in payload:
            values["push_result"] = bool(payload["push_result"])
        if "custom_emoji" in payload:
            values["custom_emoji"] = bool(payload["custom_emoji"])
        if "chat_id" in payload:
            values["chat_id"] = str(payload.get("chat_id") or "").strip()[:64]
        if "proxy" in payload:
            proxy = str(payload.get("proxy") or "").strip()
            if proxy and not re.match(r"^https?://", proxy, re.I):
                raise NotifyError("代理仅支持 http:// 或 https:// 地址")
            values["proxy"] = proxy[:200]
        if "bot_token" in payload:
            token = str(payload.get("bot_token") or "").strip()
            if token and not TOKEN_RE.match(token):
                raise NotifyError("BOT TOKEN 格式不正确（形如 123456789:AA...）")
            if token:
                values["bot_token"] = token
        preview = {**self.store.settings(), **values}
        if preview.get("enabled") and not self._ready(preview):
            raise NotifyError("启用推送前请先填写 BOT TOKEN 和 CHAT ID")
        settings = self.store.save_settings(values)
        self._api = None
        if self._ready(settings):
            self._thread and self._thread.is_alive() or self.start()
        self._reset_state_error()
        return settings

    def clear(self) -> Dict[str, Any]:
        self.store.save_settings({"enabled": False, "bot_token": "", "chat_id": "", "proxy": ""})
        self._api = None
        return self.status()

    def _reset_state_error(self) -> None:
        with self._lock:
            self._state["last_error"] = ""
            self._state["custom_emoji_error"] = ""

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._state["last_error"] = message[:200]

    # ── Telegram helpers ─────────────────────────────────────────────────
    def _client(self, settings: Optional[Dict[str, Any]] = None) -> TelegramAPI:
        settings = settings or self.store.settings()
        token = str(settings.get("bot_token") or "")
        proxy = str(settings.get("proxy") or "")
        key = (token, proxy)
        with self._lock:
            if self._api is None or self._api_key != key:
                self._api = TelegramAPI(token, proxy)
                self._api_key = key
            return self._api

    def test(self) -> str:
        settings = self.store.settings()
        if not TOKEN_RE.match(str(settings.get("bot_token") or "")):
            raise NotifyError("请先填写 BOT TOKEN")
        if not str(settings.get("chat_id") or "").strip():
            raise NotifyError("请先填写 CHAT ID")
        api = self._client(settings)
        me = api.get_me()
        username = str(me.get("username") or "")
        with self._lock:
            self._state["bot_username"] = username
        api.send_message(
            str(settings["chat_id"]),
            "<b>🎯 跟单推送机器人已连接</b>\n"
            + DIVIDER
            + "\n✅ 测试消息发送成功\n🏦 来源：HD PS38 Sports",
        )
        return username or "机器人"

    def sync(self) -> None:
        settings = self.store.settings()
        if not self._ready(settings):
            return
        api = self._client(settings)
        chat_id = str(settings["chat_id"])
        theme = emoji_theme(settings)
        since = _parse_time(settings.get("since"))
        rows = self.follow.snapshot("ALL", limit=200).get("rows") or []
        sent = 0
        for row in reversed(rows):
            group_id = str(row.get("id") or "")
            if not group_id or row.get("target_stake") in (None, "", 0, "0"):
                continue
            created = _parse_time(row.get("created_at"))
            if since and created and created < since:
                continue
            if str(row.get("status")) in {"SKIPPED", "EXPIRED"} and not row.get("accepted_stake"):
                continue
            delivery = self.store.delivery(group_id)
            detail = self._detail(group_id)
            members = detail.get("members") if detail else []
            if delivery is None:
                message = self._send_new_order(api, chat_id, row, members, group_id, theme)
                self.store.record(
                    group_id,
                    chat_id,
                    int(message.get("message_id") or 0),
                    str(row.get("status") or ""),
                )
                sent += 1
                self._touch_sent()
                if sent >= 15:
                    break
                time.sleep(0.2)
                continue
            if settings.get("push_result") and str(row.get("status")) in TERMINAL:
                if delivery.get("stage") == row.get("status"):
                    continue
                self._edit_result(api, chat_id, row, members, group_id, delivery, theme)

    def _send_new_order(self, api, chat_id, row, members, group_id, theme):
        try:
            return api.send_message(
                chat_id,
                new_order_text(row, members or [], theme),
                keyboard(group_id, theme),
            )
        except NotifyError as exc:
            if not (theme and self._is_emoji_error(exc)):
                raise
            self._note_emoji_error(exc)
            return api.send_message(
                chat_id,
                new_order_text(row, members or [], None),
                keyboard(group_id, None),
            )

    def _edit_result(self, api, chat_id, row, members, group_id, delivery, theme):
        message_id = int(delivery.get("message_id") or 0)
        target_chat = delivery.get("chat_id") or chat_id
        try:
            api.edit_message(
                target_chat,
                message_id,
                new_order_text(row, members or [], theme)
                + "\n\n"
                + _divider(theme)
                + "\n"
                + result_text(row, theme),
                keyboard(group_id, theme),
            )
        except NotifyError as exc:
            text = str(exc)
            if theme and self._is_emoji_error(exc):
                self._note_emoji_error(exc)
                api.edit_message(
                    target_chat,
                    message_id,
                    new_order_text(row, members or [], None)
                    + "\n\n"
                    + DIVIDER
                    + "\n"
                    + result_text(row, None),
                    keyboard(group_id, None),
                )
            elif "message is not modified" in text:
                pass
            elif "message to edit not found" in text:
                self.store.record(group_id, chat_id, 0, "")
                raise
            else:
                raise
        self.store.record(group_id, target_chat, message_id, str(row.get("status") or ""))

    @staticmethod
    def _is_emoji_error(error: Exception) -> bool:
        message = str(error).lower()
        return any(
            key in message
            for key in ("custom emoji", "can't parse entities", "unsupported", "button")
        )

    def _note_emoji_error(self, error: Exception) -> None:
        with self._lock:
            self._state["custom_emoji_error"] = str(error)[:160]

    def _touch_sent(self) -> None:
        with self._lock:
            self._state["last_sent_at"] = _now_iso()
            self._state["sent_count"] = int(self._state.get("sent_count") or 0) + 1

    def poll_callbacks(self) -> None:
        settings = self.store.settings()
        if not self._ready(settings):
            return
        api = self._client(settings)
        updates = api.get_updates(self._offset)
        for update in updates:
            self._offset = int(update.get("update_id") or 0) + 1
            query = update.get("callback_query") or {}
            data = str(query.get("data") or "")
            chat = ((query.get("message") or {}).get("chat") or {}).get("id")
            api.answer_callback(str(query.get("id") or ""), "已收到")
            text = self.callback_text(data)
            if text and chat:
                api.send_message(str(chat), text)

    def callback_text(self, data: str) -> str:
        match = re.match(r"^g:([0-9a-f]{32}):(res|log|pnl|amt)$", data)
        if not match:
            return "按钮已过期，请重新发送指令。"
        group_id, action = match.group(1), match.group(2)
        detail = self._detail(group_id)
        if not detail:
            return "跟单记录不存在或已清理。"
        row = detail.get("group") or {}
        if action == "res":
            return "<b>🎯 跟单结果</b>\n" + DIVIDER + "\n" + result_text(row)
        if action == "log":
            return self._log_text(detail)
        if action == "pnl":
            return self._pnl_text(detail)
        return self._report_text()

    def _detail(self, group_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self.follow.detail(group_id)
        except (KeyError, ValueError, sqlite3.Error):
            return None

    def _log_text(self, detail: Dict[str, Any]) -> str:
        history = detail.get("history") or []
        lines = ["<b>🧾 跟单日志</b>", DIVIDER]
        if not history:
            lines.append("暂无状态记录。")
        for item in history[-10:]:
            at = _display_time(item.get("at"))
            status = STATUS_LABELS.get(
                str(item.get("to_status") or ""), str(item.get("to_status") or "")
            )
            message = str(item.get("message") or "").strip()
            lines.append(
                f"🕒 {at} · {status}" + (f"\n📝 {html.escape(message[:100])}" if message else "")
            )
        return "\n".join(lines)

    def _pnl_text(self, detail: Dict[str, Any]) -> str:
        members = detail.get("members") or []
        orders = [str(member.get("order_id")) for member in members if member.get("order_id")]
        if not orders:
            return "<b>📉 输赢结果</b>\n" + DIVIDER + "\n暂无来源订单。"
        placeholders = ",".join("?" for _ in orders)
        lines = ["<b>📉 输赢结果</b>", DIVIDER]
        try:
            uri = self.orders_db.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=3)
            try:
                rows = connection.execute(
                    f"SELECT order_id,teams,wager_status,result,win_loss,stake,currency FROM partner_orders "
                    f"WHERE order_id IN ({placeholders}) ORDER BY created_at DESC",
                    orders,
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            return (
                "<b>📉 输赢结果</b>\n" + DIVIDER + f"\n读取订单库失败：{html.escape(str(exc)[:80])}"
            )
        if not rows:
            lines.append("订单库暂未查到结算结果。")
        for order_id, teams, wager_status, result, win_loss, stake, currency in rows[:5]:
            amount = _amount(win_loss) if win_loss is not None else "—"
            lines.append(
                f"🏟 {html.escape(str(teams or '—'))}\n"
                f"🧾 {html.escape(str(order_id))} · {html.escape(str(wager_status or result or '等待结算'))}\n"
                f"💵 本金 {_amount(stake)} {html.escape(str(currency or ''))} · 输赢 {amount}"
            )
        return "\n".join(lines)

    def _report_text(self) -> str:
        data = self.follow.snapshot("ALL", limit=1)
        summary = data.get("summary") or {}
        chunks = [
            "<b>📊 跟单报表</b>",
            DIVIDER,
            f"✅ 成功 {int(summary.get('succeeded') or 0)}",
            f"⏳ 处理中 {int(summary.get('processing') or 0)}",
            f"❌ 未完成 {int(summary.get('failed') or 0)}",
            f"❓ 待核对 {int(summary.get('unknown') or 0)}",
        ]
        stake = summary.get("confirmed_stake")
        if stake not in (None, "", "0"):
            chunks.append(f"💰 成功金额 {_amount(stake)} {summary.get('currency') or ''}".strip())
        return "\n".join(chunks)
