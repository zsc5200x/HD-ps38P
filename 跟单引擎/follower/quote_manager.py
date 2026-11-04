from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional


QuoteMemberSession = Dict[str, str]


def is_quote_member(member: Dict[str, Any]) -> bool:
    return str((member or {}).get("role") or "").strip().lower() == "quote"


class QuoteManager:
    """Manage quote-member selection and lightweight quote-member operations."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.quote_member: Optional[QuoteMemberSession] = None
        self._loaded_at: float = 0.0

    def _read_json(self, path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
        try:
            obj = json.loads(path.read_text(encoding="utf-8-sig"))
            return obj if isinstance(obj, dict) else dict(default)
        except Exception:
            return dict(default)

    def _load_quote_member_from_file(self) -> str:
        qdoc = self._read_json(self.data_dir / "quote_member.json", {})
        return str(qdoc.get("member_id") or "").strip()

    def _pick_quote_member(self, members: Any, selected_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(members, list):
            return None

        picked: Optional[Dict[str, Any]] = None
        if selected_id:
            for m in members:
                if not isinstance(m, dict):
                    continue
                if str(m.get("id") or "").strip() == selected_id:
                    picked = m
                    break

        if not picked:
            for m in members:
                if not isinstance(m, dict):
                    continue
                if not is_quote_member(m):
                    continue
                if str(m.get("status") or "").strip().lower() != "online":
                    continue
                picked = m
                break
        return picked if isinstance(picked, dict) else None

    def _to_session_payload(self, member: Dict[str, Any]) -> Optional[QuoteMemberSession]:
        if not is_quote_member(member):
            return None
        if str(member.get("status") or "").strip().lower() != "online":
            return None

        base_url = str(member.get("base_url") or "").strip().rstrip("/")
        ver = str(member.get("ver") or "").strip()
        uid = str(member.get("uid") or "").strip()
        cookie = str(member.get("cookie") or "").strip()
        proxy = str(member.get("proxy") or "").strip()
        if not (base_url and ver and uid and cookie):
            return None

        return {
            "member_id": str(member.get("id") or "").strip(),
            "member_label": str(
                member.get("label") or member.get("username") or member.get("id") or ""
            ).strip(),
            "base_url": base_url,
            "ver": ver,
            "uid": uid,
            "cookie": cookie,
            "proxy": proxy,
        }

    def _load_quote_member(self) -> None:
        adoc = self._read_json(self.data_dir / "arsenal.json", {"members": []})
        members = adoc.get("members")
        selected_id = self._load_quote_member_from_file()
        picked = self._pick_quote_member(members, selected_id)
        self.quote_member = self._to_session_payload(picked or {}) if picked else None
        self._loaded_at = float(time.time())

    def get_quote_member(self, force_reload: bool = False) -> Optional[QuoteMemberSession]:
        if force_reload or not self.quote_member:
            self._load_quote_member()
        if not isinstance(self.quote_member, dict):
            return None
        return dict(self.quote_member)

    def fetch_live_odds(
        self,
        gid: str,
        market: str,
        side: str,
        *,
        fetcher: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        info = self.get_quote_member(force_reload=False)
        if not info:
            return {
                "success": False,
                "spread": "",
                "ioratio": "",
                "score": "",
                "code": "QUOTE_MEMBER_MISSING",
            }
        if fetcher is None:
            return {
                "success": False,
                "spread": "",
                "ioratio": "",
                "score": "",
                "code": "FETCHER_NOT_PROVIDED",
            }
        try:
            out = fetcher(info=info, gid=str(gid or ""), market=str(market or ""), side=str(side or ""))
        except Exception:
            return {
                "success": False,
                "spread": "",
                "ioratio": "",
                "score": "",
                "code": "QUOTE_FETCH_FAILED",
            }
        if not isinstance(out, dict):
            return {
                "success": False,
                "spread": "",
                "ioratio": "",
                "score": "",
                "code": "QUOTE_FETCH_BAD_PAYLOAD",
            }
        return {
            "success": bool(out.get("success", True)),
            "spread": str(out.get("spread") or ""),
            "ioratio": str(out.get("ioratio") or ""),
            "score": str(out.get("score") or ""),
            "code": str(out.get("code") or ""),
        }

    def warmup_quote_member(
        self, warmup_func: Optional[Callable[[QuoteMemberSession], bool]] = None
    ) -> bool:
        info = self.get_quote_member(force_reload=True)
        if not info:
            return False
        if warmup_func is None:
            return True
        try:
            return bool(warmup_func(info))
        except Exception:
            return False


_QUOTE_MANAGER_SINGLETONS: Dict[str, QuoteManager] = {}


def get_quote_manager(data_dir: Path) -> QuoteManager:
    key = str(Path(data_dir).resolve())
    mgr = _QUOTE_MANAGER_SINGLETONS.get(key)
    if mgr is None:
        mgr = QuoteManager(data_dir=Path(data_dir))
        _QUOTE_MANAGER_SINGLETONS[key] = mgr
    return mgr
