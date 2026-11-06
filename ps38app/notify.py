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
from contextlib import closing
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


def _plain(value: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(value or ""))).strip()


def _clip(text: str, limit: int = 180) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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


_ORDER_DIVIDER_FALLBACKS = ("🇪🇸", "🇪🇸", "🇪🇸", "🏴", "🏴", "🏴", "🏴", "🇩🇪", "🇫🇷", "🇩🇪")


def _divider(theme: Optional[Dict[str, Any]], limit: Optional[int] = None) -> str:
    if not theme:
        return DIVIDER
    ids = list(theme.get("divider") or [])
    if limit:
        ids = ids[:limit]
    tags = []
    for index, emoji_id in enumerate(ids):
        fallback = (
            _ORDER_DIVIDER_FALLBACKS[index] if index < len(_ORDER_DIVIDER_FALLBACKS) else "🏁"
        )
        tags.append(_tag(emoji_id, fallback))
    return "".join(tags) or DIVIDER


def _line(icon_html: str, text: str, spaced: bool = True) -> str:
    if not icon_html:
        return text
    return f"{icon_html}{' ' if spaced else ''}{text}"


def _source_pick(members: List[Dict[str, Any]]) -> str:
    """平博原单方向、盘口线与水位；多单水位不同时用 ≈。"""
    items = [item for item in members if item.get("side")]
    if not items:
        return ""
    first = items[0]
    side = SIDES.get(str(first.get("side") or ""), str(first.get("side") or ""))
    line = _signed_line(first.get("line")) if first.get("line") is not None else ""
    odds_values = sorted(
        {str(item.get("odds")).strip() for item in items if item.get("odds") not in (None, "")}
    )
    odds = ""
    if odds_values:
        odds = f"@{odds_values[0]}" if len(odds_values) == 1 else f"@≈{odds_values[0]}"
    text = f"{side} {line}".strip()
    if odds:
        text = f"{text} {odds}".strip()
    if len(items) > 1 and any(
        (item.get("side"), str(item.get("line"))) != (first.get("side"), str(first.get("line")))
        for item in items[1:]
    ):
        text += f" 等{len(items)}单"
    return text


def new_order_text(
    row: Dict[str, Any],
    members: List[Dict[str, Any]],
    theme: Optional[Dict[str, Any]] = None,
) -> str:
    """旧版紧凑模板：比分、跟单水位、平博水位、系统合并单号。"""
    agent, order_id = _agent_and_order(members)
    success = row.get("accepted_stake")
    score = str(row.get("score") or (row.get("intent") or {}).get("score") or "").strip()
    if score:
        score = re.sub(r"(\d+)\s*[-:：]\s*(\d+)", r"\1-\2", score)
    teams = html.escape(_teams(row)) + (f"（{html.escape(score)}）" if score else "")
    is_live = (row.get("intent") or {}).get("is_live", True)

    follow_odds = (
        row.get("accepted_odds") or row.get("odds") or (row.get("quote") or {}).get("odds") or ""
    )
    selection = _selection(row)
    if follow_odds not in (None, ""):
        selection = f"{selection} @{follow_odds}"

    source_pick = _source_pick(members)
    batch_no = str(row.get("source_batch_no") or "").strip()

    divider = _divider(theme, 10) if theme else ""
    lines = [
        f"<b>{_icon(theme, 'title', '🤣')}新注单提醒</b>",
        *([divider] if divider else []),
        _line(
            _icon(theme, "league", "🤣"),
            html.escape(str(row.get("league") or "未知联赛")),
            spaced=False,
        ),
        _line(_icon(theme, "match", "🤣"), teams, spaced=False),
        _line(_icon(theme, "status", "📍"), f"状态：{'滚球' if is_live else '赛前'}", spaced=True),
        f"玩法：{MARKETS.get(str(row.get('market_type')), str(row.get('market_type') or '—'))}",
        f"时段：{PERIODS.get(str(row.get('period')), str(row.get('period') or '—'))}",
        f"选择：{html.escape(selection)}",
    ]
    if source_pick:
        lines.append(f"平博：{html.escape(source_pick)}")
    lines.extend(
        [
            _line(
                _icon(theme, "stake", "💰"),
                f"原注金额：{_amount(row.get('source_stake'))}",
                spaced=True,
            ),
            _line(
                _icon(theme, "follow_stake", "💲"),
                f"跟单金额：{_amount(row.get('target_stake'))}",
                spaced=True,
            ),
            _line(
                _icon(theme, "follow_success", "🐦‍⬛"),
                f"跟单成功：{_amount(success) if success is not None else '0'}",
                spaced=False,
            ),
            _line(_icon(theme, "source", ""), f"来源后台：{html.escape(agent)}", spaced=False),
            _line(
                _icon(theme, "order_id", "🧠"),
                f"单号：{html.escape(batch_no or order_id)}",
                spaced=False,
            ),
            _line(
                _icon(theme, "time", "🗓"),
                f"时间：{_card_time(row.get('source_created_at') or row.get('created_at'))}",
                spaced=False,
            ),
        ]
    )
    return "\n".join(lines)


