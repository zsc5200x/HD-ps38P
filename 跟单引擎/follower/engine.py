import atexit
import argparse
import hashlib
import html
import json
import math
import os
import queue
import random
import re
import signal
import threading
import time
import tempfile
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# 避免 macOS 系统 Python（LibreSSL）触发 urllib3 v2 的提示刷屏
warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL.*")

import requests

from hdzz.common.config import JsonFileCache
from hdzz.common.platform_maintenance import (
    PLATFORM_MAINTENANCE_CODE,
    maintenance_status_message,
    read_platform_state,
)
from member_arsenal_watch import (
    atomic_write_json,
    ensure_member_defaults,
    now_iso,
    patch_member,
    read_json_file,
)
from hdzz.common.http import HardTimeout, hard_timeout as _hard_timeout
from hdzz.common.http import requests_timeout as _requests_timeout
from hdzz.common.parse import parse_cookie_header, parse_xml_flat, parse_xml_kv
from hdzz.common.route_traffic_gate import configure_route_traffic_gate
from hdzz.core.order_display import parse_bet_display
from .models import BetParams, RetryJob
from .snapshot_direct import (
    BET_CONTEXT_MISMATCH,
    CONTEXT_RETRYABLE,
    BetContext,
    ExecutionResult,
    MarketIntent,
    MarketSnapshotService,
    MemberExecutor,
    MARKET_ERROR,
    MARKET_CHANGED_DURING_CONTEXT,
    MARKET_SUBMIT_REJECTED,
    MEMBER_ERROR,
    UNKNOWN_RESULT,
    SNAPSHOT_INVALID,
    SNAPSHOT_MISSING,
    SNAPSHOT_STALE,
    SOURCE_ORDER_DIRECT,
    SOURCE_ORDER_READY,
    TARGET_LINE_MISSING,
    SnapshotValidator,
    SNAPSHOT_VALID,
    build_source_order_submit_template,
    build_submit_template,
    intent_market_fields,
    line_matches,
    _session_cookie_diagnostics,
)
from .member_ioratio import (
    MEMBER_IORATIO_INVALID,
    MEMBER_IORATIO_MISSING,
    MEMBER_IORATIO_STALE,
    MEMBER_IORATIO_VALID,
    MemberIoratioService,
    request_member_ioratio_refresh,
)
from .executor import (
    close_member_session,
    make_member_session,
    member_post_session,
    member_warmup_menu,
    member_warmup_sport_menu,
)
from hdzz.core.member.session_state import (
    session_fingerprint as member_session_fingerprint,
    session_ready_state,
)
from .game_more_resolver import (
    match_handicap_from_game_more_parsed,
    match_total_from_game_more_parsed,
    parse_game_more_games_and_ft_gids,
)
from .strategy import (
    build_follow_line_display,
    decode_handicap_line,
    decode_total_line,
    encode_handicap_line,
    encode_total_line,
    format_handicap_line_value,
)
from .game_ctx_cache import GameContextCache, XmlResponseCache
from .attempt_dispatcher import AttemptDispatcher
from .reconcile import reconcile_uncertain_bet
from .quote_manager import is_quote_member as is_quote_role_member
from .retry_scheduler import (
    RetryScheduler,
    aggregate_independent_split_done,
    build_independent_split_jobs,
    retry_job_from_dict as scheduler_retry_job_from_dict,
    retry_job_to_dict as scheduler_retry_job_to_dict,
    split_task_key,
)
from .session_pool import MemberSessionPool

try:
    import urllib3  # type: ignore

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass


def now_ts() -> float:
    return time.time()

def now_ts_ms() -> int:
    # Use ms to make result ordering stable when multiple attempts happen within the same second.
    return int(time.time() * 1000)


def _setting_or_default(settings: Dict[str, Any], key: str, default: Any) -> Any:
    """读取配置并保留显式 0；只有缺失或空字符串时才使用默认值。"""
    raw = settings.get(key)
    if raw is None:
        return default
    if isinstance(raw, str) and not raw.strip():
        return default
    return raw

def now_cn() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

_TG_FAIL_LAST: Dict[str, float] = {}
_ENGINE_LOCK_FH = None
_ENGINE_GLOBAL_LOCK_FH = None
_MARKET_RETRY_ERRORS = frozenset({
    "1X001",
    "1X003",
    "1X015",
    "1X016",
    SNAPSHOT_MISSING,
    SNAPSHOT_STALE,
    TARGET_LINE_MISSING,
    SNAPSHOT_INVALID,
    MARKET_ERROR,
    MEMBER_IORATIO_MISSING,
    MEMBER_IORATIO_STALE,
    MEMBER_IORATIO_INVALID,
})
STAKE_EXCEEDS_MAX_ERR = "STAKE_EXCEEDS_MAX"
_TG_ORDER_CB_PREFIX = "odr"
_JSON_FILE_CACHE = JsonFileCache()
_TG_ORDER_BUTTON_SPECS: Dict[str, Dict[str, str]] = {
    "res": {"text": "跟单结果", "style": "primary"},
    "log": {"text": "跟单日志", "style": "primary"},
    "pnl": {"text": "输赢结果", "style": "danger"},
    "amt": {"text": "跟单报表", "style": "success"},
}
_ORDER_DIVIDER_FALLBACKS: Tuple[str, ...] = ("🇪🇸", "🇪🇸", "🇪🇸", "🏴", "🏴", "🏴", "🏴", "🇩🇪", "🇫🇷", "🇩🇪")


def _digits_only(v: Any) -> str:
    s = str(v or "")
    return "".join(ch for ch in s if ch.isdigit())


def _normalize_tg_ui_theme(raw: Any) -> Dict[str, Any]:
    theme = raw if isinstance(raw, dict) else {}
    btn_ids = theme.get("button_custom_emoji_ids") if isinstance(theme.get("button_custom_emoji_ids"), dict) else {}
    txt_ids = theme.get("text_custom_emoji_ids") if isinstance(theme.get("text_custom_emoji_ids"), dict) else {}
    divider_ids = theme.get("divider_custom_emoji_ids") if isinstance(theme.get("divider_custom_emoji_ids"), list) else []
    return {
        "button_custom_emoji_ids": {
            str(k): str(v or "").strip()
            for k, v in btn_ids.items()
            if str(k or "").strip()
        },
        "text_custom_emoji_ids": {
            str(k): str(v or "").strip()
            for k, v in txt_ids.items()
            if str(k or "").strip()
        },
        "divider_custom_emoji_ids": [str(v or "").strip() for v in divider_ids if str(v or "").strip()],
    }


def _load_tg_ui_theme(cfg: Any) -> Dict[str, Any]:
    if not isinstance(cfg, dict):
        return _normalize_tg_ui_theme({})
    ui = cfg.get("telegram_ui")
    if not isinstance(ui, dict):
        return _normalize_tg_ui_theme({})
    return _normalize_tg_ui_theme(
        {
            "button_custom_emoji_ids": ui.get("order_button_custom_emoji_ids"),
            "text_custom_emoji_ids": ui.get("order_text_custom_emoji_ids"),
            "divider_custom_emoji_ids": ui.get("order_divider_custom_emoji_ids"),
        }
    )


def _tg_custom_emoji_html(emoji_id: Any, fallback: str = "") -> str:
    eid = str(emoji_id or "").strip()
    if eid:
        inner = html.escape(str(fallback or "").strip())
        return f'<tg-emoji emoji-id="{html.escape(eid, quote=True)}">{inner}</tg-emoji>'
    return fallback


def _order_card_icon(ui_theme: Optional[Dict[str, Any]], key: str, fallback: str) -> str:
    theme = _normalize_tg_ui_theme(ui_theme)
    text_ids = theme.get("text_custom_emoji_ids") or {}
    return _tg_custom_emoji_html(text_ids.get(str(key) or ""), fallback)


def _order_card_suffix(ui_theme: Optional[Dict[str, Any]], key: str, fallback: str = "") -> str:
    theme = _normalize_tg_ui_theme(ui_theme)
    text_ids = theme.get("text_custom_emoji_ids") or {}
    suffix_key = f"{str(key or '').strip()}_suffix"
    emoji = _tg_custom_emoji_html(text_ids.get(suffix_key) or "", fallback)
    return f" {emoji}" if emoji else ""


def _order_card_divider(ui_theme: Optional[Dict[str, Any]], fallback: Any = "🐸") -> str:
    theme = _normalize_tg_ui_theme(ui_theme)
    ids = theme.get("divider_custom_emoji_ids") or []
    if not ids:
        return ""
    fallback_seq = list(fallback) if isinstance(fallback, (list, tuple)) else []
    out: List[str] = []
    for idx, eid in enumerate(ids):
        fb = fallback_seq[idx] if idx < len(fallback_seq) else fallback
        out.append(_tg_custom_emoji_html(eid, str(fb or "")))
    return "".join(out)


def _themed_line(icon_html: str, text: str, *, spaced: bool) -> str:
    icon = str(icon_html or "").strip()
    if not icon:
        return text
    return f"{icon}{' ' if spaced else ''}{text}"


def _order_inline_keyboard(order_id: Any, ui_theme: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    oid = _digits_only(order_id)
    if not oid:
        return None
    theme = _normalize_tg_ui_theme(ui_theme)
    icon_ids = theme.get("button_custom_emoji_ids") or {}
    rows: List[List[Dict[str, str]]] = []
    for row_actions in (("res", "log"), ("pnl", "amt")):
        row: List[Dict[str, str]] = []
        for action in row_actions:
            spec = _TG_ORDER_BUTTON_SPECS[action]
            btn = {
                "text": spec["text"],
                "style": spec["style"],
                "callback_data": f"{_TG_ORDER_CB_PREFIX}|{action}|{oid}",
            }
            icon_id = str(icon_ids.get(action) or "").strip()
            if icon_id:
                btn["icon_custom_emoji_id"] = icon_id
            row.append(btn)
        rows.append(row)
    return {"inline_keyboard": rows}


def _is_http_url(v: Any) -> bool:
    s = str(v or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def _resolve_order_preview_local_path(image_ref: Any) -> Optional[Path]:
    s = str(image_ref or "").strip()
    if not s or _is_http_url(s):
        return None

    p0 = Path(s).expanduser()
    cands: List[Path] = []
    if p0.is_absolute():
        cands.append(p0)
    else:
        cwd = Path.cwd()
        cands.append((cwd / p0).resolve())
        cands.append((cwd.parent / p0).resolve())
        try:
            repo_root = Path(__file__).resolve().parents[4]
            cands.append((repo_root / p0).resolve())
            cands.append((repo_root / "app" / p0).resolve())
        except Exception:
            pass
    seen: set[str] = set()
    for p in cands:
        k = str(p)
        if k in seen:
            continue
        seen.add(k)
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def _detect_default_order_preview_image_ref() -> str:
    for name in ("gdcg.jpg", "gdcc.jpg", "gdcg.jpeg", "gdcc.jpeg", "gdcg.png", "gdcc.png"):
        if _resolve_order_preview_local_path(name):
            return name
    return ""


def _preview_image_url_for_attach(image_ref: Any) -> str:
    s = str(image_ref or "").strip()
    return s if _is_http_url(s) else ""


def _resolve_order_preview_photo_ref(image_ref: Any) -> str:
    s = str(image_ref or "").strip()
    if not s:
        return ""
    if _is_http_url(s):
        return s
    p = _resolve_order_preview_local_path(s)
    return str(p) if p else ""


def _attach_order_preview_image(html_text: str, image_url: str) -> str:
    text = str(html_text or "")
    url = _preview_image_url_for_attach(image_url)
    if not url:
        return text
    if text.startswith("<a href="):
        return text
    u = html.escape(url, quote=True)
    return f'<a href="{u}">&#8205;</a>\n{text}'


def _cooldown_member(member_id: str, seconds: float) -> None:
    # 会员冷却已停用，保留调用点兼容历史状态恢复。
    return


def _is_member_in_cooldown(member_id: str) -> bool:
    mid = str(member_id or "").strip()
    if not mid:
        return False
    until = float(_MEMBER_COOLDOWN_UNTIL.get(mid) or 0.0)
    if until <= 0:
        return False
    if float(now_ts()) >= until:
        try:
            _MEMBER_COOLDOWN_UNTIL.pop(mid, None)
        except Exception:
            pass
        return False
    return True


def _sorted_split_part_records(split_part_results: Dict[Any, Any]) -> List[Dict[str, Any]]:
    """
    split_part_results 的 key 在内存里是 int，但落盘成 JSON 再读回来会变成 str；
    极端情况下会出现混合 key（int/str），直接 sorted(dict) 会 TypeError。
    这里统一按“可转 int 的优先”排序，保证 TG/UI 合并消息不崩溃。
    """
    if not isinstance(split_part_results, dict) or not split_part_results:
        return []

    def _k(x: Any) -> Tuple[int, str]:
        try:
            return (int(x), "")
        except Exception:
            return (10**9, str(x))

    out: List[Dict[str, Any]] = []
    for k0 in sorted(list(split_part_results.keys()), key=_k):
        v0 = split_part_results.get(k0)
        if isinstance(v0, dict):
            out.append(v0)
    return out


def _get_member_session(
    *,
    member: Dict[str, Any],
    session_pool: Optional["MemberSessionPool"] = None,
) -> requests.Session:
    if session_pool is not None and str((member or {}).get("id") or "").strip():
        return session_pool.get_or_create(member)
    return make_member_session(
        base_url=str((member or {}).get("base_url") or "").strip(),
        proxy=str((member or {}).get("proxy") or "").strip(),
        cookie=str((member or {}).get("cookie") or "").strip(),
    )


def _invalidate_member_session(
    *,
    session_pool: Optional["MemberSessionPool"],
    member_id: str,
    reason: str,
) -> None:
    if session_pool is None:
        return
    try:
        session_pool.invalidate(str(member_id or "").strip(), str(reason or "").strip())
    except Exception:
        pass


def _session_invalidation_reason_from_exception(exc: BaseException) -> str:
    name = type(exc).__name__ or "Exception"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "ProxyError"
    if isinstance(exc, requests.exceptions.SSLError):
        return "SSLError"
    if isinstance(exc, requests.exceptions.ChunkedEncodingError):
        return "ChunkedEncodingError"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "ConnectionError"
    if name in {"ProxyError", "SSLError", "ChunkedEncodingError", "ConnectionError"}:
        return name
    return ""

# ---------------------------------------------------------------------------
# Game ctx / XML caches
# ---------------------------------------------------------------------------
_GAME_CTX_CACHE = GameContextCache(ttl_seconds=600.0)
_GAME_LIST_XML_CACHE = XmlResponseCache(ttl_seconds=0.8, max_items=256)
_GAME_MORE_XML_CACHE = XmlResponseCache(ttl_seconds=0.8, max_items=256)
_GAME_OBT_XML_CACHE = XmlResponseCache(ttl_seconds=0.8, max_items=256)
_GAME_LIST_XML_CACHE_TTL_S = 0.8
_GAME_MORE_XML_CACHE_TTL_S = 0.8
_GAME_OBT_XML_CACHE_TTL_S = 0.8


def _xml_cache_get(cache: XmlResponseCache, key: str, ttl_s: float) -> str:
    return cache.get(str(key or ""), ttl_s=float(ttl_s or 0.0))


def _xml_cache_set(cache: XmlResponseCache, key: str, text: str) -> None:
    cache.set(str(key or ""), str(text or ""))


def _xml_cache_get_or_fetch(
    cache: XmlResponseCache,
    key: str,
    ttl_s: float,
    fetcher: Callable[[], str],
) -> str:
    """读取短 TTL 盘口缓存，并合并同 key 的并发 miss。"""
    return cache.get_or_fetch(str(key or ""), float(ttl_s or 0.0), fetcher)


_DEFAULT_LTYPE = "4"



def _normalize_showtype_hint(st: Any) -> str:
    s = str(st or "").strip().lower()
    if s in {"live", "today", "hot", "early"}:
        return s
    return ""




def _looks_like_ctx_blocked_msg(msg: Any) -> bool:
    """
    识别“会话上下文不足/被阻断”的典型提示，避免在同一条链路里做无意义的重复扫描。
    该判断用于提速（失败则尽快切换到 game_more 等兜底路径）。
    """
    s = str(msg or "").strip().lower()
    if not s:
        return False
    if "checkemnu" in s:
        return True
    if "variablestandard" in s:
        return True
    if ("code_type" in s) and ("null" in s):
        return True
    if ("loading" in s) and ("high" in s):
        return True
    return False


def _game_ctx_cache_key(*, base_url: str, uid: str, gid: str) -> str:
    """
    lid/ecid 与账号(uid)通常无关（同一站点同一场 gid 基本一致），
    因此缓存 key 不再绑定 uid，便于“分单换会员”时复用上下文，降低 CheckEMNU 概率。
    """
    return _GAME_CTX_CACHE.build_key(base_url=base_url, uid=uid, gid=gid)


def _get_cached_game_ctx(*, base_url: str, uid: str, gid: str, max_age_seconds: float = 600.0) -> Tuple[str, str, str]:
    return _GAME_CTX_CACHE.get(
        base_url=base_url,
        uid=uid,
        gid=gid,
        max_age_seconds=float(max_age_seconds),
    )


def _set_cached_game_ctx(*, base_url: str, uid: str, gid: str, lid: str, ecid: str, showtype: str = "") -> None:
    _GAME_CTX_CACHE.set(
        base_url=base_url,
        uid=uid,
        gid=gid,
        lid=lid,
        ecid=ecid,
        showtype=showtype,
    )


def _shrink_showtype_candidates_for_quick(showtypes: List[str]) -> List[str]:
    """
    quick 模式下只取少量 showtype，但要避免漏掉“live 掉到 today/hot”导致找不到盘口线。

    经验优先级（滚球最常见）：
    - live → today（比 hot 更全，通常包含被挪走的盘口线）
    - 否则：保留前 2 个即可
    """
    out: List[str] = []
    seen = set()
    for st in (showtypes or []):
        s = str(st or "").strip().lower()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    if len(out) <= 2:
        return out
    if out and out[0] == "live" and "today" in out:
        return ["live", "today"]
    return out[:2]


def _tg_warn_throttled(key: str, err: Exception, *, min_interval_s: float = 30.0) -> None:
    """
    只打必要的可见日志，避免 Telegram 更新失败时“静默吞掉”导致用户误以为一直在等待中。
    """
    try:
        k = str(key or "").strip() or "unknown"
        now = float(now_ts())
        last = float(_TG_FAIL_LAST.get(k) or 0.0)
        if (now - last) < float(min_interval_s):
            return
        _TG_FAIL_LAST[k] = now
        msg = str(err) if err is not None else "unknown"
        msg = msg.replace("\n", " ").replace("\r", " ").strip()
        # 脱敏：Telegram URL 里可能包含 bot token；代理 URL 可能包含 user:pass@
        try:
            msg = re.sub(r"(/bot)[^/\s]+", r"\1***", msg)
            msg = re.sub(r"(bot)\d+:[A-Za-z0-9_\-]+", r"\1***", msg)
            msg = re.sub(r"(https?://)([^/\s:@]+):([^/\s@]+)@", r"\1***:***@", msg)
        except Exception:
            pass
        if len(msg) > 240:
            msg = msg[:240] + "..."
        print(f"[{now_iso()}] Telegram 更新失败 key={k} err={msg}", flush=True)
    except Exception:
        pass


def _tg_should_fallback_send_on_edit_error(err: Exception) -> bool:
    """
    edit 失败时是否改为补发一条新消息。
    - message is not modified：无需补发
    - message can't be edited / not found：补发，避免用户一直看到“等待结果…”
    """
    try:
        s = str(err or "").lower()
        if "message is not modified" in s:
            return False
        if "message to edit not found" in s:
            return True
        if "message can't be edited" in s or "message can not be edited" in s:
            return True
        if "there is no text in the message to edit" in s:
            return True
        if "there is no caption in the message to edit" in s:
            return True
        if "message_id_invalid" in s:
            return True
    except Exception:
        return False
    return False


def acquire_single_instance_lock(lock_path: Path) -> bool:
    """
    防止同一个 data_dir 下同时跑多个 follow_engine：
    - 多实例会导致重复下注、重复写 follow_results、TG 多条播报。
    """
    global _ENGINE_LOCK_FH, _ENGINE_GLOBAL_LOCK_FH
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = lock_path.open("a+", encoding="utf-8")
    except Exception:
        return False

    try:
        if os.name == "nt":
            import msvcrt  # type: ignore

            fh.seek(0)
            # locking 需要锁定一定长度，确保文件至少有 1 byte
            try:
                if fh.read(1) == "":
                    fh.seek(0)
                    fh.write("0")
                    fh.flush()
            except Exception:
                pass
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # type: ignore

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        return False

    # A data-dir lock prevents duplicates within one runtime directory.  A
    # second per-user lock prevents an old source-tree launcher and a Tauri
    # runtime (which use different data directories) from both executing the
    # same follow engine at once.
    global_path = Path(tempfile.gettempdir()) / f"hdzz-follow-engine-{getattr(os, 'getuid', lambda: 0)()}.lock"
    try:
        global_fh = global_path.open("a+")
        if os.name == "nt":
            import msvcrt  # type: ignore

            global_fh.seek(0)
            if not global_fh.read(1):
                global_fh.seek(0)
                global_fh.write("0")
                global_fh.flush()
            global_fh.seek(0)
            msvcrt.locking(global_fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # type: ignore

            fcntl.flock(global_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        try:
            global_fh.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass
        return False

    try:
        fh.seek(0)
        fh.truncate(0)
        fh.write(f"pid={os.getpid()}\nstarted_at={now_iso()}\n")
        fh.flush()
    except Exception:
        pass

    _ENGINE_LOCK_FH = fh
    _ENGINE_GLOBAL_LOCK_FH = global_fh
    return True


def tg_upsert_order_message(
    *,
    bot_token: str,
    chat_id: str,
    message_id: Any,
    html_text: str,
    order_id: str = "",
    order_preview_image: str = "",
    timeout_s: int = 10,
    proxy: str = "",
    ui_theme: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[int]:
    """
    优先 edit 原消息；无法编辑时（not found / can't be edited 等）自动降级为 send 新消息，
    并返回新的 message_id 以便后续继续 edit，避免刷屏多条。
    """
    mid: Optional[int] = None
    try:
        mid = int(str(message_id).strip()) if str(message_id or "").strip() else None
    except Exception:
        mid = None

    preview_url = _preview_image_url_for_attach(order_preview_image)
    photo_ref = _resolve_order_preview_photo_ref(order_preview_image)
    send_html = _attach_order_preview_image(html_text, preview_url)
    reply_markup = _order_inline_keyboard(order_id, ui_theme=ui_theme)
    disable_preview = not bool(preview_url)

    if mid:
        try:
            if photo_ref:
                tg_edit_caption(
                    bot_token,
                    chat_id,
                    mid,
                    html_text,
                    reply_markup=reply_markup,
                    timeout_s=timeout_s,
                    proxy=proxy,
                )
            else:
                tg_edit(
                    bot_token,
                    chat_id,
                    mid,
                    send_html,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_preview,
                    timeout_s=timeout_s,
                    proxy=proxy,
                )
            return mid
        except Exception as e:
            # 常见：message is not modified -> 视为成功，不补发
            try:
                if "message is not modified" in str(e or "").lower():
                    return mid
            except Exception:
                pass
            if photo_ref:
                try:
                    new_mid2 = tg_send_photo(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        photo_ref=photo_ref,
                        caption_html=html_text,
                        reply_markup=reply_markup,
                        timeout_s=timeout_s,
                        proxy=proxy,
                    )
                    if new_mid2:
                        return new_mid2
                except Exception:
                    pass
            if _tg_should_fallback_send_on_edit_error(e) or photo_ref:
                new_mid = tg_send(
                    bot_token,
                    chat_id,
                    send_html,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_preview,
                    timeout_s=timeout_s,
                    proxy=proxy,
                )
                return new_mid or mid
            raise

    if photo_ref:
        try:
            new_mid2 = tg_send_photo(
                bot_token=bot_token,
                chat_id=chat_id,
                photo_ref=photo_ref,
                caption_html=html_text,
                reply_markup=reply_markup,
                timeout_s=timeout_s,
                proxy=proxy,
            )
            if new_mid2:
                return new_mid2
        except Exception:
            pass
    return tg_send(
        bot_token,
        chat_id,
        send_html,
        reply_markup=reply_markup,
        disable_web_page_preview=disable_preview,
        timeout_s=timeout_s,
        proxy=proxy,
    )

def _fmt_ts_cn(ts: Any) -> str:
    try:
        if ts in (None, ""):
            return ""
        if isinstance(ts, (int, float)):
            v = float(ts)
        else:
            v = float(str(ts).strip())
        if v > 2_000_000_000_000:  # ms
            v = v / 1000.0
        return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

def _fmt_amount(v: Any) -> str:
    try:
        f = float(v)
    except Exception:
        return str(v)
    if math.isfinite(f) and abs(f - round(f)) < 1e-9:
        return f"{f:.1f}"
    s = f"{f:.2f}"
    s = s.rstrip("0").rstrip(".")
    return s or str(v)


_SUCCESS_SPLIT_STATUSES = frozenset({"ok", "success", "succeeded", "done"})


def _split_record_succeeded(record: Any) -> bool:
    """Accept both attempt records (``ok``) and terminal records (``status``)."""
    if not isinstance(record, dict):
        return False
    if "ok" in record:
        return bool(record.get("ok"))
    return str(record.get("status") or "").strip().lower() in _SUCCESS_SPLIT_STATUSES


def _sum_follow_success_amount(follow_result: Dict[str, Any]) -> float:
    try:
        split_parts = follow_result.get("split_parts")
        if isinstance(split_parts, list) and split_parts:
            total = 0.0
            for p in split_parts:
                if not isinstance(p, dict):
                    continue
                if not _split_record_succeeded(p):
                    continue
                try:
                    total += float(p.get("stake") or 0.0)
                except Exception:
                    continue
            return total
    except Exception:
        pass

    try:
        parts_map = follow_result.get("split_part_results")
        if isinstance(parts_map, dict) and parts_map:
            total = 0.0
            for p in parts_map.values():
                if not isinstance(p, dict):
                    continue
                if not _split_record_succeeded(p):
                    continue
                try:
                    total += float(p.get("stake") or 0.0)
                except Exception:
                    continue
            return total
    except Exception:
        pass

    if bool(follow_result.get("ok")):
        try:
            return float(follow_result.get("stake") or 0.0)
        except Exception:
            return 0.0
    return 0.0


def _build_independent_split_tg_result(
    aggregate: Dict[str, Any],
    job: RetryJob,
) -> Dict[str, Any]:
    """Normalize terminal split records for the combined Telegram card."""
    result = dict(aggregate or {})
    parts: List[Dict[str, Any]] = []
    raw_parts = result.get("split_parts")
    if isinstance(raw_parts, list):
        for index, raw in enumerate(raw_parts):
            if not isinstance(raw, dict):
                continue
            part = dict(raw)
            part.setdefault("ok", _split_record_succeeded(part))
            if "part" not in part:
                try:
                    part["part"] = int(part.get("split_index")) + 1
                except Exception:
                    part["part"] = index + 1
            parts.append(part)

    plan_total = 0.0
    try:
        plan_total = sum(float(value or 0.0) for value in (job.split_stakes or []))
    except Exception:
        plan_total = 0.0
    if plan_total <= 0.0:
        try:
            plan_total = float(job.stake or 0.0)
        except Exception:
            plan_total = 0.0

    success_total = 0.0
    for part in parts:
        if not _split_record_succeeded(part):
            continue
        try:
            success_total += float(part.get("stake") or 0.0)
        except Exception:
            continue

    result["split_parts"] = parts
    result["split_total_plan"] = plan_total
    result["split_total_done"] = success_total
    result["ok"] = str(result.get("status") or "").strip().lower() in _SUCCESS_SPLIT_STATUSES
    return result


def _update_follow_tg_message(*, job: RetryJob, follow_result: Dict[str, Any]) -> None:
    """Update the existing Telegram card for one attempt or its final aggregate."""
    try:
        bot_token, default_chat_id, proxy, order_preview_image, ui_theme = load_tg_config(Path("config.json"))
        if not bot_token:
            return
        order_chat_id = str(job.order.get("_tg_chat_id") or default_chat_id).strip()
        if not order_chat_id:
            return
        merged = build_order_follow_combined_message(
            order=job.order,
            follow_result=follow_result,
            ui_theme=ui_theme,
        )
        new_mid = tg_upsert_order_message(
            bot_token=bot_token,
            chat_id=order_chat_id,
            message_id=job.order.get("_tg_message_id"),
            html_text=merged,
            order_id=job.order_id,
            order_preview_image=order_preview_image,
            timeout_s=10,
            proxy=proxy,
            ui_theme=ui_theme,
        )
        if new_mid:
            job.order["_tg_message_id"] = int(new_mid)
            job.order["_tg_chat_id"] = order_chat_id
    except Exception as exc:
        _tg_warn_throttled(job.key, exc)


class _AsyncFollowNotificationQueue:
    """Send non-critical Telegram updates outside the betting scheduler thread."""

    def __init__(self, trace_path: Path, *, maxsize: int = 1024) -> None:
        self._trace_path = Path(trace_path)
        self._queue: "queue.Queue[Optional[Tuple[RetryJob, Dict[str, Any], float]]]" = queue.Queue(
            maxsize=max(1, int(maxsize))
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="follow-notification", daemon=True)
        self._thread.start()

    @staticmethod
    def _snapshot_job(job: RetryJob) -> RetryJob:
        # Telegram formatting only reads these values. Copy mutable fields so
        # the worker cannot observe a job while the scheduler is changing it.
        split_parts: Dict[int, Dict[str, Any]] = {}
        for key, value in (job.split_part_results or {}).items():
            if not isinstance(value, dict):
                continue
            try:
                split_parts[int(key)] = dict(value)
            except Exception:
                continue
        return replace(
            job,
            order=dict(job.order or {}),
            split_tickets=list(job.split_tickets or []),
            used_member_ids=list(job.used_member_ids or []),
            split_part_results=split_parts,
        )

    def enqueue(self, *, job: RetryJob, follow_result: Dict[str, Any]) -> bool:
        queued_at = float(now_ts())
        item = (self._snapshot_job(job), dict(follow_result or {}), queued_at)
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            _append_trace_jsonl(
                self._trace_path,
                {
                    "event": "notification_dropped",
                    "key": str(job.key or ""),
                    "orderId": str(job.order_id or ""),
                    "notification_queued_at": queued_at,
                    "notification_error": "queue_full",
                },
            )
            return False
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            job, result, queued_at = item
            started_at = float(now_ts())
            error = ""
            try:
                _update_follow_tg_message(job=job, follow_result=result)
            except BaseException as exc:
                error = f"{type(exc).__name__}: {exc}"
            finished_at = float(now_ts())
            _append_trace_jsonl(
                self._trace_path,
                {
                    "event": "notification_result",
                    "key": str(job.key or ""),
                    "orderId": str(job.order_id or ""),
                    "notification_queued_at": queued_at,
                    "notification_started_at": started_at,
                    "notification_finished_at": finished_at,
                    "notification_queue_delay_ms": round(max(0.0, started_at - queued_at) * 1000.0, 3),
                    "notification_elapsed_ms": round(max(0.0, finished_at - started_at) * 1000.0, 3),
                    "notification_error": error,
                },
            )
            self._queue.task_done()

    def close(self, *, wait: bool = False) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if wait and self._thread.is_alive():
            self._thread.join(timeout=2.0)


def load_tg_config(config_path: Path) -> Tuple[str, str, str, str, Dict[str, Dict[str, str]]]:
    try:
        cfg = _JSON_FILE_CACHE.read_dict(config_path, {})
        if not isinstance(cfg, dict):
            return "", "", "", "", _normalize_tg_ui_theme({})
        order_forward_enabled = bool(cfg.get("telegram_order_forward_enabled", True))
        bot_token = str(cfg.get("telegram_bot_token") or "").strip()
        chat_id = str(cfg.get("telegram_chat_id") or "").strip()
        # 优先 telegram_proxy；若为空则兼容旧配置复用全局 proxy。
        # 注意：全局 proxy 往往用于站点/Playwright，不一定可用于 Telegram；实际发送时会在代理失败时自动降级为直连。
        proxy = str(cfg.get("telegram_proxy") or "").strip() or str(cfg.get("proxy") or "").strip()
        order_preview_image = str(cfg.get("telegram_order_preview_image") or "").strip()
        if not order_preview_image:
            order_preview_image = _detect_default_order_preview_image_ref()
        ui_theme = _load_tg_ui_theme(cfg)
        if not order_forward_enabled:
            # 订单转发总开关关闭：彻底禁用 follow_engine 的 TG 主动播报。
            return "", "", proxy, order_preview_image, ui_theme
        return bot_token, chat_id, proxy, order_preview_image, ui_theme
    except Exception:
        return "", "", "", "", _normalize_tg_ui_theme({})


def should_send_initial_tg_for_ingested_item(
    item: Dict[str, Any],
    *,
    now_ts_value: Optional[float] = None,
    max_backfill_age_s: float = 120.0,
) -> bool:
    if not isinstance(item, dict):
        return False
    if not bool(item.get("backfill")):
        return True

    order = item.get("order")
    if not isinstance(order, dict):
        return False

    origin = str(order.get("_signal_origin") or "").strip()
    is_remote_signal = origin == "remote_signal_pull" or bool(order.get("_skip_signal_forward"))
    if not is_remote_signal:
        return False

    try:
        order_ts = float(order.get("ts") or 0.0)
    except Exception:
        order_ts = 0.0
    if order_ts <= 0:
        return True

    try:
        now0 = float(now_ts_value if now_ts_value is not None else now_ts())
    except Exception:
        now0 = float(now_ts())
    return (now0 - order_ts) <= max(1.0, float(max_backfill_age_s or 0.0))


def ensure_initial_order_tg_message(
    *,
    order: Dict[str, Any],
    target_name: str,
    settings: Optional[Dict[str, Any]] = None,
    config_path: Path = Path("config.json"),
) -> Optional[int]:
    """
    为缺少 `_tg_message_id` 的订单补发首条“新注单提醒”消息。
    典型场景：远端 signal_puller 落盘的订单不会自带 TG 消息元数据，
    若此处不补发，后续就只能等到跟单结果阶段才首次出现在群里。
    """
    if not isinstance(order, dict):
        return None
    if isinstance(settings, dict):
        target_for_filter = str(target_name or order.get("target") or "").strip()
        if target_for_filter:
            if not target_follow_enabled(settings, target_for_filter):
                return None
            ratio_override = target_ratio_percent_override(settings, target_for_filter)
            if ratio_override == 0:
                return None
    try:
        existing_mid = int(str(order.get("_tg_message_id") or "").strip()) if str(order.get("_tg_message_id") or "").strip() else None
    except Exception:
        existing_mid = None
    if existing_mid:
        return existing_mid

    bot_token, default_chat_id, proxy, order_preview_image, ui_theme = load_tg_config(config_path)
    if not bot_token:
        return None
    order_chat_id = str(order.get("_tg_chat_id") or default_chat_id).strip()
    if not order_chat_id:
        return None

    try:
        from hdzz.core.watcher.ag_watcher import format_telegram_message

        html_text = format_telegram_message(str(target_name or order.get("target") or "unknown"), order, ui_theme=ui_theme)
    except Exception:
        html_text = build_order_follow_combined_message(
            order=order,
            follow_result={
                "ts": int(now_ts()),
                "ts_ms": int(now_ts_ms()),
                "target": str(target_name or order.get("target") or "unknown"),
                "orderId": str(order.get("orderId") or ""),
                "stake": 0.0,
                "ok": False,
                "message": "已收到信号，等待跟单结果。",
            },
            ui_theme=ui_theme,
        )

    new_mid = tg_upsert_order_message(
        bot_token=bot_token,
        chat_id=order_chat_id,
        message_id=None,
        html_text=html_text,
        order_id=str(order.get("orderId") or ""),
        order_preview_image=order_preview_image,
        timeout_s=10,
        proxy=proxy,
        ui_theme=ui_theme,
    )
    if new_mid:
        order["_tg_message_id"] = int(new_mid)
        order["_tg_chat_id"] = order_chat_id
    return new_mid


def tail_jsonl_objects(path: Path, *, max_lines: int = 400, max_bytes: int = 262144) -> List[Dict[str, Any]]:
    """
    读取 jsonl 文件末尾若干行（用于“近期回补”避免漏单），尽量避免全量读取大文件。
    - max_bytes: 最多向前读多少字节
    - max_lines: 最多解析多少行
    """
    max_lines = max(1, int(max_lines or 1))
    max_bytes = max(1024, int(max_bytes or 262144))

    if not path.exists():
        return []

    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
        if start > 0:
            # 丢弃第一行的“残缺片段”
            i = data.find(b"\n")
            if i >= 0:
                data = data[i + 1 :]
        text = data.decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        lines = lines[-max_lines:]
        out: List[Dict[str, Any]] = []
        for ln in lines:
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out
    except Exception:
        return []


def _order_id_from_cache_key(cache_key: str) -> str:
    s = str(cache_key or "")
    if "|" in s:
        return s.rsplit("|", 1)[-1].strip()
    return s.strip()


def _target_from_cache_key(cache_key: str) -> str:
    s = str(cache_key or "")
    if "|" in s:
        return s.split("|", 1)[0].strip()
    return ""


def tg_send_photo(
    bot_token: str,
    chat_id: str,
    photo_ref: str,
    *,
    caption_html: str = "",
    reply_markup: Optional[Dict[str, Any]] = None,
    timeout_s: int = 10,
    proxy: str = "",
) -> Optional[int]:
    bot_token = str(bot_token or "").strip()
    chat_id = str(chat_id or "").strip()
    photo_ref = str(photo_ref or "").strip()
    if not bot_token or not chat_id or not photo_ref:
        return None

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    proxies = {"http": proxy, "https": proxy} if proxy else None
    is_url = _is_http_url(photo_ref)
    local_path = None if is_url else _resolve_order_preview_local_path(photo_ref)
    if (not is_url) and (not local_path):
        return None

    def _post_once(px: Optional[Dict[str, str]]) -> requests.Response:
        if is_url:
            payload: Dict[str, Any] = {"chat_id": chat_id, "photo": photo_ref}
            if caption_html:
                payload["caption"] = caption_html
                payload["parse_mode"] = "HTML"
            if reply_markup:
                payload["reply_markup"] = reply_markup
            return requests.post(
                url,
                json=payload,
                timeout=timeout_s,
                proxies=px,
            )
        assert local_path is not None
        payload2: Dict[str, Any] = {"chat_id": chat_id}
        if caption_html:
            payload2["caption"] = caption_html
            payload2["parse_mode"] = "HTML"
        if reply_markup:
            payload2["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        with local_path.open("rb") as fh:
            return requests.post(
                url,
                data=payload2,
                files={"photo": (local_path.name, fh)},
                timeout=timeout_s,
                proxies=px,
            )

    try:
        r = _post_once(proxies)
    except Exception as e:
        if proxy:
            try:
                r = _post_once(None)
            except Exception:
                raise e
        else:
            raise

    try:
        r.raise_for_status()
    except Exception as e:
        try:
            body = str(getattr(r, "text", "") or "")[:800]
        except Exception:
            body = ""
        raise RuntimeError(f"editMessageText_http_error: {e}; body={body}") from e
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram error: {data}")
    try:
        return int((data.get("result") or {}).get("message_id"))
    except Exception:
        return None


def tg_send(
    bot_token: str,
    chat_id: str,
    html_text: str,
    *,
    reply_markup: Optional[Dict[str, Any]] = None,
    disable_web_page_preview: bool = True,
    timeout_s: int = 10,
    proxy: str = "",
) -> Optional[int]:
    bot_token = str(bot_token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not bot_token or not chat_id:
        return None
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": bool(disable_web_page_preview),
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        r = requests.post(url, json=payload, timeout=timeout_s, proxies=proxies)
    except Exception as e:
        # 代理常见问题：全局 proxy 不支持 Telegram / 本机代理没开。
        # 降级为直连再试一次（避免“新单能发但结果一直等待”的错觉）。
        if proxy:
            try:
                r = requests.post(url, json=payload, timeout=timeout_s, proxies=None)
            except Exception:
                raise e
        else:
            raise
    try:
        r.raise_for_status()
    except Exception as e:
        try:
            body = str(getattr(r, "text", "") or "")[:800]
        except Exception:
            body = ""
        raise RuntimeError(f"sendMessage_http_error: {e}; body={body}") from e
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram error: {data}")
    try:
        return int((data.get("result") or {}).get("message_id"))
    except Exception:
        return None


def tg_edit(
    bot_token: str,
    chat_id: str,
    message_id: Any,
    html_text: str,
    *,
    reply_markup: Optional[Dict[str, Any]] = None,
    disable_web_page_preview: bool = True,
    timeout_s: int = 10,
    proxy: str = "",
) -> None:
    bot_token = str(bot_token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not bot_token or not chat_id:
        return
    try:
        mid = int(str(message_id).strip())
    except Exception:
        return
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    payload = {
        "chat_id": chat_id,
        "message_id": mid,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": bool(disable_web_page_preview),
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=timeout_s, proxies=proxies)
    except Exception as e:
        if proxy:
            try:
                r = requests.post(url, json=payload, timeout=timeout_s, proxies=None)
            except Exception:
                raise e
        else:
            raise
    try:
        r.raise_for_status()
    except Exception as e:
        try:
            body = str(getattr(r, "text", "") or "")[:800]
        except Exception:
            body = ""
        raise RuntimeError(f"editMessageText_http_error: {e}; body={body}") from e
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram error: {data}")


def tg_edit_caption(
    bot_token: str,
    chat_id: str,
    message_id: Any,
    html_text: str,
    *,
    reply_markup: Optional[Dict[str, Any]] = None,
    timeout_s: int = 10,
    proxy: str = "",
) -> None:
    bot_token = str(bot_token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not bot_token or not chat_id:
        return
    try:
        mid = int(str(message_id).strip())
    except Exception:
        return
    url = f"https://api.telegram.org/bot{bot_token}/editMessageCaption"
    proxies = {"http": proxy, "https": proxy} if proxy else None
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": mid,
        "caption": html_text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=timeout_s, proxies=proxies)
    except Exception as e:
        if proxy:
            try:
                r = requests.post(url, json=payload, timeout=timeout_s, proxies=None)
            except Exception:
                raise e
        else:
            raise
    try:
        r.raise_for_status()
    except Exception as e:
        try:
            body = str(getattr(r, "text", "") or "")[:800]
        except Exception:
            body = ""
        raise RuntimeError(f"editMessageCaption_http_error: {e}; body={body}") from e
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegram error: {data}")


def build_order_follow_combined_message(
    *,
    order: Dict[str, Any],
    follow_result: Dict[str, Any],
    ui_theme: Optional[Dict[str, Any]] = None,
) -> str:
    def fmt_plain_amount(v: Any) -> str:
        try:
            f = float(v)
        except Exception:
            return str(v or "未知")
        if abs(f - round(f)) < 1e-9:
            return str(int(round(f)))
        return f"{f:.2f}".rstrip("0").rstrip(".")

    def planned_follow_amount(v: Dict[str, Any]) -> float:
        try:
            total = v.get("split_total_plan")
            if total not in (None, ""):
                return float(total)
        except Exception:
            pass
        try:
            parts = v.get("split_parts")
            if isinstance(parts, list) and parts:
                total = sum(float(part.get("stake") or 0.0) for part in parts if isinstance(part, dict))
                if total > 0:
                    return total
        except Exception:
            pass
        try:
            parts_map = v.get("split_part_results")
            if isinstance(parts_map, dict) and parts_map:
                total = sum(float(part.get("stake") or 0.0) for part in parts_map.values() if isinstance(part, dict))
                if total > 0:
                    return total
        except Exception:
            pass
        try:
            return float(v.get("stake") or 0.0)
        except Exception:
            return 0.0

    order_id = str(order.get("orderId") or "")
    league = str(order.get("league") or "未知")
    sides = str(order.get("sides") or "未知")
    backend_stake = str(order.get("stake") or "未知")
    order_status = "滚球" if bool(is_live_backend_order(order)) else "早盘"
    backend_source = resolve_order_source_name(order, follow_result=follow_result) or "-"
    dt = f"{(order.get('date') or '')} {(order.get('time') or '')}".strip()
    bet_display = parse_bet_display(order, order_status)

    lines: List[str] = []
    lines.append(
        f"<b>{_order_card_icon(ui_theme, 'title', '🤣')}新注单提醒"
        f"{_order_card_suffix(ui_theme, 'title', '🤣')}</b>"
    )
    divider = _order_card_divider(ui_theme, fallback=_ORDER_DIVIDER_FALLBACKS)
    if divider:
        lines.append(divider)
    lines.append(_themed_line(_order_card_icon(ui_theme, 'league', '🤣'), html.escape(league), spaced=False))
    lines.append(_themed_line(_order_card_icon(ui_theme, 'match', '🤣'), html.escape(sides), spaced=False))
    lines.append("")
    lines.append(_themed_line(_order_card_icon(ui_theme, 'status', '📍'), f"状态：{html.escape(order_status)}", spaced=True))
    lines.append(_themed_line(_order_card_icon(ui_theme, 'play', ''), f"玩法：{html.escape(bet_display['bet_label'])}", spaced=False))
    lines.append(_themed_line(_order_card_icon(ui_theme, 'play', ''), f"时段：{html.escape(bet_display['period'])}", spaced=False))
    lines.append(_themed_line(_order_card_icon(ui_theme, 'play', ''), f"选择：{html.escape(bet_display['selection_text'])}", spaced=False))
    lines.append("")
    lines.append(_themed_line(_order_card_icon(ui_theme, 'stake', '💰'), f"原注金额：{html.escape(fmt_plain_amount(backend_stake))}", spaced=True))

    success_total = _sum_follow_success_amount(follow_result)
    plan_total = planned_follow_amount(follow_result)
    reverse_suffix = ""
    try:
        if bool((follow_result.get("reverse_rule") or {}).get("applied")):
            reverse_suffix = "（反打订单）"
    except Exception:
        pass
    lines.append(
        _themed_line(
            _order_card_icon(ui_theme, 'follow_stake', '💲'),
            f"跟单金额：{html.escape(fmt_plain_amount(plan_total))}{html.escape(reverse_suffix)}",
            spaced=True,
        )
    )
    lines.append(
        f"{_themed_line(_order_card_icon(ui_theme, 'follow_success', '🐦‍⬛'), f'跟单成功：{html.escape(fmt_plain_amount(success_total))}', spaced=False)}"
        f"{_order_card_suffix(ui_theme, 'follow_success', '🤣')}"
    )
    lines.append("")
    lines.append(_themed_line(_order_card_icon(ui_theme, 'source', ''), f"来源后台：{html.escape(backend_source)}", spaced=False))
    lines.append(_themed_line(_order_card_icon(ui_theme, 'order_id', '🧠'), f"单号：{html.escape(order_id or '-')}", spaced=False))
    lines.append(_themed_line(_order_card_icon(ui_theme, 'time', '🗓'), f"时间：{html.escape(dt or now_cn())}", spaced=False))
    return "\n".join(lines)

def member_post(
    *,
    base_url: str,
    ver: str,
    proxy: str,
    cookie: str,
    form: Dict[str, Any],
    timeout_s: int,
) -> str:
    base = str(base_url or "").rstrip("/")
    url = f"{base}/transform.php?ver={ver}"
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "origin": base,
        "referer": base + "/",
        "content-type": "application/x-www-form-urlencoded",
        "x-requested-with": "XMLHttpRequest",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    }
    if cookie:
        headers["cookie"] = cookie
    proxies = {"http": proxy, "https": proxy} if proxy else None
    # 会员站常用 IP/自签证书，保持与浏览器一致：忽略证书校验
    hard_s = max(2, int(timeout_s or 0) + 2)
    with _hard_timeout(hard_s):
        r = requests.post(url, data=form, headers=headers, proxies=proxies, timeout=_requests_timeout(timeout_s), verify=False)
    r.raise_for_status()
    return r.text or ""


def parse_decimal_line(s: str) -> float:
    s = str(s or "").strip().replace("\\/", "/")
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def parse_split_line_avg(s: str) -> float:
    s = str(s or "").strip().replace("\\/", "/")
    if not s:
        return 0.0
    if "/" not in s:
        return parse_decimal_line(s)
    parts = [p.strip() for p in s.split("/") if p.strip()]
    vals = []
    for p in parts[:2]:
        try:
            vals.append(abs(float(p)))
        except Exception:
            pass
    if not vals:
        return 0.0
    if len(vals) == 1:
        return vals[0]
    return (vals[0] + vals[1]) / 2.0


def parse_signed_split_line_from_text(s: str) -> float:
    """
    从类似“主队 -0 / 0.5”或 HTML 片段里提取盘口线，支持“只在第一段带符号”的写法：
    - “-0 / 0.5” 视为 “-0 / -0.5”（平均=-0.25）
    - “+0 / 0.5” 视为 “+0 / +0.5”（平均=+0.25）
    """
    text = str(s or "").replace("\\/", "/")
    if not text:
        return 0.0

    pat = re.compile(r"([+-]?\d+(?:\.\d+)?)(?:\s*/\s*([+-]?\d+(?:\.\d+)?))?")
    best = None
    for m in pat.finditer(text):
        a = m.group(1)
        b = m.group(2)
        if b is None:
            continue
        best = (a, b)
        break
    if not best:
        # 没有“a / b”结构时，尝试从文本末尾提取一个“单值盘口线”（常见：'球队 +0.5' / '球队 0'）
        m1 = re.search(r"([+-]?\d+(?:\.\d+)?)\s*$", text)
        if m1:
            try:
                return float(m1.group(1))
            except Exception:
                pass
        # 再退化：从文本中找第一个带符号的数字（避免误抓球队名里的编号）
        m2 = re.search(r"([+-]\d+(?:\.\d+)?)", text)
        if m2:
            try:
                return float(m2.group(1))
            except Exception:
                pass
        return 0.0

    a_str, b_str = best
    try:
        a_val = float(a_str)
        b_val = float(b_str)
    except Exception:
        return 0.0

    sign1 = None
    a_s = a_str.strip()
    if a_s.startswith("-"):
        sign1 = -1
    elif a_s.startswith("+"):
        sign1 = 1
    # 第二段若未显式带符号，则继承第一段符号
    b_s = b_str.strip()
    if sign1 is not None and not (b_s.startswith("-") or b_s.startswith("+")):
        b_val = sign1 * abs(b_val)
    return (a_val + b_val) / 2.0


def parse_total_line_from_text(s: Any) -> float:
    """
    从大小盘展示文本里提取“目标盘口值”：
    - "大 1 / 1.5" -> 1.25
    - "小 2.5" -> 2.5
    - 回退到 parse_split_line_avg 兼容纯数值/接口字段
    """
    text = str(s or "").strip()
    if not text:
        return 0.0
    try:
        lv = abs(float(parse_signed_split_line_from_text(text)))
        if lv > 0:
            return lv
    except Exception:
        pass
    try:
        return float(max(0.0, parse_split_line_avg(text)))
    except Exception:
        return 0.0


def _split_match_sides_text(text: Any) -> Tuple[str, str]:
    s = str(text or "").strip()
    if not s:
        return "", ""
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    for sep in (" v ", " vs ", " VS ", " V "):
        if sep in s:
            left, right = s.split(sep, 1)
            return str(left or "").strip(), str(right or "").strip()
    return "", ""


def _resolve_team_names_for_order(order: Dict[str, Any], raw: Dict[str, Any]) -> Tuple[str, str]:
    team_h = str(raw.get("TEAM_H") or "").strip()
    team_c = str(raw.get("TEAM_C") or "").strip()
    if team_h and team_c:
        return team_h, team_c

    for source in (
        order.get("sides"),
        raw.get("SHOWTEXT_TEAM"),
        raw.get("TNAME"),
    ):
        h, c = _split_match_sides_text(source)
        if h and c:
            return h, c
    return team_h, team_c


def _resolve_order_pick_text(order: Dict[str, Any], raw: Dict[str, Any], order_type: str) -> str:
    pick = str(order_type or "").strip()
    if pick:
        return pick
    line = str(order.get("line") or "").strip()
    if line:
        m = re.match(r"^(.*?)(?:\s+[+-]?\d+(?:\.\d+)?(?:\s*/\s*[+-]?\d+(?:\.\d+)?)?)\s*$", line)
        if m:
            p = str(m.group(1) or "").strip()
            if p:
                return p
        return line
    show = str(raw.get("SHOWTEXT_ORDER_TYPE_IORATIO") or raw.get("TNAME") or "")
    m = re.search(r"<li[^>]*>\s*([^<]+?)\s*<tt class=\"word_red\">", show)
    if m:
        return str(m.group(1) or "").strip()
    return ""


_CORNER_NTH_WTYPES: Tuple[str, ...] = (
    "RNC1",
    "RNC2",
    "RNC3",
    "RNC4",
    "RNC5",
    "RNC6",
    "RNC7",
    "RNC8",
    "RNC9",
    "RNCA",
    "RNCB",
    "RNCC",
    "RNCD",
    "RNCE",
    "RNCF",
    "RNCG",
    "RNCH",
    "RNCI",
    "RNCJ",
    "RNCK",
    "RNCL",
    "RNCM",
    "RNCN",
    "RNCO",
    "RNCP",
    "RNCQ",
    "RNCR",
    "RNCS",
    "RNCT",
    "RNCU",
)

_CHINESE_ORDINAL_NUMBERS: Dict[str, int] = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
    "十三": 13,
    "十四": 14,
    "十五": 15,
    "十六": 16,
    "十七": 17,
    "十八": 18,
    "十九": 19,
    "二十": 20,
    "二十一": 21,
    "二十二": 22,
    "二十三": 23,
    "二十四": 24,
    "二十五": 25,
    "二十六": 26,
    "二十七": 27,
    "二十八": 28,
    "二十九": 29,
    "三十": 30,
}


def _parse_corner_nth_wtype(text: Any) -> str:
    s = str(text or "").strip()
    if not s or "角球" not in s:
        return ""
    m = re.search(r"第\s*([0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*个角球", s)
    if not m:
        return ""
    token = str(m.group(1) or "").strip()
    try:
        n = int(token)
    except Exception:
        n = int(_CHINESE_ORDINAL_NUMBERS.get(token, 0) or 0)
    if n < 1 or n > len(_CORNER_NTH_WTYPES):
        return ""
    return _CORNER_NTH_WTYPES[n - 1]


def _line_value_from_con_ratio_for_wtype(wtype: str, con: Any, ratio: Any) -> Optional[float]:
    wt = str(wtype or "").strip().upper()
    if not wt:
        return None
    # 大小盘：用绝对值表达“盘口值”，不暴露方向编码细节
    if wt.endswith("OU"):
        lv = decode_total_line(con, ratio)
        if lv is None:
            return None
        try:
            return abs(float(lv))
        except Exception:
            return None
    # 让球盘：保留正负号表达方向（+0.5/-0.5）
    if wt.endswith("RE") or wt.endswith("R"):
        lv = decode_handicap_line(con, ratio)
        if lv is None:
            return None
        try:
            return float(lv)
        except Exception:
            return None
    return None


def _format_line_value_for_message(wtype: str, lv: Optional[float]) -> str:
    if lv is None:
        return "未知"
    wt = str(wtype or "").strip().upper()
    try:
        f = float(lv)
    except Exception:
        return str(lv)
    if wt.endswith("OU"):
        return f"{abs(f):g}"
    if wt.endswith("RE") or wt.endswith("R"):
        return format_handicap_line_value(f)
    return f"{f:g}"

def _is_over_from_chose_team(chose_team: Any) -> bool:
    """
    统一大小盘方向判定：
    - 主盘常见：C=大（Over），H=小（Under）
    - 更多盘口/附加盘有时使用：O=大（Over），U=小（Under）
    """
    ct = str(chose_team or "").strip().upper()
    if ct in {"C", "O"}:
        return True
    if ct in {"H", "U"}:
        return False
    return ct == "C"


def _candidate_wtypes_for_same_line_mapping(wtype: str, *, quick: bool = False) -> List[str]:
    """
    同场比赛内“主盘口 ↔ 附加盘口”的 wtype 候选。
    仅用于“盘口值不变（例如 2.5 仍然是 2.5）”的等价映射。
    """
    wt = str(wtype or "").strip().upper()
    if not wt:
        return []

    # 附加盘口前缀：历史抓包/站点约定通常为 A-F（避免误把 HOU/HROU/HRE 等“上半场玩法”当成附加前缀）
    # quick 模式下仅扩展 A/B/C（更快）；但仍需识别 D/E/F 前缀以正确剥离 base。
    all_prefixes = ["A", "B", "C", "D", "E", "F"]
    prefixes = (["A", "B", "C"] if bool(quick) else list(all_prefixes))

    # 支持从“附加盘口 wtype”（如 AROU/BROU/ARE...）继续寻找其它附加盘口
    known_bases = {"RE", "R", "ROU", "OU", "HRE", "HR", "HROU", "HOU"}
    base = wt
    if wt not in known_bases and len(wt) > 1 and wt[0] in all_prefixes and wt[1:] in known_bases:
        base = wt[1:]

    def with_prefixes(x: str) -> List[str]:
        x2 = str(x or "").strip().upper()
        if not x2:
            return []
        return [x2] + [p + x2 for p in prefixes]

    out: List[str] = []
    seen = set()

    def add(items: List[str]) -> None:
        for it in items:
            it2 = str(it or "").strip().upper()
            if not it2 or it2 in seen:
                continue
            seen.add(it2)
            out.append(it2)

    # 让球：R <-> RE；上半场：HR <-> HRE（保持兼容）
    if base in {"RE", "R"}:
        add(with_prefixes(base))
        add(with_prefixes("R" if base == "RE" else "RE"))
        return out
    if base in {"HRE", "HR"}:
        add(with_prefixes(base))
        add(with_prefixes("HR" if base == "HRE" else "HRE"))
        return out

    # 大小：OU <-> ROU；部分站点也会用 FOU 表示全场大小盘
    if base in {"ROU", "OU", "FOU"}:
        add(with_prefixes(base))
        # 三者互转（尽量覆盖主盘/滚球/更多盘口的不同命名）
        add(with_prefixes("OU"))
        add(with_prefixes("ROU"))
        add(with_prefixes("FOU"))
        return out
    if base in {"HROU", "HOU"}:
        add(with_prefixes(base))
        add(with_prefixes("HOU" if base == "HROU" else "HROU"))
        return out

    # 上半场大小盘：部分站点用 HFOU
    if base in {"HFOU"}:
        add(with_prefixes("HFOU"))
        add(with_prefixes("HOU"))
        add(with_prefixes("HROU"))
        return out

    # 泛化：部分站点存在其它“全场大小盘”wtype（例如 FOU），但后端/接口字段仍可能以 OU/ROU 命名返回。
    # 规则：只要 wtype 以 OU 结尾，就把 OU/ROU 以及其 A-F 附加盘口加入候选（低风险：仅用于“同一盘口值”映射兜底）。
    if wt.endswith("OU"):
        # 支持 AFOU 这类写法：将 core 视为去掉前缀后的 OU-like wtype
        core = wt
        if len(wt) > 1 and wt[0] in all_prefixes and wt[1:].endswith("OU"):
            core = wt[1:]
        add(with_prefixes(core))
        add(with_prefixes("OU"))
        add(with_prefixes("ROU"))
        return out

    return [wt]


def _is_extra_market_wtype(wtype: str) -> bool:
    wt = str(wtype or "").strip().upper()
    if not wt or len(wt) < 2:
        return False
    prefixes = {"A", "B", "C", "D", "E", "F"}
    known_bases = {"RE", "R", "ROU", "OU", "HRE", "HR", "HROU", "HOU"}
    return wt[0] in prefixes and wt[1:] in known_bases


def _maybe_switch_total_to_extra_market_wtype(params: "BetParams", *, matched_wtype: str, used_ior_tag: str) -> None:
    """
    大小盘“严格跟原线”时，目标盘口线可能从主盘(ROU/OU)挪到附加盘(AROU/BROU/AOU/BOU...)。

    若本次刷新命中了附加盘（A-F 前缀），则把 params 切到对应 wtype/rtype/chose_team，
    避免出现“水位来自附加盘，但下注仍用主盘 wtype/rtype”导致 1X015/水位对不上。
    """
    wt_hit = str(matched_wtype or "").strip().upper()
    if not _is_extra_market_wtype(wt_hit):
        return

    wt_now = str(getattr(params, "wtype", "") or "").strip().upper()
    if not wt_now:
        return

    # 避免把滚球下注从 ROU 误切到 OU：只允许切到“同一 base”的附加盘口
    base_now = wt_now[1:] if _is_extra_market_wtype(wt_now) else wt_now
    base_hit = wt_hit[1:]
    if base_hit != base_now:
        return

    tag = str(used_ior_tag or "").strip().upper()
    if not tag:
        return
    if tag.startswith("IOR_"):
        rtype_hit = tag[len("IOR_") :].strip().upper()
    elif tag.startswith("IOR"):
        rtype_hit = tag[len("IOR") :].strip().lstrip("_").upper()
    else:
        return

    if not rtype_hit.startswith(wt_hit):
        return
    side = rtype_hit[len(wt_hit) :].strip().upper()
    # 只支持 1 字符 side（H/C 或 O/U）；多字符（如 HO/HU）此处不切换，避免误判为“主/客队大小球”
    if side not in {"H", "C", "O", "U"}:
        return

    params.wtype = wt_hit
    params.rtype = rtype_hit
    params.chose_team = side


def _parse_score_pair(text: Any) -> Optional[Tuple[int, int]]:
    s = str(text or "")
    if not s:
        return None
    m = re.search(r"\((\d+)\s*-\s*(\d+)\)", s)
    if not m:
        m = re.search(r"(\d+)\s*-\s*(\d+)", s)
    if not m:
        m = re.search(r"(\d+)\s*:\s*(\d+)", s)
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except Exception:
        return None


def _format_score_pair(pair: Tuple[int, int]) -> str:
    try:
        return f"{int(pair[0])}-{int(pair[1])}"
    except Exception:
        return str(pair)


def _extract_backend_score(order: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    raw = order.get("raw") or {}
    if not isinstance(raw, dict):
        raw = {}
    for k in ["sides", "line", "betType"]:
        sc = _parse_score_pair(order.get(k))
        if sc:
            return sc
    for k in ["SHOWTEXT_TEAM", "TNAME", "SHOWTEXT_ORDER_TYPE_IORATIO"]:
        sc = _parse_score_pair(raw.get(k))
        if sc:
            return sc
    return None


def _extract_live_score_from_game_node(game: ET.Element) -> Optional[Tuple[int, int]]:
    def get_text(tag: str) -> str:
        for t in [tag, tag.upper(), tag.lower()]:
            try:
                v = game.findtext(t)
            except Exception:
                v = None
            if v is None:
                continue
            v2 = str(v).strip()
            if v2:
                return v2
        return ""

    # 常见：分别存主/客分数
    for a, b in [
        ("MB_INBALL", "TG_INBALL"),
        ("MB_BALL", "TG_BALL"),
        ("MB_SCORE", "TG_SCORE"),
        ("SCORE_H", "SCORE_C"),
        ("SC_H", "SC_C"),
    ]:
        ta = get_text(a)
        tb = get_text(b)
        if ta.isdigit() and tb.isdigit():
            return int(ta), int(tb)

    # 兜底：单字段形如 "1-0" / "1:0"
    for k in ["SCORE", "SCORE_NOW", "SCORE_FULL", "FT_SCORE", "RB_SCORE"]:
        sc = _parse_score_pair(get_text(k))
        if sc:
            return sc

    # 最后兜底：从节点文本里抓取一次
    try:
        xml_text = ET.tostring(game, encoding="unicode", method="xml")
    except Exception:
        xml_text = ""
    m = re.search(r"\((\d+)\s*-\s*(\d+)\)", xml_text or "")
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except Exception:
        return None

def is_live_backend_order(order: Dict[str, Any]) -> bool:
    raw = order.get("raw") or {}
    if isinstance(raw, dict):
        market = str(raw.get("MARKET") or "").strip().lower()
        if market == "rb":
            return True
    bet_type = str(order.get("betType") or "").strip()
    return "滚球" in bet_type


def resolve_order_source_name(order: Dict[str, Any], *, follow_result: Optional[Dict[str, Any]] = None) -> str:
    if not isinstance(order, dict):
        order = {}
    if not isinstance(follow_result, dict):
        follow_result = {}
    return str(
        order.get("backend_source")
        or order.get("_source_target")
        or order.get("target")
        or follow_result.get("backend_source")
        or follow_result.get("target")
        or order.get("account")
        or "-"
    ).strip() or "-"


def _find_target_rule(settings: Dict[str, Any], target: str) -> Optional[Dict[str, Any]]:
    tname = str(target or "").strip()
    if not tname:
        return None
    items = settings.get("targets") or []
    if not isinstance(items, list):
        return None
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if name != tname:
            continue
        return it
    return None


def target_ratio_percent_override(settings: Dict[str, Any], target: str) -> Optional[float]:
    rule = _find_target_rule(settings, target)
    if not isinstance(rule, dict):
        return None
    raw = rule.get("ratio_percent")
    if raw in (None, ""):
        return None
    try:
        return max(0.0, min(500.0, float(raw)))
    except Exception:
        return None


def target_only_live(settings: Dict[str, Any], target: str) -> bool:
    """
    只跟滚球过滤：全局开关优先，旧配置回退到后台级规则。
    - settings["only_live"] 为 True：所有后台只跟滚球。
    - settings["only_live"] 为 False：明确关闭全局过滤，早盘和滚球都跟。
    - settings["only_live"] 为 None/缺失：沿用旧的 per-target 规则。
    - settings["targets"] 约定为：[{ "name": "<target>", "only_live": true/false }, ...]
    """
    if isinstance(settings, dict) and "only_live" in settings and settings.get("only_live") is not None:
        raw = settings.get("only_live")
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)
    rule = _find_target_rule(settings, target)
    return bool(rule.get("only_live") or False) if isinstance(rule, dict) else False


def target_follow_enabled(settings: Dict[str, Any], target: str) -> bool:
    """
    per-target 过滤：是否跟这个后台。
    - 未配置 enabled 时默认 True，保持旧行为。
    - 显式 enabled=false 时跳过该后台所有来单。
    """
    rule = _find_target_rule(settings, target)
    if not isinstance(rule, dict):
        return True
    return bool(rule.get("enabled", True))




def _append_text_child(parent: ET.Element, tag: str, value: Any) -> None:
    tag0 = str(tag or "").strip()
    if not tag0:
        return
    if value is None:
        return
    if isinstance(value, (dict, list, tuple)):
        return
    text = str(value).strip()
    if text == "":
        return
    child = ET.SubElement(parent, tag0.upper())
    child.text = text


def _flatten_json_game_fields(game_el: ET.Element, obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            key0 = str(key or "").strip()
            if not key0:
                continue
            if isinstance(value, (dict, list, tuple)):
                _flatten_json_game_fields(game_el, value)
            else:
                _append_text_child(game_el, key0, value)
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _flatten_json_game_fields(game_el, item)


def _game_el_from_json_game(obj: Any, *, fallback_gid: str = "") -> ET.Element:
    game_el = ET.Element("game")
    _flatten_json_game_fields(game_el, obj)
    if not str(game_el.findtext("GID") or "").strip():
        for tag in ("ECID", "GIDM", "GID_M"):
            v = str(game_el.findtext(tag) or "").strip()
            if v:
                _append_text_child(game_el, "GID", v)
                break
    if not str(game_el.findtext("GID") or "").strip() and str(fallback_gid or "").strip():
        _append_text_child(game_el, "GID", str(fallback_gid or "").strip())
    return game_el


def _game_list_games_from_json_response(text: str, *, requested_gid: str = "") -> List[ET.Element]:
    try:
        obj = json.loads(str(text or "").strip())
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []
    response = obj.get("response")
    if not isinstance(response, dict):
        return []

    raw_games: List[Any] = []
    for key, value in response.items():
        if isinstance(value, dict) and str(key or "").upper().startswith("GAME_"):
            raw_games.append(value)
    if not raw_games:
        games_obj = response.get("games") or response.get("GAMES")
        if isinstance(games_obj, list):
            raw_games.extend([x for x in games_obj if isinstance(x, dict)])
        elif isinstance(games_obj, dict):
            raw_games.extend([x for x in games_obj.values() if isinstance(x, dict)])

    fallback_gid = str(requested_gid or "").strip() if len(raw_games) == 1 else ""
    return [_game_el_from_json_game(g, fallback_gid=fallback_gid) for g in raw_games]


def fetch_game_list_game(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    gtype: str,
    showtype: str,
    rtype: str,
    p3type: str = "",
    timeout_s: int,
    gid: str,
) -> Tuple[Optional[ET.Element], str]:
    """
    从会员端 get_game_list 返回中定位指定 GID 的 <game> 节点。
    """
    base0 = str(base_url or "").strip().rstrip("/")
    uid0 = str(uid or "").strip()
    ver0 = str(ver or "").strip()
    gtype0 = str(gtype or "").strip().lower()
    showtype0 = str(showtype or "").strip().lower() or "today"
    rtype0 = str(rtype or "").strip().lower() or "r"
    p3type0 = str(p3type or "").strip()
    cache_key = f"get_game_list|{base0}|{uid0}|{ver0}|{gtype0}|{showtype0}|{rtype0}|{p3type0}"
    ts_ms = str(int(time.time() * 1000))
    form = {
        "p": "get_game_list",
        "uid": uid0,
        "ver": ver0,
        "langx": "zh-cn",
        "p3type": p3type0,
        "date": "",
        "gtype": gtype0,
        "showtype": showtype0,
        "rtype": rtype0,
        "ltype": str(_DEFAULT_LTYPE),
        "filter": "",
        "cupFantasy": "N",
        "sorttype": "L",
        "specialClick": "",
        "isFantasy": "N",
        "ts": ts_ms,
        # 浏览器端常见字段：部分站点缺失时会返回异常/不完整数据
        "chgSortTS": ts_ms,
    }
    try:
        text = _xml_cache_get_or_fetch(
            _GAME_LIST_XML_CACHE,
            cache_key,
            float(_GAME_LIST_XML_CACHE_TTL_S),
            lambda: str(member_post_session(session=session, base_url=base0, ver=ver0, form=form, timeout_s=timeout_s) or ""),
        )
    except BaseException as e:
        return None, f"game_list_request_failed: {e}"
    t = (text or "").strip()
    if not t:
        return None, "game_list_empty_response"
    if not t.startswith("<"):
        json_games = _game_list_games_from_json_response(t, requested_gid=gid)
        if json_games:
            gid2 = str(gid or "").strip()
            for g in json_games:
                if str((g.findtext("GID") or "")).strip() == gid2:
                    return g, "ok"
            return None, "game_not_found"
        # 常见：code_type is null / CheckEMNU / VariableStandard 等上下文问题，先做一次 full warmup 再重试
        if _looks_like_ctx_blocked_msg(t):
            try:
                member_warmup_menu(
                    session=session,
                    base_url=base0,
                    ver=ver0,
                    uid=uid0,
                    gtype=gtype0,
                    showtype=showtype0,
                    rtype=rtype0,
                    timeout_s=int(min(6, int(timeout_s))),
                )
            except Exception:
                pass
            try:
                text2 = member_post_session(session=session, base_url=base0, ver=ver0, form=form, timeout_s=timeout_s)
            except BaseException as e:
                return None, f"game_list_request_failed: {e}"
            _xml_cache_set(_GAME_LIST_XML_CACHE, cache_key, str(text2 or ""))
            t = (text2 or "").strip()
            if not t:
                return None, "game_list_empty_response"
            if not t.startswith("<"):
                json_games = _game_list_games_from_json_response(t, requested_gid=gid)
                if json_games:
                    gid2 = str(gid or "").strip()
                    for g in json_games:
                        if str((g.findtext("GID") or "")).strip() == gid2:
                            return g, "ok"
                    return None, "game_not_found"
                return None, f"game_list_non_xml: {t[:120]}"
        else:
            return None, f"game_list_non_xml: {t[:120]}"
    try:
        root = ET.fromstring(t)
    except Exception as e:
        return None, f"game_list_xml_parse_failed: {e}"
    gid2 = str(gid or "").strip()
    for g in root.iter("game"):
        if str((g.findtext("GID") or "")).strip() == gid2:
            return g, "ok"
    return None, "game_not_found"

def fetch_game_list_game_context(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    gtype: str,
    showtype: str,
    rtype: str,
    p3type: str = "",
    timeout_s: int,
    gid: str,
) -> Tuple[Optional[ET.Element], str, str, str]:
    """
    get_game_list: 定位指定 GID 的 <game> 节点，并尽量返回 right_score/get_game_more 所需的上下文：
    - ecid_num：纯数字 ecid（如 10419703）
    - lid：联赛 ID
    """
    base0 = str(base_url or "").strip().rstrip("/")
    uid0 = str(uid or "").strip()
    ver0 = str(ver or "").strip()
    gtype0 = str(gtype or "").strip().lower()
    showtype0 = str(showtype or "").strip().lower() or "today"
    rtype0 = str(rtype or "").strip().lower() or "r"
    p3type0 = str(p3type or "").strip()
    # 与 fetch_game_list_game 复用同一份短 TTL 缓存（请求完全一致）
    cache_key = f"get_game_list|{base0}|{uid0}|{ver0}|{gtype0}|{showtype0}|{rtype0}|{p3type0}"
    ts_ms = str(int(time.time() * 1000))
    form = {
        "p": "get_game_list",
        "uid": uid0,
        "ver": ver0,
        "langx": "zh-cn",
        "p3type": p3type0,
        "date": "",
        "gtype": gtype0,
        "showtype": showtype0,
        "rtype": rtype0,
        "ltype": str(_DEFAULT_LTYPE),
        "filter": "",
        "cupFantasy": "N",
        "sorttype": "L",
        "specialClick": "",
        "isFantasy": "N",
        "ts": ts_ms,
        "chgSortTS": ts_ms,
    }
    try:
        text = _xml_cache_get_or_fetch(
            _GAME_LIST_XML_CACHE,
            cache_key,
            float(_GAME_LIST_XML_CACHE_TTL_S),
            lambda: str(member_post_session(session=session, base_url=base0, ver=ver0, form=form, timeout_s=timeout_s) or ""),
        )
    except Exception as e:
        return None, "", "", f"game_list_request_failed: {e}"
    t = (text or "").strip()
    if not t:
        return None, "", "", "game_list_empty_response"
    if not t.startswith("<"):
        if _looks_like_ctx_blocked_msg(t):
            try:
                member_warmup_menu(
                    session=session,
                    base_url=base0,
                    ver=ver0,
                    uid=uid0,
                    gtype=gtype0,
                    showtype=showtype0,
                    rtype=rtype0,
                    timeout_s=int(min(6, int(timeout_s))),
                )
            except Exception:
                pass
            try:
                text2 = member_post_session(session=session, base_url=base0, ver=ver0, form=form, timeout_s=timeout_s)
            except Exception as e:
                return None, "", "", f"game_list_request_failed: {e}"
            _xml_cache_set(_GAME_LIST_XML_CACHE, cache_key, str(text2 or ""))
            t = (text2 or "").strip()
            if not t:
                return None, "", "", "game_list_empty_response"
            if not t.startswith("<"):
                return None, "", "", f"game_list_non_xml: {t[:120]}"
        else:
            return None, "", "", f"game_list_non_xml: {t[:120]}"
    try:
        root = ET.fromstring(t)
    except Exception as e:
        return None, "", "", f"game_list_xml_parse_failed: {e}"

    gid2 = str(gid or "").strip()

    # 优先：在 <ec id="ec123"> 结构中定位（便于提取 ecid）
    for ec in root.iter("ec"):
        ecid_attr = ""
        try:
            ecid_attr = str(ec.get("id") or ec.get("ID") or "").strip()
        except Exception:
            ecid_attr = ""
        ecid_num = ""
        if ecid_attr:
            m = re.search(r"(\d+)$", ecid_attr)
            if m:
                ecid_num = m.group(1)
        for g in ec.iter("game"):
            gid_text = str((g.findtext("GID") or g.findtext("gid") or "")).strip()
            if gid_text != gid2:
                continue
            lid = str((g.findtext("LID") or g.findtext("lid") or "")).strip()
            try:
                if ecid_num and lid:
                    _set_cached_game_ctx(base_url=base_url, uid=uid, gid=gid2, lid=lid, ecid=ecid_num, showtype=showtype0)
            except Exception:
                pass
            return g, ecid_num, lid, "ok"

    # 兜底：无 ec 包裹的列表
    for g in root.iter("game"):
        gid_text = str((g.findtext("GID") or g.findtext("gid") or "")).strip()
        if gid_text != gid2:
            continue
        lid = str((g.findtext("LID") or g.findtext("lid") or "")).strip()
        return g, "", lid, "ok_no_ecid"

    return None, "", "", "game_not_found"


def fetch_game_more_game(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    gtype: str,
    showtype: str,
    is_live_order: bool,
    lid: str,
    ecid_num: str,
    timeout_s: int,
    gid: str,
    filter: str = "Main",
) -> Tuple[Optional[ET.Element], str]:
    text, msg = fetch_game_more_xml_text(
        session=session,
        base_url=base_url,
        ver=ver,
        uid=uid,
        gtype=gtype,
        showtype=showtype,
        is_live_order=is_live_order,
        lid=lid,
        ecid_num=ecid_num,
        timeout_s=timeout_s,
        filter=filter,
    )
    if not text:
        return None, msg
    t = (text or "").strip()
    if not t.startswith("<"):
        return None, msg
    try:
        root = ET.fromstring(t)
    except Exception as e:
        return None, f"game_more_xml_parse_failed: {e}"
    gid2 = str(gid or "").strip()
    for g in root.iter("game"):
        gid_text = str((g.findtext("gid") or g.findtext("GID") or "")).strip()
        if gid_text == gid2:
            return g, "ok"
    return None, "game_not_found"


def fetch_game_more_xml_text(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    gtype: str,
    showtype: str,
    is_live_order: bool,
    lid: str,
    ecid_num: str,
    timeout_s: int,
    filter: str = "Main",
) -> Tuple[Optional[str], str]:
    """
    右侧更多盘口接口（p=get_game_more / right_score 页面）：
    - 同一联赛/分组下，会返回更完整的盘口字段（包含附加盘 A-F 的 ratio/ior）
    - ecid 必须是纯数字（例如 10419703），否则会返回 “ecid No number Data error”
    """
    base0 = str(base_url or "").strip().rstrip("/")
    uid0 = str(uid or "").strip()
    ver0 = str(ver or "").strip()
    gtype0 = str(gtype or "").strip().lower() or "ft"
    showtype0 = str(showtype or "").strip().lower() or "today"
    lid0 = str(lid or "").strip()
    ecid0 = str(ecid_num or "").strip()
    flt0 = str(filter or "Main").strip() or "Main"
    rb0 = "rb" if bool(is_live_order) else "r"
    cache_key = f"get_game_more|{base0}|{uid0}|{ver0}|{gtype0}|{showtype0}|{rb0}|{lid0}|{ecid0}|{flt0}"
    form = {
        "p": "get_game_more",
        "uid": uid0,
        "ver": ver0,
        "langx": "zh-cn",
        "gtype": gtype0,
        "showtype": showtype0,
        "ltype": str(_DEFAULT_LTYPE),
        "isRB": "Y" if bool(is_live_order) else "N",
        "lid": lid0,
        "specialClick": "",
        "mode": "NORMAL",
        "from": "game_more",
        "filter": flt0,
        "ts": str(int(time.time() * 1000)),
        "ecid": ecid0,
    }
    try:
        text = _xml_cache_get_or_fetch(
            _GAME_MORE_XML_CACHE,
            cache_key,
            float(_GAME_MORE_XML_CACHE_TTL_S),
            lambda: str(member_post_session(session=session, base_url=base0, ver=ver0, form=form, timeout_s=timeout_s) or ""),
        )
    except Exception as e:
        return None, f"game_more_request_failed: {e}"
    t = (text or "").strip()
    if not t:
        return None, "game_more_empty_response"
    if not t.startswith("<"):
        return None, f"game_more_non_xml: {t[:120]}"
    return t, "ok"


def fetch_game_obt_xml_text(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    gtype: str,
    showtype: str,
    is_live_order: bool,
    ecid_num: str,
    timeout_s: int,
) -> Tuple[Optional[str], str]:
    """
    p=get_game_OBT：网页端用于打开特殊滚球玩法的盘口源。
    当前只给 RNC*（第几个角球）做水位刷新；请求失败不阻断下注。
    """
    base0 = str(base_url or "").strip().rstrip("/")
    uid0 = str(uid or "").strip()
    ver0 = str(ver or "").strip()
    gtype0 = str(gtype or "").strip().lower() or "ft"
    showtype0 = str(showtype or "").strip().lower() or "live"
    ecid0 = str(ecid_num or "").strip()
    if not ecid0:
        return None, "bad_ecid"
    rb0 = "Y" if bool(is_live_order) else "N"
    cache_key = f"get_game_OBT|{base0}|{uid0}|{ver0}|{gtype0}|{showtype0}|{rb0}|{ecid0}"
    form = {
        "p": "get_game_OBT",
        "uid": uid0,
        "ver": ver0,
        "langx": "zh-cn",
        "gtype": gtype0,
        "showtype": showtype0,
        "isSpecial": "",
        "isEarly": "N" if showtype0 != "early" else "Y",
        "model": "CN",
        "isETWI": "N",
        "ecid": ecid0,
        "ltype": str(_DEFAULT_LTYPE),
        "is_rb": rb0,
        "ts": str(int(time.time() * 1000)),
    }
    try:
        text = _xml_cache_get_or_fetch(
            _GAME_OBT_XML_CACHE,
            cache_key,
            float(_GAME_OBT_XML_CACHE_TTL_S),
            lambda: str(member_post_session(session=session, base_url=base0, ver=ver0, form=form, timeout_s=timeout_s) or ""),
        )
    except Exception as e:
        return None, f"game_obt_request_failed: {e}"
    t = (text or "").strip()
    if not t:
        return None, "game_obt_empty_response"
    if not t.startswith("<"):
        return None, f"game_obt_non_xml: {t[:120]}"
    return t, "ok"


def _find_rnc_ior_in_xml_text(xml_text: str, rtype: str) -> str:
    rtu = str(rtype or "").strip().upper()
    if not (xml_text and rtu):
        return ""
    try:
        root = ET.fromstring(str(xml_text or "").strip())
    except Exception:
        return ""
    wanted = f"IOR_{rtu}".upper()
    for node in root.iter():
        tag = str(getattr(node, "tag", "") or "").strip().upper()
        if tag != wanted:
            continue
        val = str((getattr(node, "text", None) or "")).strip()
        if not val or val == "0":
            continue
        try:
            if float(val) <= 0:
                continue
        except Exception:
            continue
        return val
    return ""


def try_refresh_rnc_odds_via_game_obt(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    params: "BetParams",
    is_live_order: bool,
    timeout_s: int,
    quick: bool = False,
) -> Tuple[bool, str]:
    wt = str(params.wtype or "").strip().upper()
    rtype = str(params.rtype or "").strip().upper()
    if wt not in _CORNER_NTH_WTYPES or not rtype:
        return False, "not_rnc"

    gtype = str(params.gtype or "").strip().lower() or "ft"
    default_showtype = "live" if bool(is_live_order) else "today"
    requested_gid = str(params.gid or "").strip()

    lid, ecid_num, st_hint = _get_cached_game_ctx(base_url=base_url, uid=uid, gid=requested_gid)
    showtype = st_hint or default_showtype

    ctx_showtypes: List[str] = []
    for st in [str(showtype), str(default_showtype), "hot", "today", "early"]:
        st2 = _normalize_showtype_hint(st)
        if st2 and st2 not in ctx_showtypes:
            ctx_showtypes.append(st2)
    if quick:
        ctx_showtypes = _shrink_showtype_candidates_for_quick(ctx_showtypes)

    msg = ""
    if not (lid and ecid_num):
        rtype_list = "rb" if bool(is_live_order) else "r"
        for st in ctx_showtypes:
            st_low = str(st or "").strip().lower()
            if st_low == "live":
                rtypes_to_try = [rtype_list]
            else:
                rtypes_to_try = ["r"]
                if rtype_list not in rtypes_to_try:
                    rtypes_to_try.append(rtype_list)
            for rt in rtypes_to_try:
                try:
                    member_warmup_sport_menu(
                        session=session,
                        base_url=base_url,
                        ver=ver,
                        uid=uid,
                        gtype=gtype,
                        showtype=st,
                        rtype=rt,
                        timeout_s=timeout_s,
                    )
                except Exception:
                    pass
                _g, ecid_num2, lid2, msg = fetch_game_list_game_context(
                    session=session,
                    base_url=base_url,
                    ver=ver,
                    uid=uid,
                    gtype=gtype,
                    showtype=st,
                    rtype=rt,
                    timeout_s=timeout_s,
                    gid=requested_gid,
                )
                if lid2 and ecid_num2:
                    lid, ecid_num, showtype = lid2, ecid_num2, st
                    break
            if lid and ecid_num:
                break

    if not ecid_num:
        return False, f"missing_ecid: msg={msg}"

    showtype_candidates: List[str] = []
    for st in [str(showtype), str(default_showtype), "hot", "today", "early"]:
        st2 = _normalize_showtype_hint(st)
        if st2 and st2 not in showtype_candidates:
            showtype_candidates.append(st2)
    if quick:
        showtype_candidates = _shrink_showtype_candidates_for_quick(showtype_candidates)

    last_msg = ""
    for st in showtype_candidates:
        st_low = str(st or "").strip().lower()
        rb_flags: List[bool] = [bool(is_live_order)] if st_low == "live" else [False, True]
        seen_rb: set[bool] = set()
        for rb_flag in rb_flags:
            if rb_flag in seen_rb:
                continue
            seen_rb.add(rb_flag)
            xml_text, obt_msg = fetch_game_obt_xml_text(
                session=session,
                base_url=base_url,
                ver=ver,
                uid=uid,
                gtype=gtype,
                showtype=st,
                is_live_order=bool(rb_flag),
                ecid_num=ecid_num,
                timeout_s=timeout_s,
            )
            if not xml_text:
                last_msg = f"{st}:{'rb' if rb_flag else 'r'}:{obt_msg}"
                continue
            ior = _find_rnc_ior_in_xml_text(xml_text, rtype)
            if not ior:
                last_msg = f"{st}:{'rb' if rb_flag else 'r'}:{rtype}=empty"
                continue
            params.ioratio = str(ior)
            try:
                if requested_gid:
                    _set_cached_game_ctx(base_url=base_url, uid=uid, gid=requested_gid, lid=lid, ecid=ecid_num, showtype=st)
            except Exception:
                pass
            return True, f"matched:ecid={ecid_num},rtype={rtype},ior={ior},showtype={st}"

    return False, last_msg or "not_found"


def try_refresh_total_odds_via_game_list(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    params: "BetParams",
    is_live_order: bool,
    want_total_abs: float,
    timeout_s: int,
    ior_hint: float = 0.0,
    quick: bool = False,
) -> Tuple[bool, str]:
    """
    使用 get_game_list 刷新“目标盘口线”的 ioratio（支持 5 / 5.5 这种分盘线）。

    抓包确认：部分站点 get_game_more 会把 5 / 5.5 简化成 5，导致仅靠 game_more 无法命中目标盘口线。
    因此：大小盘优先用 get_game_list（它包含完整的 RATIO_ROUO/RATIO_ROUU），game_more 仅作为兜底。

    返回：(ok, msg)。若 ok=True，会就地更新 params.ioratio/con/ratio。
    """
    want = float(want_total_abs or 0.0)
    if want <= 0:
        return False, "bad_want_total"

    gtype = str(params.gtype or "").strip().lower() or "ft"
    gid = str(params.gid or "").strip()
    if not gid:
        return False, "bad_gid"

    # showtype/rtype 探测：live -> hot -> today -> early
    showtype = "live" if bool(is_live_order) else "today"
    showtype_candidates = [str(showtype)]
    for st in ["hot", "today", "early"]:
        if st not in showtype_candidates:
            showtype_candidates.append(st)
    if quick:
        showtype_candidates = _shrink_showtype_candidates_for_quick(showtype_candidates)

    side = str(params.chose_team or "").strip().upper()
    if side not in {"H", "C"}:
        return False, f"bad_side:{side!r}"

    hint = float(ior_hint or 0.0)

    def _pick_best_ior(choices: List[Tuple[str, str]]) -> Tuple[str, str]:
        """
        choices: [(tag, ior_str)]
        返回：(ior, tag)。
        - 优先：IOR_{WT}{H/C}（与 params.chose_team 一致）
        - 其次：与 hint 最接近
        - 最后：第一个非空
        """
        # 过滤空/0
        filtered: List[Tuple[str, str, float]] = []
        for tag, val in choices:
            v = str(val or "").strip()
            if (not v) or v == "0":
                continue
            try:
                fv = float(v)
            except Exception:
                continue
            filtered.append((tag, v, fv))
        if not filtered:
            return "", ""

        # 先找 ior_{wt}{side}
        for tag, v, _ in filtered:
            if tag.endswith(side):
                return v, tag

        if hint > 0:
            tag, v, _ = sorted(filtered, key=lambda x: abs(x[2] - hint))[0]
            return v, tag

        tag, v, _ = filtered[0]
        return v, tag

    def _try_pick_from_game_node(gm: ET.Element, wt_up: str) -> Tuple[bool, str]:
        wt = str(wt_up or "").strip().upper()
        if not wt:
            return False, "bad_wtype"

        # 扫描 game 节点里所有 ratio_{wtype}{suffix} 字段（suffix: O/U/HO/HU/CO/CU）
        ratio_hits: List[Tuple[str, str, str]] = []  # [(ratio_tag, ratio_txt, suffix)]
        for child in list(gm):
            tag0 = str(getattr(child, "tag", "") or "")
            if not tag0:
                continue
            tag = tag0.strip()
            up = tag.upper()
            if not up.startswith("RATIO_"):
                continue
            if not up.startswith(f"RATIO_{wt}"):
                continue
            suffix = up[len(f"RATIO_{wt}") :]
            if suffix not in {"O", "U", "HO", "HU", "CO", "CU"}:
                continue
            val = str((child.text or "")).strip()
            if not val:
                continue
            ratio_hits.append((up, val, suffix))

        last = ""
        is_over = _is_over_from_chose_team(params.chose_team)
        side_hc = "C" if bool(is_over) else "H"  # 大=C，小=H（与后端一致）
        side_ou = "O" if bool(is_over) else "U"

        def _find_ior(tag0: str) -> str:
            tag = str(tag0 or "").strip()
            if not tag:
                return ""
            v = str((gm.findtext(tag) or gm.findtext(tag.upper()) or "")).strip()
            if (not v) or v == "0":
                return ""
            return v

        for ratio_tag, ratio_txt, suffix in ratio_hits:
            lv = parse_split_line_avg(ratio_txt)
            if lv <= 0:
                last = f"{ratio_tag}={ratio_txt}"
                continue
            if abs(float(lv) - float(want)) >= 1e-6:
                last = f"{ratio_tag}={lv:g}"
                continue

            # 关键修复：同一 wtype 下可能同时存在多条盘口线（通过 suffix 区分）。
            # 命中哪条 ratio，就必须取“同一后缀家族”的 ior，否则会出现 1X015（盘口参数不一致）/betError（水位对不上）。
            prefer_ior_tags: List[str] = []
            if suffix in {"HO", "HU"}:
                prefer_ior_tags.append(f"IOR_{wt}{'HO' if bool(is_over) else 'HU'}")
            elif suffix in {"CO", "CU"}:
                prefer_ior_tags.append(f"IOR_{wt}{'CO' if bool(is_over) else 'CU'}")
            else:
                # 主盘：优先 H/C，其次 O/U（部分站点大小盘用 O/U 作为水位后缀）
                prefer_ior_tags.append(f"IOR_{wt}{side_hc}")
                prefer_ior_tags.append(f"IOR_{wt}{side_ou}")

            ior = ""
            ior_tag = ""
            for t in prefer_ior_tags:
                v = _find_ior(t)
                if v:
                    ior, ior_tag = v, t
                    break

            # fallback：按旧规则（兼容未知站点字段）
            if not ior:
                ior_choices: List[Tuple[str, str]] = []
                for t in [
                    f"IOR_{wt}{side_hc}",
                    f"IOR_{wt}{side_ou}",
                    f"IOR_{wt}H",
                    f"IOR_{wt}C",
                    f"IOR_{wt}O",
                    f"IOR_{wt}U",
                    f"IOR_{wt}HO",
                    f"IOR_{wt}HU",
                    f"IOR_{wt}CO",
                    f"IOR_{wt}CU",
                ]:
                    ior_choices.append((t, _find_ior(t)))
                ior, ior_tag = _pick_best_ior(ior_choices)
            if not ior:
                last = f"{ratio_tag}={ratio_txt},ior=empty"
                continue

            params.ioratio = str(ior)
            params.con, params.ratio = encode_total_line(float(want), is_over=bool(is_over))
            wt_before = str(params.wtype or "").strip().upper()
            rt_before = str(params.rtype or "").strip().upper()
            ct_before = str(params.chose_team or "").strip().upper()
            _maybe_switch_total_to_extra_market_wtype(params, matched_wtype=wt, used_ior_tag=ior_tag)
            wt_after = str(params.wtype or "").strip().upper()
            rt_after = str(params.rtype or "").strip().upper()
            ct_after = str(params.chose_team or "").strip().upper()
            sw = ""
            if wt_after and wt_before and wt_after != wt_before:
                sw = f",switch:{wt_before}/{rt_before}/{ct_before}->{wt_after}/{rt_after}/{ct_after}"
            return True, f"matched:{ratio_tag}={ratio_txt},{ior_tag}={ior},side={side_hc},hint={hint:g}{sw}"

        return False, last or "no_ratio_match"

    # 候选：主盘 + A-F 附加盘 + OU/ROU 对照
    candidates = list(_candidate_wtypes_for_same_line_mapping(str(params.wtype or ""), quick=bool(quick)) or [])
    wt0 = str(params.wtype or "").strip().upper()
    if wt0 and wt0 not in candidates:
        candidates.insert(0, wt0)
    if quick and len(candidates) > 6:
        candidates = candidates[:6]

    last_msg = ""
    for st in showtype_candidates:
        # showtype != live 时，该站点通常需要 rtype=r；rtype=rb 常见返回 “code_type is null”
        rtypes_to_try = ["rb"] if str(st or "").strip().lower() == "live" else ["r"]
        for rt in rtypes_to_try:
            try:
                member_warmup_sport_menu(
                    session=session,
                    base_url=base_url,
                    ver=ver,
                    uid=uid,
                    gtype=gtype,
                    showtype=st,
                    rtype=rt,
                    timeout_s=timeout_s,
                )
            except Exception:
                pass
            g, _ecid, _lid, msg = fetch_game_list_game_context(
                session=session,
                base_url=base_url,
                ver=ver,
                uid=uid,
                gtype=gtype,
                showtype=st,
                rtype=rt,
                timeout_s=timeout_s,
                gid=gid,
            )
            if not g:
                last_msg = f"game_list_no_game:{st}/{rt}:{msg}"
                continue
            for wt in candidates:
                ok, m = _try_pick_from_game_node(g, str(wt))
                if ok:
                    return True, f"{st}/{rt}:{m}"
                if m:
                    last_msg = f"{st}/{rt}:{m}"

    return False, last_msg or "game_list_not_found"


def try_refresh_total_odds_via_game_more(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    params: "BetParams",
    is_live_order: bool,
    want_total_abs: float,
    timeout_s: int,
    quick: bool = False,
    scan_all_gids: bool = True,
) -> Tuple[bool, str, str]:
    """
    大小盘“主盘变线但附加盘仍有原盘口线”时的兜底刷新：
    - get_game_list(p3type=...) 在该站点经常被 CheckEMNU 阻断
    - right_score 页面的 get_game_more 返回更完整的盘口字段（含 A-F 附加盘的 ratio/ior）

    返回：(ok, used_wtype, msg)。若 ok=True，会就地更新 params.wtype/chose_team/rtype/ioratio/con/ratio。
    """
    used_wtype = ""
    gtype = str(params.gtype or "").strip().lower() or "ft"
    default_showtype = "live" if bool(is_live_order) else "today"
    showtype = default_showtype
    rtype_list = "rb" if bool(is_live_order) else "r"
    requested_gid = str(params.gid or "").strip()

    # 优先复用缓存的 lid/ecid（分单/重试非常关键：避免 get_game_list 被 CheckEMNU 阻断）
    lid, ecid_num, st_hint = _get_cached_game_ctx(base_url=base_url, uid=uid, gid=requested_gid)
    if st_hint:
        showtype = st_hint

    # 预热 + 获取 lid/ecid：比赛可能从 live 掉到 hot/today/early，单一 showtype 下会拿不到上下文，导致 game_more 无法使用。
    ctx_showtypes: List[str] = []
    for st in [str(showtype), str(default_showtype), "hot", "today", "early"]:
        st2 = _normalize_showtype_hint(st)
        if st2 and st2 not in ctx_showtypes:
            ctx_showtypes.append(st2)
    if quick:
        ctx_showtypes = _shrink_showtype_candidates_for_quick(ctx_showtypes)
    g = None
    # 若缓存命中，则不强制依赖 g；否则再去探测上下文
    msg = ""
    if not (lid and ecid_num):
        for st in ctx_showtypes:
            # showtype != live 时，部分站点需要 rtype=r 才能返回正常上下文；这里两种都试一次。
            st_low = str(st or "").strip().lower()
            if st_low == "live":
                rtypes_to_try = [rtype_list]
            else:
                # 非 live 下优先 r（rb 常见返回非 XML：code_type is null）
                rtypes_to_try = ["r"]
                if rtype_list not in rtypes_to_try:
                    rtypes_to_try.append(rtype_list)
            # 注意：即使 quick 模式也必须尝试 rtype=r，否则 hot/today/early 很容易返回非 XML（如 code_type is null），
            # 导致拿不到 lid/ecid，从而 game_more 无法用于“主盘掉附加盘”的找回。
            # 尽量绑定菜单上下文，降低 CheckEMNU/VariableStandard 概率
            for rt in rtypes_to_try:
                try:
                    member_warmup_sport_menu(
                        session=session,
                        base_url=base_url,
                        ver=ver,
                        uid=uid,
                        gtype=gtype,
                        showtype=st,
                        rtype=rt,
                        timeout_s=timeout_s,
                    )
                except HardTimeout:
                    pass
                except Exception:
                    pass
                g, ecid_num2, lid2, msg = fetch_game_list_game_context(
                    session=session,
                    base_url=base_url,
                    ver=ver,
                    uid=uid,
                    gtype=gtype,
                    showtype=st,
                    rtype=rt,
                    timeout_s=timeout_s,
                    gid=params.gid,
                )
                if g and lid2 and ecid_num2:
                    lid, ecid_num = lid2, ecid_num2
                    showtype = st
                    break
            if lid and ecid_num:
                break

    if not (lid and ecid_num):
        return False, used_wtype, f"missing_ecid_or_lid: ecid={ecid_num!r} lid={lid!r} msg={msg}"

    # 部分站点在滚球 showtype=live 下只返回“主盘”，而“原盘口线”会被挪到热门/今日（showtype=hot/today）下的更多盘口里。
    # 因此：允许在同一 lid/ecid 上切换 showtype 再拉一次 get_game_more 作为兜底。
    showtype_candidates: List[str] = []
    for st in [str(showtype), str(default_showtype), "hot", "today", "early"]:
        st2 = _normalize_showtype_hint(st)
        if st2 and st2 not in showtype_candidates:
            showtype_candidates.append(st2)
    if quick:
        # one-shot/追水：优先快速返回，避免单次 attempt 阻塞队列太久
        showtype_candidates = _shrink_showtype_candidates_for_quick(showtype_candidates)

    # 注意：不能只取第一个成功返回的 showtype，因为 live 下可能只剩“主盘”，原盘口线会出现在 hot/today 的更多盘口里。
    gm_last_fetch_msg = ""

    want = float(want_total_abs or 0.0)
    if want <= 0:
        return False, used_wtype, "bad_want_total"

    # 候选：主盘 + A-F 附加盘 + OU/ROU 对照
    candidates = list(_candidate_wtypes_for_same_line_mapping(str(params.wtype or ""), quick=bool(quick)) or [])
    wt0 = str(params.wtype or "").strip().upper()
    if wt0 and wt0 not in candidates:
        candidates.insert(0, wt0)
    if quick and len(candidates) > 4:
        candidates = candidates[:4]

    is_over = _is_over_from_chose_team(params.chose_team)

    last_msg = ""
    # 优先 All：主盘掉到附加盘时，Main 往往缺字段/缺盘口线
    filter_candidates = ["All", "Main"]
    for st in showtype_candidates:
        # showtype != live 时，部分站点需要 rtype=r（即 is_live_order=False）才能返回热门/今日更多盘口。
        st_low = str(st or "").strip().lower()
        rb_flags: List[bool] = []
        if st_low == "live":
            rb_flags = [bool(is_live_order)]
        else:
            # 非 live 下优先 isRB=N（更常见；isRB=Y 往往返回空/非预期数据）
            rb_flags = [False]
            if bool(is_live_order):
                rb_flags.append(True)
        for rb_flag in rb_flags:
            rb_tag = "rb" if bool(rb_flag) else "r"
            for flt in filter_candidates:
                xml_text, gm_msg = fetch_game_more_xml_text(
                    session=session,
                    base_url=base_url,
                    ver=ver,
                    uid=uid,
                    gtype=gtype,
                    showtype=st,
                    is_live_order=bool(rb_flag),
                    lid=lid,
                    ecid_num=ecid_num,
                    timeout_s=timeout_s,
                    filter=flt,
                )
                gm_last_fetch_msg = gm_msg
                if not xml_text:
                    continue
                try:
                    parsed = parse_game_more_games_and_ft_gids(xml_text)
                except Exception as e:
                    last_msg = f"{st}:{rb_tag}:{flt}:game_more_parse_failed:{e}"
                    continue

                games2 = list(parsed.get("games") or [])
                ft_gids2 = list(parsed.get("ft_gids") or [])
                if not bool(scan_all_gids):
                    gid0 = str(params.gid or "").strip()
                    if gid0:
                        games2 = [
                            g
                            for g in games2
                            if str((g.findtext("gid") or g.findtext("GID") or "")).strip() == gid0
                        ]
                        ft_gids2 = [x for x in ft_gids2 if isinstance(x, dict) and str(x.get("gid") or "").strip() == gid0]

                matched = match_total_from_game_more_parsed(
                    games=games2,
                    ft_gids=ft_gids2,
                    candidates=candidates,
                    want_total_abs=float(want),
                    is_over=bool(is_over),
                )
                if not matched:
                    last_msg = f"{st}:{rb_tag}:{flt}:not_found"
                    continue

                gid_hit = str(matched.get("gid") or "").strip()
                wt_hit = str(matched.get("wtype") or "").strip().upper()
                if gid_hit:
                    params.gid = gid_hit
                    try:
                        _set_cached_game_ctx(base_url=base_url, uid=uid, gid=gid_hit, lid=lid, ecid=ecid_num, showtype=st)
                        if requested_gid and requested_gid != gid_hit:
                            _set_cached_game_ctx(base_url=base_url, uid=uid, gid=requested_gid, lid=lid, ecid=ecid_num, showtype=st)
                    except Exception:
                        pass
                if matched.get("ioratio"):
                    params.ioratio = str(matched.get("ioratio") or params.ioratio)
                if matched.get("con"):
                    params.con = str(matched.get("con") or params.con)
                if matched.get("ratio"):
                    params.ratio = str(matched.get("ratio") or params.ratio)
                _maybe_switch_total_to_extra_market_wtype(params, matched_wtype=wt_hit, used_ior_tag=str(matched.get("matched_ior_tag") or ""))
                used_wtype = wt_hit
                return (
                    True,
                    used_wtype,
                    f"matched:gid={gid_hit},wtype={wt_hit},ratio={want:g},ior={params.ioratio},ratio_tag={matched.get('matched_ratio_tag')},ior_tag={matched.get('matched_ior_tag')},showtype={st},filter={flt}",
                )

    if gm_last_fetch_msg:
        return False, used_wtype, last_msg or f"game_more_failed:{gm_last_fetch_msg}"
    return False, used_wtype, last_msg or "not_found"


def try_refresh_handicap_odds_via_game_more(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    params: "BetParams",
    is_live_order: bool,
    want_handicap_line: float,
    timeout_s: int,
    quick: bool = False,
    scan_all_gids: bool = True,
) -> Tuple[bool, str, str]:
    """
    让球盘“主盘变线但附加盘仍有原盘口线”时的兜底刷新（主盘 ↔ 附加盘 / 更多盘口）。
    - right_score 页面的 get_game_more 返回更完整的盘口字段（含 A-F 附加盘的 ratio/ior）

    返回：(ok, used_wtype, msg)。若 ok=True，会就地更新 params.wtype/rtype/ioratio/con/ratio。
    """
    used_wtype = ""
    gtype = str(params.gtype or "").strip().lower() or "ft"
    default_showtype = "live" if bool(is_live_order) else "today"
    showtype = default_showtype
    rtype_list = "rb" if bool(is_live_order) else "r"
    requested_gid = str(params.gid or "").strip()

    # 优先复用缓存的 lid/ecid（分单/重试非常关键：避免 get_game_list 被 CheckEMNU 阻断）
    lid, ecid_num, st_hint = _get_cached_game_ctx(base_url=base_url, uid=uid, gid=requested_gid)
    if st_hint:
        showtype = st_hint

    # 预热 + 获取 lid/ecid：比赛可能从 live 掉到 hot/today/early，单一 showtype 下会拿不到上下文，导致 game_more 无法使用。
    ctx_showtypes: List[str] = []
    for st in [str(showtype), str(default_showtype), "hot", "today", "early"]:
        st2 = _normalize_showtype_hint(st)
        if st2 and st2 not in ctx_showtypes:
            ctx_showtypes.append(st2)
    g = None
    msg = ""
    if not (lid and ecid_num):
        for st in ctx_showtypes:
            st_low = str(st or "").strip().lower()
            if st_low == "live":
                rtypes_to_try = [rtype_list]
            else:
                rtypes_to_try = ["r"]
                if rtype_list not in rtypes_to_try:
                    rtypes_to_try.append(rtype_list)
            for rt in rtypes_to_try:
                try:
                    member_warmup_sport_menu(
                        session=session,
                        base_url=base_url,
                        ver=ver,
                        uid=uid,
                        gtype=gtype,
                        showtype=st,
                        rtype=rt,
                        timeout_s=timeout_s,
                    )
                except Exception:
                    pass
                g, ecid_num2, lid2, msg = fetch_game_list_game_context(
                    session=session,
                    base_url=base_url,
                    ver=ver,
                    uid=uid,
                    gtype=gtype,
                    showtype=st,
                    rtype=rt,
                    timeout_s=timeout_s,
                    gid=params.gid,
                )
                if g and lid2 and ecid_num2:
                    lid, ecid_num = lid2, ecid_num2
                    showtype = st
                    break
            if lid and ecid_num:
                break

    if not (lid and ecid_num):
        return False, used_wtype, f"missing_ecid_or_lid: ecid={ecid_num!r} lid={lid!r} msg={msg}"

    showtype_candidates: List[str] = []
    for st in [str(showtype), str(default_showtype), "hot", "today", "early"]:
        st2 = _normalize_showtype_hint(st)
        if st2 and st2 not in showtype_candidates:
            showtype_candidates.append(st2)
    if quick:
        showtype_candidates = _shrink_showtype_candidates_for_quick(showtype_candidates)

    side = str(params.chose_team or "").strip().upper()
    if side not in {"H", "C"}:
        return False, used_wtype, f"bad_side:{side!r}"

    want = float(want_handicap_line or 0.0)

    # 候选：主盘 + A-F 附加盘 + R/RE（含上半场 HR/HRE）对照
    candidates = list(_candidate_wtypes_for_same_line_mapping(str(params.wtype or ""), quick=bool(quick)) or [])
    wt0 = str(params.wtype or "").strip().upper()
    if wt0 and wt0 not in candidates:
        candidates.insert(0, wt0)
    if quick and len(candidates) > 4:
        candidates = candidates[:4]

    last_msg = ""
    # 优先 All：主盘掉到附加盘时，Main 往往缺字段/缺盘口线
    filter_candidates = ["All", "Main"]
    gm_last_fetch_msg = ""
    for st in showtype_candidates:
        # showtype != live 时，部分站点需要 rtype=r（即 is_live_order=False）才能返回热门/今日更多盘口。
        st_low = str(st or "").strip().lower()
        rb_flags: List[bool] = []
        if st_low == "live":
            rb_flags = [bool(is_live_order)]
        else:
            rb_flags = [False]
            if bool(is_live_order):
                rb_flags.append(True)
        for rb_flag in rb_flags:
            rb_tag = "rb" if bool(rb_flag) else "r"
            for flt in filter_candidates:
                xml_text, gm_msg = fetch_game_more_xml_text(
                    session=session,
                    base_url=base_url,
                    ver=ver,
                    uid=uid,
                    gtype=gtype,
                    showtype=st,
                    is_live_order=bool(rb_flag),
                    lid=lid,
                    ecid_num=ecid_num,
                    timeout_s=timeout_s,
                    filter=flt,
                )
                gm_last_fetch_msg = gm_msg
                if not xml_text:
                    continue
                try:
                    parsed = parse_game_more_games_and_ft_gids(xml_text)
                except Exception as e:
                    last_msg = f"{st}:{rb_tag}:{flt}:game_more_parse_failed:{e}"
                    continue

                games2 = list(parsed.get("games") or [])
                ft_gids2 = list(parsed.get("ft_gids") or [])
                if not bool(scan_all_gids):
                    gid0 = str(params.gid or "").strip()
                    if gid0:
                        games2 = [
                            g
                            for g in games2
                            if str((g.findtext("gid") or g.findtext("GID") or "")).strip() == gid0
                        ]
                        ft_gids2 = [x for x in ft_gids2 if isinstance(x, dict) and str(x.get("gid") or "").strip() == gid0]

                matched = match_handicap_from_game_more_parsed(
                    games=games2,
                    ft_gids=ft_gids2,
                    candidates=candidates,
                    want_handicap_line=float(want),
                    side=side,
                )
                if not matched:
                    last_msg = f"{st}:{rb_tag}:{flt}:not_found"
                    continue

                gid_hit = str(matched.get("gid") or "").strip()
                wt_hit = str(matched.get("wtype") or "").strip().upper()
                if gid_hit:
                    params.gid = gid_hit
                    try:
                        _set_cached_game_ctx(base_url=base_url, uid=uid, gid=gid_hit, lid=lid, ecid=ecid_num, showtype=st)
                        if requested_gid and requested_gid != gid_hit:
                            _set_cached_game_ctx(base_url=base_url, uid=uid, gid=requested_gid, lid=lid, ecid=ecid_num, showtype=st)
                    except Exception:
                        pass
                if wt_hit:
                    params.wtype = wt_hit
                    params.rtype = f"{wt_hit}{side}"
                if matched.get("ioratio"):
                    params.ioratio = str(matched.get("ioratio") or params.ioratio)
                if matched.get("con"):
                    params.con = str(matched.get("con") or params.con)
                if matched.get("ratio"):
                    params.ratio = str(matched.get("ratio") or params.ratio)
                used_wtype = wt_hit
                return (
                    True,
                    used_wtype,
                    f"matched:gid={gid_hit},wtype={wt_hit},line={want:g},ior={params.ioratio},ratio_tag={matched.get('matched_ratio_tag')},strong={matched.get('strong')},showtype={st},filter={flt}",
                )

    if gm_last_fetch_msg:
        return False, used_wtype, last_msg or f"game_more_failed:{gm_last_fetch_msg}"
    return False, used_wtype, last_msg or "not_found"


def fetch_page_count(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    gtype: str,
    showtype: str,
    p3type: str,
    timeout_s: int,
) -> Tuple[Dict[str, int], str]:
    """
    拉取 get_page_count，返回可用分页/板块计数（键通常类似 MAIN/MOUA/RNOU...）。
    说明：站点实现可能依赖会话上下文（p3type/当前页），这里统一做“尽力而为”的探测。
    """
    form = {
        "p": "get_page_count",
        "uid": uid,
        "ver": ver,
        "langx": "zh-cn",
        "gtype": str(gtype or "").strip().lower(),
        "showtype": str(showtype or "").strip().lower() or "today",
        "filter": "",
        "ltype": str(_DEFAULT_LTYPE),
        "team_id": "",
        "specialClick": "",
        "p3type": str(p3type or "").strip(),
        "sorttype": "L",
        "date": "",
        "from": "game_list",
    }
    try:
        text = member_post_session(session=session, base_url=base_url, ver=ver, form=form, timeout_s=timeout_s)
    except Exception as e:
        return {}, f"page_count_request_failed: {e}"
    t = (text or "").strip()
    if not t:
        return {}, "page_count_empty_response"
    if not t.startswith("<"):
        return {}, f"page_count_non_xml: {t[:120]}"
    try:
        root = ET.fromstring(t)
    except Exception as e:
        return {}, f"page_count_xml_parse_failed: {e}"
    pg = root.find(".//pgcount")
    if pg is None:
        return {}, "page_count_pgcount_not_found"
    out: Dict[str, int] = {}
    for child in list(pg):
        try:
            k = str(child.tag or "").strip().upper()
            v = str(child.text or "").strip()
            if not k:
                continue
            out[k] = int(float(v or "0"))
        except Exception:
            continue
    return out, "ok"


def _candidate_p3types_for_line_mapping(wtype: str, page_count: Dict[str, int]) -> List[str]:
    """
    当“主盘口线变动且在附加盘口找不到原盘口线”时，尝试切换不同的 p3type 上下文再做 order_view 映射。
    仅在必要时触发，避免热路径增加请求。
    """
    wt = str(wtype or "").strip().upper()
    keys = []
    if isinstance(page_count, dict):
        for k, v in page_count.items():
            try:
                if int(v or 0) > 0:
                    k2 = str(k or "").strip().upper()
                    if k2:
                        keys.append(k2)
            except Exception:
                continue

    out: List[str] = []
    seen = set()

    def add(x: str) -> None:
        x2 = str(x or "").strip().upper()
        if not x2 or x2 in seen:
            return
        seen.add(x2)
        out.append(x2)

    # 优先基于 page_count 做“按玩法相关性”的候选排序
    if keys:
        if wt.endswith("OU"):
            for k in keys:
                if "MOU" in k:
                    add(k)
            for k in keys:
                if "NOU" in k:
                    add(k)
            for k in keys:
                if "OU" in k:
                    add(k)
            # MAIN：无论是否已命中其他板块，都保留一个兜底（有些站点“附加盘”仍挂在 MAIN 下的不同分页/展开里）
            add("MAIN")
        elif wt in {"R", "RE", "HR", "HRE"} or wt.endswith("RE"):
            for k in keys:
                if "MR" in k:
                    add(k)
            for k in keys:
                if ("R" in k) and ("OU" not in k):
                    add(k)

    # 兜底（跨站点兼容）：常见“更多盘口/附加盘”板块名
    if not out:
        if wt.endswith("OU"):
            for k in ["MOUA", "RNOU", "MAIN"]:
                add(k)
        else:
            add("MAIN")

    # 限制探测规模，避免极端情况下放大请求量
    limit = 6 if wt.endswith("OU") else 4
    return out[:limit]


def try_refresh_total_odds_via_game_list_p3type(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    params: "BetParams",
    is_live_order: bool,
    want_total_abs: float,
    timeout_s: int,
    quick: bool = False,
) -> Tuple[bool, str, List[str], str]:
    """
    当大小盘存在多条盘口线时，order_view 可能只返回主盘线，导致无法刷新“目标盘口线”的 ioratio。

    思路：通过 get_page_count 拿到可用 p3type（更多盘口板块），在每个板块下拉一次 get_game_list：
    - 若该板块展示的大小盘盘口线（RATIO_*O）与目标盘口线匹配，则直接取 IOR_{rtype} 作为当前水位。

    返回：(ok, used_p3type, tried_p3types, msg)。若 ok=True，会就地更新 params.ioratio/con/ratio。
    """
    used_p3type = ""
    tried: List[str] = []

    # 兼容：部分情况下 today 列表可能找不到（早盘），但此函数通常用于滚球订单。
    gtype = str(params.gtype or "").strip().lower() or "ft"
    showtype = "live" if bool(is_live_order) else "today"
    rtype_list = "rb" if bool(is_live_order) else "r"

    # 新会话（每次 attempt 都会 make_member_session）可能缺少菜单上下文，直接拉 get_game_list 会触发 CheckEMNU。
    # 这里做一次轻量预热，尽量让后续 get_page_count / get_game_list 返回 XML。
    try:
        member_warmup_sport_menu(
            session=session,
            base_url=base_url,
            ver=ver,
            uid=uid,
            gtype=gtype,
            showtype=showtype,
            rtype=rtype_list,
            timeout_s=timeout_s,
        )
    except Exception:
        pass

    pc_msg = ""
    # one-shot/追水：不要做 page_count + 大范围 p3type 扫描（会把一次 attempt 拉到分钟级）
    if quick:
        candidates = _candidate_p3types_for_line_mapping(str(params.wtype or ""), {})
    else:
        pc, pc_msg = fetch_page_count(
            session=session,
            base_url=base_url,
            ver=ver,
            uid=uid,
            gtype=gtype,
            showtype=showtype,
            p3type="",
            timeout_s=timeout_s,
        )
        # page_count 失败也不直接终止：回退到常见候选
        candidates = _candidate_p3types_for_line_mapping(str(params.wtype or ""), pc if pc else {})
    if not candidates:
        candidates = ["MAIN"]
    if quick and len(candidates) > 2:
        candidates = candidates[:2]

    wt_up = str(params.wtype or "").strip().upper()
    prefixes = {"A", "B", "C", "D", "E", "F"}
    base_wt = wt_up
    known_total = {"ROU", "OU", "HROU", "HOU"}
    if wt_up not in known_total and len(wt_up) > 1 and wt_up[0] in prefixes and wt_up[1:] in known_total:
        base_wt = wt_up[1:]
    ratio_tag = f"RATIO_{base_wt}O"

    # IOR key：优先用原 rtype；若是 AROUH 这类前缀，补一个“去前缀”的兜底
    rtype_up = str(params.rtype or "").strip().upper()
    ior_key = f"IOR_{rtype_up}"
    ior_key_alt = ""
    if len(rtype_up) > 1 and rtype_up[0] in prefixes:
        ior_key_alt = f"IOR_{rtype_up[1:]}"

    last_msg = pc_msg if pc_msg != "ok" else ""
    for p3 in candidates:
        p3u = str(p3 or "").strip().upper()
        if not p3u:
            continue
        tried.append(p3u)
        g, msg = fetch_game_list_game(
            session=session,
            base_url=base_url,
            ver=ver,
            uid=uid,
            gtype=gtype,
            showtype=showtype,
            rtype=rtype_list,
            p3type=p3u,
            timeout_s=timeout_s,
            gid=params.gid,
        )
        if not g:
            last_msg = f"{p3u}:{msg}"
            # 若明确被 CheckEMNU/VariableStandard 阻断，继续换 p3type 大概率只会重复失败；尽快进入 game_more 兜底更快。
            if _looks_like_ctx_blocked_msg(msg):
                break
            continue

        cur_total = parse_split_line_avg(g.findtext(ratio_tag) or "")
        if cur_total <= 0:
            last_msg = f"{p3u}:{ratio_tag}=empty"
            continue
        if abs(float(cur_total) - float(want_total_abs)) >= 1e-6:
            last_msg = f"{p3u}:{ratio_tag}={cur_total:g}"
            continue

        ior = str((g.findtext(ior_key) or "")).strip()
        used_ior_key = ior_key
        if not ior and ior_key_alt:
            ior = str((g.findtext(ior_key_alt) or "")).strip()
            if ior:
                used_ior_key = ior_key_alt
        if not ior:
            last_msg = f"{p3u}:{ior_key}=empty"
            continue

        # 命中：刷新水位，并按当前盘口线重算 con/ratio（方向由 chose_team 决定）
        params.ioratio = ior
        params.con, params.ratio = encode_total_line(float(cur_total), is_over=_is_over_from_chose_team(params.chose_team))
        used_p3type = p3u
        return True, used_p3type, tried, f"matched:{ratio_tag}={cur_total:g},{used_ior_key}={ior}"

    # 兜底：若 today 找不到，尝试 early（仅非滚球）。one-shot 模式下跳过（避免扩大请求量）。
    if (not quick) and (not bool(is_live_order)) and showtype == "today":
        for p3 in candidates:
            p3u = str(p3 or "").strip().upper()
            if not p3u:
                continue
            tried.append(f"EARLY:{p3u}")
            g, msg = fetch_game_list_game(
                session=session,
                base_url=base_url,
                ver=ver,
                uid=uid,
                gtype=gtype,
                showtype="early",
                rtype="r",
                p3type=p3u,
                timeout_s=timeout_s,
                gid=params.gid,
            )
            if not g:
                last_msg = f"EARLY:{p3u}:{msg}"
                if _looks_like_ctx_blocked_msg(msg):
                    break
                continue
            cur_total = parse_split_line_avg(g.findtext(ratio_tag) or "")
            if cur_total <= 0:
                last_msg = f"EARLY:{p3u}:{ratio_tag}=empty"
                continue
            if abs(float(cur_total) - float(want_total_abs)) >= 1e-6:
                last_msg = f"EARLY:{p3u}:{ratio_tag}={cur_total:g}"
                continue
            ior = str((g.findtext(ior_key) or "")).strip()
            used_ior_key = ior_key
            if not ior and ior_key_alt:
                ior = str((g.findtext(ior_key_alt) or "")).strip()
                if ior:
                    used_ior_key = ior_key_alt
            if not ior:
                last_msg = f"EARLY:{p3u}:{ior_key}=empty"
                continue
            params.ioratio = ior
            params.con, params.ratio = encode_total_line(float(cur_total), is_over=_is_over_from_chose_team(params.chose_team))
            used_p3type = p3u
            return True, used_p3type, tried, f"matched:EARLY:{ratio_tag}={cur_total:g},{used_ior_key}={ior}"

    return False, used_p3type, tried, last_msg or "not_found"


def compute_follow_stake(order: Dict[str, Any], settings: Dict[str, Any]) -> float:
    mode = str(settings.get("mode") or "fixed")
    fixed_amount = float(settings.get("fixed_amount") or 0)
    ratio_percent = float(settings.get("ratio_percent") or 0)
    source_name = resolve_order_source_name(order)
    target_ratio_override = target_ratio_percent_override(settings, source_name)
    if target_ratio_override is not None:
        mode = "ratio"
        ratio_percent = float(target_ratio_override)

    try:
        src = float(order.get("stake") or 0)
    except Exception:
        src = 0.0

    stake = 0.0
    if mode == "ratio":
        stake = src * (ratio_percent / 100.0)
    else:
        stake = fixed_amount

    if stake < 0:
        stake = 0.0
    # 下注金额必须为整数：有小数就向上取整（多买 1 块），避免 2499.75 这类无法下注。
    if stake <= 0:
        return 0.0
    return float(int(math.ceil(stake)))


MARKET_RULE_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "handicap": {"enabled": True, "ratio_percent": 100.0},
    "total_over": {"enabled": True, "ratio_percent": 100.0},
    "total_under": {"enabled": True, "ratio_percent": 100.0},
    "moneyline": {"enabled": True, "ratio_percent": 100.0},
}

REVERSE_RULE_DEFAULTS: Dict[str, bool] = {
    "all": False,
    "total": False,
    "handicap": False,
}


def normalize_market_rules(settings: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = settings.get("market_rules") if isinstance(settings, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, default in MARKET_RULE_DEFAULTS.items():
        item_raw = raw.get(key) if isinstance(raw.get(key), dict) else {}
        enabled = bool(item_raw.get("enabled", default["enabled"]))
        try:
            ratio = float(item_raw.get("ratio_percent", default["ratio_percent"]))
        except Exception:
            ratio = float(default["ratio_percent"])
        ratio = max(0.0, min(500.0, ratio))
        out[key] = {"enabled": enabled, "ratio_percent": ratio}
    return out


def normalize_reverse_rules(settings: Dict[str, Any]) -> Dict[str, bool]:
    raw = settings.get("reverse_rules") if isinstance(settings, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    out: Dict[str, bool] = {}
    for key, default in REVERSE_RULE_DEFAULTS.items():
        out[key] = bool(raw.get(key, default))
    return out


def _normalize_follow_odds_limit(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def evaluate_follow_odds_limit(
    *,
    settings: Dict[str, Any],
    ioratio: Any,
) -> Tuple[bool, str, Dict[str, Any]]:
    min_odds = _normalize_follow_odds_limit((settings or {}).get("follow_min_odds"))
    max_odds = _normalize_follow_odds_limit((settings or {}).get("follow_max_odds"))
    current_odds = _normalize_follow_odds_limit(ioratio)
    meta: Dict[str, Any] = {
        "min_odds": min_odds,
        "max_odds": max_odds,
        "current_odds": current_odds,
    }
    if current_odds is None:
        if min_odds is not None or max_odds is not None:
            return (True, "已按水位策略跳过：当前注单缺少有效水位，无法核对限制。", meta)
        return (False, "", meta)
    if min_odds is not None and current_odds < min_odds:
        return (True, f"已按水位策略跳过：当前水位 {current_odds:g} 低于最低跟单水位 {min_odds:g}。", meta)
    if max_odds is not None and current_odds > max_odds:
        return (True, f"已按水位策略跳过：当前水位 {current_odds:g} 高于最高跟单水位 {max_odds:g}。", meta)
    return (False, "", meta)




def classify_order_market(order: Dict[str, Any]) -> Tuple[str, str]:
    raw = order.get("raw") if isinstance(order.get("raw"), dict) else {}
    wagerstype = str(raw.get("WAGERSTYPE") or order.get("betType") or "").strip()
    order_type = str(raw.get("ORDER_TYPE") or "").strip()
    line = str(order.get("line") or "").strip()

    if ("独赢" in wagerstype) or ("獨贏" in wagerstype):
        return ("moneyline", "独赢")

    if "让球" in wagerstype:
        return ("handicap", "让球")

    is_total = ("大 / 小" in wagerstype) or ("大小" in wagerstype) or ("大球" in wagerstype) or ("小球" in wagerstype)
    if is_total:
        for txt in (order_type, line):
            t = str(txt or "").strip()
            if t.startswith("大"):
                return ("total_over", "大球")
            if t.startswith("小"):
                return ("total_under", "小球")
        joined = f"{order_type} {line}"
        if ("大" in joined) and ("小" not in joined):
            return ("total_over", "大球")
        if ("小" in joined) and ("大" not in joined):
            return ("total_under", "小球")
        return ("total_over", "大球")

    return ("other", "其他")


LIMIT_MARKET_LABELS: Dict[str, str] = {
    "handicap": "让球",
    "total_over": "大球",
    "total_under": "小球",
    "moneyline": "独赢",
    "other": "其他",
}


def _limit_text(value: Any) -> str:
    return str(value or "").strip()


def _limit_token(value: Any) -> str:
    return re.sub(r"\s+", "", _limit_text(value)).lower()


def _canonical_match_key(gtype: Any, gid: Any) -> str:
    gid_text = _limit_text(gid)
    if not gid_text:
        return ""
    return f"{_limit_text(gtype).upper() or 'UNKNOWN'}:{gid_text}"


def classify_effective_limit_market(order: Dict[str, Any], settings: Dict[str, Any]) -> Tuple[str, str]:
    """Return the market bucket actually submitted after reverse rules."""
    market_code, market_label = classify_order_market(order)
    rules = normalize_reverse_rules(settings)
    reverse_total = bool(rules.get("all") or rules.get("total"))
    if reverse_total and market_code == "total_over":
        return ("total_under", LIMIT_MARKET_LABELS["total_under"])
    if reverse_total and market_code == "total_under":
        return ("total_over", LIMIT_MARKET_LABELS["total_over"])
    return (market_code, market_label)


def follow_limit_identity(order: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """Build stable match and market keys for cumulative amount limits."""
    payload = order if isinstance(order, dict) else {}
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    gtype = _limit_text(raw.get("GTYPE") or payload.get("gtype") or payload.get("sport"))
    gid = _limit_text(payload.get("gid") or raw.get("_EVENT") or raw.get("GID"))
    match_key = _canonical_match_key(gtype, gid)

    if not match_key:
        home_no = _limit_token(raw.get("NUM_H") or raw.get("GNUM_H") or payload.get("home_number"))
        away_no = _limit_token(raw.get("NUM_C") or raw.get("GNUM_C") or payload.get("away_number"))
        if home_no and away_no:
            match_key = f"{gtype.upper() or 'UNKNOWN'}:NUM:{home_no}:{away_no}"

    match_label = _limit_text(payload.get("sides"))
    if not match_label:
        home = _limit_text(raw.get("TEAM_H") or payload.get("home"))
        away = _limit_text(raw.get("TEAM_C") or payload.get("away"))
        match_label = f"{home} v {away}".strip(" v")
    if not match_key:
        league = _limit_token(payload.get("league") or raw.get("LEAGUE"))
        home = _limit_token(raw.get("TEAM_H") or payload.get("home"))
        away = _limit_token(raw.get("TEAM_C") or payload.get("away"))
        date = _limit_token(payload.get("date") or raw.get("DATE"))
        if home and away:
            raw_key = "|".join((gtype.upper() or "UNKNOWN", date, league, home, away))
            match_key = f"FALLBACK:{hashlib.sha256(raw_key.encode('utf-8')).hexdigest()[:24]}"

    market_code, market_label = classify_effective_limit_market(payload, settings or {})
    market_key = f"{match_key}|{market_code}" if match_key else ""
    return {
        "match_key": match_key,
        "match_label": match_label or gid or match_key,
        "market_key": market_key,
        "market_code": market_code,
        "market_label": market_label or LIMIT_MARKET_LABELS.get(market_code, market_code),
    }


def _market_identity_from_intent(intent: Dict[str, Any]) -> Tuple[str, str]:
    market_type = _limit_text(intent.get("market_type")).upper()
    side = _limit_text(intent.get("side")).upper()
    if market_type == "TOTAL":
        code = "total_under" if side == "UNDER" else "total_over"
    elif market_type == "HANDICAP":
        code = "handicap"
    elif market_type == "MONEYLINE":
        code = "moneyline"
    else:
        code = "other"
    return code, LIMIT_MARKET_LABELS.get(code, code)


def follow_limit_identity_from_result(result: Dict[str, Any]) -> Dict[str, str]:
    """Recover limit keys from new metadata or historical Snapshot results."""
    payload = result if isinstance(result, dict) else {}
    explicit_match = _limit_text(payload.get("limit_match_key"))
    explicit_market = _limit_text(payload.get("limit_market_key"))
    if explicit_match and explicit_market:
        return {
            "match_key": explicit_match,
            "match_label": _limit_text(payload.get("limit_match_label")) or explicit_match,
            "market_key": explicit_market,
            "market_code": _limit_text(payload.get("limit_market_code")) or explicit_market.rsplit("|", 1)[-1],
            "market_label": _limit_text(payload.get("limit_market_label")) or explicit_market,
        }

    intent = payload.get("snapshot_direct_intent")
    if not isinstance(intent, dict):
        intent = payload.get("snapshot_direct_source_intent")
    if not isinstance(intent, dict):
        intent = {}
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    gid = _limit_text(intent.get("gid") or params.get("gid") or payload.get("market_row_gid"))
    gtype = _limit_text(intent.get("gtype") or params.get("gtype"))
    match_key = _canonical_match_key(gtype, gid)
    market_code, market_label = _market_identity_from_intent(intent)
    return {
        "match_key": match_key,
        "match_label": gid or match_key,
        "market_key": f"{match_key}|{market_code}" if match_key else "",
        "market_code": market_code,
        "market_label": market_label,
    }


def load_follow_limit_usage(results_path: Path) -> Tuple[Dict[str, float], Dict[str, float], set[str]]:
    """Restore successful exposure totals; ticket ids make the scan idempotent."""
    match_usage: Dict[str, float] = {}
    market_usage: Dict[str, float] = {}
    success_keys: set[str] = set()
    try:
        handle = Path(results_path).open("r", encoding="utf-8-sig")
    except OSError:
        return match_usage, market_usage, success_keys
    with handle:
        for line in handle:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if not isinstance(item, dict) or item.get("ok") is not True:
                continue
            identity = follow_limit_identity_from_result(item)
            match_key = identity.get("match_key") or ""
            market_key = identity.get("market_key") or ""
            if not match_key or not market_key:
                continue
            ticket_id = _limit_text(item.get("ticket_id"))
            success_key = (
                f"ticket:{ticket_id}"
                if ticket_id
                else "result:"
                + "|".join(
                    (
                        _limit_text(item.get("target")),
                        _limit_text(item.get("orderId") or item.get("order_id")),
                        _limit_text(item.get("split_index")),
                        _limit_text(item.get("ts_ms") or item.get("ts")),
                    )
                )
            )
            if success_key in success_keys:
                continue
            amount = _safe_float(item.get("stake"), 0.0)
            if amount <= 0:
                continue
            success_keys.add(success_key)
            match_usage[match_key] = match_usage.get(match_key, 0.0) + amount
            market_usage[market_key] = market_usage.get(market_key, 0.0) + amount
    return match_usage, market_usage, success_keys


def job_remaining_limit_reservation(job: RetryJob) -> float:
    parts = [max(0.0, _safe_float(value, 0.0)) for value in (job.split_stakes or [])]
    index = max(0, int(job.split_index or 0))
    if parts:
        if bool(job.independent_split):
            return parts[index] if index < len(parts) else 0.0
        return float(sum(parts[index:]))
    return max(0.0, _safe_float(job.stake, 0.0))


def collect_active_limit_reservations(
    jobs: Dict[str, RetryJob],
    *,
    settings: Optional[Dict[str, Any]] = None,
    exclude_parent_key: str = "",
) -> Tuple[Dict[str, float], Dict[str, float]]:
    match_reserved: Dict[str, float] = {}
    market_reserved: Dict[str, float] = {}
    excluded = _limit_text(exclude_parent_key)
    for job in (jobs or {}).values():
        if not isinstance(job, RetryJob):
            continue
        parent_key = _limit_text(job.parent_key or job.key)
        if excluded and parent_key == excluded:
            continue
        identity = follow_limit_identity(job.order, settings or {})
        stored = job.order.get("_follow_limit") if isinstance(job.order.get("_follow_limit"), dict) else {}
        match_key = _limit_text(stored.get("match_key") or identity.get("match_key"))
        market_key = _limit_text(stored.get("market_key") or identity.get("market_key"))
        amount = job_remaining_limit_reservation(job)
        if amount <= 0 or not match_key or not market_key:
            continue
        match_reserved[match_key] = match_reserved.get(match_key, 0.0) + amount
        market_reserved[market_key] = market_reserved.get(market_key, 0.0) + amount
    return match_reserved, market_reserved


def apply_follow_amount_limits(
    *,
    order: Dict[str, Any],
    settings: Dict[str, Any],
    requested_stake: float,
    match_committed: Dict[str, float],
    market_committed: Dict[str, float],
    match_reserved: Dict[str, float],
    market_reserved: Dict[str, float],
    min_stake: float = 1.0,
) -> Tuple[float, str, Dict[str, Any]]:
    """Cap a new order by match and same-match market exposure."""
    identity = follow_limit_identity(order, settings)
    requested = float(int(math.ceil(max(0.0, _safe_float(requested_stake, 0.0)))))
    match_limit = max(0.0, _safe_float(settings.get("match_limit_amount"), 0.0))
    market_limit = max(0.0, _safe_float(settings.get("market_limit_amount"), 0.0))
    match_key = identity.get("match_key") or ""
    market_key = identity.get("market_key") or ""
    meta: Dict[str, Any] = {
        **identity,
        "requested_stake": requested,
        "match_limit_amount": match_limit,
        "market_limit_amount": market_limit,
        "match_committed_amount": _safe_float(match_committed.get(match_key), 0.0),
        "market_committed_amount": _safe_float(market_committed.get(market_key), 0.0),
        "match_reserved_amount": _safe_float(match_reserved.get(match_key), 0.0),
        "market_reserved_amount": _safe_float(market_reserved.get(market_key), 0.0),
    }
    if requested <= 0:
        meta["allowed_stake"] = 0.0
        return 0.0, "跟单金额为 0。", meta
    if (match_limit > 0 or market_limit > 0) and not match_key:
        meta["allowed_stake"] = 0.0
        return 0.0, "无法识别比赛，已按累计限额保护跳过。", meta

    allowed = requested
    reached: List[str] = []
    if match_limit > 0:
        match_remaining = match_limit - meta["match_committed_amount"] - meta["match_reserved_amount"]
        meta["match_remaining_amount"] = max(0.0, match_remaining)
        allowed = min(allowed, max(0.0, match_remaining))
        if match_remaining <= 0:
            reached.append("单场限额")
    if market_limit > 0:
        market_remaining = market_limit - meta["market_committed_amount"] - meta["market_reserved_amount"]
        meta["market_remaining_amount"] = max(0.0, market_remaining)
        allowed = min(allowed, max(0.0, market_remaining))
        if market_remaining <= 0:
            reached.append(f"{identity.get('market_label') or '盘口'}限额")

    allowed_int = float(max(0, int(math.floor(allowed + 1e-9))))
    minimum = max(1.0, float(math.ceil(max(0.0, _safe_float(min_stake, 1.0)))))
    if allowed_int < minimum:
        meta["allowed_stake"] = 0.0
        reason = "、".join(reached) or "累计限额剩余额度"
        return 0.0, f"已按{reason}跳过：剩余额度不足最小下注金额 {minimum:g} 元。", meta
    meta["allowed_stake"] = allowed_int
    meta["limited"] = allowed_int < requested
    return allowed_int, "", meta


def store_follow_limit_metadata(order: Dict[str, Any], meta: Dict[str, Any]) -> None:
    if not isinstance(order, dict) or not isinstance(meta, dict):
        return
    order["_follow_limit"] = {
        key: meta.get(key)
        for key in (
            "match_key",
            "match_label",
            "market_key",
            "market_code",
            "market_label",
            "requested_stake",
            "allowed_stake",
            "match_limit_amount",
            "market_limit_amount",
            "match_committed_amount",
            "market_committed_amount",
            "match_reserved_amount",
            "market_reserved_amount",
            "limited",
        )
    }


def attach_follow_limit_metadata(result: Dict[str, Any], order: Dict[str, Any], settings: Dict[str, Any]) -> None:
    if not isinstance(result, dict):
        return
    stored = order.get("_follow_limit") if isinstance(order.get("_follow_limit"), dict) else {}
    identity = follow_limit_identity(order, settings)
    combined = dict(identity)
    combined.update(stored)
    for source, target in (
        ("match_key", "limit_match_key"),
        ("match_label", "limit_match_label"),
        ("market_key", "limit_market_key"),
        ("market_code", "limit_market_code"),
        ("market_label", "limit_market_label"),
        ("requested_stake", "limit_requested_stake"),
        ("allowed_stake", "limit_allowed_stake"),
        ("match_limit_amount", "limit_match_amount"),
        ("market_limit_amount", "limit_market_amount"),
    ):
        if combined.get(source) not in (None, ""):
            result[target] = combined.get(source)


def record_follow_limit_success(
    result: Dict[str, Any],
    *,
    match_committed: Dict[str, float],
    market_committed: Dict[str, float],
    success_keys: set[str],
) -> bool:
    if not isinstance(result, dict) or result.get("ok") is not True:
        return False
    identity = follow_limit_identity_from_result(result)
    match_key = identity.get("match_key") or ""
    market_key = identity.get("market_key") or ""
    if not match_key or not market_key:
        return False
    ticket_id = _limit_text(result.get("ticket_id"))
    success_key = (
        f"ticket:{ticket_id}"
        if ticket_id
        else "result:"
        + "|".join(
            (
                _limit_text(result.get("target")),
                _limit_text(result.get("orderId") or result.get("order_id")),
                _limit_text(result.get("split_index")),
                _limit_text(result.get("ts_ms") or result.get("ts")),
            )
        )
    )
    if success_key in success_keys:
        return False
    amount = _safe_float(result.get("stake"), 0.0)
    if amount <= 0:
        return False
    success_keys.add(success_key)
    match_committed[match_key] = match_committed.get(match_key, 0.0) + amount
    market_committed[market_key] = market_committed.get(market_key, 0.0) + amount
    return True


def apply_market_rule_to_stake(
    *,
    order: Dict[str, Any],
    settings: Dict[str, Any],
    base_stake: float,
) -> Tuple[float, str, Dict[str, Any]]:
    base = float(base_stake or 0.0)
    market_code, market_label = classify_order_market(order)
    rules = normalize_market_rules(settings)
    rule = rules.get(market_code)
    if not isinstance(rule, dict):
        return (
            base,
            "",
            {
                "market_code": market_code,
                "market_label": market_label,
                "rule_applied": False,
                "rule_enabled": True,
                "rule_ratio_percent": 100.0,
            },
        )

    enabled = bool(rule.get("enabled"))
    try:
        ratio = float(rule.get("ratio_percent") or 0.0)
    except Exception:
        ratio = 0.0
    ratio = max(0.0, min(500.0, ratio))

    meta: Dict[str, Any] = {
        "market_code": market_code,
        "market_label": market_label,
        "rule_applied": True,
        "rule_enabled": enabled,
        "rule_ratio_percent": ratio,
    }

    if not enabled:
        return (0.0, f"已按盘口策略跳过：{market_label}禁跟。", meta)

    stake_after_ratio = base * (ratio / 100.0)
    if stake_after_ratio <= 0:
        return (0.0, f"已按盘口策略跳过：{market_label}比例为 0%。", meta)
    return (float(int(math.ceil(stake_after_ratio))), "", meta)


def load_settings(data_dir: Path) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
            "enabled": False,
            # Tri-state migration: None keeps legacy per-target only_live
            # rules active until the new global switch is explicitly saved.
            "only_live": None,
            "mode": "fixed",
            "fixed_amount": 50,
            "ratio_percent": 100,
            "match_limit_amount": 0,
            "market_limit_amount": 0,
            # 防止重启/重登时把历史订单当新单：只“监听启动后”的订单
            # - 若订单含 bet_ts（下注时间戳），且 bet_ts < (引擎启动时间 - grace) 则直接忽略该订单
            "ignore_history_on_start": True,
            "ignore_history_grace_seconds": 120,
            # 全局去重：多个后台同时监听时，如果“后台订单号”相同，则只跟一单（避免重复跟单风险）
            "dedupe_by_order_id_global": True,
            # 近期回补（秒）：从 orders/*.jsonl 末尾扫描“最近 N 秒”订单，避免因时区/启停/临时 bug 导致漏单
            # 默认给 2 小时：覆盖“引擎重启/停机”期间产生但尚未处理的订单，避免因 ignore_history 等策略误过滤后永远漏单。
            "backfill_recent_seconds": 7200,
            "backfill_tail_lines": 400,
            "backfill_tail_bytes": 262144,
            # 近期回补频率（秒）：避免在主循环每一帧都扫尾部导致性能下降
            "backfill_interval_seconds": 10,
            # 分单：把本次应跟金额均分到 N 个会员执行
            "split_count": 2,
            # 分单间隔：同一订单的第2/3/...笔延迟多少秒再下
            "split_delay_seconds": 0,
            # 重试策略（用于滚球/水位变化等瞬态失败）
            # 注意：过高的重试可能导致追单过慢或在极端情况下重复下注风险。
            # 默认：失败最多重试 5 次，每 20 秒提交一次
            "retry_max_attempts": 5,
            "retry_delay_seconds": 20.0,
            # 默认不做“总超时”强制失败（盘口关闭这类情况经常要等更久）
            "retry_max_seconds": 0,
            # role=quote 的会员是否允许参与真实下注（默认 false：只用于查水/行情）
            "allow_quote_members": False,
            # Snapshot Direct 是唯一生产执行路径。
            "execution_mode": "snapshot_direct",
            "snapshot_direct_enabled": True,
            "snapshot_max_age_ms": 8000,
            # 8000ms is the preferred local-run path. Diagnostic BetContext may
            # use a bounded structure candidate because FT_order_view performs
            # the final submit-time structure/quote confirmation.
            "snapshot_preferred_age_ms": 8000,
            "snapshot_structure_max_age_ms": 15000,
            # 在同一个 fresh Session 中先执行 FT_order_view，
            # 取得提交时平台认可的盘口结构/ioratio，再继续 FT_bet。
            "bet_context_mode": "diagnostic_order_view",
            # UNKNOWN_RESULT 的核验窗口。窗口结束后只允许进入
            # CONFIRMED_FAILED 或 STILL_UNKNOWN，绝不降级成普通失败换会员重下。
            "unknown_result_reconcile_max_seconds": 120,
            "unknown_result_reconcile_max_attempts": 5,
            # FT_bet 明确返回 betError* 且没有票号时可以安全重试。
            # 与 UNKNOWN_RESULT 不同，这类响应已确认没有成交；使用独立
            # 上限防止通用 retry_max_attempts=0 时无限提交。
            "market_reject_max_attempts": 3,
            "market_reject_retry_delay_seconds": 0.5,
            "member_error_cooldown_seconds": 0,
            # 会员登录失效（1X014）冷却；设为 0 表示不做额外冷却（失效会话仍会标记离线并重登）。
            "member_cooldown_1X014_seconds": 0,
            # 目标盘口线暂时找不到（常见于变盘瞬间/主盘↔附加盘切换）：短暂探测窗口
            # - 设为 0：禁用（保持旧行为：找不到就“终态停止该订单跟单”）
            # - 设为 N：最多等待 N 秒，期间按 probe 频率反复尝试“找回原盘口线”
            "wait_line_not_found_probe_seconds": 2,
            "wait_line_not_found_max_seconds": 160,
            # 盘口关闭/暂停下注（1X001）：
            # 每 wait_1X001_probe_seconds 探测一次盘口是否已开盘；只要盘口线仍一致即可继续下注
            "wait_1X001_probe_seconds": 1,
            # 最多等待 wait_1X001_max_seconds；超时则停止该订单跟单（避免长时间占用队列/会员）
            "wait_1X001_max_seconds": 300,
            # 1X001 也遵循同样的“最多重试5次”
            "retry_max_attempts_on_1X001": 5,
            "retry_max_seconds_on_1X001": 0,
            "follow_min_odds": None,
            "follow_max_odds": None,
            # 是否允许盘口线(con/ratio)随会员端变盘而变化：
            # - 严格跟原线：保持 false（推荐）
            # - 允许变盘换线：设为 true（可能导致同一订单分单打到不同盘口线）
            "allow_line_change": False,
            # 仅大小盘允许变盘换线（让球仍严格）。默认 false（严格跟原线）。
            "allow_total_line_change": False,
            # 每轮最多处理多少个待重试任务（避免单笔卡死导致漏单）
            "queue_max_per_tick": 5,
            # 有界并发：不同会员可并行，每个会员最多允许两个进行中的 attempt。
            # 默认并发数按本机运行配置设置为 8。
            # 调度器会在实际可用会员不足时自动降为可运行数量。
            "parallel_attempts": 8,
            "per_member_parallel_attempts": 2,
            "market_rules": normalize_market_rules({}),
            "reverse_rules": normalize_reverse_rules({}),
            "targets": [],
            "members": [],
            "updated_at": "",
        }

    doc = _JSON_FILE_CACHE.read_dict(data_dir / "follow_settings.json", defaults)
    # 补齐当前 Direct 执行必需字段。
    if isinstance(doc, dict):
        for k, v in defaults.items():
            doc.setdefault(k, v)
    else:
        doc = dict(defaults)
    doc.pop("max_amount", None)
    # 硬隔离：quote/read 号永远不参与真实下注
    doc["allow_quote_members"] = False
    # 当前生产执行模式：Snapshot 精确盘口 + 同会话 FT_order_view。
    doc["execution_mode"] = "snapshot_direct"
    doc["snapshot_direct_enabled"] = True
    # 不允许旧配置/旧客户端把已验证的生产链路降级为直接提交源水位。
    doc["bet_context_mode"] = "diagnostic_order_view"
    for retired_key in (
        "snapshot_missing_policy",
        "snapshot_only_for_market",
        "snapshot_max_age_seconds",
        "order_view_confirm_before_bet",
        "order_view_confirm_timeout_s",
        "use_odds_cache_for_total_odds",
        "odds_cache_max_age_seconds",
        "use_quote_member_for_total_odds",
        "quote_member_total_odds_timeout_s",
        "fast_retry_1X015_max_attempts",
        "fast_retry_betError_max_attempts",
        "fast_retry_betError_delay_seconds",
        "fast_first_bet_on_new_order",
        "game_more_quick_mode",
        "game_more_scan_all_gids",
        "one_shot_force_full_scan_on_line_not_found",
        "delay_1X001_first_seconds",
        "delay_1X001_repeat_seconds",
    ):
        doc.pop(retired_key, None)
    doc["follow_min_odds"] = _normalize_follow_odds_limit(doc.get("follow_min_odds"))
    doc["follow_max_odds"] = _normalize_follow_odds_limit(doc.get("follow_max_odds"))
    doc["market_rules"] = normalize_market_rules(doc)
    doc["reverse_rules"] = normalize_reverse_rules(doc)
    return doc


def extract_err_from_result(result: Dict[str, Any]) -> str:
    if bool(result.get("ok")):
        return ""
    terminal_reconcile = str(result.get("reconcile_terminal_status") or "").strip().upper()
    if terminal_reconcile in {"CONFIRMED_FAILED", "STILL_UNKNOWN"}:
        return terminal_reconcile
    if bool(result.get("reconcile_pending")) or bool(result.get("unknown_result")):
        return UNKNOWN_RESULT
    direct_result_status = str(result.get("snapshot_direct_result_status") or "").strip().upper()
    if direct_result_status in {MEMBER_ERROR, MARKET_ERROR, MARKET_SUBMIT_REJECTED, CONTEXT_RETRYABLE}:
        return direct_result_status
    for head_key in ["order_view_head", "bet_head"]:
        head = result.get(head_key)
        if isinstance(head, dict):
            err = str(head.get("errormsg") or "").strip()
            if err:
                return err
            code = str(head.get("code") or "").strip()
            if result.get("execution_mode") == "snapshot_direct" and code and code not in {"501", "560"}:
                return code

    direct_status = str(result.get("snapshot_direct_status") or "").strip().upper()
    if direct_status in {
        SNAPSHOT_MISSING,
        SNAPSHOT_STALE,
        TARGET_LINE_MISSING,
        SNAPSHOT_INVALID,
        MEMBER_IORATIO_MISSING,
        MEMBER_IORATIO_STALE,
        MEMBER_IORATIO_INVALID,
        BET_CONTEXT_MISMATCH,
        MARKET_CHANGED_DURING_CONTEXT,
        MARKET_SUBMIT_REJECTED,
        CONTEXT_RETRYABLE,
        UNKNOWN_RESULT,
    }:
        return direct_status
    if direct_result_status == UNKNOWN_RESULT:
        return UNKNOWN_RESULT
    msg = str(result.get("message") or "")
    m = re.search(r"err=([0-9A-Za-z_]+)", msg)
    if m:
        return m.group(1).strip()

    # 网络异常以前只落在 error_type/exception 字段，导致 last_err 为空，
    # 调度器把 502/超时误当成普通错误并每秒重试。统一成稳定的内部错误码，
    # 既便于 UI 展示，也让退避策略能按故障类型生效。
    error_type = str(result.get("error_type") or "").strip().lower()
    exception = str(result.get("exception") or "").strip().lower()
    text = f"{error_type} {exception} {msg.lower()}"
    if "hardtimeout" in text or "hard_timeout" in text or "单次尝试超时" in text:
        return "HARD_TIMEOUT"
    if "readtimeout" in text or "read timed out" in text or "读取超时" in text:
        return "READ_TIMEOUT"
    if "502 server error" in text or "bad gateway" in text:
        return "UPSTREAM_502"
    if (
        "remote end closed" in text
        or "connection aborted" in text
        or "connectionerror" in text
        or "chunkedencodingerror" in text
        or "连接中断" in text
        or "连接断开" in text
    ):
        return "CONNECTION_ERROR"
    if "httperror" in text:
        return "HTTP_ERROR"
    return ""


def classify_execution_error(result: Dict[str, Any]) -> str:
    """将失败按会员、基础设施、盘口和结果不确定性分层。"""
    if bool(result.get("ok")):
        return "SUCCESS"
    terminal = str(result.get("reconcile_terminal_status") or "").strip().upper()
    if terminal in {"CONFIRMED_FAILED", "STILL_UNKNOWN"}:
        return terminal
    if bool(result.get("reconcile_pending")) or bool(result.get("unknown_result")):
        return UNKNOWN_RESULT
    direct_status = str(result.get("snapshot_direct_result_status") or "").strip().upper()
    if direct_status in {MEMBER_ERROR, MARKET_ERROR, MARKET_SUBMIT_REJECTED, CONTEXT_RETRYABLE}:
        return direct_status
    err = extract_err_from_result(result).upper()
    text = " ".join(
        str(result.get(key) or "").lower()
        for key in ("error_type", "exception", "message", "reconcile_error")
    )
    if err in {"1X012", "1X014", "DOUBLELOGIN", "MEMBER_ERROR", "SESSION_MISSING"} or "doublelogin" in text:
        return "MEMBER_ERROR"
    if err == CONTEXT_RETRYABLE:
        return CONTEXT_RETRYABLE
    if err in _MARKET_RETRY_ERRORS or err.startswith("BETERROR"):
        return "MARKET_ERROR"
    if any(token in text for token in (
        "proxyerror", "connectionerror", "connection aborted", "remote end closed",
        "readtimeout", "read timed out", "502 server error", "bad gateway",
        "httperror", "chunkedencodingerror", "连接中断", "连接断开",
    )):
        return "INFRA_ERROR"
    return "UNKNOWN_ERROR"


def split_stakes(total_stake: float, split_count: int) -> List[float]:
    """
    将总跟单金额拆成 split_count 份（整数金额），尽量平均，保证总和等于 total_stake。
    - total_stake <= 0 或 split_count <= 1：返回 [total_stake]
    - 会过滤掉 0 金额的分片
    """
    # 拆分金额也必须为整数：如果 total_stake 带小数（例如比例跟单/外部输入），统一向上取整。
    total_raw = float(total_stake or 0)
    total = int(math.ceil(total_raw)) if total_raw > 0 else 0
    n = int(split_count or 1)
    if total <= 0:
        return []
    if n <= 1:
        return [float(total)]
    n = max(1, min(50, n))
    base = total // n
    rem = total % n
    parts = [base + 1] * rem + [base] * (n - rem)
    parts = [p for p in parts if p > 0]
    return [float(p) for p in parts]


def effective_split_count(total_stake: float, desired_split_count: int, min_stake_per_bet: float) -> int:
    """
    计算“有效分单数”，避免出现单笔金额 < 最小投注导致被会员端拒单（常见 err=1X004）。
    - total_stake：本次跟单总金额（允许小数，内部会向上取整）
    - desired_split_count：配置期望分单数
    - min_stake_per_bet：单笔最小投注（默认 50，按常见 RMB 站点）
    """
    total_raw = float(total_stake or 0)
    total = int(math.ceil(total_raw)) if total_raw > 0 else 0
    desired = int(desired_split_count or 1)
    if total <= 0 or desired <= 1:
        return 1
    min_raw = float(min_stake_per_bet or 0)
    min_int = int(math.ceil(min_raw)) if min_raw > 0 else 0
    if min_int <= 0:
        return max(1, min(50, desired))
    max_n = total // min_int
    if max_n <= 1:
        return 1
    return max(1, min(50, min(desired, int(max_n))))


def effective_parallel_attempts(configured: int, split_count: int, max_workers: int = 50) -> int:
    """Allow every split Task to run concurrently when enough members are available."""
    limit = max(1, int(max_workers or 1))
    return max(1, min(limit, max(int(configured or 1), int(split_count or 1))))


def _extract_member_gold_gmax(result: Dict[str, Any]) -> float:
    for key in ("order_view_min", "order_view_head"):
        head = result.get(key)
        if not isinstance(head, dict):
            continue
        val = _safe_float(head.get("gold_gmax"), 0.0)
        if val > 0:
            return float(val)

    msg = str(result.get("message") or "")
    m = re.search(r"超过最大投注[（(]\s*\d+(?:\.\d+)?\s*>\s*(\d+(?:\.\d+)?)", msg)
    if m:
        return _safe_float(m.group(1), 0.0)
    return 0.0


def split_job_current_part_on_member_max_limit(
    job: RetryJob,
    result: Dict[str, Any],
    *,
    min_stake_per_bet: float = 50.0,
    max_split_count: int = 50,
) -> bool:
    """
    会员端返回 gold_gmax 且当前分片金额超限时，将“当前分片”二拆一并插回队列。
    这样不改变订单计划总额，只把单注金额压到更容易通过会员单注限额的范围。
    """
    if bool(result.get("ok")):
        return False

    gold_gmax = _extract_member_gold_gmax(result)
    if gold_gmax <= 0:
        return False

    try:
        idx = int(job.split_index or 0)
    except Exception:
        idx = 0
    parts = [float(x) for x in (job.split_stakes or []) if _safe_float(x, 0.0) > 0]
    if not parts:
        parts = split_stakes(float(job.stake or result.get("stake") or 0.0), 1)
    if idx < 0 or idx >= len(parts):
        return False

    current = float(parts[idx])
    if current <= gold_gmax:
        return False
    if len(parts) >= max(1, int(max_split_count or 50)):
        return False

    new_parts = split_stakes(current, 2)
    if len(new_parts) < 2:
        return False
    min_stake = max(0.0, float(min_stake_per_bet or 0.0))
    if min_stake > 0 and any(float(p) < min_stake for p in new_parts):
        return False

    shift = len(new_parts) - 1
    parts[idx : idx + 1] = [float(p) for p in new_parts]
    job.split_stakes = parts
    job.split_count = len(parts)
    job.stake = float(sum(parts))

    if isinstance(job.split_part_results, dict) and job.split_part_results:
        shifted: Dict[int, Dict[str, Any]] = {}
        for k, v in job.split_part_results.items():
            try:
                old_idx = int(k)
            except Exception:
                continue
            if old_idx < idx:
                shifted[old_idx] = v
            elif old_idx > idx:
                shifted[old_idx + shift] = v
        job.split_part_results = shifted

    result["dynamic_split"] = {
        "reason": "gold_gmax",
        "gold_gmax": float(gold_gmax),
        "old_stake": float(current),
        "new_stakes": [float(p) for p in new_parts],
        "split_index": int(idx) + 1,
        "split_count": int(job.split_count or 1),
    }
    old_msg = str(result.get("message") or "").strip()
    new_desc = " + ".join(str(int(p)) if abs(float(p) - round(float(p))) < 1e-9 else str(float(p)) for p in new_parts)
    split_msg = f"已按会员最大投注自动拆分当前分单：{int(current)} -> {new_desc}，继续打满计划金额。"
    result["message"] = f"{old_msg}；{split_msg}" if old_msg else split_msg
    return True


def _is_retryable_code_err(code: str, err: str, msg: str) -> Optional[bool]:
    code = str(code or "").strip()
    err = str(err or "").strip()
    msg = str(msg or "").strip()

    if code.lower() == "error" and msg.lower() == "doublelogin":
        return True
    if code == "555":
        if err == "1X012":
            return False
        if err:
            return True
    # 站点的 order_view 接口以 501/560 表示“OK”（见 ft_order_view），该返回头经常会被一并落盘到 result。
    # 它不能用来判断“是否重试”，否则会出现：
    # - LINE_NOT_FOUND 等需要进入探测窗口的情况，被误判为“不重试”直接终态失败（你看到的“盘口还在但找不到”）。
    if code in {"501", "560"}:
        return None
    return None


def is_retryable_follow_result(result: Dict[str, Any]) -> bool:
    if bool(result.get("ok")):
        return False
    if bool(result.get("skipped")):
        return False
    if str(result.get("reconcile_terminal_status") or "").strip().upper() in {"CONFIRMED_FAILED", "STILL_UNKNOWN"}:
        return False
    # UNKNOWN_RESULT is retryable only in the scheduler sense: the next action
    # is reconcile-only and can never send FT_bet again.  This check must run
    # before the diagnostic one-submit guard, otherwise a transport failure
    # after FT_bet is incorrectly finalized as a normal failed split.
    if bool(result.get("reconcile_pending")) or bool(result.get("unknown_result")):
        return True
    direct_status = str(result.get("snapshot_direct_result_status") or "").strip().upper()
    if direct_status == UNKNOWN_RESULT:
        return True
    # Diagnostic mode normally permits at most one real FT_bet per split.
    # A structured betError* response is the exception: the upstream has
    # explicitly rejected the bet and returned no ticket, so a bounded retry
    # cannot duplicate a successful wager. UNKNOWN_RESULT remains reconcile
    # only and is handled above.
    if (
        str(result.get("bet_context_mode") or "").strip().lower() == "diagnostic_order_view"
        and bool(result.get("snapshot_direct_submission"))
    ):
        return bool(result.get("market_submit_reject_retryable")) and direct_status == MARKET_ERROR
    if direct_status == MARKET_SUBMIT_REJECTED:
        return False
    if direct_status in {
        MEMBER_ERROR,
        MARKET_ERROR,
        UNKNOWN_RESULT,
        "WAITING_MARKET",
        SNAPSHOT_MISSING,
        SNAPSHOT_STALE,
        TARGET_LINE_MISSING,
        SNAPSHOT_INVALID,
        MEMBER_IORATIO_MISSING,
        MEMBER_IORATIO_STALE,
        MEMBER_IORATIO_INVALID,
        BET_CONTEXT_MISMATCH,
        MARKET_CHANGED_DURING_CONTEXT,
        CONTEXT_RETRYABLE,
    }:
        return True

    msg = str(result.get("message") or "")
    if "已停止该订单跟单" in msg or "已停止跟单" in msg:
        return False
    if "暂不支持" in msg or "不支持的玩法" in msg:
        return False
    if "余额不足" in msg or "额度不足" in msg:
        return direct_status == MEMBER_ERROR

    # 优先从返回头判断
    for head_key in ["bet_head", "order_view_head"]:
        head = result.get(head_key)
        if isinstance(head, dict):
            code = str(head.get("code") or "")
            err = str(head.get("errormsg") or "")
            m2 = str(head.get("msg") or "")
            rr = _is_retryable_code_err(code, err, m2)
            if rr is not None:
                return rr

    # 网络/解析/响应格式异常等：默认可重试
    if (
        "正在重试" in msg
        or "请求失败" in msg
        or "网络" in msg
        or "超时" in msg
        or "无响应" in msg
        or "响应为空" in msg
        or "响应格式" in msg
        or "解析失败" in msg
        or "断开" in msg
    ):
        return True

    # 保守：未知失败默认可重试（由 retry_max_* 兜底）
    return True


def _explicit_market_rejection_code(result: Dict[str, Any]) -> str:
    """Return an upstream betError* only when FT_bet definitely rejected it."""
    if not isinstance(result, dict) or bool(result.get("ok")):
        return ""
    if not bool(result.get("snapshot_direct_submission")):
        return ""
    request = result.get("ft_bet_request")
    if not isinstance(request, dict) or str(request.get("p") or "").strip() != "FT_bet":
        return ""
    raw = result.get("ft_bet_raw_response") if isinstance(result.get("ft_bet_raw_response"), dict) else {}
    try:
        http_status = int(raw.get("http_status") or 0)
    except Exception:
        http_status = 0
    if http_status < 200 or http_status >= 300:
        return ""
    parsed = raw.get("parsed_fields") if isinstance(raw.get("parsed_fields"), dict) else {}
    bet_head = result.get("bet_head") if isinstance(result.get("bet_head"), dict) else {}
    if any(
        str(ticket or "").strip()
        for ticket in (result.get("ticket_id"), bet_head.get("ticket_id"), parsed.get("ticket_id"))
    ):
        return ""
    code = str(raw.get("errormsg") or result.get("error_code") or "").strip()
    return code if code.lower().startswith("beterror") else ""


def _apply_market_submit_breaker(
    job: RetryJob,
    result: Dict[str, Any],
    *,
    max_attempts: int = 3,
    retry_delay_seconds: float = 0.5,
) -> bool:
    """Bound explicit betError* retries for one split and exact MarketRow.

    Only a real FT_bet with a structured betError* response and no ticket is
    counted. Transport failures remain UNKNOWN_RESULT and never enter this
    retry path. A changed MarketRow or error code starts a fresh bounded
    window because the next request is no longer the same rejected payload.
    """
    if not isinstance(job, RetryJob) or not isinstance(result, dict):
        return False
    error_code = _explicit_market_rejection_code(result)
    if not error_code:
        return False
    fingerprint = str(
        result.get("market_row_fingerprint")
        or result.get("snapshot_direct_fingerprint")
        or (result.get("submit_params") or {}).get("market_row_fingerprint")
        or ""
    ).strip()
    if not fingerprint:
        request = result.get("ft_bet_request") if isinstance(result.get("ft_bet_request"), dict) else {}
        fingerprint = "|".join(
            str(request.get(key) or "").strip()
            for key in ("gid", "gtype", "wtype", "rtype", "chose_team", "con", "ratio")
        )
    if not fingerprint.strip("|"):
        return False
    previous_fp = str(getattr(job, "market_submit_reject_fingerprint", "") or "").strip()
    previous_code = str(getattr(job, "market_submit_reject_code", "") or "").strip()
    if previous_fp == fingerprint and previous_code == error_code:
        count = int(getattr(job, "market_submit_reject_count", 0) or 0) + 1
    else:
        count = 1
    job.market_submit_reject_fingerprint = fingerprint
    job.market_submit_reject_code = error_code
    job.market_submit_reject_count = count
    result["market_submit_reject_fingerprint"] = fingerprint
    result["market_submit_reject_code"] = error_code
    result["market_submit_reject_count"] = count
    limit = max(1, min(20, int(max_attempts)))
    delay = max(0.0, min(30.0, float(retry_delay_seconds)))
    result["market_submit_reject_max_attempts"] = limit
    result["market_submit_reject_retry_delay_seconds"] = delay
    if count < limit:
        result["market_submit_reject_retryable"] = True
        result["message"] = f"平台明确拒绝下注（{error_code}），{delay:g}秒后换会员重试（{count}/{limit}）。"
        return False
    result["market_submit_rejected"] = True
    result["market_submit_original_error_code"] = error_code
    result["snapshot_direct_status"] = MARKET_SUBMIT_REJECTED
    result["snapshot_direct_result_status"] = MARKET_SUBMIT_REJECTED
    result["result_status"] = MARKET_SUBMIT_REJECTED
    result["error_code"] = MARKET_SUBMIT_REJECTED
    result["message"] = (
        f"同一拆分单同一盘口连续 {count} 次返回 {error_code}，"
        "已达到平台拒单重试上限，停止继续 FT_bet（MARKET_SUBMIT_REJECTED）。"
    )
    return True


def _schedule_explicit_market_rejection_retry(job: RetryJob, result: Dict[str, Any], *, now: float) -> bool:
    """Schedule a bounded betError* retry without waiting for a revision bump."""
    if not isinstance(job, RetryJob) or not bool(result.get("market_submit_reject_retryable")):
        return False
    delay = max(0.0, min(30.0, _safe_float(result.get("market_submit_reject_retry_delay_seconds"), 0.5)))
    retry_at = float(now) + delay
    job.order.pop("_wait_for_snapshot_revision", None)
    job.order["_market_reject_retry_not_before"] = retry_at
    job.line_not_found_wait_started_at = 0.0
    job.line_not_found_wait_deadline_at = 0.0
    job.next_try_at = retry_at
    return True


def retry_delay_for_result(result: Dict[str, Any], attempt_no: int = 0) -> float:
    """Choose a small, bounded delay for transient failures.

    Upstream 502/断连 and read/hard timeouts are infrastructure failures, not
    odds changes.  Retrying them every second hammers the upstream and keeps all
    members occupied.  A short capped backoff gives other queued orders a turn
    while retaining quick recovery for the live market.
    """
    if bool(result.get("reconcile_pending")):
        return 15.0
    err = extract_err_from_result(result)
    if err == CONTEXT_RETRYABLE:
        n = max(1, int(attempt_no or 1))
        return (0.15, 0.30, 0.50)[min(n - 1, 2)]
    if err.startswith("betError") or err in {"1X015", "1X016"}:
        return 0.0
    if err == "1X014":
        return 2.0

    error_type = str(result.get("error_type") or "").strip().lower()
    exception = str(result.get("exception") or "").strip().lower()
    message = str(result.get("message") or "").strip().lower()
    transient = (
        error_type in {"hardtimeout", "readtimeout", "connectionerror", "chunkedencodingerror", "proxyerror", "httperror"}
        or "502 server error" in exception
        or "bad gateway" in exception
        or "remote end closed" in exception
        or "connection aborted" in exception
        or "readtimeout" in exception
        or "请求超时" in message
        or "单次尝试超时" in message
        or "系统异常（httperror" in message
        or "系统异常（readtimeout" in message
    )
    if not transient:
        return 1.0
    n = max(1, int(attempt_no or 1))
    return min(12.0, 2.0 + (n - 1) * 1.5)


def _begin_unknown_result_state(job: RetryJob, result: Dict[str, Any], settings: Dict[str, Any]) -> None:
    """把一次不确定提交转换成持久化核验状态。"""
    now = float(now_ts())
    max_seconds = max(1.0, min(3600.0, _safe_float(settings.get("unknown_result_reconcile_max_seconds"), 120.0)))
    if float(getattr(job, "unknown_result_started_at", 0.0) or 0.0) <= 0.0:
        started = _safe_float(result.get("bet_submitted_at"), now)
        job.unknown_result_started_at = started if started > 0.0 else now
    if float(getattr(job, "unknown_result_deadline_at", 0.0) or 0.0) <= 0.0:
        job.unknown_result_deadline_at = float(job.unknown_result_started_at) + max_seconds
    job.unknown_result_status = "UNKNOWN_RESULT"
    # 这些内部字段随 job.order 持久化，重启后仍能只做 reconcile。
    job.order["_unknown_result_started_at"] = float(job.unknown_result_started_at)
    job.order["_unknown_result_deadline_at"] = float(job.unknown_result_deadline_at)
    job.order["_unknown_result_check_count"] = int(getattr(job, "unknown_result_check_count", 0) or 0)
    job.order["_unknown_result_submitted_at"] = _safe_float(
        result.get("bet_submitted_at"),
    ) or _safe_float(job.order.get("_unknown_result_submitted_at"), float(job.unknown_result_started_at))
    params = result.get("params") if isinstance(result.get("params"), dict) else {}
    direct_intent = result.get("snapshot_direct_source_intent") if isinstance(result.get("snapshot_direct_source_intent"), dict) else {}
    gid = str(params.get("gid") or direct_intent.get("gid") or job.order.get("gid") or "").strip()
    if gid:
        job.order["_unknown_result_gid"] = gid


def _clear_unknown_result_state(job: RetryJob) -> None:
    job.unknown_result_started_at = 0.0
    job.unknown_result_deadline_at = 0.0
    job.unknown_result_check_count = 0
    job.unknown_result_status = ""
    job.unknown_result_last_check_at = 0.0
    for key in (
        "_reconcile_only",
        "_unknown_result_started_at",
        "_unknown_result_deadline_at",
        "_unknown_result_check_count",
        "_unknown_result_submitted_at",
        "_unknown_result_gid",
    ):
        job.order.pop(key, None)


def load_arsenal(data_dir: Path) -> Dict[str, Any]:
    doc = _JSON_FILE_CACHE.read_dict(data_dir / "arsenal.json", {"members": []})
    doc["request_cooldown_enabled"] = False
    for member in doc.get("members") or []:
        if isinstance(member, dict):
            member["request_cooldown_enabled"] = False
            member["request_cooldown_until"] = 0
            member["network_cooldown_until"] = 0
    return doc


def _load_current_members(data_dir: Path) -> List[Dict[str, Any]]:
    """Load a fresh member snapshot for the final acquire/dispatch gate.

    The scheduler keeps a per-tick snapshot for cheap candidate probing, but
    status/session patches may be written by another worker between probing
    and dispatch.  This helper deliberately bypasses that stale in-memory
    list while retaining the normal member-default normalization.
    """
    try:
        doc = load_arsenal(data_dir)
        return [ensure_member_defaults(m) for m in (doc.get("members") or []) if isinstance(m, dict)]
    except Exception:
        return []


def load_mappings(data_dir: Path) -> Dict[str, str]:
    return _JSON_FILE_CACHE.read_dict(data_dir / "mappings.json", {})


def _to_float(x: Any, default: float = 0.0) -> float:
    """Parse a numeric value without letting optional telemetry break the engine."""
    try:
        if x is None:
            return float(default)
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if not s:
            return float(default)
        return float(s)
    except Exception:
        return float(default)


def member_balance(member: Dict[str, Any]) -> float:
    """
    余额(额度)取值策略：
    - 优先 maxcredit（弹药库 watcher 里解析的额度/余额）
    - 其次 nowcredit（下注返回的实时余额）
    - 最后 cash（部分站点会返回现金余额）
    """
    return max(0.0, _to_float(member.get("maxcredit") or member.get("nowcredit") or member.get("cash") or 0))


def _member_has_transient_network_issue(member: Dict[str, Any], *, now_s: Optional[float] = None) -> bool:
    # 502/超时只记录诊断，不阻止会员再次被选中。
    return False


def _network_error_class(result: Dict[str, Any]) -> str:
    """Classify post-submit transport failures without changing result semantics."""

    response = result.get("ft_bet_response_fingerprint") if isinstance(result.get("ft_bet_response_fingerprint"), dict) else {}
    status = _safe_int(response.get("status") or result.get("http_status"), 0)
    error_code = str(result.get("error_code") or "").strip().lower()
    message = str(result.get("error_message") or result.get("message") or "").strip().lower()
    if status == 502 or "bad gateway" in message:
        return "UPSTREAM_502"
    if "readtimeout" in error_code or "read timed out" in message:
        return "READ_TIMEOUT"
    if "ssl" in error_code or "ssl eof" in message:
        return "SSL_EOF"
    if any(token in error_code for token in ("connectionerror", "proxyerror", "connecttimeout", "chunkedencoding")):
        return error_code.upper()
    return ""


def _context_network_error_class(*, http_status: Any = None, error: Any = "") -> str:
    """Classify a pre-submit FT_order_view transport failure."""
    try:
        status = int(http_status or 0)
    except Exception:
        status = 0
    message = str(error or "").strip().lower()
    if status == 502 or "bad gateway" in message:
        return "UPSTREAM_502"
    if status in {503, 504}:
        return f"UPSTREAM_{status}"
    if "readtimeout" in message or "read timed out" in message:
        return "READ_TIMEOUT"
    if "ssl" in message or "ssl eof" in message:
        return "SSL_EOF"
    if any(token in message for token in ("connectionerror", "proxyerror", "connecttimeout", "chunkedencoding", "connection aborted", "connection reset", "remote end closed")):
        return "CONNECTION_ERROR"
    return ""


def _record_member_network_issue(*, data_dir: Path, member_id: str, error_class: str) -> None:
    """Record a transport error without making the member unavailable."""

    if not member_id or not error_class:
        return
    try:
        patch_member(
            data_dir / "arsenal.json",
            member_id,
            {
                "network_health": "degraded",
                "network_last_error": error_class,
                "network_cooldown_until": 0,
                "request_cooldown_until": 0,
                "request_cooldown_enabled": False,
            },
        )
    except Exception:
        return


def _member_has_login_seed(member: Dict[str, Any]) -> bool:
    return bool(
        str((member or {}).get("base_url") or "").strip()
        and str((member or {}).get("username") or "").strip()
        and str((member or {}).get("password") or "").strip()
    )


def _member_target_match(mid: str, *, target: str, mappings: Dict[str, str]) -> bool:
    if mid in mappings:
        assigned_target = str(mappings.get(mid) or "").strip()
        if assigned_target and assigned_target != str(target or "").strip():
            return False
    return True


def _member_eligible_for_stake(
    member: Dict[str, Any],
    *,
    stake: float,
    avoid_ids: set,
    target: str,
    mappings: Dict[str, str],
) -> bool:
    mid = str((member or {}).get("id") or "").strip()
    if not mid or mid in avoid_ids:
        return False
    # 硬隔离：拉数据/quote 号永远不参与真实下注
    if bool(is_quote_role_member(member)) or str((member or {}).get("usage") or "").strip().lower() == "read":
        return False
    if not _member_target_match(mid, target=target, mappings=mappings):
        return False
    if str((member or {}).get("status") or "").strip().lower() != "online":
        return False
    # 网络抖动不再触发会员冷却；真正的可用性由在线状态和会话完整性决定。
    if not all(
        str((member or {}).get(key) or "").strip()
        for key in ("base_url", "ver", "uid", "cookie")
    ):
        return False
    session_ready, _session_reason = session_ready_state(member)
    if not session_ready:
        return False
    if member_balance(member) + 1e-9 < float(stake or 0.0):
        return False
    return True


def _member_pick_score(
    member: Dict[str, Any],
    *,
    stake: float,
    now_s: float,
    target: str,
    mappings: Dict[str, str],
) -> Tuple[int, int, int, float, float, int, str]:
    mid = str((member or {}).get("id") or "").strip()
    status_online = 1 if str((member or {}).get("status") or "").strip().lower() == "online" else 0
    stable = 1 if (not _member_has_transient_network_issue(member, now_s=now_s)) else 0
    mapped_exact = 1 if (mid in mappings and str(mappings.get(mid) or "").strip() == str(target or "").strip()) else 0
    margin = float(member_balance(member) - float(stake or 0.0))
    last_ok_at = _to_float((member or {}).get("last_ok_at"))
    # 最近成功过优先，降低“刚重登/刚异常”的会员命中概率
    ok_recency_score = 0.0
    if last_ok_at > 0.0:
        ok_recency_score = max(0.0, 600.0 - max(0.0, now_s - float(last_ok_at)))
    last_err_penalty = 0 if not str((member or {}).get("last_error") or "").strip() else -1
    return (
        status_online,
        stable,
        mapped_exact,
        margin,
        ok_recency_score,
        last_err_penalty,
        mid,
    )


def inspect_member_capacity_for_stake(
    members: List[Dict[str, Any]],
    stake: float,
    *,
    target: str = "",
    mappings: Optional[Dict[str, str]] = None,
    allow_quote_members: bool = False,
) -> Dict[str, int]:
    stake = float(stake or 0.0)
    mappings = mappings or {}
    now_s = float(now_ts())
    online_any = 0
    online_stable_any = 0
    online_non_quote = 0
    for m in members or []:
        if not isinstance(m, dict):
            continue
        if not _member_eligible_for_stake(m, stake=stake, avoid_ids=set(), target=target, mappings=mappings):
            continue
        st = str(m.get("status") or "").strip().lower()
        is_quote = bool(is_quote_role_member(m))
        if st == "online":
            online_any += 1
            if not _member_has_transient_network_issue(m, now_s=now_s):
                online_stable_any += 1
            if not is_quote:
                online_non_quote += 1
    if allow_quote_members:
        online_eff = online_any
    else:
        # quote/read 账号不会进入真实执行容量。
        online_eff = online_non_quote if online_non_quote > 0 else online_any
    return {
        "online": int(online_eff),
        "online_stable": int(online_stable_any),
        "fallback": 0,
        "pickable": int(online_eff),
    }


def pick_random_member_for_stake(
    members: List[Dict[str, Any]],
    stake: float,
    *,
    avoid_ids: Optional[set] = None,
    target: str = "",
    mappings: Optional[Dict[str, str]] = None,
    allow_quote_members: bool = False,
) -> Optional[Dict[str, Any]]:
    # 规则：
    # 1) 先做资格过滤（余额、映射、avoid_ids）
    # 2) 只允许 ONLINE + Session 完整的会员；网络错误不会触发时间冷却
    # 3) IDLE 由 AttemptDispatcher 的会员租约保证
    # 4) 在同一资格池内随机选择
    stake = float(stake or 0)
    if stake <= 0:
        return None
    avoid_ids = avoid_ids or set()
    mappings = mappings or {}

    def eligible(m: Dict[str, Any]) -> bool:
        return _member_eligible_for_stake(
            m,
            stake=float(stake),
            avoid_ids=avoid_ids,
            target=target,
            mappings=mappings,
        )

    def choose(pool: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not pool:
            return None
        try:
            return random.choice(pool)
        except Exception:
            return pool[0]

    online_all = [m for m in members if eligible(m)]
    if online_all:
        now_pick = float(now_ts())
        online_pool = [m for m in online_all if not _member_has_transient_network_issue(m, now_s=now_pick)]
        if bool(allow_quote_members):
            return choose(online_pool)
        online_non_quote = [m for m in online_pool if not is_quote_role_member(m)]
        return choose(online_non_quote)
    return None


def ensure_member_session(
    *,
    data_dir: Path,
    member: Dict[str, Any],
    debug: bool,
) -> Dict[str, Any]:
    """
    返回 member dict。
    注意：不在跟单热路径里同步重登（避免 Playwright 卡住导致订单堵塞）。
    若缺少 ver/uid/cookie，会标记该会员为 offline，交由 member_arsenal_watch 自动重登。
    """
    member_id = str(member.get("id") or "").strip()
    base_url = str(member.get("base_url") or "").strip()
    username = str(member.get("username") or "").strip()
    password = str(member.get("password") or "").strip()
    proxy = str(member.get("proxy") or "").strip()
    ver = str(member.get("ver") or "").strip()
    uid = str(member.get("uid") or "").strip()
    cookie = str(member.get("cookie") or "").strip()

    if ver and uid and cookie:
        return member

    try:
        patch_member(
            data_dir / "arsenal.json",
            member_id,
            {
                "status": "offline",
                "last_error": "session_missing: need relogin",
            },
        )
    except Exception:
        pass

    return member


def _final_session_gate(
    *,
    data_dir: Path,
    member: Dict[str, Any],
    selected_generation: int,
    selected_fingerprint: str,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Reject a stale BetContext when the member session changed meanwhile."""

    member_id = str(member.get("id") or "").strip()
    current_doc = load_arsenal(data_dir)
    current = next(
        (
            item
            for item in (current_doc.get("members") or [])
            if isinstance(item, dict) and str(item.get("id") or "").strip() == member_id
        ),
        None,
    )
    if not isinstance(current, dict):
        return False, "SESSION_MEMBER_MISSING", {}
    ready, reason = session_ready_state(current)
    if not ready:
        return False, str(reason or "SESSION_NOT_READY"), current

    current_generation = _safe_int(current.get("session_generation"), 0)
    if selected_generation > 0 and current_generation > 0 and current_generation != selected_generation:
        return False, "SESSION_CHANGED_BEFORE_SUBMIT", current

    current_fingerprint = member_session_fingerprint(current)
    if selected_fingerprint and current_fingerprint and current_fingerprint != selected_fingerprint:
        return False, "SESSION_CHANGED_BEFORE_SUBMIT", current

    # Compare the individual identity fields as a final guard for legacy rows
    # that predate session_fingerprint/session_generation persistence.
    for key in ("base_url", "uid", "ver", "cookie"):
        if str(current.get(key) or "").strip() != str(member.get(key) or "").strip():
            return False, "SESSION_CHANGED_BEFORE_SUBMIT", current
    return True, "SESSION_READY", current


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _append_jsonl_timed(path: Path, obj: Dict[str, Any]) -> Tuple[float, float]:
    started_at = float(now_ts())
    append_jsonl(path, obj)
    return started_at, float(now_ts())


def _append_trace_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    """
    轻量时序落盘：用于复盘“从后台来单→入队→每次尝试→分单下一笔→终态”的全过程耗时与原因。
    """
    try:
        payload = dict(obj or {})
        payload.setdefault("ts", int(now_ts()))
        payload.setdefault("ts_ms", int(now_ts_ms()))
        append_jsonl(path, payload)
    except Exception:
        return


def _query_member_recent_wager(
    *,
    session: requests.Session,
    base_url: str,
    ver: str,
    uid: str,
    member_id: str,
    gid: str,
    stake: float,
    submitted_at: float,
    timeout_s: int,
) -> Dict[str, Any]:
    """下注结果不确定时刷新今日注单，避免因超时重复下注。"""
    form = {
        "p": "get_today_wagers",
        "uid": str(uid or ""),
        "ver": str(ver or ""),
        "langx": "zh-cn",
        "LS": "g",
        "selGtype": "ALL",
        "chk_cw": "N",
        "ts": str(int(time.time() * 1000)),
        "format": "json",
        "db_slow": "N",
    }
    try:
        raw = member_post_session(
            session=session,
            base_url=str(base_url or "").strip().rstrip("/"),
            ver=str(ver or ""),
            form=form,
            timeout_s=max(2, min(10, int(timeout_s or 5))),
        )
        text = str(raw or "").strip()
        payload: Any
        try:
            payload = json.loads(text) if text else {}
        except Exception:
            payload = {"raw": text}
        return reconcile_uncertain_bet(
            lambda: payload,
            member_id=str(member_id or ""),
            gid=str(gid or ""),
            stake=float(stake or 0.0),
            submitted_at=float(submitted_at or 0.0),
            window_seconds=90.0,
        )
    except BaseException as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}


def _reconcile_only_result(
    *,
    data_dir: Path,
    settings: Dict[str, Any],
    member: Dict[str, Any],
    order: Dict[str, Any],
    stake: float,
    timeout_s: int,
    session_pool: Optional["MemberSessionPool"] = None,
) -> Dict[str, Any]:
    """核验 UNKNOWN_RESULT，严禁在该路径重新调用 FT_bet。

    这是调度器的第二阶段动作：只查询会员今日注单，并把结果归一为
    SUCCESS、CONFIRMED_FAILED 或 STILL_UNKNOWN。调用方会根据窗口决定
    是否继续核验；任何情况下都不会把 UNKNOWN_RESULT 当成普通会员失败。
    """
    member_id = str(member.get("id") or "")
    label = str(member.get("label") or member.get("username") or member_id)
    submitted_at = _safe_float(order.get("_unknown_result_submitted_at"), 0.0)
    gid = str(order.get("_unknown_result_gid") or order.get("gid") or "").strip()
    if not gid:
        try:
            gid = str(MarketIntent.from_order(order).gid or "")
        except Exception:
            gid = ""
    now = float(now_ts())
    deadline = _safe_float(order.get("_unknown_result_deadline_at"), 0.0)
    checks = _safe_int(order.get("_unknown_result_check_count"), 0) + 1
    max_attempts = max(1, min(100, _safe_int(settings.get("unknown_result_reconcile_max_attempts"), 5)))
    max_seconds = max(1.0, min(3600.0, _safe_float(settings.get("unknown_result_reconcile_max_seconds"), 120.0)))
    if deadline <= 0.0:
        started = _safe_float(order.get("_unknown_result_started_at"), now)
        deadline = started + max_seconds
    execution_mode = str(settings.get("execution_mode") or "snapshot_direct").strip().lower()

    base: Dict[str, Any] = {
        "ts": int(now),
        "ts_ms": int(now * 1000),
        "live": bool(is_live_backend_order(order)),
        "member_id": member_id,
        "member": label,
        "target": str(order.get("target") or ""),
        "orderId": str(order.get("orderId") or ""),
        "order_id": str(order.get("orderId") or ""),
        "stake": float(stake or 0.0),
        "ok": False,
        "reconcile_only": True,
        "unknown_result": True,
        "unknown_result_check_count": checks,
        "unknown_result_deadline_at": deadline,
        "execution_error_class": UNKNOWN_RESULT,
        "execution_mode": execution_mode,
        "execution_path": execution_mode,
        "real_submission": "reconcile_only",
        "result_status": UNKNOWN_RESULT,
    }
    if not (submitted_at > 0.0 and gid and member.get("base_url") and member.get("ver") and member.get("uid") and member.get("cookie")):
        base.update({
            "reconcile_status": "unavailable",
            "reconcile_state": "STILL_UNKNOWN",
            "reconcile_terminal_status": "STILL_UNKNOWN",
            "reconcile_pending": False,
            "message": "UNKNOWN_RESULT 缺少核验所需上下文，已进入 STILL_UNKNOWN，禁止换会员重下。",
        })
        return base

    try:
        session = _get_member_session(member=member, session_pool=session_pool)
        rec = _query_member_recent_wager(
            session=session,
            base_url=str(member.get("base_url") or ""),
            ver=str(member.get("ver") or ""),
            uid=str(member.get("uid") or ""),
            member_id=member_id,
            gid=gid,
            stake=float(stake or 0.0),
            submitted_at=submitted_at,
            timeout_s=min(max(2, int(timeout_s or 0)), 8),
        )
    except BaseException as exc:
        rec = {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}

    rec_status = str(rec.get("status") or "unavailable").strip().lower()
    base["reconcile_status"] = rec_status
    base["reconcile_checked_at"] = now
    if rec_status == "matched":
        base.update({
            "ok": True,
            "ticket_id": str(rec.get("ticket_id") or ""),
            "reconciled": True,
            "reconcile_state": "SUCCESS",
            "reconcile_terminal_status": "SUCCESS",
            "reconcile_pending": False,
            "message": "UNKNOWN_RESULT 已通过注单核验确认成交。",
            "execution_error_class": "SUCCESS",
        })
        return base

    expired = now >= deadline or checks >= max_attempts
    if expired and rec_status == "not_found":
        base.update({
            "reconcile_state": "CONFIRMED_FAILED",
            "reconcile_terminal_status": "CONFIRMED_FAILED",
            "reconcile_pending": False,
            "message": "UNKNOWN_RESULT 核验窗口结束，确认未找到对应注单，已停止且不会换会员重下。",
        })
        return base
    if expired:
        base.update({
            "reconcile_state": "STILL_UNKNOWN",
            "reconcile_terminal_status": "STILL_UNKNOWN",
            "reconcile_pending": False,
            "reconcile_error": str(rec.get("error") or "核验接口不可用"),
            "message": "UNKNOWN_RESULT 核验窗口结束但仍无法确认，已进入 STILL_UNKNOWN，需人工处理。",
        })
        return base
    base.update({
        "reconcile_state": "RETRY_PENDING",
        "reconcile_pending": True,
        "message": "UNKNOWN_RESULT 正在核验，暂不提交、暂不更换会员。",
    })
    return base


def _snapshot_max_age_ms(settings: Dict[str, Any]) -> int:
    """Return the configured freshness window, with one bounded migration default."""
    try:
        value = int(settings.get("snapshot_max_age_ms") or 0)
    except Exception:
        value = 0
    if value <= 0:
        value = 8000
    return max(50, min(30000, value))


def _snapshot_preferred_age_ms(settings: Dict[str, Any]) -> int:
    """Preferred cache age; the configured 8000ms remains the default."""
    raw = settings.get("snapshot_preferred_age_ms")
    try:
        value = int(raw) if raw not in (None, "") else _snapshot_max_age_ms(settings)
    except Exception:
        value = _snapshot_max_age_ms(settings)
    return max(50, min(30000, value))


def _snapshot_structure_max_age_ms(settings: Dict[str, Any], *, context_mode: str) -> int:
    """Bound the candidate row window used by diagnostic BetContext only.

    A bounded 15-second structure window gives the diagnostic context a candidate
    while the subsequent FT_order_view remains the authoritative submit-time
    structure/quote check. Non-context paths retain the preferred-age gate.
    """
    preferred = _snapshot_preferred_age_ms(settings)
    if str(context_mode or "").strip().lower() != "diagnostic_order_view":
        return preferred
    raw = settings.get("snapshot_structure_max_age_ms")
    try:
        value = int(raw) if raw not in (None, "") else 15000
    except Exception:
        value = 15000
    return max(preferred, min(30000, value))


def _lookup_snapshot_candidate(
    service: MarketSnapshotService,
    intent: MarketIntent,
    *,
    settings: Dict[str, Any],
    context_mode: str,
) -> Tuple[Any, int]:
    """Prefer a row within the configured age, then use bounded diagnostic fallback."""
    preferred = _snapshot_preferred_age_ms(settings)
    # A failed FT_order_view is a context transport problem, not a public
    # revision problem.  On its bounded retry we may reuse the current exact
    # structure window; FT_order_view will perform the submit-time validation
    # again.  This avoids waiting for a new Snapshot revision after a 502.
    if bool(settings.get("_context_retry")) and str(context_mode or "").strip().lower() == "diagnostic_order_view":
        structure_max = _snapshot_structure_max_age_ms(settings, context_mode=context_mode)
        lookup = service.lookup(intent, max_age_ms=structure_max)
        return lookup, structure_max
    lookup = service.lookup(intent, max_age_ms=preferred)
    if lookup.snapshot is not None and lookup.status == SNAPSHOT_VALID:
        return lookup, preferred
    structure_max = _snapshot_structure_max_age_ms(settings, context_mode=context_mode)
    if structure_max > preferred:
        fallback = service.lookup(intent, max_age_ms=structure_max)
        if fallback.snapshot is not None and fallback.status == SNAPSHOT_VALID:
            return fallback, structure_max
        # Preserve the more useful target/stale/missing status from the wider
        # lookup when the preferred window was simply too narrow.
        if fallback.status != SNAPSHOT_STALE or lookup.status == SNAPSHOT_STALE:
            lookup = fallback
    return lookup, preferred


def _member_ioratio_max_age_ms(settings: Dict[str, Any]) -> int:
    """Member quote freshness is independent from the board revision."""
    try:
        value = int(settings.get("member_ioratio_max_age_ms") or 0)
    except Exception:
        value = 0
    if value <= 0:
        value = _snapshot_max_age_ms(settings)
    return max(50, min(10000, value))


def _snapshot_status_message(status: str, message: str = "") -> str:
    messages = {
        SNAPSHOT_MISSING: "盘口快照暂不可用，正在等待盘口更新。",
        SNAPSHOT_STALE: "盘口快照已过期，正在等待最新盘口。",
        TARGET_LINE_MISSING: "目标盘口线暂时不存在，正在等待盘口更新。",
        SNAPSHOT_INVALID: "盘口快照参数无效，正在等待盘口更新。",
        MEMBER_IORATIO_MISSING: "执行会员的最新水位尚未同步，正在等待盘口厨房更新。",
        MEMBER_IORATIO_STALE: "执行会员的水位已过期，正在等待盘口厨房更新。",
        MEMBER_IORATIO_INVALID: "执行会员水位与目标盘口行不一致，禁止提交。",
        BET_CONTEXT_MISMATCH: "FT_order_view 确认盘口已变化，正在等待目标盘口更新。",
        MARKET_CHANGED_DURING_CONTEXT: "FT_order_view 返回期间盘口已变化，正在等待最新盘口。",
        CONTEXT_RETRYABLE: "FT_order_view 传输失败，正在刷新会话后快速重试。",
    }
    base = messages.get(str(status or "").strip(), "盘口快照暂不可用，正在重试。")
    detail = str(message or "").strip()
    return f"{base} err={status}" + (f" ({detail})" if detail else "")


def _snapshot_intent_for_settings(
    intent: MarketIntent,
    *,
    order: Dict[str, Any],
    settings: Dict[str, Any],
) -> MarketIntent:
    """Apply the existing reverse strategy before resolving a market snapshot."""
    rules = normalize_reverse_rules(settings)
    market_code, _ = classify_order_market(order)
    source = ""
    if bool(rules.get("all")) and market_code in {"handicap", "total_over", "total_under"}:
        source = "all"
    elif market_code == "handicap" and bool(rules.get("handicap")):
        source = "handicap"
    elif market_code in {"total_over", "total_under"} and bool(rules.get("total")):
        source = "total"
    if not source:
        return intent
    if intent.market_type == "HANDICAP":
        side = {"HOME": "AWAY", "AWAY": "HOME"}.get(intent.side, intent.side)
        line = -float(intent.target_line) if intent.target_line is not None else None
        return replace(intent, side=side, target_line=line)
    if intent.market_type == "TOTAL":
        side = {"OVER": "UNDER", "UNDER": "OVER"}.get(intent.side, intent.side)
        return replace(intent, side=side)
    return intent


def _snapshot_direct_result(
    *,
    result: ExecutionResult,
    member: Dict[str, Any],
    order: Dict[str, Any],
    stake: float,
    template: Optional[Any] = None,
    intent: Optional[MarketIntent] = None,
    snapshot_status: str = "",
    snapshot_message: str = "",
) -> Dict[str, Any]:
    """Translate the pure executor result into the scheduler result shape."""
    member_id = str(member.get("id") or "")
    label = str(member.get("label") or member.get("username") or member_id)
    response = dict(result.response or {}) if isinstance(result.response, dict) else {}
    out: Dict[str, Any] = {
        "ts": int(now_ts()),
        "ts_ms": int(now_ts_ms()),
        "live": bool(is_live_backend_order(order)),
        "member_id": member_id,
        "member": label,
        "member_account": str(member.get("account") or member.get("username") or member.get("login") or label),
        "target": str(order.get("target") or ""),
        "orderId": str(order.get("orderId") or ""),
        "order_id": str(order.get("orderId") or ""),
        "stake": float(stake or 0.0),
        "source_stake": _safe_float(order.get("stake"), 0.0),
        "ok": result.status == "SUCCESS",
        "execution_mode": "snapshot_direct",
        "execution_path": "snapshot_direct",
        "real_submission": "snapshot_direct",
        "snapshot_direct_submission": True,
        "snapshot_direct_status": str(snapshot_status or "VALID"),
        "snapshot_direct_result_status": str(result.status or UNKNOWN_RESULT),
        "result_status": str(result.status or UNKNOWN_RESULT),
        "snapshot_direct_message": str(snapshot_message or ""),
        "snapshot_direct": {
            "execution_mode": "snapshot_direct",
            "snapshot_direct_submission": True,
            "status": str(result.status or UNKNOWN_RESULT),
        },
    }
    if intent is not None:
        out["snapshot_direct_intent"] = intent.to_dict()
        out["snapshot_direct_target_line"] = intent.target_line
    if template is not None:
        try:
            template_data = template.to_dict()
        except Exception:
            template_data = {}
        if isinstance(template_data, dict):
            out["submit_params"] = dict(template_data)
            out["snapshot_direct"]["submit_template"] = dict(template_data)
            for source_key, target_key in (
                ("snapshot_revision", "snapshot_direct_revision"),
                ("snapshot_fetched_at", "snapshot_direct_fetched_at"),
                ("snapshot_age_ms", "snapshot_direct_age_ms"),
                ("wtype", "snapshot_direct_wtype"),
                ("rtype", "snapshot_direct_rtype"),
                ("chose_team", "snapshot_direct_chose_team"),
                ("con", "snapshot_direct_con"),
                ("ratio", "snapshot_direct_ratio"),
                ("ioratio", "snapshot_direct_ioratio"),
                ("line", "snapshot_direct_actual_line"),
                ("snapshot_fingerprint", "snapshot_direct_fingerprint"),
                ("quote_source_member", "quote_source_member"),
                ("execution_member", "execution_member"),
                ("member_ioratio_source_member", "member_ioratio_source_member"),
                ("public_snapshot_revision", "public_snapshot_revision"),
                ("public_line", "public_line"),
                ("public_con", "public_con"),
                ("public_ratio", "public_ratio"),
                ("member_ioratio", "member_ioratio"),
                ("member_ioratio_fetched_at", "member_ioratio_fetched_at"),
                ("member_ioratio_age_ms", "member_ioratio_age_ms"),
                ("market_row_gid", "market_row_gid"),
                ("market_row_line", "market_row_line"),
                ("market_row_con", "market_row_con"),
                ("market_row_ratio", "market_row_ratio"),
                ("market_row_revision", "market_row_revision"),
                ("market_row_fingerprint", "market_row_fingerprint"),
            ):
                if source_key in template_data:
                    out[target_key] = template_data[source_key]
    if response:
        out["bet_head"] = response
        if response.get("nowcredit") not in (None, ""):
            out["nowcredit"] = response.get("nowcredit")
    if result.request:
        out["ft_bet_request"] = dict(result.request)
    response_fingerprint = result.session_meta.get("ft_bet_response_fingerprint") if isinstance(result.session_meta, dict) else {}
    if not isinstance(response_fingerprint, dict):
        response_fingerprint = {}
    out["ft_bet_raw_response"] = {
        "http_status": result.http_status,
        "response_body_length": int(
            response_fingerprint.get("body_length")
            or len(str(result.raw_response or "").encode("utf-8"))
        ),
        "response_body_sha256": str(
            response_fingerprint.get("body_sha256")
            or hashlib.sha256(str(result.raw_response or "").encode("utf-8")).hexdigest()
        ),
        "content_type": str(response_fingerprint.get("content_type") or ""),
        "server": str(response_fingerprint.get("server") or ""),
        "via": str(response_fingerprint.get("via") or ""),
        "ticket_present": bool(result.ticket_id),
        "xml_code": str(result.xml_code or ""),
        "errormsg": response_fingerprint.get("xml_errormsg", str(result.xml_errormsg or "")),
        # Keep only fields used by retry/UI decisions.  The full upstream XML
        # remains in memory for parsing but is never persisted to JSONL.
        "parsed_fields": {
            key: response.get(key)
            for key in ("code", "errormsg", "msg", "ticket_id", "ticketId")
            if key in response
        },
    }
    out["execution_member"] = member_id
    if result.ticket_id:
        out["ticket_id"] = str(result.ticket_id)
    if result.error_code:
        out["error_code"] = str(result.error_code)
        out["message"] = str(result.error_message or "") or f"下注失败 err={result.error_code}"
    elif result.error_message:
        out["message"] = str(result.error_message)
    if result.status == MEMBER_ERROR:
        out["message"] = out.get("message") or "会员失败，正在切换健康会员。"
    elif result.status == MARKET_ERROR:
        out["message"] = out.get("message") or "盘口变化，正在等待新的 Snapshot。"
    elif result.status == UNKNOWN_RESULT:
        out["message"] = out.get("message") or "下注结果不确定，正在核验，暂不更换会员。"
        out["reconcile_pending"] = True
        out["unknown_result"] = True
    elif result.status == "SUCCESS":
        out["message"] = "下注成功"
    return out


def _snapshot_attempt_trace_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten direct-attempt telemetry for follow_trace.jsonl consumers."""
    direct = result.get("snapshot_direct") if isinstance(result.get("snapshot_direct"), dict) else {}
    template = direct.get("submit_template") if isinstance(direct.get("submit_template"), dict) else {}
    return {
        "execution_mode": str(result.get("execution_mode") or "snapshot_direct"),
        "execution_path": str(result.get("execution_path") or result.get("execution_mode") or "snapshot_direct"),
        "source_order_direct_submission": bool(result.get("source_order_direct_submission")),
        "member_id": str(result.get("member_id") or ""),
        "member_account": str(result.get("member_account") or result.get("member") or ""),
        "snapshot_direct_status": str(result.get("snapshot_direct_status") or direct.get("status") or ""),
        "snapshot_direct_revision": int(result.get("snapshot_direct_revision") or template.get("snapshot_revision") or 0),
        "snapshot_direct_fetched_at": float(result.get("snapshot_direct_fetched_at") or template.get("snapshot_fetched_at") or 0.0),
        "snapshot_direct_age_ms": int(result.get("snapshot_direct_age_ms") or template.get("snapshot_age_ms") or 0),
        "snapshot_fingerprint": str(result.get("snapshot_direct_fingerprint") or template.get("snapshot_fingerprint") or ""),
        "target_line": result.get("snapshot_direct_target_line"),
        "actual_line": result.get("snapshot_direct_actual_line"),
        "wtype": str(result.get("snapshot_direct_wtype") or template.get("wtype") or ""),
        "rtype": str(result.get("snapshot_direct_rtype") or template.get("rtype") or ""),
        "chose_team": str(result.get("snapshot_direct_chose_team") or template.get("chose_team") or ""),
        "con": str(result.get("snapshot_direct_con") or template.get("con") or ""),
        "ratio": str(result.get("snapshot_direct_ratio") or template.get("ratio") or ""),
        "ioratio": str(result.get("snapshot_direct_ioratio") or template.get("ioratio") or ""),
        "quote_source_member": str(result.get("quote_source_member") or template.get("quote_source_member") or ""),
        "execution_member": str(result.get("execution_member") or template.get("execution_member") or ""),
        "member_ioratio_source_member": str(result.get("member_ioratio_source_member") or template.get("member_ioratio_source_member") or ""),
        "public_snapshot_revision": int(result.get("public_snapshot_revision") or template.get("public_snapshot_revision") or 0),
        "public_line": result.get("public_line", template.get("public_line")),
        "public_con": str(result.get("public_con") or template.get("public_con") or ""),
        "public_ratio": str(result.get("public_ratio") or template.get("public_ratio") or ""),
        "member_ioratio": str(result.get("member_ioratio") or template.get("member_ioratio") or ""),
        "member_ioratio_fetched_at": float(result.get("member_ioratio_fetched_at") or template.get("member_ioratio_fetched_at") or 0.0),
        "member_ioratio_age_ms": int(result.get("member_ioratio_age_ms") or template.get("member_ioratio_age_ms") or 0),
        "market_row_gid": str(result.get("market_row_gid") or template.get("market_row_gid") or ""),
        "market_row_line": result.get("market_row_line", template.get("market_row_line")),
        "market_row_con": str(result.get("market_row_con") or template.get("market_row_con") or ""),
        "market_row_ratio": str(result.get("market_row_ratio") or template.get("market_row_ratio") or ""),
        "market_row_revision": int(result.get("market_row_revision") or template.get("market_row_revision") or 0),
        "market_row_fingerprint": str(result.get("market_row_fingerprint") or template.get("market_row_fingerprint") or ""),
        "ft_bet_request": result.get("ft_bet_request") if isinstance(result.get("ft_bet_request"), dict) else {},
        "ft_bet_raw_response": result.get("ft_bet_raw_response") if isinstance(result.get("ft_bet_raw_response"), dict) else {},
        "ft_bet_request_fingerprint": result.get("ft_bet_request_fingerprint") if isinstance(result.get("ft_bet_request_fingerprint"), dict) else {},
        "ft_bet_response_fingerprint": result.get("ft_bet_response_fingerprint") if isinstance(result.get("ft_bet_response_fingerprint"), dict) else {},
        "amount": float(result.get("stake") or 0.0),
        "ft_bet_started_at": result.get("ft_bet_started_at") or result.get("submit_started_at"),
        "ft_bet_elapsed_ms": int(result.get("ft_bet_elapsed_ms") or result.get("submit_elapsed_ms") or 0),
        "result_status": str(result.get("snapshot_direct_result_status") or direct.get("status") or ("SUCCESS" if result.get("ok") else "")),
        "ticket_id": str(result.get("ticket_id") or ""),
        "error_code": str(result.get("error_code") or ""),
        "error_message": str(result.get("message") or result.get("snapshot_direct_message") or ""),
        "session_mode": str(result.get("session_mode") or ""),
        "session_created_at": result.get("session_created_at"),
        "session_object_id": str(result.get("session_object_id") or ""),
        "build_version": str(result.get("build_version") or ""),
        "git_commit": str(result.get("git_commit") or ""),
        "build_id": str(result.get("build_id") or ""),
        "member_id_hash": str(result.get("member_id_hash") or result.get("member_id_sha256") or ""),
        "uid_hash": str(result.get("uid_hash") or result.get("uid_sha256") or ""),
        "ver_sha256": str(result.get("ver_sha256") or ""),
        "cookie_count": int(result.get("cookie_count") or 0),
        "trust_env": bool(result.get("trust_env")),
        "proxy_enabled": bool(result.get("proxy_enabled")),
        "session_generation": int(result.get("session_generation") or 0),
        "session_ready": bool(result.get("session_ready")),
        "session_ready_reason": str(result.get("session_ready_reason") or ""),
        "session_fingerprint": str(result.get("session_fingerprint") or ""),
        "network_health": str(result.get("network_health") or ""),
        "network_error_class": str(result.get("network_error_class") or ""),
        "bet_context_mode": str(result.get("bet_context_mode") or ""),
        "bet_context_request": result.get("bet_context_request") if isinstance(result.get("bet_context_request"), dict) else {},
        "bet_context_http_status": result.get("bet_context_http_status"),
        "bet_context_raw_response": str(result.get("bet_context_raw_response") or ""),
        "bet_context_response": result.get("bet_context_response") if isinstance(result.get("bet_context_response"), dict) else {},
        "bet_context_code": str(result.get("bet_context_code") or ""),
        "bet_context_fields": result.get("bet_context_fields") if isinstance(result.get("bet_context_fields"), dict) else {},
        "bet_context_request_binding": result.get("bet_context_request_binding") if isinstance(result.get("bet_context_request_binding"), dict) else {},
        "bet_context_optional_fields": result.get("bet_context_optional_fields") if isinstance(result.get("bet_context_optional_fields"), dict) else {},
        "bet_context_ioratio_match": bool(result.get("bet_context_ioratio_match")),
        "bet_context_spread": str(result.get("bet_context_spread") or ""),
        "bet_context_elapsed_ms": int(result.get("bet_context_elapsed_ms") or 0),
        "bet_context_error": str(result.get("bet_context_error") or ""),
        "member_cached_ioratio": str(result.get("member_cached_ioratio") or ""),
        "member_cached_ioratio_age_ms": int(result.get("member_cached_ioratio_age_ms") or 0),
        "result_persist_started_at": result.get("result_persist_started_at"),
        "result_persist_finished_at": result.get("result_persist_finished_at"),
        "result_persist_elapsed_ms": result.get("result_persist_elapsed_ms"),
        "member_patch_started_at": result.get("member_patch_started_at"),
        "member_patch_finished_at": result.get("member_patch_finished_at"),
        "member_patch_elapsed_ms": result.get("member_patch_elapsed_ms"),
        "context_ioratio": str(result.get("context_ioratio") or ""),
        "context_ratio": str(result.get("context_ratio") or ""),
        "final_submit_ioratio": str(result.get("final_submit_ioratio") or ""),
        "final_submit_ratio": str(result.get("final_submit_ratio") or ""),
        "ioratio_source": str(result.get("ioratio_source") or ""),
        "context_started_at": result.get("context_started_at"),
        "context_finished_at": result.get("context_finished_at"),
        "market_row_fingerprint_before": str(result.get("market_row_fingerprint_before") or ""),
        "market_row_fingerprint_after": str(result.get("market_row_fingerprint_after") or ""),
        "market_row_revision_before": int(result.get("market_row_revision_before") or 0),
        "market_row_revision_after": int(result.get("market_row_revision_after") or 0),
        "snapshot_preferred_age_ms": int(result.get("snapshot_preferred_age_ms") or 0),
        "snapshot_structure_max_age_ms": int(result.get("snapshot_structure_max_age_ms") or 0),
        "snapshot_candidate_age_ms": int(result.get("snapshot_candidate_age_ms") or 0),
        "context_to_ft_bet_ms": result.get("context_finished_to_ft_bet_started_ms"),
        "ft_bet_to_result_ms": result.get("ft_bet_started_to_result_ms"),
        "first_candidate_snapshot_ms": result.get("first_candidate_snapshot_ms"),
        "candidate_to_member_acquired_ms": result.get("candidate_to_member_acquired_ms"),
        "member_acquired_to_context_finished_ms": result.get("member_acquired_to_context_finished_ms"),
        "member_released_at": result.get("member_released_at"),
        "member_release_ms": result.get("member_release_ms"),
        "total_order_elapsed_ms": result.get("total_order_elapsed_ms"),
        "snapshot_publish_at": result.get("snapshot_publish_at"),
        "snapshot_wakeup_at": result.get("snapshot_wakeup_at"),
        "snapshot_publish_to_wakeup_ms": result.get("snapshot_publish_to_wakeup_ms"),
        "trigger_reason": str(result.get("trigger_reason") or ""),
        "trigger_event_seq": int(result.get("trigger_event_seq") or 0),
        "trigger_publish_at": result.get("trigger_publish_at"),
        "trigger_wakeup_at": result.get("trigger_wakeup_at"),
        "trigger_publish_to_wakeup_ms": result.get("trigger_publish_to_wakeup_ms"),
        "candidate_detected_at": result.get("candidate_detected_at"),
        "job_marked_runnable_at": result.get("job_marked_runnable_at"),
        "runnable_queue_entered_at": result.get("runnable_queue_entered_at"),
        "dispatcher_selected_at": result.get("dispatcher_selected_at"),
        "attempt_started_at": result.get("attempt_started_at"),
        "candidate_to_runnable_ms": result.get("candidate_to_runnable_ms"),
        "runnable_queue_wait_ms": result.get("runnable_queue_wait_ms"),
        "dispatcher_wait_ms": result.get("dispatcher_wait_ms"),
        "member_acquire_ms": result.get("member_acquire_ms"),
        "context_retry_count": int(result.get("context_retry_count") or 0),
        "context_retry_delay_ms": result.get("context_retry_delay_ms"),
        "context_retry_fast": bool(result.get("context_retry_fast")),
        "market_wait_count": int(result.get("market_wait_count") or 0),
        "member_wait_count": int(result.get("member_wait_count") or 0),
        "split_completed_at": result.get("split_completed_at"),
        "aggregate_started_at": result.get("aggregate_started_at"),
        "aggregate_finished_at": result.get("aggregate_finished_at"),
    }


def _snapshot_waiting_result(
    *,
    status: str,
    message: str,
    member: Dict[str, Any],
    order: Dict[str, Any],
    stake: float,
    intent: Optional[MarketIntent] = None,
) -> Dict[str, Any]:
    member_id = str(member.get("id") or "")
    label = str(member.get("label") or member.get("username") or member_id)
    is_context_retry = str(status or "").strip().upper() == CONTEXT_RETRYABLE
    state_name = CONTEXT_RETRYABLE if is_context_retry else "WAITING_MARKET"
    out: Dict[str, Any] = {
        "ts": int(now_ts()),
        "ts_ms": int(now_ts_ms()),
        "live": bool(is_live_backend_order(order)),
        "member_id": member_id,
        "member": label,
        "member_account": str(member.get("account") or member.get("username") or label),
        "target": str(order.get("target") or ""),
        "orderId": str(order.get("orderId") or ""),
        "order_id": str(order.get("orderId") or ""),
        "stake": float(stake or 0.0),
        "source_stake": _safe_float(order.get("stake"), 0.0),
        "ok": False,
        "status": state_name,
        "execution_state": state_name,
        "execution_mode": "snapshot_direct",
        "execution_path": "snapshot_direct",
        "real_submission": "none",
        "snapshot_direct_submission": False,
        "snapshot_direct_status": str(status or SNAPSHOT_MISSING),
        "snapshot_direct_result_status": str(status or SNAPSHOT_MISSING) if is_context_retry else "WAITING_MARKET",
        "result_status": str(status or SNAPSHOT_MISSING) if is_context_retry else "WAITING_MARKET",
        "snapshot_direct": {
            "execution_mode": "snapshot_direct",
            "snapshot_direct_submission": False,
            "status": str(status or SNAPSHOT_MISSING),
            "message": str(message or ""),
        },
        "message": _snapshot_status_message(status, message),
        "error_code": str(status or SNAPSHOT_MISSING),
    }
    if intent is not None:
        out["snapshot_direct_intent"] = intent.to_dict()
        out["snapshot_direct_target_line"] = intent.target_line
    return out


def _probe_snapshot_market(
    *,
    data_dir: Path,
    settings: Dict[str, Any],
    order: Dict[str, Any],
    stake: float,
) -> Dict[str, Any]:
    """在租用会员前确认盘口厨房已准备好可提交的最新 Snapshot。"""
    try:
        source_intent = MarketIntent.from_order(order, split_amount=float(stake or 0.0))
        intent = _snapshot_intent_for_settings(source_intent, order=order, settings=settings)
        if not intent.gid:
            raise ValueError("source order has no gid")
        execution_mode = str(settings.get("execution_mode") or "snapshot_direct").strip().lower()
        if execution_mode == SOURCE_ORDER_DIRECT:
            template = build_source_order_submit_template(intent, execution_member="")
            return {
                "ready": True,
                "status": SOURCE_ORDER_READY,
                "message": "源订单 FT_bet 协议字段已通过本地校验。",
                "intent": intent,
                "revision": 0,
                "fetched_at": 0.0,
                "age_ms": 0,
                "submit_template": template.to_dict(),
            }
        context_mode = str(settings.get("bet_context_mode") or "disabled").strip().lower()
        service = MarketSnapshotService(Path(data_dir) / "odds_snapshot_cache.json")
        lookup, candidate_max_age_ms = _lookup_snapshot_candidate(
            service,
            intent,
            settings=settings,
            context_mode=context_mode,
        )
        if lookup.snapshot is None or lookup.status != SNAPSHOT_VALID:
            status = str(lookup.status or SNAPSHOT_MISSING)
            return {
                "ready": False,
                "status": status,
                "message": _snapshot_status_message(status, lookup.message),
                "intent": intent,
            }
        validated = SnapshotValidator().validate(
            intent,
            lookup.snapshot,
            max_age_ms=candidate_max_age_ms,
        )
        if validated.snapshot is None or validated.status != SNAPSHOT_VALID:
            status = str(validated.status or SNAPSHOT_INVALID)
            return {
                "ready": False,
                "status": status,
                "message": _snapshot_status_message(status, validated.message),
                "intent": intent,
            }
        expected_retry_fingerprint = str(settings.get("_context_retry_fingerprint") or "").strip()
        if bool(settings.get("_context_retry")) and expected_retry_fingerprint and (
            str(validated.snapshot.market_row_fingerprint or "") != expected_retry_fingerprint
        ):
            return {
                "ready": False,
                "status": MARKET_ERROR,
                "message": "Context 重试前盘口 fingerprint 已变化，等待新的 Exact MarketRow。",
                "intent": intent,
            }
        market_row = validated.snapshot
        return {
            "ready": True,
            "status": SNAPSHOT_VALID,
            "intent": intent,
            "revision": int(market_row.revision or 0),
            "fetched_at": float(market_row.fetched_at or 0.0),
            "age_ms": int(market_row.age_ms or 0),
            "snapshot_preferred_age_ms": _snapshot_preferred_age_ms(settings),
            "snapshot_structure_max_age_ms": _snapshot_structure_max_age_ms(settings, context_mode=context_mode),
            "snapshot_candidate_age_ms": int(candidate_max_age_ms),
            "market_row": market_row.to_dict(),
        }
    except Exception as exc:
        return {
            "ready": False,
            "status": SNAPSHOT_INVALID,
            "message": _snapshot_status_message(SNAPSHOT_INVALID, f"{type(exc).__name__}: {exc}"),
        }


def _source_order_waiting_result(
    *,
    status: str,
    message: str,
    member: Dict[str, Any],
    order: Dict[str, Any],
    stake: float,
    intent: Optional[MarketIntent] = None,
) -> Dict[str, Any]:
    result = _snapshot_waiting_result(
        status=status,
        message=message,
        member=member,
        order=order,
        stake=stake,
        intent=intent,
    )
    result.update(
        {
            "execution_mode": SOURCE_ORDER_DIRECT,
            "execution_path": SOURCE_ORDER_DIRECT,
            "real_submission": "none",
            "source_order_direct_submission": False,
        }
    )
    direct = result.get("snapshot_direct")
    if isinstance(direct, dict):
        direct["execution_mode"] = SOURCE_ORDER_DIRECT
    return result


def _mark_source_order_execution(result: Dict[str, Any]) -> Dict[str, Any]:
    result.update(
        {
            "execution_mode": SOURCE_ORDER_DIRECT,
            "execution_path": SOURCE_ORDER_DIRECT,
            "real_submission": SOURCE_ORDER_DIRECT,
            "source_order_direct_submission": True,
        }
    )
    direct = result.get("snapshot_direct")
    if isinstance(direct, dict):
        direct["execution_mode"] = SOURCE_ORDER_DIRECT
    return result


def _place_bet_source_order_direct(
    *,
    data_dir: Path,
    settings: Dict[str, Any],
    member: Dict[str, Any],
    order: Dict[str, Any],
    stake: float,
    timeout_s: int,
    debug: bool,
    session_pool: Optional["MemberSessionPool"] = None,
) -> Dict[str, Any]:
    """Submit one validated source order directly without Snapshot/FT_order_view."""

    del debug
    execution_session = None
    execution_session_meta: Dict[str, Any] = {}
    ft_bet_started = False
    try:
        selected_session_generation = _safe_int(member.get("session_generation"), 0)
        selected_session_fingerprint = member_session_fingerprint(member)
        source_intent = MarketIntent.from_order(order, split_amount=float(stake or 0.0))
        intent = _snapshot_intent_for_settings(source_intent, order=order, settings=settings)
        template = build_source_order_submit_template(
            intent,
            execution_member=str(member.get("id") or ""),
        )

        odds_skipped, odds_message, odds_meta = evaluate_follow_odds_limit(
            settings=settings,
            ioratio=template.ioratio,
        )
        if odds_skipped:
            result = _snapshot_direct_result(
                result=ExecutionResult(
                    "SKIPPED",
                    str(member.get("id") or ""),
                    error_code="ODDS_LIMIT",
                    error_message=odds_message,
                ),
                member=member,
                order=order,
                stake=stake,
                template=template,
                intent=intent,
                snapshot_status=SOURCE_ORDER_READY,
            )
            _mark_source_order_execution(result)
            result["real_submission"] = "none"
            result["source_order_direct_submission"] = False
            result["skipped"] = True
            result["skip_reason"] = "odds_limit"
            result["odds_limit"] = odds_meta
            result["snapshot_direct_source_intent"] = source_intent.to_dict()
            result["ioratio_source"] = "source_order"
            result["bet_context_mode"] = "disabled"
            return result

        session_ok, session_reason, current_member = _final_session_gate(
            data_dir=data_dir,
            member=member,
            selected_generation=selected_session_generation,
            selected_fingerprint=selected_session_fingerprint,
        )
        if not session_ok:
            waiting = _source_order_waiting_result(
                status=session_reason,
                message=f"提交前 Session gate 拒绝源订单直投：{session_reason}",
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )
            waiting.update(
                {
                    "session_ready": False,
                    "session_ready_reason": session_reason,
                    "session_generation": selected_session_generation,
                    "session_fingerprint": selected_session_fingerprint,
                    "execution_member": str(member.get("id") or ""),
                    "bet_context_mode": "disabled",
                    "ioratio_source": "source_order",
                }
            )
            return waiting

        selected_session_generation = _safe_int(
            current_member.get("session_generation"), selected_session_generation
        )
        selected_session_fingerprint = member_session_fingerprint(current_member)
        session_created_at = float(now_ts())
        execution_session = make_member_session(
            base_url=str(current_member.get("base_url") or ""),
            proxy=str(current_member.get("proxy") or ""),
            cookie=str(current_member.get("cookie") or ""),
        )
        execution_session_meta = {
            "session_mode": "fresh_per_attempt",
            "session_created_at": session_created_at,
            "session_object_id": str(id(execution_session)),
            "proxy": "configured" if str(current_member.get("proxy") or "").strip() else "",
            "proxy_enabled": bool(str(current_member.get("proxy") or "").strip()),
            **_session_cookie_diagnostics(execution_session),
        }

        submit_started_at = float(now_ts())
        try:
            ft_bet_started = True
            execution = MemberExecutor().submit(
                current_member,
                template,
                float(stake or 0.0),
                session=execution_session,
                timeout_s=max(2, int(timeout_s or 0)),
            )
        except (TimeoutError, HardTimeout) as exc:
            execution = ExecutionResult(
                UNKNOWN_RESULT,
                str(current_member.get("id") or ""),
                error_code=type(exc).__name__ or "HardTimeout",
                error_message="下注请求超时，结果待核验。",
            )
        finally:
            if execution_session is not None:
                close_member_session(execution_session)
                execution_session = None
        submit_finished_at = float(now_ts())

        result = _snapshot_direct_result(
            result=execution,
            member=current_member,
            order=order,
            stake=stake,
            template=template,
            intent=intent,
            snapshot_status=SOURCE_ORDER_READY,
        )
        _mark_source_order_execution(result)
        result.update(
            {
                "snapshot_direct_source_intent": source_intent.to_dict(),
                "source_order_direct_intent": intent.to_dict(),
                "submit_started_at": submit_started_at,
                "submit_finished_at": submit_finished_at,
                "submit_elapsed_ms": int(max(0.0, submit_finished_at - submit_started_at) * 1000.0),
                "ft_bet_started_at": submit_started_at,
                "ft_bet_elapsed_ms": int(max(0.0, submit_finished_at - submit_started_at) * 1000.0),
                "bet_submitted_at": submit_started_at,
                "session_generation": selected_session_generation,
                "session_fingerprint": selected_session_fingerprint,
                "session_ready": True,
                "session_ready_reason": "SESSION_READY",
                "bet_context_mode": "disabled",
                "bet_context_request": {},
                "bet_context_http_status": None,
                "bet_context_response": {},
                "bet_context_code": "",
                "bet_context_error": "",
                "context_started_at": 0.0,
                "context_finished_at": 0.0,
                "context_ioratio": "",
                "context_ratio": "",
                "final_submit_ioratio": template.ioratio,
                "final_submit_ratio": template.ratio,
                "ioratio_source": "source_order",
                "params": template.to_dict(),
                "snapshot_direct_actual_line": template.line,
                "snapshot_direct_line_change_allowed": False,
                "snapshot_direct_line_changed": False,
            }
        )
        if isinstance(execution.session_meta, dict):
            result.update(execution.session_meta)
        result.update(execution_session_meta)
        if execution.ft_bet_started_at:
            result["ft_bet_started_at"] = execution.ft_bet_started_at
            result["bet_submitted_at"] = execution.ft_bet_started_at
        if execution.ft_bet_finished_at:
            result["ft_bet_finished_at"] = execution.ft_bet_finished_at
        if execution.ft_bet_elapsed_ms:
            result["ft_bet_elapsed_ms"] = execution.ft_bet_elapsed_ms
            result["submit_elapsed_ms"] = execution.ft_bet_elapsed_ms
        result["timings"] = {
            "ft_bet_s": round(float(result.get("ft_bet_elapsed_ms") or 0) / 1000.0, 3),
            "attempt_wall_s": round(float(result.get("ft_bet_elapsed_ms") or 0) / 1000.0, 3),
        }

        network_error_class = _network_error_class(result)
        result["network_health"] = "degraded" if network_error_class else "healthy"
        result["network_error_class"] = network_error_class
        if execution.status == UNKNOWN_RESULT and network_error_class:
            _record_member_network_issue(
                data_dir=data_dir,
                member_id=str(current_member.get("id") or ""),
                error_class=network_error_class,
            )
        if execution.status == UNKNOWN_RESULT:
            try:
                reconcile_session = _get_member_session(
                    member=current_member,
                    session_pool=session_pool,
                )
                reconciliation = _query_member_recent_wager(
                    session=reconcile_session,
                    base_url=str(current_member.get("base_url") or ""),
                    ver=str(current_member.get("ver") or ""),
                    uid=str(current_member.get("uid") or ""),
                    member_id=str(current_member.get("id") or ""),
                    gid=str(template.gid or ""),
                    stake=float(stake or 0.0),
                    submitted_at=float(result.get("bet_submitted_at") or submit_started_at),
                    timeout_s=min(max(2, int(timeout_s or 0)), 8),
                )
                result["reconcile_status"] = str(reconciliation.get("status") or "")
                if reconciliation.get("status") == "matched":
                    result["ok"] = True
                    result["ticket_id"] = str(reconciliation.get("ticket_id") or "")
                    result["reconciled"] = True
                    result["reconcile_pending"] = False
                    result["message"] = "下注请求结果不确定，已通过注单核验确认成交。"
                elif reconciliation.get("status") == "unavailable":
                    result["reconcile_error"] = str(reconciliation.get("error") or "")
            except (TimeoutError, HardTimeout) as exc:
                result["reconcile_error"] = f"{type(exc).__name__}: {exc}"
                result["reconcile_pending"] = True
            except Exception as exc:
                result["reconcile_error"] = f"{type(exc).__name__}: {exc}"
        elif execution.status == MEMBER_ERROR:
            _invalidate_member_session(
                session_pool=session_pool,
                member_id=str(current_member.get("id") or ""),
                reason=str(execution.error_code or "MEMBER_ERROR"),
            )
        return result
    except (TimeoutError, HardTimeout) as exc:
        if execution_session is not None:
            close_member_session(execution_session)
        if not ft_bet_started:
            return _source_order_waiting_result(
                status=SNAPSHOT_INVALID,
                message=f"源订单直投准备超时：{type(exc).__name__}: {exc}",
                member=member,
                order=order,
                stake=stake,
            )
        return {
            "ts": int(now_ts()),
            "ts_ms": int(now_ts_ms()),
            "live": bool(is_live_backend_order(order)),
            "member_id": str(member.get("id") or ""),
            "member": str(member.get("label") or member.get("username") or member.get("id") or ""),
            "target": str(order.get("target") or ""),
            "orderId": str(order.get("orderId") or ""),
            "stake": float(stake or 0.0),
            "ok": False,
            "execution_mode": SOURCE_ORDER_DIRECT,
            "execution_path": SOURCE_ORDER_DIRECT,
            "real_submission": SOURCE_ORDER_DIRECT,
            "source_order_direct_submission": True,
            "result_status": UNKNOWN_RESULT,
            "snapshot_direct_result_status": UNKNOWN_RESULT,
            "error_code": type(exc).__name__ or "HardTimeout",
            "message": "下注请求超时，结果待核验，暂不更换会员。",
            "reconcile_pending": True,
            "unknown_result": True,
            "bet_context_mode": "disabled",
            "ioratio_source": "source_order",
        }
    except Exception as exc:
        if execution_session is not None:
            close_member_session(execution_session)
        return _source_order_waiting_result(
            status=SNAPSHOT_INVALID,
            message=f"源订单 FT_bet 参数构造失败：{type(exc).__name__}: {exc}",
            member=member,
            order=order,
            stake=stake,
        )


def _place_bet_snapshot_direct(
    *,
    data_dir: Path,
    settings: Dict[str, Any],
    member: Dict[str, Any],
    order: Dict[str, Any],
    stake: float,
    timeout_s: int,
    debug: bool,
    session_pool: Optional["MemberSessionPool"] = None,
) -> Dict[str, Any]:
    """Execute one Snapshot Direct attempt without querying member-side markets."""
    def _attach_http_telemetry_meta(session: Any) -> None:
        try:
            current = getattr(session, "_hdzz_telemetry_meta", {})
            merged = dict(current) if isinstance(current, dict) else {}
            member_id = str(member.get("id") or "")
            merged.update(
                {
                    "order_id": str(order.get("orderId") or order.get("order_id") or order.get("id") or "") or None,
                    "split_id": str(order.get("split_id") or order.get("splitId") or "") or None,
                    "attempt": settings.get("_attempt_no") or settings.get("attempt") or None,
                    "member_hash": hashlib.sha256(member_id.encode("utf-8")).hexdigest() if member_id else None,
                }
            )
            setattr(session, "_hdzz_telemetry_meta", merged)
        except Exception:
            pass
    try:
        selected_session_generation = _safe_int(member.get("session_generation"), 0)
        selected_session_fingerprint = member_session_fingerprint(member)
        source_intent = MarketIntent.from_order(order, split_amount=float(stake or 0.0))
        intent = _snapshot_intent_for_settings(source_intent, order=order, settings=settings)
        if not intent.gid:
            raise ValueError("source order has no gid")
        service = MarketSnapshotService(Path(data_dir) / "odds_snapshot_cache.json")
        context_mode = str(settings.get("bet_context_mode") or "disabled").strip().lower()
        lookup, candidate_max_age_ms = _lookup_snapshot_candidate(
            service,
            intent,
            settings=settings,
            context_mode=context_mode,
        )
        if lookup.snapshot is None or lookup.status != SNAPSHOT_VALID:
            status = str(lookup.status or SNAPSHOT_MISSING)
            return _snapshot_waiting_result(
                status=status,
                message=lookup.message,
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )

        validated = SnapshotValidator().validate(
            intent,
            lookup.snapshot,
            max_age_ms=candidate_max_age_ms,
        )
        if validated.snapshot is None or validated.status != SNAPSHOT_VALID:
            status = str(validated.status or SNAPSHOT_INVALID)
            return _snapshot_waiting_result(
                status=status,
                message=validated.message,
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )

        expected_retry_fingerprint = str(settings.get("_context_retry_fingerprint") or "").strip()
        if bool(settings.get("_context_retry")) and expected_retry_fingerprint and (
            str(validated.snapshot.market_row_fingerprint or "") != expected_retry_fingerprint
        ):
            return _snapshot_waiting_result(
                status=MARKET_ERROR,
                message="Context 重试前盘口 fingerprint 已变化，等待新的 Exact MarketRow。",
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )

        # Do not acquire a member Session while waiting or validating. The
        # executor creates a fresh Session only after the exact row and member
        # quote have both passed their final checks, immediately before FT_bet.
        # Re-read the exact target row before building FinalSubmitParams so a
        # concurrent public-board update cannot combine stale row data.
        final_lookup, final_candidate_max_age_ms = _lookup_snapshot_candidate(
            service,
            intent,
            settings=settings,
            context_mode=context_mode,
        )
        if final_lookup.snapshot is None or final_lookup.status != SNAPSHOT_VALID:
            return _snapshot_waiting_result(
                status=str(final_lookup.status or SNAPSHOT_MISSING),
                message=final_lookup.message,
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )
        final_validated = SnapshotValidator().validate(
            intent,
            final_lookup.snapshot,
            max_age_ms=final_candidate_max_age_ms,
        )
        if final_validated.snapshot is None or final_validated.status != SNAPSHOT_VALID:
            return _snapshot_waiting_result(
                status=str(final_validated.status or SNAPSHOT_INVALID),
                message=final_validated.message,
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )
        validated = final_validated

        candidate_max_age_ms = final_candidate_max_age_ms

        # The execution member is already fixed at this point. Read its exact
        # row-level quote for diagnostics/candidate selection.  In the
        # diagnostic BetContext path it is deliberately not a hard gate: the
        # server's code=501 response supplies the submit-time ioratio.
        quote_max_age_ms = _member_ioratio_max_age_ms(settings)
        quote_lookup = MemberIoratioService(
            Path(data_dir) / "member_ioratio_cache.json"
        ).lookup(
            str(member.get("id") or ""),
            validated.snapshot,
            max_age_ms=quote_max_age_ms,
        )
        if (
            quote_lookup.quote is None or quote_lookup.status != MEMBER_IORATIO_VALID
        ) and context_mode != "diagnostic_order_view":
            priority_request = None
            try:
                priority_request = request_member_ioratio_refresh(
                    data_dir,
                    member_id=str(member.get("id") or ""),
                    market_row=validated.snapshot,
                    mode=str(
                        (validated.snapshot.raw or {}).get("mode")
                        or ("live" if intent.is_live else "today")
                    ),
                )
            except Exception as exc:
                priority_request = {"error": f"{type(exc).__name__}: {exc}"}
            waiting = _snapshot_waiting_result(
                status=str(quote_lookup.status or MEMBER_IORATIO_MISSING),
                message=quote_lookup.message,
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )
            waiting.update(
                {
                    "execution_member": str(member.get("id") or ""),
                    "public_snapshot_revision": int(validated.snapshot.revision or 0),
                    "market_row_gid": validated.snapshot.gid,
                    "market_row_line": validated.snapshot.line,
                    "market_row_con": validated.snapshot.con,
                    "market_row_ratio": validated.snapshot.ratio,
                    "market_row_revision": int(validated.snapshot.revision or 0),
                    "market_row_fingerprint": validated.snapshot.market_row_fingerprint,
                    "member_ioratio_max_age_ms": quote_max_age_ms,
                    "priority_refresh_requested": bool(priority_request and not priority_request.get("error")),
                    "priority_refresh_request": priority_request or {},
                }
            )
            return waiting

        # Quote lookup and final submission must use the same exact MarketRow.
        # A public board update on another thread may replace the row while the
        # member quote request is being read; re-read immediately before the
        # atomic template build and reject a changed fingerprint.
        latest_lookup, latest_candidate_max_age_ms = _lookup_snapshot_candidate(
            service,
            intent,
            settings=settings,
            context_mode=context_mode,
        )
        latest_validated = SnapshotValidator().validate(
            intent,
            latest_lookup.snapshot,
            max_age_ms=latest_candidate_max_age_ms,
        ) if latest_lookup.snapshot is not None and latest_lookup.status == SNAPSHOT_VALID else latest_lookup
        if latest_validated.snapshot is None or latest_validated.status != SNAPSHOT_VALID:
            return _snapshot_waiting_result(
                status=str(latest_validated.status or SNAPSHOT_MISSING),
                message=latest_validated.message,
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )
        if latest_validated.snapshot.market_row_fingerprint != validated.snapshot.market_row_fingerprint:
            try:
                request_member_ioratio_refresh(
                    data_dir,
                    member_id=str(member.get("id") or ""),
                    market_row=latest_validated.snapshot,
                    mode=str((latest_validated.snapshot.raw or {}).get("mode") or ("live" if intent.is_live else "today")),
                )
            except Exception:
                pass
            return _snapshot_waiting_result(
                status=MARKET_ERROR,
                message="提交前盘口行已变化，等待新的会员水位。",
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )
        validated = latest_validated
        quote_lookup = MemberIoratioService(
            Path(data_dir) / "member_ioratio_cache.json"
        ).lookup(
            str(member.get("id") or ""),
            validated.snapshot,
            max_age_ms=quote_max_age_ms,
        )
        if (
            quote_lookup.quote is None or quote_lookup.status != MEMBER_IORATIO_VALID
        ) and context_mode != "diagnostic_order_view":
            try:
                request_member_ioratio_refresh(
                    data_dir,
                    member_id=str(member.get("id") or ""),
                    market_row=validated.snapshot,
                    mode=str((validated.snapshot.raw or {}).get("mode") or ("live" if intent.is_live else "today")),
                )
            except Exception:
                pass
            return _snapshot_waiting_result(
                status=str(quote_lookup.status or MEMBER_IORATIO_MISSING),
                message=quote_lookup.message,
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )

        template = None
        if context_mode != "diagnostic_order_view":
            template = build_submit_template(
                validated.snapshot,
                quote_lookup.quote,
                execution_member=str(member.get("id") or ""),
            )
            odds_skipped, odds_message, odds_meta = evaluate_follow_odds_limit(
                settings=settings,
                ioratio=template.ioratio,
            )
            if odds_skipped:
                result = _snapshot_direct_result(
                    result=ExecutionResult(
                        "SKIPPED",
                        str(member.get("id") or ""),
                        error_code="ODDS_LIMIT",
                        error_message=odds_message,
                    ),
                    member=member,
                    order=order,
                    stake=stake,
                    template=template,
                    intent=intent,
                    snapshot_status=SNAPSHOT_VALID,
                )
                result["skipped"] = True
                result["skip_reason"] = "odds_limit"
                result["odds_limit"] = odds_meta
                result["snapshot_direct_source_intent"] = source_intent.to_dict()
                result["snapshot_direct_actual_line"] = validated.snapshot.line
                return result
        submit_started_at = float(now_ts())
        context_meta: Dict[str, Any] = {
            "bet_context_mode": context_mode,
            "bet_context_request": {},
            "bet_context_http_status": None,
            "bet_context_raw_response": "",
            "bet_context_response": {},
            "bet_context_code": "",
            "bet_context_fields": {},
            "bet_context_request_binding": {},
            "bet_context_optional_fields": {},
            "bet_context_ioratio_match": False,
            "bet_context_spread": "",
            "bet_context_elapsed_ms": 0,
            "bet_context_error": "",
            "context_started_at": 0.0,
            "context_finished_at": 0.0,
            "member_cached_ioratio": str(getattr(quote_lookup.quote, "ioratio", "") or ""),
            "member_cached_ioratio_age_ms": int(getattr(quote_lookup.quote, "age_ms", 0) or 0),
            "context_ioratio": "",
            "context_ratio": "",
            "final_submit_ioratio": "",
            "final_submit_ratio": "",
            "ioratio_source": "member_ioratio_cache" if context_mode != "diagnostic_order_view" else "ft_order_view",
            "market_row_fingerprint_before": validated.snapshot.market_row_fingerprint,
            "market_row_fingerprint_after": "",
            "market_row_revision_before": int(validated.snapshot.revision or 0),
            "market_row_revision_after": 0,
            "snapshot_preferred_age_ms": _snapshot_preferred_age_ms(settings),
            "snapshot_structure_max_age_ms": _snapshot_structure_max_age_ms(settings, context_mode=context_mode),
            "snapshot_candidate_age_ms": int(candidate_max_age_ms),
        }
        execution_session = None
        execution_session_meta: Dict[str, Any] = {}
        if context_mode == "diagnostic_order_view":
            # The diagnostic context and FT_bet intentionally share this one
            # fresh Session.  No context request is made while waiting for a
            # Snapshot or member quote; this point is after all final checks.
            session_created_at = float(now_ts())
            try:
                execution_session = make_member_session(
                    base_url=str(member.get("base_url") or ""),
                    proxy=str(member.get("proxy") or ""),
                    cookie=str(member.get("cookie") or ""),
                )
                _attach_http_telemetry_meta(execution_session)
                execution_session_meta = {
                    "session_mode": "fresh_per_attempt",
                    "session_created_at": session_created_at,
                    "session_object_id": str(id(execution_session)),
                    "proxy": "configured" if str(member.get("proxy") or "").strip() else "",
                    "proxy_enabled": bool(str(member.get("proxy") or "").strip()),
                    **_session_cookie_diagnostics(execution_session),
                }
                # Keep the submit sequence identical to the proven path:
                # FT_order_view -> FT_bet on one fresh Session. Extra menu
                # requests can trigger the platform edge's 502 Blocked page.
                context_meta["context_started_at"] = float(now_ts())
                context = BetContext.prepare(
                    session=execution_session,
                    member=member,
                    market_row=validated.snapshot,
                    expected_ioratio=None,
                    timeout_s=max(2, int(timeout_s or 0)),
                )
                context_meta["context_finished_at"] = float(now_ts())
                context_meta.update(
                    {
                        "bet_context_request": dict(context.request or {}),
                        "bet_context_http_status": context.http_status,
                        "bet_context_raw_response": str(context.raw_response or ""),
                        "bet_context_response": dict(context.response or {}),
                        "bet_context_code": str((context.response or {}).get("code") or ""),
                        "bet_context_fields": dict((context.response or {}).get("context_observed") or {}),
                        "bet_context_request_binding": dict((context.response or {}).get("request_binding") or {}),
                        "bet_context_optional_fields": dict((context.response or {}).get("context_optional_fields") or {}),
                        "bet_context_ioratio_match": bool((context.response or {}).get("context_ioratio_match")),
                        "member_cached_ioratio": str((context.response or {}).get("cached_ioratio") or getattr(quote_lookup.quote, "ioratio", "") or ""),
                        "member_cached_ioratio_age_ms": int(getattr(quote_lookup.quote, "age_ms", 0) or 0),
                        "context_ioratio": str((context.response or {}).get("context_ioratio") or ((context.response or {}).get("context_observed") or {}).get("ioratio") or ""),
                        "context_ratio": str(((context.response or {}).get("context_observed") or {}).get("ratio") or (context.response or {}).get("ratio") or ""),
                        "bet_context_spread": str((context.response or {}).get("context_spread") or ""),
                        "bet_context_elapsed_ms": int(context.elapsed_ms or 0),
                        "bet_context_error": str(context.error or ""),
                    }
                )
                if context.error or not context.matches_market_row:
                    status = (
                        BET_CONTEXT_MISMATCH
                        if context.error == BET_CONTEXT_MISMATCH
                        else CONTEXT_RETRYABLE
                        if context.error == CONTEXT_RETRYABLE
                        else "BET_CONTEXT_FAILED"
                    )
                    context_network_error = _context_network_error_class(
                        http_status=context.http_status,
                        error=context.error,
                    )
                    if status == CONTEXT_RETRYABLE and context_network_error:
                        # FT_order_view 尚未提交下注；记录错误后立即关闭本次
                        # Session，调度器会用新的 Session 快速重试。
                        _record_member_network_issue(
                            data_dir=Path(data_dir),
                            member_id=str(member.get("id") or ""),
                            error_class=context_network_error,
                        )
                    waiting = _snapshot_waiting_result(
                        status=status,
                        message=(
                            "FT_order_view 返回参数与 Snapshot MarketRow 不一致，禁止 FT_bet。"
                            if status == BET_CONTEXT_MISMATCH
                            else f"BetContext.prepare 失败：{context.error or 'unknown'}"
                        ),
                        member=member,
                        order=order,
                        stake=stake,
                        intent=intent,
                    )
                    waiting.update(context_meta)
                    if context_network_error:
                        waiting["network_health"] = "degraded"
                        waiting["network_error_class"] = context_network_error
                    waiting.update(execution_session_meta)
                    close_member_session(execution_session)
                    execution_session = None
                    return waiting
            except BaseException as exc:
                context_meta["context_finished_at"] = float(now_ts())
                context_meta["bet_context_error"] = f"{type(exc).__name__}: {exc}"
                context_error_text = f"{type(exc).__name__}: {exc}"
                lowered_context_error = context_error_text.lower()
                context_retryable = any(
                    token in lowered_context_error
                    for token in (
                        "connectionerror",
                        "remote end closed",
                        "remotedisconnected",
                        "readtimeout",
                        "read timed out",
                        "connection aborted",
                        "connection reset",
                        "timeout",
                    )
                )
                context_network_error = _context_network_error_class(
                    http_status=context_meta.get("bet_context_http_status"),
                    error=context_meta.get("bet_context_error"),
                )
                if context_retryable and context_network_error:
                    _record_member_network_issue(
                        data_dir=Path(data_dir),
                        member_id=str(member.get("id") or ""),
                        error_class=context_network_error,
                    )
                waiting = _snapshot_waiting_result(
                    status=CONTEXT_RETRYABLE if context_retryable else "BET_CONTEXT_FAILED",
                    message=f"BetContext.prepare 失败：{context_meta['bet_context_error']}",
                    member=member,
                    order=order,
                    stake=stake,
                    intent=intent,
                )
                waiting.update(context_meta)
                if context_network_error:
                    waiting["network_health"] = "degraded"
                    waiting["network_error_class"] = context_network_error
                waiting.update(execution_session_meta)
                close_member_session(execution_session)
                execution_session = None
                return waiting
        if context_mode == "diagnostic_order_view":
            # Context is the submit-time quote source.  Re-read the exact
            # public row after it returns and refuse to combine a changed
            # structure with the context quote.
            context_ioratio = str(context_meta.get("context_ioratio") or "").strip()
            # Context is the submit-time confirmation.  Read the current exact
            # cached row without applying the preferred-age rejection a second
            # time; a missing row or a changed fingerprint remains a hard stop.
            latest_lookup = service.lookup(intent, max_age_ms=None)
            latest_validated = SnapshotValidator().validate(
                intent,
                latest_lookup.snapshot,
                max_age_ms=None,
            ) if latest_lookup.snapshot is not None and latest_lookup.status == SNAPSHOT_VALID else latest_lookup
            if latest_validated.snapshot is None or latest_validated.status != SNAPSHOT_VALID:
                waiting = _snapshot_waiting_result(
                    status=str(latest_validated.status or SNAPSHOT_MISSING),
                    message=latest_validated.message,
                    member=member,
                    order=order,
                    stake=stake,
                    intent=intent,
                )
                waiting.update(context_meta)
                waiting.update(execution_session_meta)
                close_member_session(execution_session)
                execution_session = None
                return waiting
            context_meta["market_row_fingerprint_after"] = latest_validated.snapshot.market_row_fingerprint
            context_meta["market_row_revision_after"] = int(latest_validated.snapshot.revision or 0)
            if latest_validated.snapshot.market_row_fingerprint != validated.snapshot.market_row_fingerprint:
                waiting = _snapshot_waiting_result(
                    status=MARKET_CHANGED_DURING_CONTEXT,
                    message="BetContext 返回后盘口行已变化，禁止 FT_bet。",
                    member=member,
                    order=order,
                    stake=stake,
                    intent=intent,
                )
                waiting.update(context_meta)
                waiting.update(execution_session_meta)
                close_member_session(execution_session)
                execution_session = None
                return waiting
            validated = latest_validated
            try:
                template = build_submit_template(
                    validated.snapshot,
                    # In diagnostic BetContext mode the cache quote is only
                    # candidate/diagnostic data.  The context response is
                    # the submit-time quote, so a stale quote from a prior
                    # MarketRow must not become a final-submit hard gate.
                    None,
                    execution_member=str(member.get("id") or ""),
                    ioratio_override=context_ioratio,
                    ratio_override=(
                        str(context_meta.get("context_ratio") or "").strip()
                        if str(validated.snapshot.market_type or "").upper() == "MONEYLINE"
                        else None
                    ),
                    ioratio_source="ft_order_view",
                )
            except ValueError as exc:
                waiting = _snapshot_waiting_result(
                    status=BET_CONTEXT_MISMATCH,
                    message=f"BetContext ioratio 无效，禁止 FT_bet：{exc}",
                    member=member,
                    order=order,
                    stake=stake,
                    intent=intent,
                )
                waiting.update(context_meta)
                waiting.update(execution_session_meta)
                close_member_session(execution_session)
                execution_session = None
                return waiting
            context_meta["final_submit_ioratio"] = template.ioratio
            context_meta["final_submit_ratio"] = template.ratio
            odds_skipped, odds_message, odds_meta = evaluate_follow_odds_limit(
                settings=settings,
                ioratio=template.ioratio,
            )
            if odds_skipped:
                result = _snapshot_direct_result(
                    result=ExecutionResult("SKIPPED", str(member.get("id") or ""), error_code="ODDS_LIMIT", error_message=odds_message),
                    member=member,
                    order=order,
                    stake=stake,
                    template=template,
                    intent=intent,
                    snapshot_status=SNAPSHOT_VALID,
                )
                result.update(context_meta)
                result.update(execution_session_meta)
                result["odds_limit"] = odds_meta
                close_member_session(execution_session)
                execution_session = None
                return result
        # The normal direct path also uses a fresh Session, without adding
        # menu requests immediately before FT_bet.
        if execution_session is None:
            session_created_at = float(now_ts())
            execution_session = make_member_session(
                base_url=str(member.get("base_url") or ""),
                proxy=str(member.get("proxy") or ""),
                cookie=str(member.get("cookie") or ""),
            )
            _attach_http_telemetry_meta(execution_session)
            execution_session_meta = {
                "session_mode": "fresh_per_attempt",
                "session_created_at": session_created_at,
                "session_object_id": str(id(execution_session)),
                "proxy": "configured" if str(member.get("proxy") or "").strip() else "",
                "proxy_enabled": bool(str(member.get("proxy") or "").strip()),
                **_session_cookie_diagnostics(execution_session),
            }

        # Final freshness gate: queueing/member acquisition/context work must
        # never turn an old MarketRow into a new FT_bet submission.
        final_freshness_lookup = service.lookup(
            intent,
            max_age_ms=_snapshot_preferred_age_ms(settings),
        )
        if final_freshness_lookup.snapshot is None or final_freshness_lookup.status != SNAPSHOT_VALID:
            if execution_session is not None:
                close_member_session(execution_session)
                execution_session = None
            return _snapshot_waiting_result(
                status=str(final_freshness_lookup.status or SNAPSHOT_STALE),
                message=(
                    "提交前盘口已超过允许 freshness，等待最新盘口。"
                    if final_freshness_lookup.status == SNAPSHOT_STALE
                    else final_freshness_lookup.message
                ),
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )
        final_freshness_validated = SnapshotValidator().validate(
            intent,
            final_freshness_lookup.snapshot,
            max_age_ms=_snapshot_preferred_age_ms(settings),
        )
        if final_freshness_validated.snapshot is None or final_freshness_validated.status != SNAPSHOT_VALID:
            if execution_session is not None:
                close_member_session(execution_session)
                execution_session = None
            return _snapshot_waiting_result(
                status=str(final_freshness_validated.status or SNAPSHOT_STALE),
                message=final_freshness_validated.message,
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )
        if final_freshness_validated.snapshot.market_row_fingerprint != validated.snapshot.market_row_fingerprint:
            if execution_session is not None:
                close_member_session(execution_session)
                execution_session = None
            return _snapshot_waiting_result(
                status=MARKET_ERROR,
                message="提交前盘口 fingerprint 已变化，禁止使用旧 BetContext。",
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )
        validated = final_freshness_validated

        session_ok, session_reason, current_member = _final_session_gate(
            data_dir=data_dir,
            member=member,
            selected_generation=selected_session_generation,
            selected_fingerprint=selected_session_fingerprint,
        )
        if not session_ok:
            if execution_session is not None:
                close_member_session(execution_session)
                execution_session = None
            waiting = _snapshot_waiting_result(
                status=session_reason,
                message=f"提交前 Session gate 拒绝旧上下文：{session_reason}",
                member=member,
                order=order,
                stake=stake,
                intent=intent,
            )
            waiting.update(
                {
                    "session_ready": False,
                    "session_ready_reason": session_reason,
                    "session_generation": selected_session_generation,
                    "session_fingerprint": selected_session_fingerprint,
                    "execution_member": str(member.get("id") or ""),
                }
            )
            return waiting
        # Use only the current identity metadata for telemetry. The actual
        # payload is still rebuilt from the validated current snapshot below.
        selected_session_generation = _safe_int(current_member.get("session_generation"), selected_session_generation)
        selected_session_fingerprint = member_session_fingerprint(current_member)

        try:
            submit_kwargs = {
                "timeout_s": max(2, int(timeout_s or 0)),
            }
            # Both diagnostic and normal direct paths submit through the same
            # fresh session that passed the final identity gate.
            submit_kwargs["session"] = execution_session
            execution = MemberExecutor().submit(
                member,
                template,
                float(stake or 0.0),
                **submit_kwargs,
            )
        except (TimeoutError, HardTimeout) as exc:
            # member_post_session 的硬超时继承自 BaseException，不能被
            # MemberExecutor 的普通 Exception 分支吞掉；在这里转换成
            # 可核验的 UNKNOWN_RESULT，避免调度器马上换号重复下注。
            execution = ExecutionResult(
                UNKNOWN_RESULT,
                str(member.get("id") or ""),
                error_code=type(exc).__name__ or "HardTimeout",
                error_message="下注请求超时，结果待核验。",
            )
        finally:
            if execution_session is not None:
                close_member_session(execution_session)
        submit_finished_at = float(now_ts())
        result = _snapshot_direct_result(
            result=execution,
            member=member,
            order=order,
            stake=stake,
            template=template,
            intent=intent,
            snapshot_status=SNAPSHOT_VALID,
        )
        result["member_ioratio_max_age_ms"] = quote_max_age_ms
        result["market_row_fingerprint"] = validated.snapshot.market_row_fingerprint
        result["snapshot_direct_source_intent"] = source_intent.to_dict()
        result["submit_started_at"] = submit_started_at
        result["submit_finished_at"] = submit_finished_at
        result["submit_elapsed_ms"] = int(max(0.0, submit_finished_at - submit_started_at) * 1000.0)
        result["ft_bet_started_at"] = submit_started_at
        result["ft_bet_elapsed_ms"] = result["submit_elapsed_ms"]
        result["bet_submitted_at"] = submit_started_at
        if isinstance(execution.session_meta, dict):
            result.update(execution.session_meta)
            result["session_mode"] = str(execution.session_meta.get("session_mode") or "")
        result["session_generation"] = selected_session_generation
        result["session_fingerprint"] = selected_session_fingerprint
        result["session_ready"] = True
        result["session_ready_reason"] = "SESSION_READY"
        network_error_class = _network_error_class(result)
        result["network_health"] = "degraded" if network_error_class else "healthy"
        result["network_error_class"] = network_error_class
        if execution_session_meta:
            result.update(execution_session_meta)
        if execution.status == UNKNOWN_RESULT and network_error_class:
            _record_member_network_issue(
                data_dir=data_dir,
                member_id=str(member.get("id") or ""),
                error_class=network_error_class,
            )
        if context_mode == "diagnostic_order_view":
            result.update(context_meta)
            result.update(execution_session_meta)
            result["bet_context_session_object_id"] = execution_session_meta.get("session_object_id", "")
        if execution.ft_bet_started_at:
            result["ft_bet_started_at"] = execution.ft_bet_started_at
        if execution.ft_bet_finished_at:
            result["ft_bet_finished_at"] = execution.ft_bet_finished_at
        if execution.ft_bet_elapsed_ms:
            result["ft_bet_elapsed_ms"] = execution.ft_bet_elapsed_ms
            result["submit_elapsed_ms"] = execution.ft_bet_elapsed_ms
        result["params"] = template.to_dict()
        result["timings"] = {
            "ft_bet_s": round(float(result["ft_bet_elapsed_ms"]) / 1000.0, 3),
            "attempt_wall_s": round(float(result["ft_bet_elapsed_ms"]) / 1000.0, 3),
        }
        result["snapshot_direct_actual_line"] = validated.snapshot.line
        result["snapshot_direct_line_change_allowed"] = False
        result["snapshot_direct_line_changed"] = bool(
            intent.target_line is not None
            and validated.snapshot.line is not None
            and not line_matches(
                intent.target_line,
                validated.snapshot.line,
                market_type=intent.market_type,
                period=intent.period,
                is_corner=intent.is_corner,
            )
        )
        if execution.status == UNKNOWN_RESULT:
            try:
                # Reconciliation is a post-submit read only path. It may use
                # the pool, but this Session is never passed back to FT_bet.
                reconcile_session = _get_member_session(member=member, session_pool=session_pool)
                reconciliation = _query_member_recent_wager(
                    session=reconcile_session,
                    base_url=str(member.get("base_url") or ""),
                    ver=str(member.get("ver") or ""),
                    uid=str(member.get("uid") or ""),
                    member_id=str(member.get("id") or ""),
                    gid=str(template.gid or ""),
                    stake=float(stake or 0.0),
                    submitted_at=submit_started_at,
                    timeout_s=min(max(2, int(timeout_s or 0)), 8),
                )
                result["reconcile_status"] = str(reconciliation.get("status") or "")
                if reconciliation.get("status") == "matched":
                    result["ok"] = True
                    result["ticket_id"] = str(reconciliation.get("ticket_id") or "")
                    result["reconciled"] = True
                    result["reconcile_pending"] = False
                    result["message"] = "下注请求结果不确定，已通过注单核验确认成交。"
                elif reconciliation.get("status") == "unavailable":
                    result["reconcile_error"] = str(reconciliation.get("error") or "")
            except (TimeoutError, HardTimeout) as exc:
                result["reconcile_error"] = f"{type(exc).__name__}: {exc}"
                result["reconcile_pending"] = True
            except Exception as exc:
                result["reconcile_error"] = f"{type(exc).__name__}: {exc}"
        elif execution.status == MEMBER_ERROR:
            _invalidate_member_session(
                session_pool=session_pool,
                member_id=str(member.get("id") or ""),
                reason=str(execution.error_code or "MEMBER_ERROR"),
            )
        return result
    except (TimeoutError, HardTimeout) as exc:
        return {
            "ts": int(now_ts()),
            "ts_ms": int(now_ts_ms()),
            "live": bool(is_live_backend_order(order)),
            "member_id": str(member.get("id") or ""),
            "member": str(member.get("label") or member.get("username") or member.get("id") or ""),
            "target": str(order.get("target") or ""),
            "orderId": str(order.get("orderId") or ""),
            "stake": float(stake or 0.0),
            "ok": False,
            "execution_mode": "snapshot_direct",
            "snapshot_direct_status": "VALID",
            "snapshot_direct_result_status": UNKNOWN_RESULT,
            "error_code": type(exc).__name__ or "HardTimeout",
            "error_type": type(exc).__name__ or "HardTimeout",
            "exception": repr(exc),
            "message": "下注请求超时，结果待核验，暂不更换会员。",
            "reconcile_pending": True,
            "unknown_result": True,
        }
    except Exception as exc:
        # 到达 FT_bet 之前的本地构建异常不可能产生成交，按盘口准备失败等待，
        # 不能误入 UNKNOWN_RESULT 核验，也不能切换到其它执行链。
        return _snapshot_waiting_result(
            status=SNAPSHOT_INVALID,
            message=f"{type(exc).__name__}: {exc}",
            member=member,
            order=order,
            stake=stake,
        )


def place_bet(
    *,
    data_dir: Path,
    settings: Dict[str, Any],
    member: Dict[str, Any],
    order: Dict[str, Any],
    stake: float,
    timeout_s: int,
    debug: bool,
    session_pool: Optional["MemberSessionPool"] = None,
) -> Dict[str, Any]:
    """执行配置的生产链；UNKNOWN 后只允许核验。"""
    # UNKNOWN_RESULT 的后续尝试只能 reconcile，不能再次触发 FT_bet。
    if bool(settings.get("_reconcile_only")):
        return _reconcile_only_result(
            data_dir=data_dir,
            settings=settings,
            member=member,
            order=order,
            stake=stake,
            timeout_s=timeout_s,
            session_pool=session_pool,
        )
    execution_mode = str(settings.get("execution_mode") or "snapshot_direct").strip().lower()
    if execution_mode == SOURCE_ORDER_DIRECT:
        return _place_bet_source_order_direct(
            data_dir=data_dir,
            settings=settings,
            member=member,
            order=order,
            stake=stake,
            timeout_s=timeout_s,
            debug=debug,
            session_pool=session_pool,
        )
    return _place_bet_snapshot_direct(
        data_dir=data_dir,
        settings=settings,
        member=member,
        order=order,
        stake=stake,
        timeout_s=timeout_s,
        debug=debug,
        session_pool=session_pool,
    )


def init_offsets(orders_dir: Path) -> Dict[str, int]:
    offsets: Dict[str, int] = {}
    for p in orders_dir.glob("*.jsonl"):
        try:
            offsets[str(p)] = p.stat().st_size
        except Exception:
            offsets[str(p)] = 0
    return offsets


def load_offsets_from_state(state_path: Path, orders_dir: Path) -> Dict[str, int]:
    """
    读取上次运行写入的 offsets，避免重启后漏读“停机期间追加的订单”。
    若 state 不存在或不合法，则回退到 init_offsets（默认从文件末尾开始）。
    """
    try:
        raw = read_json_file(state_path, {})
    except Exception:
        return init_offsets(orders_dir)

    raw_offsets = raw.get("offsets") if isinstance(raw, dict) else None
    if not isinstance(raw_offsets, dict):
        return init_offsets(orders_dir)

    out: Dict[str, int] = {}
    for p in orders_dir.glob("*.jsonl"):
        pkey = str(p)
        offset = None
        try:
            cur_size = int(p.stat().st_size)
        except Exception:
            cur_size = 0
        if pkey in raw_offsets:
            offset = raw_offsets.get(pkey)
        else:
            # 兼容：state 里可能是绝对路径/不同分隔符，按文件名兜底匹配
            for k, v in raw_offsets.items():
                try:
                    if Path(str(k)).name == p.name:
                        offset = v
                        break
                except Exception:
                    continue
        try:
            if offset is None:
                out[pkey] = cur_size
            else:
                off = max(0, int(offset))
                # 文件被截断/重建时：从头读（done 会去重，避免重复跟单）
                if off > cur_size:
                    off = 0
                out[pkey] = off
        except Exception:
            out[pkey] = 0
    return out


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def retry_job_to_dict(job: RetryJob) -> Dict[str, Any]:
    return scheduler_retry_job_to_dict(job)


def retry_job_from_dict(d: Dict[str, Any]) -> Optional[RetryJob]:
    return scheduler_retry_job_from_dict(d, now_value=float(now_ts()))


def load_follow_state(
    state_path: Path, orders_dir: Path
) -> Tuple[Dict[str, int], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, RetryJob]]:
    raw = read_json_file(state_path, {})
    offsets = load_offsets_from_state(state_path, orders_dir)

    done: Dict[str, Dict[str, Any]] = {}
    pending_orders: Dict[str, Dict[str, Any]] = {}
    jobs: Dict[str, RetryJob] = {}
    if isinstance(raw, dict):
        raw_done = raw.get("done")
        if isinstance(raw_done, dict):
            for k, v in raw_done.items():
                if isinstance(v, dict):
                    done[str(k)] = v

        raw_pending = raw.get("pending_orders")
        if isinstance(raw_pending, dict):
            for k, v in raw_pending.items():
                if isinstance(v, dict):
                    pending_orders[str(k)] = v

        raw_jobs = raw.get("jobs")
        if isinstance(raw_jobs, dict):
            for k, v in raw_jobs.items():
                if isinstance(v, dict):
                    j = retry_job_from_dict(v)
                    if j:
                        jobs[j.key] = j
        elif isinstance(raw_jobs, list):
            for v in raw_jobs:
                if isinstance(v, dict):
                    j = retry_job_from_dict(v)
                    if j:
                        jobs[j.key] = j

    # 清理：已经 done 的订单，不应继续留在 pending/jobs（避免 state 文件膨胀与误解）
    for k in list(pending_orders.keys()):
        if k in done:
            pending_orders.pop(k, None)
    for k in list(jobs.keys()):
        if k in done:
            jobs.pop(k, None)

    return offsets, done, pending_orders, jobs


def hydrate_done_from_results(done: Dict[str, Dict[str, Any]], results_path: Path) -> None:
    """
    兼容旧版本 state 未保存 done 的情况：从 follow_results.jsonl 回填“已成功”的订单，
    避免重启时把历史成功单再跟一遍。
    """
    try:
        if done or (not results_path.exists()):
            return
    except Exception:
        return
    split_successes: Dict[str, Dict[int, Dict[str, Any]]] = {}
    split_totals: Dict[str, int] = {}
    try:
        with results_path.open("r", encoding="utf-8-sig") as f:
            for line in f:
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("ok") is not True:
                    continue
                target = str(obj.get("target") or "").strip()
                order_id = str(obj.get("orderId") or "").strip()
                if not (target and order_id):
                    continue
                key = f"{target}|{order_id}"
                if key in done:
                    continue
                split_count = max(1, _safe_int(obj.get("split_count"), 1))
                if split_count > 1:
                    split_index = _safe_int(obj.get("split_index"), 0)
                    ticket_id = str(obj.get("ticket_id") or "").strip()
                    if 1 <= split_index <= split_count and ticket_id:
                        split_totals[key] = max(split_count, split_totals.get(key, 0))
                        split_successes.setdefault(key, {})[split_index] = obj
                        done[split_task_key(key, split_index - 1, split_count)] = {
                            "status": "ok",
                            "ticket_id": ticket_id,
                            "ts": _safe_int(obj.get("ts"), int(now_ts())),
                            "split_index": split_index - 1,
                            "stake": _safe_float(obj.get("stake"), 0.0),
                            "member": str(obj.get("member") or ""),
                        }
                    continue
                done[key] = {
                    "status": "ok",
                    "ticket_id": str(obj.get("ticket_id") or ""),
                    "ts": _safe_int(obj.get("ts"), int(now_ts())),
                }
    except Exception:
        return

    for key, total in split_totals.items():
        parts = split_successes.get(key) or {}
        if any(index not in parts for index in range(1, total + 1)):
            continue
        ordered = [parts[index] for index in range(1, total + 1)]
        tickets = [str(item.get("ticket_id") or "").strip() for item in ordered]
        done[key] = {
            "status": "ok",
            "ticket_id": tickets[-1],
            "tickets": tickets,
            "ts": max(_safe_int(item.get("ts"), 0) for item in ordered),
            "split_done": total,
            "split_total": total,
        }
        for index in range(total):
            done.pop(split_task_key(key, index, total), None)


def collect_follow_generated_order_keys(
    *,
    done: Dict[str, Dict[str, Any]],
    results_path: Path,
) -> set[str]:
    """
    收集“系统自己跟出来的票据”对应的后台订单键（target|ticket_id）。
    这些票据如果再次出现在 orders/*.jsonl，说明是跟单成功后的订单回流，必须跳过，避免自我递归跟单。
    """
    out: set[str] = set()

    try:
        for key, meta in (done or {}).items():
            target = _target_from_cache_key(str(key or ""))
            if not target or not isinstance(meta, dict):
                continue
            tid = str(meta.get("ticket_id") or "").strip()
            if tid:
                out.add(f"{target}|{tid}")
            tickets = meta.get("tickets")
            if isinstance(tickets, list):
                for item in tickets:
                    tid2 = str(item or "").strip()
                    if tid2:
                        out.add(f"{target}|{tid2}")
    except Exception:
        pass

    # Older state files may not contain the completed-order map.  Preserve
    # the same self-follow protection by recovering ticket ids from results.
    try:
        if results_path.exists():
            with results_path.open("r", encoding="utf-8-sig") as handle:
                for line in handle:
                    line = (line or "").strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        continue
                    target = str(item.get("target") or "").strip()
                    ticket_id = str(item.get("ticket_id") or "").strip()
                    if target and ticket_id:
                        out.add(f"{target}|{ticket_id}")
    except Exception:
        pass

    return out


def _job_active_market_key(job: RetryJob, settings: Dict[str, Any]) -> str:
    """Match the watcher key without importing the watcher module."""
    try:
        source = MarketIntent.from_order(job.order, split_amount=float(job.stake or 0.0))
        intent = _snapshot_intent_for_settings(source, order=job.order, settings=settings)
        fields = intent_market_fields(intent)
        target_line = fields.get("target_line")
        line_text = "" if target_line is None else str(target_line)
        return "|".join(
            (
                "live" if intent.is_live else "today",
                str(intent.gid or ""),
                str(intent.period or "").upper(),
                str(intent.market_type or "").upper(),
                str(intent.side or "").upper(),
                line_text,
                str(fields.get("wtype") or "").upper(),
                "CORNER" if bool(fields.get("is_corner")) else "STANDARD",
            )
        )
    except Exception:
        return ""


def _consume_snapshot_publish_wakeup(
    job: RetryJob,
    *,
    settings: Dict[str, Any],
    latest_events: Dict[str, Dict[str, Any]],
    now_value: float,
) -> bool:
    """Consume one newer publish event for a job waiting on market data.

    The latest event is retained independently of waiter registration.  This
    makes the publish-before-waiter race explicit and gives the scheduler a
    single, idempotent transition for ``event_seq`` consumption.
    """
    if not isinstance(job, RetryJob):
        return False
    reject_retry_not_before = _safe_float(job.order.get("_market_reject_retry_not_before"), 0.0)
    if reject_retry_not_before > float(now_value):
        return False
    if str(job.last_err or "").upper() not in {
        SNAPSHOT_MISSING,
        SNAPSHOT_STALE,
        TARGET_LINE_MISSING,
        SNAPSHOT_INVALID,
        MARKET_ERROR,
    }:
        return False
    market_key = _job_active_market_key(job, settings)
    latest_event = latest_events.get(market_key)
    if not isinstance(latest_event, dict):
        return False
    event_seq = _safe_int(latest_event.get("event_seq"), 0)
    if event_seq <= int(job.last_seen_event_seq or 0):
        return False
    wake_at = float(now_value)
    job.last_seen_event_seq = event_seq
    job.next_try_at = min(float(job.next_try_at or wake_at), wake_at)
    job.trigger_reason = "SNAPSHOT_EVENT"
    job.trigger_event_seq = event_seq
    job.trigger_publish_at = float(latest_event.get("publish_at") or wake_at)
    job.trigger_wakeup_at = wake_at
    job.job_marked_runnable_at = wake_at
    return True


def _read_snapshot_publish_events(path: Path, offset: int) -> Tuple[int, List[Dict[str, Any]]]:
    """Read complete new publish lines, retaining a partial trailing line."""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            handle.seek(max(0, int(offset or 0)))
            data = handle.read()
            new_offset = handle.tell()
    except (FileNotFoundError, OSError):
        return 0, []
    if not data:
        return new_offset, []
    lines = data.splitlines(keepends=True)
    complete: List[str] = []
    consumed = 0
    for line in lines:
        if not line.endswith("\n"):
            break
        complete.append(line)
        consumed += len(line)
    parsed: List[Dict[str, Any]] = []
    for line in complete:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                parsed.append(item)
        except Exception:
            continue
    return int(offset or 0) + consumed, parsed


def _trim_state_maps(
    *,
    done: Dict[str, Dict[str, Any]],
    pending_orders: Dict[str, Dict[str, Any]],
    jobs: Dict[str, RetryJob],
    max_done: int = 8000,
    max_pending: int = 3000,
    max_jobs: int = 2000,
) -> None:
    if len(done) > max_done:
        items = sorted(done.items(), key=lambda kv: _safe_int((kv[1] or {}).get("ts"), 0), reverse=True)
        done.clear()
        done.update(dict(items[:max_done]))
    if len(pending_orders) > max_pending:
        items2 = sorted(
            pending_orders.items(), key=lambda kv: _safe_int(((kv[1] or {}).get("ts")), 0), reverse=True
        )
        pending_orders.clear()
        pending_orders.update(dict(items2[:max_pending]))
    if len(jobs) > max_jobs:
        items3 = sorted(jobs.items(), key=lambda kv: float(getattr(kv[1], "created_at", 0.0)), reverse=True)
        keep_keys = set([k for k, _ in items3[:max_jobs]])
        for k in list(jobs.keys()):
            if k not in keep_keys:
                jobs.pop(k, None)


def _run_parallel_place_bet(
    *,
    data_dir: Path,
    settings: Dict[str, Any],
    member: Dict[str, Any],
    order: Dict[str, Any],
    stake: float,
    timeout_s: int,
    debug: bool,
    session_pool: Optional["MemberSessionPool"],
    hard_timeout_s: int,
) -> Dict[str, Any]:
    """Worker-side one-shot attempt. Shared state is finalized by the main thread."""
    member_id = str(member.get("id") or "")
    execution_mode = str(settings.get("execution_mode") or "snapshot_direct").strip().lower()
    fallback = {
        "ts": int(now_ts()),
        "ts_ms": int(now_ts_ms()),
        "live": bool(is_live_backend_order(order)),
        "member_id": member_id,
        "member": str(member.get("label") or member.get("username") or member_id),
        "target": str(order.get("target") or ""),
        "orderId": str(order.get("orderId") or ""),
        "order_id": str(order.get("orderId") or ""),
        "stake": float(stake),
        "source_stake": _safe_float(order.get("stake"), 0.0),
        "ok": False,
        "message": "FT_bet 结果不确定，正在核验，禁止重复提交。",
        "execution_mode": execution_mode,
        "execution_path": execution_mode,
        "real_submission": execution_mode,
        # 硬超时无法判断请求是否已经到达 FT_bet；必须先核验，不能把
        # 它当成普通会员失败立即切换会员重下。
        "snapshot_direct_result_status": UNKNOWN_RESULT,
        "error_code": UNKNOWN_RESULT,
        "error_type": "HardTimeout",
        "reconcile_pending": True,
        "unknown_result": True,
        "result_status": UNKNOWN_RESULT,
        "bet_submitted_at": float(now_ts()),
        "timings": {},
    }
    started = time.monotonic()
    try:
        with _hard_timeout(int(hard_timeout_s or 0)):
            result = place_bet(
                data_dir=data_dir,
                settings=settings,
                member=member,
                order=order,
                stake=float(stake),
                timeout_s=int(timeout_s),
                debug=bool(debug),
                session_pool=session_pool,
            )
    except (TimeoutError, HardTimeout):
        result = fallback
    except BaseException as exc:
        result = dict(fallback)
        result["message"] = f"并发 attempt 异常：{type(exc).__name__}: {exc}"
    if not isinstance(result, dict):
        result = dict(fallback)
    timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
    timings["attempt_wall_s"] = round(float(time.monotonic() - started), 3)
    timings["attempt_hard_timeout_s"] = float(hard_timeout_s or 0)
    result["timings"] = timings
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Follow Engine (live betting) ⚠️")
    parser.add_argument("--orders-dir", default="orders")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    orders_dir = Path(args.orders_dir)
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    configure_route_traffic_gate(data_dir)

    state_path = data_dir / "follow_state.json"
    results_path = data_dir / "follow_results.jsonl"
    trace_path = data_dir / "follow_trace.jsonl"
    queue_cmd_path = data_dir / "follow_queue_cmd.json"

    lock_path = data_dir / "follow_engine.lock"
    if not acquire_single_instance_lock(lock_path):
        print(f"[{now_iso()}] follow_engine å·²åœ¨è¿è¡Œï¼Œé€€å‡º lock={lock_path}", flush=True)
        return 2

    offsets, done, pending_orders, jobs = load_follow_state(state_path, orders_dir)
    retry_scheduler = RetryScheduler(jobs)
    jobs = retry_scheduler.retry_jobs
    hydrate_done_from_results(done, results_path)
    limit_match_committed, limit_market_committed, limit_success_keys = load_follow_limit_usage(results_path)
    follow_generated_order_keys = collect_follow_generated_order_keys(done=done, results_path=results_path)
    if follow_generated_order_keys:
        for key in list(pending_orders.keys()):
            if key in follow_generated_order_keys:
                pending_orders.pop(key, None)
        for key in list(jobs.keys()):
            if key in follow_generated_order_keys:
                jobs.pop(key, None)

    print(f"[{now_iso()}] follow_engine 启动 orders_dir={orders_dir} data_dir={data_dir}", flush=True)
    print(
        f"[{now_iso()}] 线路闸门已启用 read并发=2 context并发=2 FT_bet最小间隔=500ms 502退避=1000ms",
        flush=True,
    )
    print(
        f"[{now_iso()}] follow_state 加载完成 offsets={len(offsets)} done={len(done)} pending={len(pending_orders)} jobs={len(jobs)}",
        flush=True,
    )

    engine_started_at = float(now_ts())
    last_backfill_at = 0.0
    last_clear_req_at = 0.0
    session_pool = MemberSessionPool()
    last_session_prune_at = 0.0
    atexit.register(session_pool.close_all)
    # 线程池只负责网络 attempt；结果回收仍在主线程完成，避免并发写状态文件/TG。
    attempt_dispatcher = AttemptDispatcher(max_workers=50, per_member_limit=2)
    parallel_inflight: Dict[str, Dict[str, Any]] = {}
    notification_queue = _AsyncFollowNotificationQueue(trace_path)
    last_state_persist_at = 0.0
    snapshot_publish_path = data_dir / "snapshot_publish_events.jsonl"
    try:
        snapshot_publish_offset = int(snapshot_publish_path.stat().st_size)
    except OSError:
        snapshot_publish_offset = 0
    # Keep the latest event per active market in memory.  This closes the
    # publish-before-waiter race: an event read before a job enters waiting is
    # still visible to the post-registration recheck below.
    snapshot_latest_publish_events: Dict[str, Dict[str, Any]] = {}
    snapshot_event_fallback_seq = 0
    atexit.register(attempt_dispatcher.shutdown, wait=False)
    atexit.register(notification_queue.close, wait=False)

    def _persist_follow_state(*, force: bool = False) -> bool:
        """Persist durable queue state without turning the 20ms wake loop into an I/O loop."""
        nonlocal last_state_persist_at
        now_value = float(now_ts())
        if not force and last_state_persist_at > 0.0 and (now_value - last_state_persist_at) < 0.1:
            return False
        _trim_state_maps(done=done, pending_orders=pending_orders, jobs=jobs)
        started_at = float(now_ts())
        try:
            atomic_write_json(
                state_path,
                {
                    "offsets": offsets,
                    "done": done,
                    "pending_orders": pending_orders,
                    "jobs": retry_scheduler.to_state_dict(),
                    "done_count": len(done),
                    "queue_count": retry_scheduler.size(),
                    "updated_at": now_iso(),
                },
            )
        except Exception:
            return False
        finished_at = float(now_ts())
        last_state_persist_at = finished_at
        elapsed_ms = round(max(0.0, finished_at - started_at) * 1000.0, 3)
        if force or elapsed_ms >= 20.0:
            _append_trace_jsonl(
                trace_path,
                {
                    "event": "state_persist",
                    "forced": bool(force),
                    "state_persist_started_at": started_at,
                    "state_persist_finished_at": finished_at,
                    "state_persist_elapsed_ms": elapsed_ms,
                    "queue_count": retry_scheduler.size(),
                },
            )
        return True

    def _enqueue_follow_notification(job: RetryJob, follow_result: Dict[str, Any]) -> None:
        notification_queue.enqueue(job=job, follow_result=follow_result)

    def _finish_terminal_job(job: RetryJob, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Record one Task terminal result and finalize its parent when every part is done."""
        completed_record = dict(record or {})
        completed_record.setdefault("split_completed_at", float(now_ts()))
        done[job.key] = completed_record
        retry_scheduler.remove_job(job.key)
        if not bool(job.independent_split):
            pending_orders.pop(job.key, None)
            _persist_follow_state(force=True)
            return done[job.key]

        parent_key = str(job.parent_key or job.key)
        aggregate_started_at = float(now_ts())
        aggregate = aggregate_independent_split_done(
            parent_key=parent_key,
            split_count=int(job.split_count or 1),
            done=done,
            active_keys=set(retry_scheduler.retry_jobs.keys()),
            created_at=float(job.created_at or now_ts()),
            now=float(now_ts()),
        )
        aggregate_finished_at = float(now_ts())
        if aggregate is None:
            _persist_follow_state(force=True)
            return None
        aggregate["order"] = job.order
        aggregate["aggregate_started_at"] = aggregate_started_at
        aggregate["aggregate_finished_at"] = aggregate_finished_at
        done[parent_key] = aggregate
        for index in range(int(job.split_count or 1)):
            done.pop(split_task_key(parent_key, index, int(job.split_count or 1)), None)
        pending_orders.pop(parent_key, None)
        _persist_follow_state(force=True)
        _enqueue_follow_notification(
            job,
            _build_independent_split_tg_result(aggregate, job),
        )
        return aggregate

    def _finish_parallel_attempt(meta: Dict[str, Any], result: Dict[str, Any]) -> None:
        job = meta.get("job")
        if not isinstance(job, RetryJob):
            return
        attempt_no = int(meta.get("attempt_no") or 0)
        result = dict(result or {})
        result["attempt"] = attempt_no
        result["attempt_no"] = attempt_no
        result.setdefault("split_order_id", job.key)
        result["split_index"] = int(job.split_index or 0) + 1
        result["split_count"] = int(job.split_count or 1)
        result.setdefault("orderId", job.order_id)
        result.setdefault("target", job.target)
        result.setdefault("stake", float(meta.get("cur_stake") or 0.0))
        attach_follow_limit_metadata(result, job.order, settings)
        candidate_at = float(meta.get("candidate_snapshot_at") or job.first_candidate_snapshot_at or 0.0)
        member_acquired_at = float(meta.get("member_acquired_at") or 0.0)
        result["candidate_snapshot_at"] = candidate_at
        result["member_acquired_at"] = member_acquired_at
        trigger_reason = str(meta.get("trigger_reason") or job.trigger_reason or "INITIAL_READY")
        trigger_event_seq = _safe_int(meta.get("trigger_event_seq"), _safe_int(job.trigger_event_seq, 0))
        trigger_publish_at = _to_float(meta.get("trigger_publish_at"), _to_float(job.trigger_publish_at, 0.0))
        trigger_wakeup_at = _to_float(meta.get("trigger_wakeup_at"), _to_float(job.trigger_wakeup_at, 0.0))
        result["trigger_reason"] = trigger_reason
        result["trigger_event_seq"] = trigger_event_seq
        result["trigger_publish_at"] = trigger_publish_at or None
        result["trigger_wakeup_at"] = trigger_wakeup_at or None
        if trigger_publish_at and trigger_wakeup_at:
            result["trigger_publish_to_wakeup_ms"] = round(max(0.0, trigger_wakeup_at - trigger_publish_at) * 1000.0, 3)
        candidate_detected_at = _to_float(meta.get("candidate_detected_at"), _to_float(job.candidate_detected_at, candidate_at))
        runnable_at = _to_float(meta.get("job_marked_runnable_at"), _to_float(job.job_marked_runnable_at, candidate_detected_at))
        queue_entered_at = _to_float(meta.get("runnable_queue_entered_at"), _to_float(job.runnable_queue_entered_at, runnable_at))
        dispatcher_selected_at = _to_float(meta.get("dispatcher_selected_at"), _to_float(job.dispatcher_selected_at, 0.0))
        attempt_started_at = _to_float(meta.get("attempt_started_at"), _to_float(job.attempt_started_at, 0.0))
        result["candidate_detected_at"] = candidate_detected_at or None
        result["job_marked_runnable_at"] = runnable_at or None
        result["runnable_queue_entered_at"] = queue_entered_at or None
        result["dispatcher_selected_at"] = dispatcher_selected_at or None
        result["attempt_started_at"] = attempt_started_at or None
        result["candidate_to_runnable_ms"] = round(max(0.0, runnable_at - candidate_detected_at) * 1000.0, 3) if candidate_detected_at and runnable_at else None
        result["runnable_queue_wait_ms"] = round(max(0.0, queue_entered_at - runnable_at) * 1000.0, 3) if runnable_at and queue_entered_at else None
        result["dispatcher_wait_ms"] = round(max(0.0, dispatcher_selected_at - queue_entered_at) * 1000.0, 3) if dispatcher_selected_at and queue_entered_at else None
        result["member_acquire_ms"] = round(max(0.0, member_acquired_at - dispatcher_selected_at) * 1000.0, 3) if member_acquired_at and dispatcher_selected_at else None
        # ``_to_float`` already supplies a zero fallback.  Keep this telemetry
        # path tolerant of missing release timestamps without passing a
        # second positional default to the helper.
        member_released_at = _to_float(meta.get("member_released_at"))
        if member_released_at > 0.0:
            result["member_released_at"] = member_released_at
            if ft_finished := _to_float(result.get("ft_bet_finished_at"), 0.0):
                result["member_release_ms"] = round(max(0.0, member_released_at - ft_finished) * 1000.0, 3)
        # Do not read mutable order-level publish fields here.  The trigger
        # metadata was captured when this attempt was queued and cannot be
        # overwritten by a later Snapshot event.
        if trigger_publish_at > 0.0:
            result["snapshot_publish_at"] = trigger_publish_at
        if trigger_wakeup_at > 0.0:
            result["snapshot_wakeup_at"] = trigger_wakeup_at
        if trigger_publish_at > 0.0 and trigger_wakeup_at > 0.0:
            result["snapshot_publish_to_wakeup_ms"] = round(max(0.0, trigger_wakeup_at - trigger_publish_at) * 1000.0, 3)
        result["first_candidate_snapshot_ms"] = (
            round(max(0.0, (candidate_at - float(job.created_at or 0.0)) * 1000.0), 3)
            if candidate_at else None
        )
        result["candidate_to_member_acquired_ms"] = (
            round(max(0.0, (member_acquired_at - candidate_at) * 1000.0), 3)
            if member_acquired_at and candidate_at else None
        )
        context_finished_at = float(result.get("context_finished_at") or 0.0)
        if context_finished_at and member_acquired_at:
            result["member_acquired_to_context_finished_ms"] = round(
                max(0.0, (context_finished_at - member_acquired_at) * 1000.0), 3
            )
        ft_started = float(result.get("ft_bet_started_at") or 0.0)
        if context_finished_at and ft_started:
            result["context_finished_to_ft_bet_started_ms"] = round(
                max(0.0, (ft_started - context_finished_at) * 1000.0), 3
            )
        ft_finished = float(result.get("ft_bet_finished_at") or 0.0)
        if ft_started and ft_finished:
            result["ft_bet_started_to_result_ms"] = round(
                max(0.0, (ft_finished - ft_started) * 1000.0), 3
            )
        result["total_order_elapsed_ms"] = round(
            max(0.0, (float(now_ts()) - float(job.created_at or 0.0)) * 1000.0), 3
        )
        result["market_wait_count"] = int(job.market_wait_count or 0)
        result["member_wait_count"] = int(job.member_wait_count or 0)
        waiting_status = str(
            result.get("snapshot_direct_status") or result.get("error_code") or ""
        ).strip().upper()
        if waiting_status in {
            SNAPSHOT_MISSING,
            SNAPSHOT_STALE,
            TARGET_LINE_MISSING,
            SNAPSHOT_INVALID,
            MEMBER_IORATIO_MISSING,
            MEMBER_IORATIO_STALE,
            MEMBER_IORATIO_INVALID,
            BET_CONTEXT_MISMATCH,
            MARKET_CHANGED_DURING_CONTEXT,
        } and str(result.get("real_submission") or "").lower() == "none":
            if waiting_status in {
                SNAPSHOT_MISSING,
                SNAPSHOT_STALE,
                TARGET_LINE_MISSING,
                SNAPSHOT_INVALID,
                BET_CONTEXT_MISMATCH,
                MARKET_CHANGED_DURING_CONTEXT,
            }:
                job.market_wait_count = int(job.market_wait_count or 0) + 1
            else:
                job.member_wait_count = int(job.member_wait_count or 0) + 1
            job.attempts = max(0, int(attempt_no) - 1)
            job.last_err = waiting_status
            job.last_message = str(result.get("message") or "")
            job.member_id = ""
            job.next_try_at = float(now_ts())
            return
        # A context-only transport failure happened before FT_bet.  Persist
        # the diagnostic attempt, release the member lease, and schedule a
        # bounded 150/300/500ms retry.  This path deliberately bypasses the
        # Snapshot wait window and never touches UNKNOWN_RESULT.
        if extract_err_from_result(result).upper() == CONTEXT_RETRYABLE:
            retry_now = float(now_ts())
            count, delay, fast = retry_scheduler.schedule_context_retry(
                job,
                now=retry_now,
                slow_delay=max(1.0, float(meta.get("retry_delay") or 1.0)),
            )
            result["context_retry_count"] = count
            result["context_retry_delay_ms"] = round(delay * 1000.0, 3)
            result["context_retry_fast"] = bool(fast)
            result["real_submission"] = "none"
            result["snapshot_direct_submission"] = False
            result["error_class"] = CONTEXT_RETRYABLE
            result["order_elapsed_s"] = round(max(0.0, retry_now - float(job.created_at or retry_now)), 3)
            result["total_order_elapsed_ms"] = round(max(0.0, retry_now - float(job.created_at or retry_now)) * 1000.0, 3)
            fingerprint = str(result.get("market_row_fingerprint_before") or result.get("market_row_fingerprint") or "")
            if fingerprint:
                job.order["_context_retry_fingerprint"] = fingerprint
            job.order["_context_retry"] = True
            job.last_err = CONTEXT_RETRYABLE
            job.last_message = str(result.get("message") or "FT_order_view 传输失败，正在快速重试。")
            job.trigger_reason = "CONTEXT_RETRY"
            job.trigger_event_seq = 0
            job.trigger_publish_at = 0.0
            job.trigger_wakeup_at = retry_now
            job.job_marked_runnable_at = retry_now
            job.member_id = str(result.get("member_id") or result.get("execution_member") or job.member_id or "")
            persist_started_at, persist_finished_at = _append_jsonl_timed(results_path, result)
            result["result_persist_started_at"] = persist_started_at
            result["result_persist_finished_at"] = persist_finished_at
            result["result_persist_elapsed_ms"] = round(max(0.0, persist_finished_at - persist_started_at) * 1000.0, 3)
            _append_trace_jsonl(
                trace_path,
                {
                    "event": "context_retry_scheduled",
                    "key": job.key,
                    "orderId": job.order_id,
                    "attempt": attempt_no,
                    "member": str(result.get("member") or ""),
                    "err": CONTEXT_RETRYABLE,
                    "context_retry_count": count,
                    "context_retry_delay_ms": result["context_retry_delay_ms"],
                    "context_retry_fast": bool(fast),
                    "member_released_at": result.get("member_released_at"),
                    "market_row_fingerprint": fingerprint,
                },
            )
            _enqueue_follow_notification(job, result)
            return
        _apply_market_submit_breaker(
            job,
            result,
            max_attempts=int(_setting_or_default(meta, "market_reject_max_attempts", 3)),
            retry_delay_seconds=float(_setting_or_default(meta, "market_reject_retry_delay", 0.5)),
        )
        result["error_class"] = classify_execution_error(result)
        try:
            result["order_elapsed_s"] = round(float(now_ts()) - float(job.created_at or 0.0), 3)
        except Exception:
            pass
        persist_started_at, persist_finished_at = _append_jsonl_timed(results_path, result)
        record_follow_limit_success(
            result,
            match_committed=limit_match_committed,
            market_committed=limit_market_committed,
            success_keys=limit_success_keys,
        )
        result["result_persist_started_at"] = persist_started_at
        result["result_persist_finished_at"] = persist_finished_at
        result["result_persist_elapsed_ms"] = round(max(0.0, persist_finished_at - persist_started_at) * 1000.0, 3)
        try:
            nc = result.get("nowcredit")
            if nc not in (None, "") and job.member_id:
                member_patch_started_at = float(now_ts())
                patch_member(
                    data_dir / "arsenal.json",
                    str(job.member_id),
                    {"maxcredit": _to_float(nc), "nowcredit": _to_float(nc), "balance_updated_at": now_ts()},
                )
                result["member_patch_started_at"] = member_patch_started_at
                result["member_patch_finished_at"] = float(now_ts())
                result["member_patch_elapsed_ms"] = round(
                    max(0.0, result["member_patch_finished_at"] - member_patch_started_at) * 1000.0, 3
                )
        except Exception:
            pass
        if (not bool(job.independent_split)) and split_job_current_part_on_member_max_limit(
            job,
            result,
            min_stake_per_bet=float(meta.get("split_min_stake") or 50.0),
            max_split_count=50,
        ):
            job.attempts = 0
            job.last_err = STAKE_EXCEEDS_MAX_ERR
            job.last_message = str(result.get("message") or "")
            job.next_try_at = float(now_ts())
            return
        _enqueue_follow_notification(job, result)
        _append_trace_jsonl(
            trace_path,
            {
                "event": "attempt_result",
                "key": job.key,
                "target": job.target,
                "orderId": job.order_id,
                "order_id": job.order_id,
                "split_order_id": job.key,
                "attempt": attempt_no,
                "attempt_no": attempt_no,
                "split_index": int(result.get("split_index") or 0),
                "split_count": int(result.get("split_count") or 1),
                "ok": bool(result.get("ok")),
                "member": str(result.get("member") or ""),
                "err": extract_err_from_result(result),
                "message": str(result.get("message") or ""),
                "elapsed_s": float(result.get("order_elapsed_s") or 0.0),
                "attempt_wall_s": float(((result.get("timings") or {}) if isinstance(result.get("timings"), dict) else {}).get("attempt_wall_s") or 0.0),
                **_snapshot_attempt_trace_fields(result),
            },
        )

        if bool(result.get("ok")):
            _clear_unknown_result_state(job)
            tid = str(result.get("ticket_id") or "").strip()
            if tid:
                job.split_tickets.append(tid)
                follow_generated_order_keys.add(f"{job.target}|{tid}")
            if job.member_id and job.member_id not in job.used_member_ids:
                job.used_member_ids.append(job.member_id)
            if (not bool(job.independent_split)) and int(job.split_count or 1) > 1 and (int(job.split_index or 0) + 1) < int(job.split_count or 1):
                job.split_index = int(job.split_index or 0) + 1
                job.next_try_at = float(now_ts()) + float(job.split_delay_seconds or 0.0)
                job.attempts = 0
                job.last_err = ""
                job.last_message = f"分单已完成 {len(job.split_tickets)}/{int(job.split_count or 1)}"
                return
            terminal_record = {
                "status": "ok",
                "ticket_id": job.split_tickets[-1] if job.split_tickets else tid,
                "ts": int(now_ts()),
                "elapsed_s": round(float(now_ts()) - float(job.created_at or 0.0), 3),
                "split_index": int(job.split_index or 0),
                "stake": float(result.get("stake") or 0.0),
                "member": str(result.get("member") or ""),
            }
            if len(job.split_tickets) > 1:
                terminal_record["tickets"] = list(job.split_tickets)
            _finish_terminal_job(job, terminal_record)
            return

        if not is_retryable_follow_result(result):
            terminal_reconcile = str(result.get("reconcile_terminal_status") or "").strip().upper()
            terminal_direct = str(result.get("snapshot_direct_result_status") or "").strip().upper()
            terminal_status = (
                MARKET_SUBMIT_REJECTED.lower()
                if terminal_direct == MARKET_SUBMIT_REJECTED
                else terminal_reconcile.lower()
                if terminal_reconcile in {"CONFIRMED_FAILED", "STILL_UNKNOWN"}
                else ("skipped" if bool(result.get("skipped")) else "failed")
            )
            _finish_terminal_job(job, {
                "status": terminal_status,
                "reason": str(result.get("message") or ""),
                "ts": int(now_ts()),
                "split_index": int(job.split_index or 0),
                "stake": float(result.get("stake") or 0.0),
                "member": str(result.get("member") or ""),
            })
            return

        job.attempts = attempt_no
        job.last_message = str(result.get("message") or "")
        job.last_err = extract_err_from_result(result)
        if _schedule_explicit_market_rejection_retry(job, result, now=float(now_ts())):
            _append_trace_jsonl(
                trace_path,
                {
                    "event": "market_reject_retry_scheduled",
                    "key": job.key,
                    "orderId": job.order_id,
                    "attempt": attempt_no,
                    "member": str(result.get("member") or ""),
                    "error_code": str(result.get("market_submit_reject_code") or ""),
                    "reject_count": int(result.get("market_submit_reject_count") or 0),
                    "reject_limit": int(result.get("market_submit_reject_max_attempts") or 0),
                    "retry_at": float(job.next_try_at or 0.0),
                },
            )
            return
        if job.last_err == MARKET_ERROR:
            revision = _safe_int(result.get("snapshot_direct_revision"), 0)
            if revision > 0:
                job.order["_wait_for_snapshot_revision"] = revision
        if job.last_err == UNKNOWN_RESULT:
            _begin_unknown_result_state(job, result, settings)
            job.unknown_result_check_count = _safe_int(result.get("unknown_result_check_count"), job.unknown_result_check_count)
            job.unknown_result_last_check_at = _safe_float(result.get("reconcile_checked_at"), 0.0)
            job.order["_unknown_result_check_count"] = int(job.unknown_result_check_count)
            job.next_try_at = float(now_ts()) + (15.0 if bool(result.get("reconcile_pending")) else 0.0)
            if str(result.get("reconcile_terminal_status") or "").upper() in {"CONFIRMED_FAILED", "STILL_UNKNOWN"}:
                _finish_terminal_job(job, {
                    "status": str(result.get("reconcile_terminal_status") or "STILL_UNKNOWN").lower(),
                    "reason": str(result.get("message") or ""),
                    "ts": int(now_ts()),
                    "split_index": int(job.split_index or 0),
                })
            return
        if job.last_err == "1X001":
            wait_max = float(meta.get("wait_1x001_max") or 0.0)
            if wait_max > 0:
                retry_scheduler.schedule_market_closed(
                    job,
                    now=float(now_ts()),
                    wait_max=wait_max,
                    probe_seconds=float(meta.get("wait_1x001_probe") or 1.0),
                )
            else:
                job.next_try_at = float(now_ts()) + float(meta.get("retry_delay") or 1.0)
        elif job.last_err in {"LINE_NOT_FOUND", SNAPSHOT_MISSING, SNAPSHOT_STALE, TARGET_LINE_MISSING, SNAPSHOT_INVALID, MEMBER_IORATIO_MISSING, MEMBER_IORATIO_STALE, MEMBER_IORATIO_INVALID, MARKET_ERROR}:
            wait_max = float(meta.get("wait_line_not_found_max") or 0.0)
            if wait_max > 0:
                retry_scheduler.schedule_line_not_found(
                    job,
                    now=float(now_ts()),
                    wait_max=wait_max,
                    probe_seconds=float(meta.get("wait_line_not_found_probe") or 1.0),
                )
            else:
                job.next_try_at = float(now_ts())
        else:
            delay = retry_delay_for_result(result, attempt_no)
            if bool(result.get("reconcile_pending")):
                delay = max(10.0, float(meta.get("reconcile_retry_delay") or delay))
            job.next_try_at = float(now_ts()) + delay
    try:
        cmd0 = read_json_file(queue_cmd_path, {})
        if isinstance(cmd0, dict):
            last_clear_req_at = float(cmd0.get("clear_pending_applied_req_at") or cmd0.get("clear_pending_requested_at") or 0.0)
    except Exception:
        last_clear_req_at = 0.0

    while True:
        settings = load_settings(data_dir)
        attempt_dispatcher.set_per_member_limit(
            max(1, min(50, int(settings.get("per_member_parallel_attempts") or 2)))
        )
        # Cross-process watcher publish wakeup.  This only moves matching
        # waiting jobs to the front of the fallback scheduler; it never
        # changes the MarketRow or bypasses validation.
        snapshot_publish_offset, publish_events = _read_snapshot_publish_events(
            snapshot_publish_path,
            snapshot_publish_offset,
        )
        now_wake = float(now_ts())
        if publish_events:
            for event in publish_events:
                market_key = str(event.get("active_market_key") or "").strip()
                if not market_key:
                    continue
                try:
                    event_seq = int(event.get("event_seq") or 0)
                except Exception:
                    event_seq = 0
                if event_seq <= 0:
                    snapshot_event_fallback_seq += 1
                    event_seq = snapshot_event_fallback_seq
                    event = dict(event)
                    event["event_seq"] = event_seq
                previous = snapshot_latest_publish_events.get(market_key)
                previous_seq = int((previous or {}).get("event_seq") or 0)
                if previous is None or event_seq > previous_seq:
                    snapshot_latest_publish_events[market_key] = dict(event)
        # Recheck the retained latest event on every loop, including the loop
        # in which a job first became a waiter.  This is the registration-after
        # recheck half of the missed-wakeup fix.
        for job in list(retry_scheduler.retry_jobs.values()):
            _consume_snapshot_publish_wakeup(
                job,
                settings=settings,
                latest_events=snapshot_latest_publish_events,
                now_value=now_wake,
            )
        # 先回收已完成的并发 attempt，所有状态变更都在主线程进行。
        for completed_key, _completed_member, completed_future in attempt_dispatcher.poll_completed():
            meta = parallel_inflight.pop(completed_key, None)
            if not meta:
                continue
            # AttemptDispatcher removes the member lease before returning the
            # completed future. Capture that exact boundary for latency data.
            meta["member_released_at"] = float(now_ts())
            try:
                completed_result = completed_future.result()
            except BaseException as exc:
                completed_result = {
                    "ok": False,
                    "message": f"并发 attempt future 异常：{type(exc).__name__}: {exc}",
                    "member_id": str(meta.get("member_id") or ""),
                    "member": str(meta.get("member_label") or meta.get("member_id") or ""),
                    "stake": float(meta.get("cur_stake") or 0.0),
                    "timings": {},
                }
            _finish_parallel_attempt(meta, completed_result if isinstance(completed_result, dict) else {})
            # A completed attempt releases its member lease.  Wake jobs that
            # were deferred specifically for member capacity immediately; the
            # regular timer remains only as a fallback.
            wake_at = float(now_ts())
            for waiting_job in retry_scheduler.retry_jobs.values():
                if not isinstance(waiting_job, RetryJob):
                    continue
                message = str(waiting_job.last_message or "")
                if "忙碌" in message or "等待执行会员空闲" in message:
                    waiting_job.next_try_at = min(float(waiting_job.next_try_at or wake_at), wake_at)
        now_loop = float(now_ts())
        if (now_loop - float(last_session_prune_at or 0.0)) >= 30.0:
            try:
                session_pool.prune_idle(max_idle_s=120.0)
            except Exception:
                pass
            last_session_prune_at = now_loop
        enabled = bool(settings.get("enabled") or False)
        ignore_history_on_start = bool(settings.get("ignore_history_on_start") or False)
        ignore_history_grace_seconds = float(settings.get("ignore_history_grace_seconds") or 0.0)
        ignore_history_cutoff = engine_started_at - max(0.0, ignore_history_grace_seconds)
        dedupe_by_order_id_global = bool(settings.get("dedupe_by_order_id_global") or False)
        # 近期回补：即使 offsets 已推进，也会从每个 orders/*.jsonl 末尾扫描最近 N 秒的订单，避免因时区/临时 bug/启停顺序导致漏单。
        backfill_recent_seconds = float(settings.get("backfill_recent_seconds") or 0.0)
        backfill_tail_lines = int(settings.get("backfill_tail_lines") or 0)
        backfill_tail_bytes = int(settings.get("backfill_tail_bytes") or 262144)
        backfill_interval_seconds = float(settings.get("backfill_interval_seconds") or 0.0)

        # WebUI 清空跟单队列：通过 data/follow_queue_cmd.json 发指令给引擎内存队列。
        try:
            cmd = read_json_file(queue_cmd_path, {})
        except Exception:
            cmd = {}
        try:
            clear_req_at = float((cmd or {}).get("clear_pending_requested_at") or 0.0)
        except Exception:
            clear_req_at = 0.0
        if clear_req_at > 0 and clear_req_at > float(last_clear_req_at):
            pending_before = int(len(pending_orders))
            jobs_before = int(retry_scheduler.size())
            pending_items = list(pending_orders.items())
            job_items = list(retry_scheduler.retry_jobs.items())
            cleared_keys = set([str(k) for k, _v in pending_items + job_items if str(k)])
            ts0 = int(now_ts())
            for k in cleared_keys:
                if k in done:
                    continue
                done[k] = {
                    "status": "cleared",
                    "reason": "manual_queue_clear",
                    "ts": ts0,
                }
            try:
                bot_token, default_chat_id, proxy, order_preview_image, ui_theme = load_tg_config(Path("config.json"))
            except Exception:
                bot_token = default_chat_id = proxy = order_preview_image = ""
                ui_theme = {}
            if bot_token:
                clear_rows: List[Tuple[str, Any, Optional[Any]]] = []
                clear_rows.extend((str(k), order, None) for k, order in pending_items)
                clear_rows.extend((str(k), getattr(job, "order", None), job) for k, job in job_items)
                sent_keys: set = set()
                for k, order_obj, job_obj in clear_rows:
                    if not k or k in sent_keys or not isinstance(order_obj, dict):
                        continue
                    sent_keys.add(k)
                    try:
                        plan_total = 0.0
                        if job_obj is not None:
                            try:
                                plan_total = float(getattr(job_obj, "stake", 0.0) or 0.0)
                            except Exception:
                                plan_total = 0.0
                        if plan_total <= 0:
                            try:
                                plan_total = float(order_obj.get("stake") or 0.0)
                            except Exception:
                                plan_total = 0.0
                        result = {
                            "ts": ts0,
                            "ts_ms": int(now_ts_ms()),
                            "target": str(order_obj.get("target") or str(k).split("|", 1)[0] or ""),
                            "orderId": str(order_obj.get("orderId") or _order_id_from_cache_key(k) or ""),
                            "stake": float(plan_total or 0.0),
                            "ok": False,
                            "message": "队列已手动清空，未跟单。",
                            "status": "cleared",
                            "skip_reason": "manual_queue_clear",
                            "split_total_plan": float(plan_total or 0.0),
                            "split_total_done": 0.0,
                        }
                        order_chat_id = str(order_obj.get("_tg_chat_id") or default_chat_id).strip()
                        if not order_chat_id:
                            continue
                        merged = build_order_follow_combined_message(order=order_obj, follow_result=result, ui_theme=ui_theme)
                        new_mid = tg_upsert_order_message(
                            bot_token=bot_token,
                            chat_id=order_chat_id,
                            message_id=order_obj.get("_tg_message_id"),
                            html_text=merged,
                            order_id=str(order_obj.get("orderId") or _order_id_from_cache_key(k) or ""),
                            order_preview_image=order_preview_image,
                            timeout_s=10,
                            proxy=proxy,
                            ui_theme=ui_theme,
                        )
                        if new_mid:
                            order_obj["_tg_message_id"] = int(new_mid)
                            order_obj["_tg_chat_id"] = order_chat_id
                    except Exception as e:
                        _tg_warn_throttled(k, e)
            pending_orders.clear()
            retry_scheduler.retry_jobs.clear()
            jobs = retry_scheduler.retry_jobs
            last_clear_req_at = float(clear_req_at)
            print(
                f"[{now_iso()}] 清空跟单队列：pending={pending_before} jobs={jobs_before} req_at={clear_req_at}",
                flush=True,
            )
            try:
                cmd_out = cmd if isinstance(cmd, dict) else {}
                cmd_out["clear_pending_applied_req_at"] = float(clear_req_at)
                cmd_out["clear_pending_applied_at"] = now_iso()
                cmd_out["last_cleared_pending"] = int(pending_before)
                cmd_out["last_cleared_jobs"] = int(jobs_before)
                cmd_out["last_cleared_total"] = int(pending_before + jobs_before)
                cmd_out["last_marked_done"] = int(len(cleared_keys))
                cmd_out["updated_at"] = now_iso()
                atomic_write_json(queue_cmd_path, cmd_out)
            except Exception:
                pass

        # 关键：即使未启用跟单/未选会员，也要持续“读 orders + 推进 offsets”，否则会出现：
        # 后台订单已落盘 + TG 已提示“等待中”，但用户稍后才启用跟单 -> 引擎永远读不到那条订单。
        new_ingested: List[Dict[str, Any]] = []
        seen_order_ids: set = set()
        if dedupe_by_order_id_global:
            try:
                seen_order_ids.update([_order_id_from_cache_key(k) for k in (done or {}).keys()])
                seen_order_ids.update([_order_id_from_cache_key(k) for k in (pending_orders or {}).keys()])
                seen_order_ids.update([_order_id_from_cache_key(k) for k in (jobs or {}).keys()])
                seen_order_ids.discard("")
            except Exception:
                seen_order_ids = set()
        order_files = list(orders_dir.glob("*.jsonl"))
        for p in order_files:
            pkey = str(p)
            if pkey not in offsets:
                try:
                    offsets[pkey] = int(p.stat().st_size)
                except Exception:
                    offsets[pkey] = 0
            start_offset = int(offsets.get(pkey) or 0)
            try:
                with p.open("r", encoding="utf-8-sig") as f:
                    f.seek(offsets[pkey])
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        offsets[pkey] = f.tell()
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            order = json.loads(line)
                        except Exception:
                            continue

                        target = str(order.get("target") or p.stem)
                        order_id = str(order.get("orderId") or "")
                        if not order_id:
                            continue
                        # 多后台全局去重：同一后台订单号只跟一次（避免重复跟单）
                        if dedupe_by_order_id_global and order_id in seen_order_ids:
                            continue
                        # 忽略历史订单（防止重启/重登时回放导致二次跟单）
                        # 注意：只在“从文件开头回放”这种危险场景才启用时间过滤。
                        # 正常情况下我们依赖 offsets 去重；否则引擎停机/重启期间产生的订单会被误当作历史单丢弃。
                        replaying_history = start_offset <= 0
                        if ignore_history_on_start and replaying_history:
                            # 关键：优先用“本软件接收到/落盘时间 ts”判断；后台展示时间可能有时区偏移，导致 bet_ts 偏小被误判为历史单。
                            try:
                                recv_ts = float(order.get("ts") or 0)
                            except Exception:
                                recv_ts = 0.0
                            if recv_ts > 0 and float(recv_ts) < float(ignore_history_cutoff):
                                continue
                            try:
                                bet_ts = int(order.get("bet_ts") or 0)
                            except Exception:
                                bet_ts = 0
                            if recv_ts <= 0 and bet_ts > 0 and float(bet_ts) < float(ignore_history_cutoff):
                                continue
                        cache_key = f"{target}|{order_id}"
                        if cache_key in follow_generated_order_keys:
                            continue
                        if cache_key in done:
                            continue
                        pending_orders[cache_key] = order
                        new_ingested.append({"target": target, "orderId": order_id, "order": order})
                        if dedupe_by_order_id_global:
                            seen_order_ids.add(order_id)
            except Exception:
                continue

        # 近期回补：从文件末尾扫描一遍，避免因为 bet_ts 时区偏移等原因误过滤后“永远错过该单”。
        try:
            now_backfill = float(now_ts())
            should_backfill = (
                backfill_recent_seconds > 0
                and backfill_tail_lines > 0
                and order_files
                and (
                    backfill_interval_seconds <= 0
                    or last_backfill_at <= 0
                    or (now_backfill - last_backfill_at) >= max(0.5, float(backfill_interval_seconds))
                )
            )
            if should_backfill:
                last_backfill_at = now_backfill
                now_backfill = float(now_ts())
                recent_cutoff = now_backfill - max(0.0, backfill_recent_seconds)
                backfill_tail_lines = max(1, min(5000, int(backfill_tail_lines)))
                backfill_tail_bytes = max(16 * 1024, min(2 * 1024 * 1024, int(backfill_tail_bytes)))
                for p in order_files:
                    for order in tail_jsonl_objects(p, max_lines=backfill_tail_lines, max_bytes=backfill_tail_bytes):
                        try:
                            order_id = str(order.get("orderId") or "")
                            if not order_id:
                                continue
                            # 多后台全局去重：同一后台订单号只跟一次（避免重复跟单）
                            if dedupe_by_order_id_global and order_id in seen_order_ids:
                                continue
                            target = str(order.get("target") or p.stem)
                            cache_key = f"{target}|{order_id}"
                            if cache_key in follow_generated_order_keys:
                                continue
                            if cache_key in done or cache_key in pending_orders or cache_key in jobs:
                                continue
                            try:
                                recv_ts = float(order.get("ts") or 0.0)
                            except Exception:
                                recv_ts = 0.0
                            if recv_ts <= 0:
                                try:
                                    recv_ts = float(order.get("bet_ts") or 0.0)
                                except Exception:
                                    recv_ts = 0.0
                            if recv_ts > 0 and recv_ts < float(recent_cutoff):
                                continue
                            pending_orders[cache_key] = order
                            new_ingested.append({"target": target, "orderId": order_id, "order": order, "backfill": True})
                            if dedupe_by_order_id_global:
                                seen_order_ids.add(order_id)
                        except Exception:
                            continue
        except Exception:
            pass

        if new_ingested:
            for it in new_ingested:
                try:
                    if not should_send_initial_tg_for_ingested_item(
                        it,
                        max_backfill_age_s=max(120.0, float(backfill_interval_seconds or 0.0) + 30.0),
                    ):
                        continue
                    order_obj = it.get("order")
                    if not isinstance(order_obj, dict):
                        continue
                    ensure_initial_order_tg_message(
                        order=order_obj,
                        target_name=str(it.get("target") or order_obj.get("target") or "unknown"),
                        settings=settings,
                    )
                except Exception:
                    continue

        # 控制 pending_orders 大小并按有界频率持久化，避免 20ms 唤醒循环变成 I/O 循环。
        _persist_follow_state()

        if new_ingested and (not enabled):
            for it in new_ingested[:5]:
                print(
                    f"[{now_iso()}] 收到后台订单(已暂存，等待启用跟单) target={it.get('target')} order={it.get('orderId')}",
                    flush=True,
                )

            if not enabled:
                time.sleep(max(0.5, float(args.interval)))
                continue

        doc = load_arsenal(data_dir)
        mappings = load_mappings(data_dir)
        members = [ensure_member_defaults(m) for m in (doc.get("members") or []) if isinstance(m, dict)]
        if not members:
            if new_ingested:
                for it in new_ingested[:5]:
                    print(
                        f"[{now_iso()}] 收到后台订单(已暂存，但弹药库还没有会员账号) target={it.get('target')} order={it.get('orderId')}",
                        flush=True,
                    )
            time.sleep(max(0.5, float(args.interval)))
            continue

        retry_max_attempts = int(_setting_or_default(settings, "retry_max_attempts", 5))
        retry_delay_seconds = float(_setting_or_default(settings, "retry_delay_seconds", 20.0))
        retry_max_seconds = float(_setting_or_default(settings, "retry_max_seconds", 0))
        wait_1x001_probe = float(settings.get("wait_1X001_probe_seconds") or 1)
        wait_1x001_max = float(settings.get("wait_1X001_max_seconds") or 300)
        wait_line_not_found_probe = float(settings.get("wait_line_not_found_probe_seconds") or 2)
        wait_line_not_found_max = float(settings.get("wait_line_not_found_max_seconds") or 30)
        market_reject_max_attempts = int(_setting_or_default(settings, "market_reject_max_attempts", 3))
        market_reject_retry_delay = float(_setting_or_default(settings, "market_reject_retry_delay_seconds", 0.5))
        retry_max_attempts_on_1x001 = int(_setting_or_default(settings, "retry_max_attempts_on_1X001", 5))
        retry_max_seconds_on_1x001 = float(_setting_or_default(settings, "retry_max_seconds_on_1X001", 0))
        queue_max_per_tick = int(settings.get("queue_max_per_tick") or 3)
        parallel_attempts = int(settings.get("parallel_attempts") or 1)
        per_member_parallel_attempts = int(settings.get("per_member_parallel_attempts") or 1)
        allow_line_change = bool(settings.get("allow_line_change") or False)
        split_count_cfg = int(settings.get("split_count") or 1)
        split_delay_seconds_cfg = float(settings.get("split_delay_seconds") or 0)
        split_min_stake_cfg = float(settings.get("split_min_stake") or 50.0)

        # 0 表示不限制（直到成功或你手动停掉进程）
        retry_max_attempts = max(0, min(5000, retry_max_attempts))
        retry_delay_seconds = max(0.0, min(120.0, retry_delay_seconds))
        retry_max_seconds = max(0.0, float(retry_max_seconds))
        wait_1x001_probe = max(0.2, min(60.0, float(wait_1x001_probe)))
        wait_1x001_max = max(0.0, min(600.0, float(wait_1x001_max)))
        wait_line_not_found_probe = max(0.2, min(60.0, float(wait_line_not_found_probe)))
        wait_line_not_found_max = max(0.0, min(600.0, float(wait_line_not_found_max)))
        market_reject_max_attempts = max(1, min(20, int(market_reject_max_attempts)))
        market_reject_retry_delay = max(0.0, min(30.0, float(market_reject_retry_delay)))
        retry_max_attempts_on_1x001 = max(0, min(5000, retry_max_attempts_on_1x001))
        retry_max_seconds_on_1x001 = max(0.0, float(retry_max_seconds_on_1x001))
        split_count_cfg = max(1, min(50, int(split_count_cfg)))
        parallel_attempts = effective_parallel_attempts(parallel_attempts, split_count_cfg)
        per_member_parallel_attempts = max(1, min(8, int(per_member_parallel_attempts)))
        queue_max_per_tick = effective_parallel_attempts(queue_max_per_tick, split_count_cfg)
        split_delay_seconds_cfg = max(0.0, min(600.0, float(split_delay_seconds_cfg)))
        split_min_stake_cfg = max(1.0, min(100000.0, float(split_min_stake_cfg)))

        # 确保 place_bet 能读到 allow_line_change
        settings = dict(settings)
        settings["allow_line_change"] = allow_line_change
        allow_quote_members = bool(settings.get("allow_quote_members") or False)

        # 从 pending_orders 生成跟单任务；多笔分单各自成为可并发、可独立重试的 Task。
        for order in list(pending_orders.values()):
            try:
                target = str(order.get("target") or "").strip() or "unknown"
                order_id = str(order.get("orderId") or "").strip()
            except Exception:
                continue
            if not order_id:
                continue

            key = f"{target}|{order_id}"
            if key in done:
                continue

            parent_jobs = retry_scheduler.get_jobs_for_parent(key)

            if not target_follow_enabled(settings, target):
                try:
                    result = {
                        "ts": int(now_ts()),
                        "ts_ms": int(now_ts_ms()),
                        "live": bool(is_live_backend_order(order)),
                        "member_id": "",
                        "member": "-",
                        "target": target,
                        "orderId": order_id,
                        "stake": 0.0,
                        "source_stake": _safe_float(order.get("stake"), 0.0),
                        "ok": False,
                        "message": "已按后台过滤跳过：该后台已设置禁跟。",
                        "skipped": True,
                        "skip_reason": "target_disabled",
                    }
                    append_jsonl(results_path, result)
                except Exception:
                    pass
                try:
                    done[key] = {"status": "skipped", "reason": "target_disabled", "ts": int(now_ts())}
                except Exception:
                    pass
                try:
                    pending_orders.pop(key, None)
                except Exception:
                    pass
                continue

            base_stake = compute_follow_stake(order, settings)
            if base_stake <= 0:
                continue

            total_stake, market_skip_msg, market_meta = apply_market_rule_to_stake(
                order=order,
                settings=settings,
                base_stake=base_stake,
            )
            if market_skip_msg:
                try:
                    result = {
                        "ts": int(now_ts()),
                        "ts_ms": int(now_ts_ms()),
                        "live": bool(is_live_backend_order(order)),
                        "member_id": "",
                        "member": "-",
                        "target": target,
                        "orderId": order_id,
                        "stake": 0.0,
                        "source_stake": _safe_float(order.get("stake"), 0.0),
                        "policy_base_stake": float(base_stake),
                        "market_code": str(market_meta.get("market_code") or ""),
                        "market_label": str(market_meta.get("market_label") or ""),
                        "policy_market_ratio_percent": _safe_float(market_meta.get("rule_ratio_percent"), 100.0),
                        "ok": False,
                        "message": str(market_skip_msg),
                        "skipped": True,
                        "skip_reason": f"market_rule:{str(market_meta.get('market_code') or 'unknown')}",
                    }
                    append_jsonl(results_path, result)
                except Exception:
                    pass
                try:
                    done[key] = {"status": "skipped", "reason": str(market_skip_msg), "ts": int(now_ts())}
                except Exception:
                    pass
                try:
                    pending_orders.pop(key, None)
                except Exception:
                    pass
                try:
                    bot_token, default_chat_id, proxy, order_preview_image, ui_theme = load_tg_config(Path("config.json"))
                    if bot_token:
                        order_chat_id = str(order.get("_tg_chat_id") or default_chat_id).strip()
                        order_msg_id = order.get("_tg_message_id")
                        if order_chat_id:
                            merged = build_order_follow_combined_message(order=order, follow_result=result, ui_theme=ui_theme)
                            new_mid = tg_upsert_order_message(
                                bot_token=bot_token,
                                chat_id=order_chat_id,
                                message_id=order_msg_id,
                                html_text=merged,
                                order_id=order_id,
                                order_preview_image=order_preview_image,
                                timeout_s=10,
                                proxy=proxy,
                                ui_theme=ui_theme,
                            )
                            if new_mid:
                                order["_tg_message_id"] = int(new_mid)
                                order["_tg_chat_id"] = order_chat_id
                except Exception:
                    pass
                continue

            if not parent_jobs:
                match_reserved, market_reserved = collect_active_limit_reservations(
                    retry_scheduler.retry_jobs,
                    settings=settings,
                    exclude_parent_key=key,
                )
                limited_stake, limit_skip_msg, limit_meta = apply_follow_amount_limits(
                    order=order,
                    settings=settings,
                    requested_stake=total_stake,
                    match_committed=limit_match_committed,
                    market_committed=limit_market_committed,
                    match_reserved=match_reserved,
                    market_reserved=market_reserved,
                    min_stake=split_min_stake_cfg,
                )
                store_follow_limit_metadata(order, limit_meta)
                if limit_skip_msg:
                    result = {
                        "ts": int(now_ts()),
                        "ts_ms": int(now_ts_ms()),
                        "live": bool(is_live_backend_order(order)),
                        "member_id": "",
                        "member": "-",
                        "target": target,
                        "orderId": order_id,
                        "stake": 0.0,
                        "source_stake": _safe_float(order.get("stake"), 0.0),
                        "policy_base_stake": float(base_stake),
                        "policy_market_stake": float(total_stake),
                        "ok": False,
                        "message": str(limit_skip_msg),
                        "skipped": True,
                        "skip_reason": "amount_limit",
                    }
                    attach_follow_limit_metadata(result, order, settings)
                    append_jsonl(results_path, result)
                    done[key] = {"status": "skipped", "reason": str(limit_skip_msg), "ts": int(now_ts())}
                    pending_orders.pop(key, None)
                    _append_trace_jsonl(
                        trace_path,
                        {
                            "event": "amount_limit_skipped",
                            "key": key,
                            "target": target,
                            "orderId": order_id,
                            "message": str(limit_skip_msg),
                            **{f"limit_{name}": value for name, value in limit_meta.items()},
                        },
                    )
                    try:
                        bot_token, default_chat_id, proxy, order_preview_image, ui_theme = load_tg_config(Path("config.json"))
                        if bot_token:
                            order_chat_id = str(order.get("_tg_chat_id") or default_chat_id).strip()
                            if order_chat_id:
                                merged = build_order_follow_combined_message(order=order, follow_result=result, ui_theme=ui_theme)
                                new_mid = tg_upsert_order_message(
                                    bot_token=bot_token,
                                    chat_id=order_chat_id,
                                    message_id=order.get("_tg_message_id"),
                                    html_text=merged,
                                    order_id=order_id,
                                    order_preview_image=order_preview_image,
                                    timeout_s=10,
                                    proxy=proxy,
                                    ui_theme=ui_theme,
                                )
                                if new_mid:
                                    order["_tg_message_id"] = int(new_mid)
                                    order["_tg_chat_id"] = order_chat_id
                    except Exception:
                        pass
                    continue
                total_stake = float(limited_stake)
                if bool(limit_meta.get("limited")):
                    _append_trace_jsonl(
                        trace_path,
                        {
                            "event": "amount_limit_applied",
                            "key": key,
                            "target": target,
                            "orderId": order_id,
                            "requested_stake": _safe_float(limit_meta.get("requested_stake"), 0.0),
                            "allowed_stake": float(total_stake),
                            "match_key": str(limit_meta.get("match_key") or ""),
                            "market_key": str(limit_meta.get("market_key") or ""),
                            "match_committed_amount": _safe_float(limit_meta.get("match_committed_amount"), 0.0),
                            "match_reserved_amount": _safe_float(limit_meta.get("match_reserved_amount"), 0.0),
                            "market_committed_amount": _safe_float(limit_meta.get("market_committed_amount"), 0.0),
                            "market_reserved_amount": _safe_float(limit_meta.get("market_reserved_amount"), 0.0),
                        },
                    )

            # per-target 过滤：仅跟滚球
            try:
                only_live = bool(target_only_live(settings, target) or False)
                live = bool(is_live_backend_order(order))
            except Exception:
                only_live = False
                live = False
            if only_live and (not live):
                # 记录“已跳过”，避免 UI/TG 长期显示等待中
                try:
                    result = {
                        "ts": int(now_ts()),
                        "ts_ms": int(now_ts_ms()),
                        "live": bool(live),
                        "member_id": "",
                        "member": "-",
                        "target": target,
                        "orderId": order_id,
                        "stake": 0.0,
                        "source_stake": _safe_float(order.get("stake"), 0.0),
                        "ok": False,
                        "message": "已跳过非滚球订单（该后台已设置：只跟滚球订单）。",
                        "skipped": True,
                        "skip_reason": "not_live",
                    }
                    append_jsonl(results_path, result)
                except Exception:
                    pass
                try:
                    done[key] = {"status": "skipped", "reason": "not_live", "ts": int(now_ts())}
                except Exception:
                    pass
                try:
                    pending_orders.pop(key, None)
                except Exception:
                    pass
                # 同步到 TG（更新原订单消息，避免误会“等待中”）
                try:
                    bot_token, default_chat_id, proxy, order_preview_image, ui_theme = load_tg_config(Path("config.json"))
                    if bot_token:
                        order_chat_id = str(order.get("_tg_chat_id") or default_chat_id).strip()
                        order_msg_id = order.get("_tg_message_id")
                        if order_chat_id:
                            merged = build_order_follow_combined_message(order=order, follow_result=result, ui_theme=ui_theme)
                            new_mid = tg_upsert_order_message(
                                bot_token=bot_token,
                                chat_id=order_chat_id,
                                message_id=order_msg_id,
                                html_text=merged,
                                order_id=order_id,
                                order_preview_image=order_preview_image,
                                timeout_s=10,
                                proxy=proxy,
                                ui_theme=ui_theme,
                            )
                            if new_mid:
                                order["_tg_message_id"] = int(new_mid)
                                order["_tg_chat_id"] = order_chat_id
                except Exception:
                    pass
                continue

            if not parent_jobs:
                eff_split = effective_split_count(total_stake, split_count_cfg, split_min_stake_cfg)
                parts = split_stakes(total_stake, eff_split)
                if not parts:
                    continue
                # 便于排查“按比例却算出 35/金额不对”等：打印一次计算明细（仅在创建任务时输出）。
                try:
                    src_stake = float(order.get("stake") or 0)
                except Exception:
                    src_stake = 0.0
                try:
                    parts_preview = parts if len(parts) <= 6 else (parts[:6] + ["..."])  # type: ignore[list-item]
                except Exception:
                    parts_preview = parts
                print(
                    f"[{now_iso()}] stake_calc target={target} order={order_id} src_stake={src_stake} "
                    f"mode={settings.get('mode')} ratio%={settings.get('ratio_percent')} fixed={settings.get('fixed_amount')} "
                    f"split={eff_split} total={float(sum([p for p in parts if isinstance(p,(int,float))]))} "
                    f"parts={parts_preview}",
                    flush=True,
                )
                created_at = float(now_ts())
                new_jobs = build_independent_split_jobs(
                    parent_key=key,
                    order_id=order_id,
                    target=target,
                    total_stake=float(sum(parts)),
                    order=order,
                    parts=parts,
                    created_at=created_at,
                    split_delay_seconds=float(split_delay_seconds_cfg),
                )
                for new_job in new_jobs:
                    if new_job.key not in done:
                        retry_scheduler.add_job(new_job)
                _append_trace_jsonl(
                    trace_path,
                    {
                        "event": "job_created",
                        "key": key,
                        "target": target,
                        "orderId": order_id,
                        "source_stake": src_stake,
                        "total_stake": float(sum(parts)),
                        "split_count": int(len(parts) or 1),
                        "split_delay_seconds": float(split_delay_seconds_cfg),
                    },
                )
                if args.debug:
                    print(
                        f"[{now_iso()}] 入队跟单任务 target={target} order={order_id} stake={float(sum(parts))} split={len(parts)} delay={float(split_delay_seconds_cfg)} min_stake={float(split_min_stake_cfg)}",
                        flush=True,
                    )
            elif parent_jobs:
                # 已入队任务保留创建时的金额和额度预留，避免运行中改设置导致任务被放大。
                for parent_job in parent_jobs:
                    parent_job.order = order

        # 处理待重试队列；每个真实 Attempt 都重新读取最新 Snapshot。
        now = float(now_ts())
        one_shot_settings = dict(settings)
        one_shot_settings["retry_max_attempts"] = 1
        one_shot_settings["retry_delay_seconds"] = 0
        one_shot_settings["retry_max_seconds"] = 1

        # 平台维护/恢复验证期间，订单继续保留在队列，但禁止任何新的
        # Snapshot、FT_order_view 或 FT_bet 请求。维护状态由后台 watcher
        # 通过 data/platform_maintenance.json 共享；恢复为 online 后自动放行。
        platform_state = read_platform_state(data_dir)
        platform_status = str(platform_state.get("status") or "").strip().lower()
        platform_paused = platform_status in {"maintenance", "recovering"}
        if platform_paused:
            pause_message = maintenance_status_message(platform_state)
            for paused_job in list(retry_scheduler.retry_jobs.values()):
                if not isinstance(paused_job, RetryJob):
                    continue
                # UNKNOWN_RESULT 代表下注请求可能已经到达平台，必须保留原会员
                # 和核验状态；维护期间只延后核验，绝不能把它改成普通重试。
                if str(paused_job.last_err or "").strip().upper() == UNKNOWN_RESULT:
                    paused_job.next_try_at = float(now_ts()) + 5.0
                    paused_job.order["_platform_maintenance_paused"] = True
                    continue
                already_paused = str(paused_job.last_err or "").strip().upper() == PLATFORM_MAINTENANCE_CODE
                paused_job.last_err = PLATFORM_MAINTENANCE_CODE
                paused_job.last_message = pause_message
                paused_job.member_id = ""
                paused_job.next_try_at = float(now_ts()) + 5.0
                paused_job.order["_platform_maintenance_paused"] = True
                if not already_paused:
                    _enqueue_follow_notification(
                        paused_job,
                        {
                            "ts": int(now_ts()),
                            "ts_ms": int(now_ts_ms()),
                            "target": paused_job.target,
                            "orderId": paused_job.order_id,
                            "stake": float(paused_job.stake or 0.0),
                            "member": "-",
                            "member_id": "",
                            "ok": False,
                            "status": "maintenance",
                            "error_code": PLATFORM_MAINTENANCE_CODE,
                            "message": pause_message,
                            "split_index": int(paused_job.split_index or 0) + 1,
                            "split_count": int(paused_job.split_count or 1),
                        },
                    )
            _persist_follow_state()
            time.sleep(max(0.5, min(5.0, float(args.interval))))
            continue

        # 维护结束并确认平台在线后，清除暂停标记，让原订单从当前盘口重新
        # 准备；不增加 attempts，也不把维护等待算作失败。
        if platform_status == "online":
            for resumed_job in list(retry_scheduler.retry_jobs.values()):
                if not isinstance(resumed_job, RetryJob):
                    continue
                if str(resumed_job.last_err or "").strip().upper() != PLATFORM_MAINTENANCE_CODE:
                    continue
                resumed_job.last_err = ""
                resumed_job.last_message = ""
                resumed_job.member_id = ""
                resumed_job.order.pop("_platform_maintenance_paused", None)
                resumed_job.next_try_at = float(now_ts())

        processed = 0
        used_member_ids: set = set()
        ready_jobs = retry_scheduler.get_ready_jobs(
            now=float(now),
            exclude_keys=set(parallel_inflight),
        )

        for job in ready_jobs:
            if processed >= queue_max_per_tick:
                break
            # 并发执行模式下没有空槽时只让出本轮调度，绝不能落入下面的
            # 同步 place_bet 路径，否则一个慢请求会把整个队列卡住几十秒。
            if parallel_attempts > 1 and attempt_dispatcher.available_slots() <= 0:
                break

            if not str(job.trigger_reason or "").strip():
                job.trigger_reason = "INITIAL_READY" if not str(job.last_err or "").strip() else "FALLBACK_PROBE"

            # UNKNOWN_RESULT 已进入专用 reconciliation 状态后，普通提交
            # attempt/time cap 不得越权终止。专用核验窗口（次数/时间）
            # 由 _reconcile_only_result 单独决定最终状态。
            in_unknown_result_wait = str(job.last_err or "").strip().upper() == UNKNOWN_RESULT

            # 截止：超过限制则终止该单重试（1X001 使用“封盘等待策略”，走自己的超时控制）
            eff_max_attempts, eff_max_seconds = retry_scheduler.get_effective_limits(
                job,
                retry_max_attempts=int(retry_max_attempts),
                retry_max_seconds=float(retry_max_seconds),
                retry_max_attempts_on_1x001=int(retry_max_attempts_on_1x001),
                retry_max_seconds_on_1x001=float(retry_max_seconds_on_1x001),
                wait_1x001_max=float(wait_1x001_max or 0.0),
                wait_line_not_found_max=float(wait_line_not_found_max or 0.0),
            )
            wait_timeout_reason = retry_scheduler.check_wait_timeout(job, now=float(now))
            if wait_timeout_reason:
                _finish_terminal_job(job, {"status": "failed", "reason": wait_timeout_reason, "ts": int(now_ts()), "split_index": int(job.split_index or 0)})
                if str(job.last_err or "") == "1X001":
                    print(f"[{now_iso()}] 跟单失败(封盘等待超时) member={job.member_id or '-'} order={job.order_id}", flush=True)
                elif str(job.last_err or "") == "LINE_NOT_FOUND":
                    print(f"[{now_iso()}] 跟单失败(目标盘口线等待超时) member={job.member_id or '-'} order={job.order_id}", flush=True)
                continue

            if (not in_unknown_result_wait) and eff_max_attempts and job.attempts >= eff_max_attempts:
                _finish_terminal_job(job, {"status": "failed", "reason": "重试次数已达上限", "ts": int(now_ts()), "split_index": int(job.split_index or 0)})
                continue
            if (not in_unknown_result_wait) and eff_max_seconds and (now - float(job.created_at or now)) >= eff_max_seconds:
                _finish_terminal_job(job, {"status": "failed", "reason": "重试已超时", "ts": int(now_ts()), "split_index": int(job.split_index or 0)})
                continue

            # 当前分单金额（顺序执行）
            cur_stake = float(job.stake)
            if job.split_stakes and 0 <= int(job.split_index) < len(job.split_stakes):
                cur_stake = float(job.split_stakes[int(job.split_index)])
            if cur_stake <= 0:
                _finish_terminal_job(job, {"status": "failed", "reason": "分单金额为0", "ts": int(now_ts()), "split_index": int(job.split_index or 0)})
                continue

            last_job_err = str(job.last_err or "").strip().upper()
            # 只有 UNKNOWN_RESULT 必须固定原会员做 reconcile。盘口等待不占用
            # 会员；恢复后重新从健康空闲池动态获取执行会员。
            in_unknown_result_wait = last_job_err == UNKNOWN_RESULT
            in_pinned_wait = in_unknown_result_wait
            if in_unknown_result_wait:
                # 给 worker 一个明确的 reconcile-only 标记；即使进程重启，
                # job.order 中的上下文也会阻止再次调用 FT_bet。
                job.order["_reconcile_only"] = True
                job.order["_unknown_result_started_at"] = float(getattr(job, "unknown_result_started_at", 0.0) or 0.0)
                job.order["_unknown_result_deadline_at"] = float(getattr(job, "unknown_result_deadline_at", 0.0) or 0.0)
                job.order["_unknown_result_check_count"] = int(getattr(job, "unknown_result_check_count", 0) or 0)

            # 盘口厨房先准备参数，会员池后租用。Snapshot 未就绪时不选择、不占用会员。
            if not in_unknown_result_wait:
                reject_retry_not_before = _safe_float(job.order.get("_market_reject_retry_not_before"), 0.0)
                if reject_retry_not_before > float(now):
                    job.next_try_at = reject_retry_not_before
                    continue
                job.order.pop("_market_reject_retry_not_before", None)
                probe_settings = dict(one_shot_settings)
                probe_settings["_context_retry"] = bool(last_job_err == CONTEXT_RETRYABLE)
                probe_settings["_context_retry_fingerprint"] = str(job.order.get("_context_retry_fingerprint") or "")
                probe = _probe_snapshot_market(
                    data_dir=data_dir,
                    settings=probe_settings,
                    order=job.order,
                    stake=float(cur_stake),
                )
                required_revision = _safe_int(job.order.get("_wait_for_snapshot_revision"), 0)
                current_revision = _safe_int(probe.get("revision"), 0)
                waiting_same_revision = bool(
                    probe.get("ready")
                    and required_revision > 0
                    and current_revision <= required_revision
                    and last_job_err != CONTEXT_RETRYABLE
                )
                if not bool(probe.get("ready")) or waiting_same_revision:
                    status = MARKET_ERROR if waiting_same_revision else str(probe.get("status") or SNAPSHOT_MISSING)
                    message = (
                        f"盘口变化，等待新 Snapshot（当前 revision={current_revision}）。"
                        if waiting_same_revision
                        else str(probe.get("message") or _snapshot_status_message(status))
                    )
                    job.last_err = status
                    job.last_message = message
                    job.member_id = ""
                    job.snapshot_wait_observed_revision = current_revision
                    try:
                        observed_key = _job_active_market_key(job, one_shot_settings)
                        observed_event = snapshot_latest_publish_events.get(observed_key) or {}
                        job.snapshot_wait_observed_event_seq = _safe_int(observed_event.get("event_seq"), 0)
                    except Exception:
                        job.snapshot_wait_observed_event_seq = 0
                    job.market_wait_count = int(job.market_wait_count or 0) + 1
                    now_wait = float(now_ts())
                    retry_scheduler.schedule_wait_by_err(
                        job,
                        err=status,
                        now=now_wait,
                        wait_max=float(wait_line_not_found_max or 0.0),
                        probe_seconds=float(wait_line_not_found_probe or 1.0),
                    )
                    _append_trace_jsonl(
                        trace_path,
                        {
                            "event": "waiting_market",
                            "execution_path": str(one_shot_settings.get("execution_mode") or "snapshot_direct"),
                            "real_submission": "none",
                            "key": job.key,
                            "target": job.target,
                            "orderId": job.order_id,
                            "split_index": int(job.split_index or 0) + 1,
                            "split_count": int(job.split_count or 1),
                            "snapshot_status": status,
                            "snapshot_revision": current_revision,
                            "market_wait_count": int(job.market_wait_count or 0),
                            "message": message,
                        },
                    )
                    continue
                job.order.pop("_wait_for_snapshot_revision", None)
                job.line_not_found_wait_started_at = 0.0
                job.line_not_found_wait_deadline_at = 0.0
                candidate_detected_at = float(now_ts())
                job.candidate_detected_at = candidate_detected_at
                if float(job.job_marked_runnable_at or 0.0) <= 0.0:
                    job.job_marked_runnable_at = candidate_detected_at
                job.runnable_queue_entered_at = float(now_ts())
                if not float(job.first_candidate_snapshot_at or 0.0):
                    job.first_candidate_snapshot_at = float(probe.get("fetched_at") or now_ts())
            else:
                # Reconcile is a real network operation and consumes one
                # scheduler slot; pure market/member waits below do not.
                processed += 1

            attempt_no = int(job.attempts or 0) + 1
            dispatcher_selected_at = float(now_ts())
            job.dispatcher_selected_at = dispatcher_selected_at

            m = None
            in_member_quote_wait = last_job_err in {
                MEMBER_IORATIO_MISSING,
                MEMBER_IORATIO_STALE,
                MEMBER_IORATIO_INVALID,
            }
            # UNKNOWN_RESULT 核验期间固定使用原会员，严禁换号再次提交。
            if in_pinned_wait and job.member_id:
                pinned_id = str(job.member_id)
                pinned = next((mm for mm in members if str(mm.get("id") or "") == pinned_id), None)
                if pinned and all(str(pinned.get(key) or "").strip() for key in ("base_url", "ver", "uid", "cookie")):
                    # reconcile 不是再次下注；即使状态刚转离线，也必须用原会员核验。
                    m = pinned

            # Quote freshness is member-specific.  Keep the selected healthy
            # member while the kitchen priority-refresh request is in flight;
            # switching immediately would create a revision/quote chase across
            # the whole pool.  If that member is no longer usable, normal pool
            # selection below will fall back to another member.
            if (not m) and in_member_quote_wait and job.member_id:
                pinned_id = str(job.member_id)
                pinned = next((mm for mm in members if str(mm.get("id") or "") == pinned_id), None)
                if pinned and all(str(pinned.get(key) or "").strip() for key in ("base_url", "ver", "uid", "cookie")):
                    m = pick_random_member_for_stake(
                        [pinned],
                        float(cur_stake),
                        avoid_ids=set(),
                        target=job.target,
                        mappings=mappings,
                        allow_quote_members=bool(allow_quote_members),
                    )

            if (not m) and str(job.last_err or "") == STAKE_EXCEEDS_MAX_ERR and job.member_id:
                pinned_id = str(job.member_id)
                pinned = next((mm for mm in members if str(mm.get("id") or "") == pinned_id), None)
                if pinned:
                    m = pick_random_member_for_stake(
                        [pinned],
                        float(cur_stake),
                        avoid_ids=set(),
                        target=job.target,
                        mappings=mappings,
                        allow_quote_members=bool(allow_quote_members),
                    )

            # 先排除已经在网络请求中的会员。submit() 仍会做最终原子校验，
            # 但这里提前过滤可以避免选中忙会员后误走同步执行分支。
            busy_member_ids = attempt_dispatcher.saturated_member_ids() if parallel_attempts > 1 else set()
            avoid = set(used_member_ids) | set(busy_member_ids)
            if job.member_id and (not in_pinned_wait) and (not in_member_quote_wait):
                avoid.add(str(job.member_id))
            for mid in (job.used_member_ids or []):
                avoid.add(str(mid))
            if not m:
                m = pick_random_member_for_stake(
                    members,
                    float(cur_stake),
                    avoid_ids=avoid,
                    target=job.target,
                    mappings=mappings,
                    allow_quote_members=bool(allow_quote_members),
                )
            # 分单场景优先不重复会员；如果实在找不到可用会员，则允许复用已用过的会员继续分单
            if (not m) and ((job.used_member_ids or []) or used_member_ids):
                # 第一轮优先把任务铺到不同会员；没有新会员时放宽本轮
                # 去重，只保留真正达到并发上限的会员。
                avoid2 = set(busy_member_ids)
                if job.member_id and (not in_pinned_wait) and (not in_member_quote_wait):
                    avoid2.add(str(job.member_id))
                m = pick_random_member_for_stake(
                    members,
                    float(cur_stake),
                    avoid_ids=avoid2,
                    target=job.target,
                    mappings=mappings,
                    allow_quote_members=bool(allow_quote_members),
                )
            # 重试场景：如果没有其它可用会员，但本单之前已选中过某个会员，允许复用该会员继续重试。
            # 否则会出现：第一次失败后进入重试，但因为“避免复用”导致永远挑不到会员，Telegram/UI 显示 member='-'。
            if (not m) and job.member_id and (not in_pinned_wait) and (not in_member_quote_wait):
                try:
                    # 忙会员不能在“放宽复用”时重新进入候选池。
                    relax = set(avoid) | set(busy_member_ids)
                    relax.discard(str(job.member_id))
                    relax.update(set(busy_member_ids))
                    m = pick_random_member_for_stake(
                        members,
                        float(cur_stake),
                        avoid_ids=relax,
                        target=job.target,
                        mappings=mappings,
                        allow_quote_members=bool(allow_quote_members),
                    )
                except Exception:
                    m = None

            # 最终 dispatch gate：候选池是在本轮开始时读取的，而 member
            # watcher/上一笔 FT_bet 可能刚刚把会员写成离线或
            # busy。此处必须重新读取持久化快照，禁止沿用旧 member dict。
            # UNKNOWN_RESULT 是固定原会员核验，允许使用原会员的最新会话
            # 信息，但绝不会重新 FT_bet。
            latest_members = _load_current_members(data_dir)
            members = latest_members
            selected_id = str((m or {}).get("id") or "").strip()
            latest_by_id = {
                str(mm.get("id") or "").strip(): mm
                for mm in latest_members
                if str(mm.get("id") or "").strip()
            }
            if in_pinned_wait:
                # 固定原会员做 reconciliation；状态/余额变化不应
                # 把一次已发出的 UNKNOWN_RESULT 变成换号下注。
                m = latest_by_id.get(str(job.member_id or "").strip())
            else:
                fresh_selected = latest_by_id.get(selected_id) if selected_id else None
                fresh_selected_ok = bool(
                    fresh_selected
                    and _member_eligible_for_stake(
                        fresh_selected,
                        stake=float(cur_stake),
                        avoid_ids=set(),
                        target=job.target,
                        mappings=mappings,
                    )
                    and selected_id not in busy_member_ids
                )
                if fresh_selected_ok:
                    m = fresh_selected
                else:
                    # 候选在 acquire 与 dispatch 之间变为不可用时，从
                    # 同一份最新快照重选，不能退回旧 members 列表。
                    fresh_avoid = set(avoid) | set(busy_member_ids)
                    if selected_id:
                        fresh_avoid.add(selected_id)
                    m = pick_random_member_for_stake(
                        latest_members,
                        float(cur_stake),
                        avoid_ids=fresh_avoid,
                        target=job.target,
                        mappings=mappings,
                        allow_quote_members=bool(allow_quote_members),
                    )
                    if not m and job.member_id:
                        # 只有当原会员在最新快照中仍可用时才允许重试复用。
                        fresh_previous = latest_by_id.get(str(job.member_id or "").strip())
                        if fresh_previous and _member_eligible_for_stake(
                            fresh_previous,
                            stake=float(cur_stake),
                            avoid_ids=set(busy_member_ids),
                            target=job.target,
                            mappings=mappings,
                        ):
                            m = fresh_previous

            # 再做一次极窄的持久化状态检查，覆盖 member watcher 恰好在
            # 上一份快照之后更新状态的竞态。若无健康替代会员，
            # 明确进入 MEMBER_WAIT，绝不把旧候选送入 Context。
            if m and not in_pinned_wait:
                dispatch_members = _load_current_members(data_dir)
                dispatch_by_id = {
                    str(mm.get("id") or "").strip(): mm
                    for mm in dispatch_members
                    if str(mm.get("id") or "").strip()
                }
                dispatch_member = dispatch_by_id.get(str(m.get("id") or "").strip())
                if dispatch_member and _member_eligible_for_stake(
                    dispatch_member,
                    stake=float(cur_stake),
                    avoid_ids=set(busy_member_ids),
                    target=job.target,
                    mappings=mappings,
                ):
                    m = dispatch_member
                    members = dispatch_members or members
                else:
                    rejected_id = str((m or {}).get("id") or "").strip()
                    replacement = pick_random_member_for_stake(
                        dispatch_members,
                        float(cur_stake),
                        avoid_ids=set(avoid) | set(busy_member_ids) | ({rejected_id} if rejected_id else set()),
                        target=job.target,
                        mappings=mappings,
                        allow_quote_members=bool(allow_quote_members),
                    )
                    m = replacement
                    members = dispatch_members or members
                    if m:
                        _append_trace_jsonl(
                            trace_path,
                            {
                                "event": "member_dispatch_rejected_stale_state",
                                "key": job.key,
                                "orderId": job.order_id,
                                "rejected_member": rejected_id,
                                "replacement_member": str(m.get("id") or ""),
                                "reason": "unavailable_before_dispatch",
                            },
                        )
            if not m:
                try:
                    job.last_attempted_at = float(now_ts())
                except Exception:
                    job.last_attempted_at = float(now_ts())
                # UNKNOWN_RESULT reconciliation 必须固定原会员；即使
                # 该会员暂时不可读/不可用，也不能清空 pinned member_id，
                # 否则下一轮无法继续在原会员上核验。
                if not in_pinned_wait:
                    job.member_id = ""
                if busy_member_ids:
                    job.last_message = f"执行会员暂时忙碌，已排队等待（当前 {len(busy_member_ids)} 个执行中）"
                else:
                    job.last_message = f"无可用会员：可能余额不足/离线/映射不匹配（需要 {int(cur_stake)}）"
                job.member_wait_count = int(job.member_wait_count or 0) + 1
                # 这是容量等待，不是一次下注尝试；不递增 attempts，避免
                # 会员短暂忙碌时把重试次数虚耗掉。稍短退避让出 CPU 和队列顺序。
                job.next_try_at = float(now_ts()) + (0.25 if busy_member_ids else 1.0)
                print(f"[{now_iso()}] 跟单等待 member=none order={job.order_id} msg={job.last_message}", flush=True)
                # 状态提示也走异步队列，不能阻塞下一次 Snapshot/会员调度。
                pseudo = {
                    "ts": int(now_ts()),
                    "ts_ms": int(now_ts_ms()),
                    "live": bool(is_live_backend_order(job.order)),
                    "member": "-",
                    "member_id": "",
                    "target": job.target,
                    "orderId": job.order_id,
                    "stake": float(cur_stake),
                    "ok": False,
                    "message": job.last_message,
                    "attempt": int(job.attempts or 0),
                    "split_index": int(job.split_index or 0) + 1,
                    "split_count": int(job.split_count or 1),
                }
                if int(job.split_count or 1) > 1:
                    try:
                        idx0 = int(job.split_index or 0)
                    except Exception:
                        idx0 = 0
                    done_total = 0.0
                    for rec in (job.split_part_results or {}).values():
                        try:
                            if bool(rec.get("ok")):
                                done_total += float(rec.get("stake") or 0)
                        except Exception:
                            pass
                    parts_map: Dict[int, Dict[str, Any]] = dict(job.split_part_results or {})
                    parts_map[idx0] = {
                        "state": "waiting",
                        "part": idx0 + 1,
                        "ok": False,
                        "attempt": int(job.attempts or 0),
                        "member": "-",
                        "stake": float(cur_stake),
                        "ticket_id": "",
                        "message": str(job.last_message or ""),
                        "ts": int(now_ts()),
                        "params": {"ioratio": str(job.order.get("odds") or "")},
                    }
                    pseudo["split_parts"] = [parts_map[k] for k in sorted(parts_map)]
                    pseudo["split_total_plan"] = float(job.stake or 0.0)
                    pseudo["split_total_done"] = float(done_total)
                _enqueue_follow_notification(job, pseudo)
                continue

            used_member_ids.add(str(m.get("id") or ""))
            job.member_id = str(m.get("id") or "")
            member_acquired_at = float(now_ts())
            job.member_acquired_at = member_acquired_at

            m2 = ensure_member_session(data_dir=data_dir, member=m, debug=bool(args.debug))
            if parallel_attempts > 1 and attempt_dispatcher.active_count() < parallel_attempts:
                # 把网络 attempt 放到有界线程池；当前线程只负责调度，结果由下一轮统一回收。
                attempt_no_async = int(job.attempts or 0) + 1
                timeout_async = int(args.timeout)
                if _member_has_transient_network_issue(m2):
                    timeout_async = max(3, min(timeout_async, 6))
                hard_async = int(one_shot_settings.get("attempt_hard_timeout_s") or 0)
                if hard_async <= 0:
                    hard_async = max(15, min(120, int(one_shot_settings.get("one_shot_timeout_s") or 8) * 4))
                settings_async = dict(one_shot_settings)
                settings_async["_reconcile_only"] = bool(in_unknown_result_wait)
                settings_async["_context_retry"] = bool(last_job_err == CONTEXT_RETRYABLE)
                settings_async["_context_retry_fingerprint"] = str(job.order.get("_context_retry_fingerprint") or "")
                future_async = attempt_dispatcher.submit(
                    job.key,
                    str(m2.get("id") or ""),
                    _run_parallel_place_bet,
                    member_parallel_limit=per_member_parallel_attempts,
                    data_dir=data_dir,
                    settings=settings_async,
                    member=m2,
                    order=job.order,
                    stake=float(cur_stake),
                    timeout_s=int(timeout_async),
                    debug=bool(args.debug),
                    session_pool=session_pool,
                    hard_timeout_s=int(hard_async),
                )
                if future_async is not None:
                    processed += 1
                    job.attempts = attempt_no_async
                    job.last_attempted_at = float(now_ts())
                    job.attempt_started_at = float(now_ts())
                    if not float(job.first_candidate_snapshot_at or 0.0):
                        job.first_candidate_snapshot_at = float(now_ts())
                    _append_trace_jsonl(
                        trace_path,
                        {
                            "event": "attempt_start",
                            "execution_path": str(one_shot_settings.get("execution_mode") or "snapshot_direct"),
                            "real_submission": "reconcile_only" if in_unknown_result_wait else str(one_shot_settings.get("execution_mode") or "snapshot_direct"),
                            "key": job.key,
                            "target": job.target,
                            "orderId": job.order_id,
                            "order_id": job.order_id,
                            "split_order_id": job.key,
                            "attempt": int(attempt_no_async),
                            "attempt_no": int(attempt_no_async),
                            "split_index": int(job.split_index or 0) + 1,
                            "split_count": int(job.split_count or 1),
                            "elapsed_s": round(float(now_ts()) - float(job.created_at or 0.0), 3),
                            "market_wait_count": int(job.market_wait_count or 0),
                            "member_wait_count": int(job.member_wait_count or 0),
                            "execution_member": str(m2.get("id") or ""),
                        },
                    )
                    job.next_try_at = float(now_ts()) + float(hard_async)
                    parallel_inflight[job.key] = {
                        "job": job,
                        "attempt_no": attempt_no_async,
                        "member_id": str(m2.get("id") or ""),
                        "member_acquired_at": member_acquired_at,
                        "dispatcher_selected_at": dispatcher_selected_at,
                        "attempt_started_at": float(job.attempt_started_at or now_ts()),
                        "candidate_detected_at": float(job.candidate_detected_at or job.first_candidate_snapshot_at or 0.0),
                        "job_marked_runnable_at": float(job.job_marked_runnable_at or 0.0),
                        "runnable_queue_entered_at": float(job.runnable_queue_entered_at or 0.0),
                        "trigger_reason": str(job.trigger_reason or "INITIAL_READY"),
                        "trigger_event_seq": int(job.trigger_event_seq or 0),
                        "trigger_publish_at": float(job.trigger_publish_at or 0.0),
                        "trigger_wakeup_at": float(job.trigger_wakeup_at or 0.0),
                        "candidate_snapshot_at": float(job.first_candidate_snapshot_at or 0.0),
                        "member_label": str(m2.get("label") or m2.get("username") or ""),
                        "cur_stake": float(cur_stake),
                        "wait_1x001_max": float(wait_1x001_max),
                        "wait_1x001_probe": float(wait_1x001_probe),
                        "wait_line_not_found_max": float(wait_line_not_found_max),
                        "wait_line_not_found_probe": float(wait_line_not_found_probe),
                        "retry_delay": float(retry_delay_seconds),
                        "market_reject_max_attempts": int(market_reject_max_attempts),
                        "market_reject_retry_delay": float(market_reject_retry_delay),
                        "split_min_stake": float(split_min_stake_cfg),
                    }
                    job.trigger_reason = ""
                    job.trigger_event_seq = 0
                    job.trigger_publish_at = 0.0
                    job.trigger_wakeup_at = 0.0
                    job.job_marked_runnable_at = 0.0
                    job.runnable_queue_entered_at = 0.0
                    _append_trace_jsonl(
                        trace_path,
                        {"event": "attempt_submitted", "key": job.key, "orderId": job.order_id, "attempt": attempt_no_async, "member": str(m2.get("id") or "")},
                    )
                    continue
                # submit() 可能在选员与提交之间观察到会员刚变忙，或并发槽位刚被
                # 其它任务占用。此时必须把任务放回队列，不能继续走同步 place_bet。
                job.last_message = "等待执行会员空闲，已排队"
                job.member_wait_count = int(job.member_wait_count or 0) + 1
                job.next_try_at = float(now_ts()) + 0.25
                _append_trace_jsonl(
                    trace_path,
                    {
                        "event": "attempt_deferred",
                        "key": job.key,
                        "target": job.target,
                        "orderId": job.order_id,
                        "reason": "member_or_slot_busy",
                        "member": str(m2.get("id") or ""),
                    },
                )
                continue
            prev_err = str(job.last_err or "")
            processed += 1
            try:
                job.last_attempted_at = float(now_ts())
            except Exception:
                job.last_attempted_at = float(now_ts())
            t_attempt0 = time.monotonic()
            job.attempt_started_at = float(now_ts())
            if not float(job.first_candidate_snapshot_at or 0.0):
                job.first_candidate_snapshot_at = float(now_ts())
            _append_trace_jsonl(
                trace_path,
                {
                    "event": "reconcile_start" if in_unknown_result_wait else "attempt_start",
                    "execution_path": str(one_shot_settings.get("execution_mode") or "snapshot_direct"),
                    "real_submission": "reconcile_only" if in_unknown_result_wait else str(one_shot_settings.get("execution_mode") or "snapshot_direct"),
                    "key": job.key,
                    "target": job.target,
                    "orderId": job.order_id,
                    "order_id": job.order_id,
                    "split_order_id": job.key,
                    "attempt": int(attempt_no),
                    "attempt_no": int(attempt_no),
                    "split_index": int(job.split_index or 0) + 1,
                    "split_count": int(job.split_count or 1),
                    "elapsed_s": round(float(now_ts()) - float(job.created_at or 0.0), 3),
                    "market_wait_count": int(job.market_wait_count or 0),
                    "member_wait_count": int(job.member_wait_count or 0),
                    "execution_member": str(m2.get("id") or ""),
                    "trigger_reason": str(job.trigger_reason or "INITIAL_READY"),
                    "trigger_event_seq": int(job.trigger_event_seq or 0),
                },
            )
            settings_run = dict(one_shot_settings)
            settings_run["_reconcile_only"] = bool(in_unknown_result_wait)
            settings_run["_context_retry"] = bool(last_job_err == CONTEXT_RETRYABLE)
            settings_run["_context_retry_fingerprint"] = str(job.order.get("_context_retry_fingerprint") or "")
            timeout_run = int(args.timeout)
            if _member_has_transient_network_issue(m2):
                # 对“刚发生网络抖动”的会员本轮快速失败，尽快换到其它会员，避免单笔订单被慢请求拖住。
                timeout_run = max(3, min(int(timeout_run), 6))
            attempt_hard_timeout_s = int(one_shot_settings.get("attempt_hard_timeout_s") or 0)
            if attempt_hard_timeout_s <= 0:
                # 默认：one-shot 下把“单次 attempt 总耗时”限制在几十秒以内，避免你看到“卡几分钟”
                try:
                    base_timeout = int(one_shot_settings.get("one_shot_timeout_s") or 8)
                except Exception:
                    base_timeout = 8
                attempt_hard_timeout_s = max(15, min(120, int(base_timeout) * 4))
            if _member_has_transient_network_issue(m2):
                attempt_hard_timeout_s = max(8, min(int(attempt_hard_timeout_s), int(timeout_run) * 2 + 4))
            try:
                with _hard_timeout(int(attempt_hard_timeout_s)):
                    result = place_bet(
                        data_dir=data_dir,
                        settings=settings_run,
                        member=m2,
                        order=job.order,
                        stake=cur_stake,
                        timeout_s=int(timeout_run),
                        debug=bool(args.debug),
                        session_pool=session_pool,
                    )
            except (TimeoutError, HardTimeout):
                # 注意：这里是“单次 attempt 级别”的硬超时（不是单个接口超时）
                # 目的：不让任何一笔 attempt 把整个队列拖到分钟级。
                result = {
                    "ts": int(now_ts()),
                    "ts_ms": int(now_ts_ms()),
                    "live": bool(is_live_backend_order(job.order)),
                    "member_id": str(m2.get("id") or ""),
                    "member": str(m2.get("label") or m2.get("username") or m2.get("id") or ""),
                    "target": str(job.target or ""),
                    "orderId": str(job.order_id or ""),
                    "order_id": str(job.order_id or ""),
                    "stake": float(cur_stake),
                    "source_stake": _safe_float(job.order.get("stake"), 0.0),
                    "settings_mode": str(one_shot_settings.get("mode") or ""),
                    "settings_ratio_percent": _safe_float(one_shot_settings.get("ratio_percent"), 0.0),
                    "settings_fixed_amount": _safe_float(one_shot_settings.get("fixed_amount"), 0.0),
                    "ok": False,
                    "message": "FT_bet 结果不确定，正在核验，禁止重复提交。",
                    "execution_mode": str(one_shot_settings.get("execution_mode") or "snapshot_direct"),
                    "execution_path": str(one_shot_settings.get("execution_mode") or "snapshot_direct"),
                    "real_submission": str(one_shot_settings.get("execution_mode") or "snapshot_direct"),
                    # 外层硬超时无法判断 FT_bet 是否已发出，先进入核验等待，
                    # 防止下一轮立即更换会员重复下注。
                    "snapshot_direct_result_status": UNKNOWN_RESULT,
                    "error_code": UNKNOWN_RESULT,
                    "error_type": "HardTimeout",
                    "reconcile_pending": True,
                    "unknown_result": True,
                    "result_status": UNKNOWN_RESULT,
                    "bet_submitted_at": float(now_ts()),
                    "timings": {},
                }
            try:
                wall_s = round(float(time.monotonic() - float(t_attempt0)), 3)
            except Exception:
                wall_s = None
            try:
                tmap = result.get("timings") if isinstance(result.get("timings"), dict) else {}
                if wall_s is not None:
                    tmap["attempt_wall_s"] = float(wall_s)
                tmap["attempt_hard_timeout_s"] = float(attempt_hard_timeout_s)
                result["timings"] = tmap
            except Exception:
                pass
            try:
                if wall_s is not None and (bool(args.debug) or float(wall_s) >= 5.0):
                    t0 = result.get("timings") if isinstance(result.get("timings"), dict) else {}
                    print(
                        f"[{now_iso()}] attempt_timing member={result.get('member')} order={job.order_id} wall_s={wall_s} "
                        f"ft_bet_ms={result.get('ft_bet_elapsed_ms') or result.get('submit_elapsed_ms')}",
                        flush=True,
                    )
            except Exception:
                pass
            result["attempt"] = int(attempt_no)
            result["attempt_no"] = int(attempt_no)
            result.setdefault("split_order_id", job.key)
            result["split_index"] = int(job.split_index or 0) + 1
            result["split_count"] = int(job.split_count or 1)
            attach_follow_limit_metadata(result, job.order, one_shot_settings)
            _apply_market_submit_breaker(
                job,
                result,
                max_attempts=market_reject_max_attempts,
                retry_delay_seconds=market_reject_retry_delay,
            )
            result["error_class"] = classify_execution_error(result)
            try:
                result["split_delay_seconds"] = float(getattr(job, "split_delay_seconds", 0.0) or 0.0)
            except Exception:
                pass
            try:
                result["next_try_at"] = float(getattr(job, "next_try_at", 0.0) or 0.0)
            except Exception:
                pass
            # 时序字段：便于离线统计与复盘
            try:
                result["order_elapsed_s"] = round(float(now_ts()) - float(job.created_at or 0.0), 3)
                result["job_created_at"] = float(job.created_at or 0.0)
                result["job_last_attempted_at"] = float(job.last_attempted_at or 0.0)
                result["candidate_snapshot_at"] = float(job.first_candidate_snapshot_at or 0.0)
                result["member_acquired_at"] = member_acquired_at
                result["trigger_reason"] = str(job.trigger_reason or "INITIAL_READY")
                result["trigger_event_seq"] = int(job.trigger_event_seq or 0)
                result["trigger_publish_at"] = float(job.trigger_publish_at or 0.0) or None
                result["trigger_wakeup_at"] = float(job.trigger_wakeup_at or 0.0) or None
                if float(job.trigger_publish_at or 0.0) and float(job.trigger_wakeup_at or 0.0):
                    result["trigger_publish_to_wakeup_ms"] = round(
                        max(0.0, float(job.trigger_wakeup_at) - float(job.trigger_publish_at)) * 1000.0, 3
                    )
                result["candidate_detected_at"] = float(job.candidate_detected_at or job.first_candidate_snapshot_at or 0.0) or None
                result["job_marked_runnable_at"] = float(job.job_marked_runnable_at or 0.0) or None
                result["runnable_queue_entered_at"] = float(job.runnable_queue_entered_at or 0.0) or None
                result["dispatcher_selected_at"] = float(job.dispatcher_selected_at or 0.0) or None
                result["attempt_started_at"] = float(job.attempt_started_at or 0.0) or None
                candidate_detected_at = float(job.candidate_detected_at or job.first_candidate_snapshot_at or 0.0)
                runnable_at = float(job.job_marked_runnable_at or 0.0)
                queue_entered_at = float(job.runnable_queue_entered_at or 0.0)
                dispatcher_selected_at = float(job.dispatcher_selected_at or 0.0)
                if candidate_detected_at and runnable_at:
                    result["candidate_to_runnable_ms"] = round(max(0.0, runnable_at - candidate_detected_at) * 1000.0, 3)
                if runnable_at and queue_entered_at:
                    result["runnable_queue_wait_ms"] = round(max(0.0, queue_entered_at - runnable_at) * 1000.0, 3)
                if queue_entered_at and dispatcher_selected_at:
                    result["dispatcher_wait_ms"] = round(max(0.0, dispatcher_selected_at - queue_entered_at) * 1000.0, 3)
                if dispatcher_selected_at and member_acquired_at:
                    result["member_acquire_ms"] = round(max(0.0, member_acquired_at - dispatcher_selected_at) * 1000.0, 3)
                result["first_candidate_snapshot_ms"] = round(max(0.0, (float(job.first_candidate_snapshot_at or 0.0) - float(job.created_at or 0.0)) * 1000.0), 3) if job.first_candidate_snapshot_at else None
                result["candidate_to_member_acquired_ms"] = round(max(0.0, (member_acquired_at - float(job.first_candidate_snapshot_at or member_acquired_at)) * 1000.0), 3)
                context_finished_at = float(result.get("context_finished_at") or 0.0)
                if context_finished_at:
                    result["member_acquired_to_context_finished_ms"] = round(max(0.0, (context_finished_at - member_acquired_at) * 1000.0), 3)
                    ft_started = float(result.get("ft_bet_started_at") or 0.0)
                    if ft_started:
                        result["context_finished_to_ft_bet_started_ms"] = round(max(0.0, (ft_started - context_finished_at) * 1000.0), 3)
                ft_started = float(result.get("ft_bet_started_at") or 0.0)
                ft_finished = float(result.get("ft_bet_finished_at") or 0.0)
                if ft_started and ft_finished:
                    result["ft_bet_started_to_result_ms"] = round(max(0.0, (ft_finished - ft_started) * 1000.0), 3)
                result["total_order_elapsed_ms"] = round(max(0.0, (float(now_ts()) - float(job.created_at or 0.0)) * 1000.0), 3)
                result["market_wait_count"] = int(job.market_wait_count or 0)
                result["member_wait_count"] = int(job.member_wait_count or 0)
                job.trigger_reason = ""
                job.trigger_event_seq = 0
                job.trigger_publish_at = 0.0
                job.trigger_wakeup_at = 0.0
                job.job_marked_runnable_at = 0.0
                job.runnable_queue_entered_at = 0.0
            except Exception:
                pass
            waiting_status = str(
                result.get("snapshot_direct_status") or result.get("error_code") or ""
            ).strip().upper()
            if waiting_status in {
                SNAPSHOT_MISSING,
                SNAPSHOT_STALE,
                TARGET_LINE_MISSING,
                SNAPSHOT_INVALID,
                MEMBER_IORATIO_MISSING,
                MEMBER_IORATIO_STALE,
                MEMBER_IORATIO_INVALID,
                BET_CONTEXT_MISMATCH,
                MARKET_CHANGED_DURING_CONTEXT,
            } and str(result.get("real_submission") or "").lower() == "none":
                if waiting_status in {
                    SNAPSHOT_MISSING,
                    SNAPSHOT_STALE,
                    TARGET_LINE_MISSING,
                    SNAPSHOT_INVALID,
                    BET_CONTEXT_MISMATCH,
                    MARKET_CHANGED_DURING_CONTEXT,
                }:
                    job.market_wait_count = int(job.market_wait_count or 0) + 1
                else:
                    job.member_wait_count = int(job.member_wait_count or 0) + 1
                job.attempts = max(0, int(attempt_no) - 1)
                job.last_err = waiting_status
                job.last_message = str(result.get("message") or "")
                job.member_id = ""
                job.next_try_at = float(now_ts())
                continue
            err_now = extract_err_from_result(result)
            context_retry_scheduled = False
            if err_now.upper() == CONTEXT_RETRYABLE:
                retry_now = float(now_ts())
                count, delay, fast = retry_scheduler.schedule_context_retry(
                    job,
                    now=retry_now,
                    slow_delay=max(1.0, float(retry_delay_seconds or 1.0)),
                )
                result["context_retry_count"] = count
                result["context_retry_delay_ms"] = round(delay * 1000.0, 3)
                result["context_retry_fast"] = bool(fast)
                result["real_submission"] = "none"
                result["snapshot_direct_submission"] = False
                result["error_class"] = CONTEXT_RETRYABLE
                fingerprint = str(result.get("market_row_fingerprint_before") or result.get("market_row_fingerprint") or "")
                if fingerprint:
                    job.order["_context_retry_fingerprint"] = fingerprint
                job.order["_context_retry"] = True
                job.last_err = CONTEXT_RETRYABLE
                job.last_message = str(result.get("message") or "FT_order_view 传输失败，正在快速重试。")
                job.trigger_reason = "CONTEXT_RETRY"
                job.trigger_event_seq = 0
                job.trigger_publish_at = 0.0
                job.trigger_wakeup_at = retry_now
                job.job_marked_runnable_at = retry_now
                job.member_id = str(result.get("member_id") or result.get("execution_member") or job.member_id or "")
                context_retry_scheduled = True
            if err_now != "1X001" and prev_err == "1X001":
                job.market_closed_wait_started_at = 0.0
                job.market_closed_wait_deadline_at = 0.0
                job.market_closed_fails = 0
            if err_now not in {"LINE_NOT_FOUND", SNAPSHOT_MISSING, SNAPSHOT_STALE, TARGET_LINE_MISSING, SNAPSHOT_INVALID, MEMBER_IORATIO_MISSING, MEMBER_IORATIO_STALE, MEMBER_IORATIO_INVALID, MARKET_ERROR} and prev_err in {"LINE_NOT_FOUND", SNAPSHOT_MISSING, SNAPSHOT_STALE, TARGET_LINE_MISSING, SNAPSHOT_INVALID, MEMBER_IORATIO_MISSING, MEMBER_IORATIO_STALE, MEMBER_IORATIO_INVALID, MARKET_ERROR}:
                job.line_not_found_wait_started_at = 0.0
                job.line_not_found_wait_deadline_at = 0.0

            # 仅在未触发“终态停止”时才进入“目标盘口线找回”探测窗口
            if (
                err_now == "LINE_NOT_FOUND"
                and float(wait_line_not_found_max or 0.0) > 0.0
                and ("已停止该订单跟单" not in str(result.get("message") or ""))
            ):
                now2 = float(now_ts())
                if prev_err != "LINE_NOT_FOUND" or float(getattr(job, "line_not_found_wait_deadline_at", 0.0) or 0.0) <= 0.0:
                    job.line_not_found_wait_started_at = now2
                    job.line_not_found_wait_deadline_at = now2 + float(wait_line_not_found_max)
                deadline = float(getattr(job, "line_not_found_wait_deadline_at", 0.0) or 0.0)
                remain = max(0.0, deadline - now2)
                if remain <= 0.0:
                    result["message"] = "目标盘口线等待超时，已停止该订单跟单。"
                else:
                    # 注意：必须保留 err=LINE_NOT_FOUND，方便后续调度识别
                    result["message"] = f"目标盘口线暂时找不到，正在重试（剩余{int(math.ceil(remain))}秒） err=LINE_NOT_FOUND"

            # 分单：你要的是“错峰但不中断”。因此每一笔成功后明确提示“下一笔何时执行”，避免误以为卡住没跑。
            try:
                if bool(result.get("ok")) and int(result.get("split_count") or 1) > 1:
                    cur_part = int(result.get("split_index") or 1)
                    total_parts = int(result.get("split_count") or 1)
                    if 1 <= cur_part < total_parts:
                        delay_s = float(job.split_delay_seconds or 0.0)
                        if not str(result.get("message") or "").strip():
                            result["message"] = f"分单成功 {cur_part}/{total_parts}，下一笔将在 {delay_s:g}s 后执行。"
                        else:
                            result["message"] = f"{str(result.get('message') or '').strip()}（下一笔 {delay_s:g}s 后执行）"
            except Exception:
                pass
            result["error_class"] = classify_execution_error(result)
            persist_started_at, persist_finished_at = _append_jsonl_timed(results_path, result)
            record_follow_limit_success(
                result,
                match_committed=limit_match_committed,
                market_committed=limit_market_committed,
                success_keys=limit_success_keys,
            )
            result["result_persist_started_at"] = persist_started_at
            result["result_persist_finished_at"] = persist_finished_at
            result["result_persist_elapsed_ms"] = round(max(0.0, persist_finished_at - persist_started_at) * 1000.0, 3)
            try:
                _append_trace_jsonl(
                    trace_path,
                    {
                        "event": "attempt_result",
                        "key": job.key,
                        "target": job.target,
                        "orderId": job.order_id,
                        "order_id": job.order_id,
                        "split_order_id": job.key,
                        "attempt": int(attempt_no),
                        "attempt_no": int(attempt_no),
                        "split_index": int(result.get("split_index") or 0),
                        "split_count": int(result.get("split_count") or 1),
                        "ok": bool(result.get("ok")),
                        "member": str(result.get("member") or ""),
                        "err": extract_err_from_result(result),
                        "message": str(result.get("message") or ""),
                        "elapsed_s": float(result.get("order_elapsed_s") or 0.0),
                        "attempt_wall_s": float(((result.get("timings") or {}) if isinstance(result.get("timings"), dict) else {}).get("attempt_wall_s") or 0.0),
                        **_snapshot_attempt_trace_fields(result),
                    },
                )
            except Exception:
                pass

            # 下注返回了 nowcredit 时，立刻回写弹药库余额(额度)，便于“余额够才随机选”更准确
            try:
                nc = result.get("nowcredit")
                if nc not in (None, "") and job.member_id:
                    member_patch_started_at = float(now_ts())
                    patch_member(
                        data_dir / "arsenal.json",
                        str(job.member_id),
                        {"maxcredit": _to_float(nc), "nowcredit": _to_float(nc), "balance_updated_at": now_ts()},
                    )
                    result["member_patch_started_at"] = member_patch_started_at
                    result["member_patch_finished_at"] = float(now_ts())
                    result["member_patch_elapsed_ms"] = round(
                        max(0.0, result["member_patch_finished_at"] - member_patch_started_at) * 1000.0, 3
                    )
            except Exception:
                pass

            if (not bool(job.independent_split)) and split_job_current_part_on_member_max_limit(
                job,
                result,
                min_stake_per_bet=float(split_min_stake_cfg),
                max_split_count=50,
            ):
                job.attempts = 0
                job.last_err = STAKE_EXCEEDS_MAX_ERR
                job.last_message = str(result.get("message") or "")
                job.next_try_at = float(now_ts())
                try:
                    dyn = result.get("dynamic_split") if isinstance(result.get("dynamic_split"), dict) else {}
                    _append_trace_jsonl(
                        trace_path,
                        {
                            "event": "part_resplit",
                            "key": job.key,
                            "target": job.target,
                            "orderId": job.order_id,
                            "member": str(result.get("member") or ""),
                            "gold_gmax": _safe_float(dyn.get("gold_gmax"), 0.0),
                            "old_stake": _safe_float(dyn.get("old_stake"), 0.0),
                            "new_stakes": dyn.get("new_stakes") or [],
                            "split_index": int(job.split_index or 0) + 1,
                            "split_count": int(job.split_count or 1),
                            "message": str(result.get("message") or ""),
                        },
                    )
                except Exception:
                    pass
                print(
                    f"[{now_iso()}] 分单金额超会员限额，已自动拆分 member={result.get('member')} "
                    f"order={job.order_id} split_index={int(job.split_index or 0) + 1}/{int(job.split_count or 1)} "
                    f"parts={job.split_stakes}",
                    flush=True,
                )
                continue

            result_for_tg = result
            if int(job.split_count or 1) > 1:
                try:
                    idx0 = int(job.split_index or 0)
                except Exception:
                    idx0 = 0
                part_rec = {
                    "state": "ok" if bool(result.get("ok")) else "failed",
                    "part": idx0 + 1,
                    "ok": bool(result.get("ok")),
                    "attempt": int(result.get("attempt") or 0),
                    "member": str(result.get("member") or ""),
                    "stake": result.get("stake"),
                    "ticket_id": str(result.get("ticket_id") or ""),
                    "message": str(result.get("message") or ""),
                    "ts": int(result.get("ts") or now_ts()),
                    "params": result.get("params") or {},
                }
                job.split_part_results[idx0] = part_rec
                done_total = 0.0
                for rec in (job.split_part_results or {}).values():
                    try:
                        if bool(rec.get("ok")):
                            done_total += float(rec.get("stake") or 0)
                    except Exception:
                        pass
                result_for_tg = dict(result)
                result_for_tg["split_parts"] = _sorted_split_part_records(job.split_part_results or {})
                result_for_tg["split_total_plan"] = float(job.stake or 0.0)
                result_for_tg["split_total_done"] = float(done_total)

            # Telegram：每次尝试都把结果合并回“后台订单”那条消息（避免一直停留在等待中）
            _enqueue_follow_notification(job, result_for_tg)

            if result.get("ok"):
                _clear_unknown_result_state(job)
                job.attempts = 0
                job.market_closed_fails = 0
                job.market_closed_wait_started_at = 0.0
                job.market_closed_wait_deadline_at = 0.0
                job.line_not_found_wait_started_at = 0.0
                job.line_not_found_wait_deadline_at = 0.0
                # 分单：记录已完成的票
                tid = str(result.get("ticket_id") or "").strip()
                if tid:
                    job.split_tickets.append(tid)
                    follow_generated_order_keys.add(f"{job.target}|{tid}")
                if job.member_id and job.member_id not in job.used_member_ids:
                    job.used_member_ids.append(job.member_id)

                # 是否还有下一笔分单
                if (not bool(job.independent_split)) and int(job.split_count or 1) > 1 and (int(job.split_index or 0) + 1) < int(job.split_count or 1):
                    job.split_index = int(job.split_index or 0) + 1
                    job.next_try_at = float(now_ts()) + float(job.split_delay_seconds or 0.0)
                    job.last_message = f"分单已完成 {len(job.split_tickets)}/{int(job.split_count or 1)}"
                    if str(job.last_err or "") != STAKE_EXCEEDS_MAX_ERR:
                        job.last_err = ""
                    try:
                        _append_trace_jsonl(
                            trace_path,
                            {
                                "event": "part_scheduled",
                                "key": job.key,
                                "target": job.target,
                                "orderId": job.order_id,
                                "split_index_next": int(job.split_index or 0) + 1,
                                "split_count": int(job.split_count or 1),
                                "delay_s": float(job.split_delay_seconds or 0.0),
                                "next_try_at": float(job.next_try_at or 0.0),
                                "elapsed_s": round(float(now_ts()) - float(job.created_at or 0.0), 3),
                            },
                        )
                    except Exception:
                        pass
                    print(
                        f"[{now_iso()}] 分单成功 member={result.get('member')} order={job.order_id} part={len(job.split_tickets)}/{int(job.split_count or 1)} ticket={tid} next_in={float(job.split_delay_seconds or 0.0)}s",
                        flush=True,
                    )
                    continue

                # 全部完成
                terminal_record = {
                    "status": "ok",
                    "ticket_id": job.split_tickets[-1] if job.split_tickets else tid,
                    "ts": int(now_ts()),
                    "split_index": int(job.split_index or 0),
                    "stake": float(result.get("stake") or 0.0),
                    "member": str(result.get("member") or ""),
                }
                if len(job.split_tickets) > 1:
                    terminal_record["tickets"] = list(job.split_tickets)
                try:
                    terminal_record["elapsed_s"] = round(float(now_ts()) - float(job.created_at or 0.0), 3)
                    terminal_record["split_done"] = int(len(job.split_tickets) or 1)
                    terminal_record["split_total"] = int(job.split_count or 1)
                except Exception:
                    pass
                _finish_terminal_job(job, terminal_record)
                _append_trace_jsonl(
                    trace_path,
                    {
                        "event": "job_done",
                        "key": job.key,
                        "target": job.target,
                        "orderId": job.order_id,
                        "status": "ok",
                        "elapsed_s": round(float(now_ts()) - float(job.created_at or 0.0), 3),
                        "tickets": list(job.split_tickets),
                    },
                )
                try:
                    elapsed_s0 = round(float(now_ts()) - float(job.created_at or 0.0), 3)
                except Exception:
                    elapsed_s0 = None
                print(
                    f"[{now_iso()}] 跟单成功 member={result.get('member')} order={job.order_id} "
                    f"tickets={len(job.split_tickets) or 1} elapsed_s={elapsed_s0}",
                    flush=True,
                )
                continue

            retryable = is_retryable_follow_result(result)
            if not retryable:
                reason = str(result.get("message") or "")
                if (job.split_tickets or []) and int(job.split_count or 1) > 1:
                    reason = f"分单已完成 {len(job.split_tickets)}/{int(job.split_count or 1)}，后续失败：{reason}"
                terminal_reconcile = str(result.get("reconcile_terminal_status") or "").strip().upper()
                terminal_direct = str(result.get("snapshot_direct_result_status") or "").strip().upper()
                done_status = (
                    MARKET_SUBMIT_REJECTED.lower()
                    if terminal_direct == MARKET_SUBMIT_REJECTED
                    else terminal_reconcile.lower()
                    if terminal_reconcile in {"CONFIRMED_FAILED", "STILL_UNKNOWN"}
                    else ("skipped" if bool(result.get("skipped")) else "failed")
                )
                terminal_record = {
                    "status": done_status,
                    "reason": reason,
                    "ts": int(now_ts()),
                    "split_index": int(job.split_index or 0),
                    "stake": float(result.get("stake") or 0.0),
                    "member": str(result.get("member") or ""),
                }
                try:
                    terminal_record["elapsed_s"] = round(float(now_ts()) - float(job.created_at or 0.0), 3)
                    terminal_record["split_done"] = int(len(job.split_tickets) or 0)
                    terminal_record["split_total"] = int(job.split_count or 1)
                except Exception:
                    pass
                _finish_terminal_job(job, terminal_record)
                _append_trace_jsonl(
                    trace_path,
                    {
                        "event": "job_done",
                        "key": job.key,
                        "target": job.target,
                        "orderId": job.order_id,
                        "status": done_status,
                        "elapsed_s": round(float(now_ts()) - float(job.created_at or 0.0), 3),
                        "reason": reason,
                        "tickets": list(job.split_tickets),
                    },
                )
                try:
                    elapsed_s0 = round(float(now_ts()) - float(job.created_at or 0.0), 3)
                except Exception:
                    elapsed_s0 = None
                print(
                    f"[{now_iso()}] 跟单{('跳过' if done_status == 'skipped' else '失败')}(不重试) member={result.get('member')} order={job.order_id} "
                    f"elapsed_s={elapsed_s0} msg={result.get('message')}",
                    flush=True,
                )
                continue

            job.attempts = int(attempt_no)
            job.last_message = str(result.get("message") or "")
            job.last_err = extract_err_from_result(result)
            if _schedule_explicit_market_rejection_retry(job, result, now=float(now_ts())):
                _append_trace_jsonl(
                    trace_path,
                    {
                        "event": "market_reject_retry_scheduled",
                        "key": job.key,
                        "orderId": job.order_id,
                        "attempt": int(attempt_no),
                        "member": str(result.get("member") or ""),
                        "error_code": str(result.get("market_submit_reject_code") or ""),
                        "reject_count": int(result.get("market_submit_reject_count") or 0),
                        "reject_limit": int(result.get("market_submit_reject_max_attempts") or 0),
                        "retry_at": float(job.next_try_at or 0.0),
                    },
                )
                print(
                    f"[{now_iso()}] 平台拒单，换会员重试 order={job.order_id} "
                    f"attempt={job.attempts} code={result.get('market_submit_reject_code')} "
                    f"next_in={result.get('market_submit_reject_retry_delay_seconds')}s",
                    flush=True,
                )
                continue
            if job.last_err == MARKET_ERROR:
                revision = _safe_int(result.get("snapshot_direct_revision"), 0)
                if revision > 0:
                    job.order["_wait_for_snapshot_revision"] = revision
            if job.last_err == UNKNOWN_RESULT:
                _begin_unknown_result_state(job, result, settings)
                job.unknown_result_check_count = _safe_int(result.get("unknown_result_check_count"), job.unknown_result_check_count)
                job.unknown_result_last_check_at = _safe_float(result.get("reconcile_checked_at"), 0.0)
                job.order["_unknown_result_check_count"] = int(job.unknown_result_check_count)
                job.next_try_at = float(now_ts()) + (15.0 if bool(result.get("reconcile_pending")) else 0.0)
                if str(result.get("reconcile_terminal_status") or "").upper() in {"CONFIRMED_FAILED", "STILL_UNKNOWN"}:
                    _finish_terminal_job(job, {
                        "status": str(result.get("reconcile_terminal_status") or "STILL_UNKNOWN").lower(),
                        "reason": str(result.get("message") or ""),
                        "ts": int(now_ts()),
                        "split_index": int(job.split_index or 0),
                    })
                continue

            if context_retry_scheduled or job.last_err == CONTEXT_RETRYABLE:
                print(
                    f"[{now_iso()}] Context传输失败，快速重试 order={job.order_id} "
                    f"attempt={job.attempts} count={getattr(job, 'context_retry_count', 0)} "
                    f"delay_ms={result.get('context_retry_delay_ms')}",
                    flush=True,
                )
            elif job.last_err == "1X001":
                job.market_closed_fails += 1
                # 1X001：封盘等待（固定频率探测 + 超时终止）
                if float(wait_1x001_max or 0.0) > 0.0:
                    now2 = float(now_ts())
                    st, remain, next_delay = retry_scheduler.schedule_wait_by_err(
                        job,
                        err=job.last_err,
                        now=now2,
                        wait_max=float(wait_1x001_max or 0.0),
                        probe_seconds=float(wait_1x001_probe or 0.0),
                    )
                    if st == "expired":
                        timeout_reason = retry_scheduler.check_wait_timeout(job, now=now2) or "盘口封盘等待超时，已停止该订单跟单。"
                        _finish_terminal_job(job, {"status": "failed", "reason": timeout_reason, "ts": int(now_ts()), "split_index": int(job.split_index or 0)})
                        print(
                            f"[{now_iso()}] 跟单失败(封盘等待超时) member={result.get('member')} order={job.order_id} attempt={job.attempts}",
                            flush=True,
                        )
                        continue
                    job.last_message = f"盘口封盘，正在等待开盘重试（剩余{int(math.ceil(remain))}秒）"
                    print(
                        f"[{now_iso()}] 跟单失败(盘口封盘，{int(next_delay)}秒后探测) member={result.get('member')} order={job.order_id} attempt={job.attempts} msg={job.last_message}",
                        flush=True,
                    )
                else:
                    job.next_try_at = float(now_ts()) + 20.0
                    print(
                        f"[{now_iso()}] 跟单失败(盘口关闭，退避后重试) member={result.get('member')} order={job.order_id} attempt={job.attempts} msg={job.last_message}",
                        flush=True,
                    )
            elif job.last_err in {"LINE_NOT_FOUND", SNAPSHOT_MISSING, SNAPSHOT_STALE, TARGET_LINE_MISSING, SNAPSHOT_INVALID, MEMBER_IORATIO_MISSING, MEMBER_IORATIO_STALE, MEMBER_IORATIO_INVALID, MARKET_ERROR}:
                # LINE_NOT_FOUND：目标盘口线短暂找不到 -> 探测窗口（固定频率探测 + 超时终止）
                if float(wait_line_not_found_max or 0.0) > 0.0:
                    now2 = float(now_ts())
                    st, remain, next_delay = retry_scheduler.schedule_wait_by_err(
                        job,
                        err=job.last_err,
                        now=now2,
                        wait_max=float(wait_line_not_found_max or 0.0),
                        probe_seconds=float(wait_line_not_found_probe or 0.0),
                    )
                    if st == "expired":
                        timeout_reason = retry_scheduler.check_wait_timeout(job, now=now2) or "目标盘口线等待超时，已停止该订单跟单。"
                        _finish_terminal_job(job, {"status": "failed", "reason": timeout_reason, "ts": int(now_ts()), "split_index": int(job.split_index or 0)})
                        print(
                            f"[{now_iso()}] 跟单失败(目标盘口线等待超时) member={result.get('member')} order={job.order_id} attempt={job.attempts}",
                            flush=True,
                        )
                        continue
                    job.last_message = f"目标盘口线暂时找不到，正在重试（剩余{int(math.ceil(remain))}秒） err=LINE_NOT_FOUND"
                    print(
                        f"[{now_iso()}] 跟单失败(目标盘口线暂时找不到，{int(next_delay)}秒后探测) member={result.get('member')} order={job.order_id} attempt={job.attempts} msg={job.last_message}",
                        flush=True,
                    )
                else:
                    # 兜底：如果 wait_line_not_found_max_seconds=0（禁用等待），保持“让出队列立即重试”
                    job.next_try_at = float(now_ts())
                    print(
                        f"[{now_iso()}] 跟单失败(目标盘口线暂时找不到，立即重试) member={result.get('member')} order={job.order_id} attempt={job.attempts} msg={job.last_message}",
                        flush=True,
                    )
            else:
                # 离开 1X001：清空封盘等待状态（下一次再封盘时“每次重置”重新计时）
                job.market_closed_fails = 0
                job.market_closed_wait_started_at = 0.0
                job.market_closed_wait_deadline_at = 0.0
                # 离开目标盘口等待状态：清空探测窗口
                job.line_not_found_wait_started_at = 0.0
                job.line_not_found_wait_deadline_at = 0.0
                # 你的要求：一旦报错（已有失败记录），不要再等 retry_delay_seconds（例如 6 秒），
                # 立即让出队列去处理下一单；本单在下一轮循环中尽快再尝试（由 last_attempted_at 保证公平轮转）。
                err_now = str(job.last_err or "").strip()
                delay_s = retry_delay_for_result(result, attempt_no)
                job.next_try_at = float(now_ts()) + float(delay_s)
                print(
                    f"[{now_iso()}] 跟单失败(让出队列，{delay_s:.1f}秒后重试) member={result.get('member')} order={job.order_id} attempt={job.attempts} err={err_now} msg={job.last_message}",
                    flush=True,
                )

        # enabled 模式下也持续落盘（包含 done/jobs/pending），保证 stop/restart 不会重复跟单。
        _persist_follow_state()

        # Active jobs are primarily woken by snapshot/member events. Keep a
        # short bounded fallback tick so an event arriving between iterations
        # cannot add a full one-second scheduler delay.
        time.sleep(min(0.02, max(0.005, float(args.interval))))


if __name__ == "__main__":
    raise SystemExit(main())