def _card_time(value: Any) -> str:
    parsed = _parse_time(value)
    return parsed.strftime("%m-%d-%Y %H:%M:%S") if parsed else "—"


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
        ("res", "跟单结果", "primary", "🎯"),
        ("log", "跟单日志", "primary", "🧾"),
        ("pnl", "输赢结果", "danger", "📉"),
        ("amt", "跟单报表", "success", "📊"),
    ]
    buttons: List[Dict[str, str]] = []
    for action, text, style, native in specs:
        emoji_id = ((theme or {}).get("buttons") or {}).get(action)
        button = {
            # 有自定义表情图标时不再放原生 emoji，避免双图标。
            "text": text if emoji_id else f"{native} {text}",
            "style": style,
            "callback_data": f"g:{group_id}:{action}",
        }
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

    def answer_callback(self, callback_id: str, text: str = "", show_alert: bool = False) -> bool:
        payload = {
            "callback_query_id": callback_id,
            "text": str(text or "")[:200],
            "show_alert": bool(show_alert),
        }
        try:
            self.call("answerCallbackQuery", payload)
            return True
        except NotifyError:
            if show_alert:
                try:
                    self.call(
                        "answerCallbackQuery",
                        {**payload, "text": str(text or "")[:120], "show_alert": False},
                    )
                    return True
                except NotifyError:
                    pass
            return False

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
        self.config = dict(config)
        self.follow_db = Path(config["follow_db_path"])
        self.orders_db = Path(config["db_path"])
        self.store = NotifyStore(self.follow_db.with_name("notify.json"))
        self._crown_adapter = None
        self._report_store = None
        self._account_cache: Dict[str, str] = {}
        self._settlement_cache: Dict[Tuple[str, str], Tuple[float, Any]] = {}
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
                new_order_text(row, members or [], theme),
                keyboard(group_id, theme),
            )
        except NotifyError as exc:
            text = str(exc)
            if theme and self._is_emoji_error(exc):
                self._note_emoji_error(exc)
                api.edit_message(
                    target_chat,
                    message_id,
                    new_order_text(row, members or [], None),
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

    def _debug(self, message: str) -> None:
        try:
            path = self.follow_db.with_name("notify_debug.log")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"{_now_iso()} {message}\n")
        except OSError:
            pass

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
            try:
                text = self.alert_text(data)
            except Exception as exc:  # noqa: BLE001 - 诊断用
                self._debug(f"alert_text failed data={data} err={type(exc).__name__}:{exc}")
                text = "查询失败，请稍后重试。"
            ok = api.answer_callback(str(query.get("id") or ""), text, show_alert=True)
            self._debug(f"callback data={data} len={len(text)} answered={ok}")

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

    def alert_text(self, data: str) -> str:
        """按钮弹窗用的纯文本短摘要（Telegram 上限 200 字）。"""
        match = re.match(r"^g:([0-9a-f]{32}):(res|log|pnl|amt)$", data)
        if not match:
            return "按钮已过期，请重新发送指令。"
        group_id, action = match.group(1), match.group(2)
        detail = self._detail(group_id)
        if not detail:
            return "跟单记录不存在或已清理。"
        row = detail.get("group") or {}
        try:
            if action == "res":
                text = self._alert_result(row, detail)
            elif action == "log":
                text = self._alert_log(detail)
            elif action == "pnl":
                text = self._alert_pnl(detail)
            else:
                text = self._alert_report()
        except Exception:  # noqa: BLE001 - 弹窗失败不能影响其它按钮
            return "查询失败，请稍后重试。"
        return _clip(text, 200)

    def _account_tail(self, account_id: Any) -> str:
        account = str(account_id or "").strip()
        if not account:
            return "----"
        cached = self._account_cache.get(account)
        if cached:
            return cached
        name = ""
        try:
            database = Path(self.config["pool_db_path"])
            with closing(
                sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
            ) as connection:
                row = connection.execute(
                    "SELECT name FROM accounts WHERE id=?", (account,)
                ).fetchone()
                name = str(row[0] or "") if row else ""
        except (KeyError, sqlite3.Error, OSError):
            name = ""
        tail = (name or account)[-4:]
        self._account_cache[account] = tail
        return tail

    @staticmethod
    def _tail4(value: Any) -> str:
        text = str(value or "").strip()
        return text[-4:] if text else "----"

    def _alert_result(self, row: Dict[str, Any], detail: Dict[str, Any]) -> str:
        home = str(row.get("crown_home") or row.get("home") or "—")
        away = str(row.get("crown_away") or row.get("away") or "—")
        market = MARKETS.get(str(row.get("market_type")), str(row.get("market_type") or "—"))
        odds = row.get("accepted_odds") or row.get("odds") or (row.get("quote") or {}).get("odds")
        account_tail = self._account_tail(row.get("account_id"))
        tickets = [str(item) for item in (row.get("split_tickets") or []) if item]
        for key in ("receipts", "receipt_id"):
            value = row.get(key)
            values = value if isinstance(value, list) else [value]
            for item in values:
                item = str(item or "").strip()
                if item and item not in tickets:
                    tickets.append(item)
        if tickets:
            lines = [
                f"{account_tail}-{self._tail4(ticket)}"
                + (f" &{odds}" if odds not in (None, "") else " &—")
                for ticket in tickets[:3]
            ]
            if len(tickets) > 3:
                lines.append(f"…等{len(tickets)}单")
        else:
            lines = [f"{account_tail}-未出票"]
        return "\n".join(
            [
                "📋 跟单详细",
                "",
                f"🎯 比赛：{_plain(home)} VS {_plain(away)}",
                f"⚽ 玩法：{market}",
                f"📊 盘口：{_selection(row)}",
                "",
                "👥 跟单会员：",
                *lines,
            ]
        )

    @staticmethod
    def _alert_log(detail: Dict[str, Any]) -> str:
        history = detail.get("history") or []
        lines = ["🧾 跟单日志"]
        if not history:
            lines.append("暂无记录")
        for item in history[-3:]:
            parsed = _parse_time(item.get("at"))
            clock = parsed.strftime("%H:%M") if parsed else "—"
            status = STATUS_LABELS.get(
                str(item.get("to_status") or ""), str(item.get("to_status") or "")
            )
            message = _plain(item.get("message") or "").strip()
            line = f"{clock} {status}"
            if message:
                line += f" {message[:24]}"
            lines.append(line)
        return "\n".join(lines)

    def _crown_settlement(self, account_id: Any, ticket: Any):
        if self._stop.is_set():
            return None
        account = str(account_id or "").strip()
        number = str(ticket or "").strip()
        if not account or not number:
            return None
        key = (account, number)
        now = time.monotonic()
        cached = self._settlement_cache.get(key)
        if cached and now - cached[0] < 60:
            return cached[1]
        data = None
        try:
            from .follow.crown import CrownAdapter

            if self._crown_adapter is None:
                self._crown_adapter = CrownAdapter(self.config, should_stop=self._stop.is_set)
            data = self._crown_adapter.find_history_wager(account, number)
        except Exception:  # noqa: BLE001 - 查不到就退回旧展示
            data = None
        if len(self._settlement_cache) > 100:
            self._settlement_cache.clear()
        self._settlement_cache[key] = (now, data)
        return data

    def _alert_pnl(self, detail: Dict[str, Any]) -> str:
        """只显示皇冠回执号的输赢；分单时汇总所有票号的盈利。"""
        row = detail.get("group") or {}
        tickets = []
        for value in row.get("split_tickets") or []:
            ticket = str(value or "").strip()
            if ticket and ticket not in tickets:
                tickets.append(ticket)
        for value in row.get("receipts") or []:
            ticket = str(value or "").strip()
            if ticket and ticket not in tickets:
                tickets.append(ticket)
        receipt = str(row.get("receipt_id") or "").strip()
        if receipt and receipt not in tickets:
            tickets.append(receipt)

        if not tickets:
            return "📉 输赢结果\n本单没有皇冠回执号（未跟单成功），\n没有皇冠输赢可查。"

        settlements = []
        for ticket in tickets:
            if self._stop.is_set():
                break
            item = self._crown_settlement(row.get("account_id"), ticket)
            if item:
                settlements.append(item)
        if not settlements:
            return "📉 输赢结果\n皇冠结算暂未查到（可能未结算或历史未同步）。"

        base = settlements[0]
        settled_count = 0
        total = 0.0
        for item in settlements:
            raw = item.get("win_gold")
            if raw in (None, ""):
                continue
            try:
                total += float(raw)
                settled_count += 1
            except (TypeError, ValueError):
                continue
        if settled_count == 0:
            head, profit = "⏳ 注单未结算", "—"
        elif settled_count < len(tickets):
            head = f"⏳ 部分结算（{settled_count}/{len(tickets)} 单）"
            profit = f"{total:+,.2f}"
        else:
            head, profit = "✅ 注单结算成功", f"{total:+,.2f}"

        league = _plain(base.get("league") or row.get("league") or "—")
        home = _plain(base.get("team_h_show") or row.get("crown_home") or row.get("home") or "—")
        away = _plain(base.get("team_c_show") or row.get("crown_away") or row.get("away") or "—")
        side = str(row.get("target_side") or "")
        team = (
            row.get("crown_home") or row.get("home")
            if side == "HOME"
            else row.get("crown_away") or row.get("away")
        )
        if row.get("market_type") == "TOTAL":
            play = _selection(row)
        else:
            line = _signed_line(row.get("target_line"))
            play = f"{_plain(team)} {line}".strip() or _selection(row)
        odds = base.get("ioratio") or row.get("accepted_odds") or "—"
        lines = [
            head,
            f"🏆 {league}",
            f"⚽ {home} vs {away}",
            f"🎯 玩法：{play}",
            f"📊 水位：{odds}",
            f"💰 盈利：{profit}",
        ]
        score = base.get("result_data") or base.get("score")
        if score:
            lines.append(f"📋 赛果：{_plain(score)}")
        return "\n".join(lines)

    def _alert_report(self) -> str:
        """全部会员合并的输赢报表，只读本地报表库，不请求皇冠。"""
        try:
            from .crown_reports import CrownReportStore, report_db_path

            if self._report_store is None:
                self._report_store = CrownReportStore(report_db_path(self.config))
            store = self._report_store
            today = store.crown_today()
            today_date = datetime.strptime(today, "%Y-%m-%d").date()
            week_start = today_date - timedelta(days=today_date.weekday())
            week_end = week_start + timedelta(days=6)
            today_stats = store.totals(today, today)
            week_stats = store.totals(
                week_start.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")
            )

            def block(label: str, date_text: str, stats: Dict[str, Any]) -> str:
                total = stats["wins"] + stats["losses"] + stats["pushes"]
                rate = f"{stats['wins'] / total * 100:.1f}%" if total else "--"
                head = f"📅 {label}" + (f" ({date_text})" if date_text else "")
                return "\n".join(
                    [
                        head,
                        f"💰 盈亏：{stats['pnl']:.1f}",
                        f"🎯 胜率：{rate}",
                        f"📊 战绩：{stats['wins']}胜/{stats['losses']}负/{stats['pushes']}走",
                        f"🎲 场次：{stats['total']} | 已结 {stats['settled']} | 未结 {stats['pending']}",
                    ]
                )

            return (
                "📈 输赢报表\n"
                "──────────\n"
                + block("今日", today, today_stats)
                + "\n──────────\n"
                + block("本周累计", "", week_stats)
            )
        except Exception:  # noqa: BLE001 - 报表库不可用时退回跟单快照
            summary = self.follow.snapshot("ALL", limit=1).get("summary") or {}
            succeeded = int(summary.get("succeeded") or 0)
            failed = int(summary.get("failed") or 0)
            stake = summary.get("confirmed_stake")
            money = _amount(stake) if stake not in (None, "", "0") else "0"
            return f"📈 输赢报表\n累计：成功 {succeeded} / 未完成 {failed}\n成交 {money}"

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
