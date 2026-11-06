"""AdsPower local API client and environment-name parsing helpers.

Only the local AdsPower client API is used (default ``http://localhost:50325``).
Proxy credentials are read from AdsPower but never projected to the HTTP API,
written to logs or stored in the project database. Environment names in this
pool encode the login account and an expiry date, so they are parsed here once
and returned as plain fields for the import UI.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Dict, List, Optional

DEFAULT_BASE_URL = "http://localhost:50325"
DEFAULT_TIMEOUT = 20
LIST_INTERVAL = 1.0
MAX_PAGES = 20
PAGE_LIMIT = 100

_SAFE_ENV_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_ACCOUNT_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}")
_EXPIRY_MARK = re.compile(r"过期\s*[：:]?\s*(\d{1,2})\s*-\s*(\d{1,2})")
_TRAILING_DATE = re.compile(r"(?<![\d-])(\d{1,2})-(\d{1,2})(?![\d-])")
_MARKERS = ("会：", "会:", "会员：", "会员:", "会员", "会", "登")


class AdsPowerError(RuntimeError):
    """The AdsPower client rejected a call or could not be reached."""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


def is_env_id(value: Any) -> bool:
    return bool(_SAFE_ENV_ID.fullmatch(str(value or "")))


def parse_account_name(name: Any) -> str:
    """Extract the login account from a pool environment name.

    Examples: ``1号会员：iigg1989 过期12-05`` -> ``iigg1989``;
    ``登zy2411 会：llyyl120 过期12-04`` -> ``llyyl120``;
    ``平博后台cokdex77.`` -> ``cokdex77.`` (callers may correct it before import).
    """
    text = str(name or "").strip()
    if not text:
        return ""
    cleaned = _EXPIRY_MARK.sub(" ", text)
    cleaned = _TRAILING_DATE.sub(" ", cleaned)
    for marker in _MARKERS:
        index = cleaned.find(marker)
        if index < 0:
            continue
        match = _ACCOUNT_TOKEN.search(cleaned[index + len(marker) :])
        if match:
            return match.group(0).strip("-_")
    tokens = [item for item in re.split(r"[\s，,、/]+", cleaned) if item]
    for token in reversed(tokens):
        match = _ACCOUNT_TOKEN.search(token)
        if match and any(char.isdigit() for char in match.group(0)):
            return match.group(0).strip("-_")
    return ""


def parse_expiry(name: Any, *, today: Optional[date] = None) -> Dict[str, Any]:
    """Return ``{expire_date, expired}`` parsed from ``过期MM-DD`` in the name."""
    text = str(name or "")
    now = today or date.today()
    match = _EXPIRY_MARK.search(text) or _TRAILING_DATE.search(_EXPIRY_MARK.sub(" ", text))
    if not match:
        return {"expire_date": "", "expired": False}
    try:
        month, day = int(match.group(1)), int(match.group(2))
    except (TypeError, ValueError):
        return {"expire_date": "", "expired": False}
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return {"expire_date": "", "expired": False}
    try:
        parsed = date(now.year, month, day)
        if (parsed - now).days < -180:
            parsed = date(now.year + 1, month, day)
    except ValueError:
        return {"expire_date": "", "expired": False}
    return {"expire_date": parsed.isoformat(), "expired": parsed < now}


def is_stopped(name: Any) -> bool:
    """Environment names carry a ``停`` marker when the account is paused."""
    return "停" in str(name or "")


def detect_platform(name: Any) -> str:
    """Infer the pool platform from an environment name; empty when unknown."""
    text = str(name or "").lower()
    if any(marker in text for marker in ("平博", "ps38", "ps3838")):
        return "ps38_backend"
    if any(marker in text for marker in ("皇冠", "hga", "mos", "会员")):
        return "crown_member"
    return ""


def _safe_proxy(row: Dict[str, Any]) -> Dict[str, str]:
    config = row.get("user_proxy_config") if isinstance(row, dict) else None
    if not isinstance(config, dict):
        config = {}
    host = str(config.get("proxy_host") or row.get("ip") or "").strip()
    port = str(config.get("proxy_port") or "").strip()
    kind = str(config.get("proxy_type") or "").strip()
    return {
        "proxy_type": kind,
        "proxy_host": host,
        "proxy_port": port,
        "proxy_ready": "yes" if host else "no",
    }


def environment_projection(
    row: Dict[str, Any],
    bound: Optional[Dict[str, Any]] = None,
    *,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Public shape for one environment; proxy credentials are never included."""
    name = str(row.get("name") or "").strip()
    expiry = parse_expiry(name, today=today)
    proxy = _safe_proxy(row)
    return {
        "profile_id": str(row.get("profile_id") or ""),
        "profile_no": str(row.get("profile_no") or ""),
        "name": name,
        "group_name": str(row.get("group_name") or ""),
        "ip": str(row.get("ip") or "").strip(),
        "proxy_type": proxy["proxy_type"],
        "proxy_host": proxy["proxy_host"],
        "proxy_port": proxy["proxy_port"],
        "proxy_ready": proxy["proxy_ready"],
        "account": parse_account_name(name),
        "expire_date": expiry["expire_date"],
        "expired": expiry["expired"],
        "stopped": is_stopped(name),
        "last_open_time": str(row.get("last_open_time") or ""),
        "bound_account": bound or None,
    }


_PROFILE_CACHE: Dict[str, Any] = {}
_ADSPOWER_LOCK = threading.Lock()
_ADSPOWER_LAST_CALL = [0.0]
_ADSPOWER_MIN_INTERVAL = 1.1  # AdsPower 本地 API 有每秒请求上限


def _throttle_global() -> None:
    """全局节流：所有调用（含多实例）共享 1.1 秒最小间隔，避免触发限流。"""
    with _ADSPOWER_LOCK:
        wait = _ADSPOWER_LAST_CALL[0] + _ADSPOWER_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _ADSPOWER_LAST_CALL[0] = time.monotonic()


class AdsPowerClient:
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        config = config or {}
        self.base_url = (
            str(base_url or config.get("adspower_base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
        )
        self.api_key = str(
            api_key if api_key is not None else config.get("adspower_api_key", "")
        ).strip()
        self.timeout = int(timeout or config.get("adspower_timeout_seconds") or DEFAULT_TIMEOUT)
        self._lock = threading.Lock()
        self._last_call = 0.0
        # The macOS system proxy (and any http_proxy) must not intercept the
        # loopback AdsPower API; build an opener that never proxies.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ── low level ─────────────────────────────────────────────────────────

    def _throttle(self, interval: float) -> None:
        with self._lock:
            wait = max(0.0, interval - (time.monotonic() - self._last_call))
            if wait:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        interval: float = 0.0,
        retries: int = 3,
    ) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_error = ""
        retries = max(retries, 5)
        for attempt in range(max(1, retries)):
            self._throttle(interval)
            _throttle_global()
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                last_error = f"AdsPower 返回 HTTP {exc.code}"
                if attempt + 1 < retries:
                    time.sleep(1.0 + attempt)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"无法连接 AdsPower（{self.base_url}）：{exc}"
                if attempt + 1 < retries:
                    time.sleep(1.0 + attempt)
                    continue
                break
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise AdsPowerError("AdsPower 返回格式异常") from exc
            if not isinstance(payload, dict):
                raise AdsPowerError("AdsPower 返回格式异常")
            if payload.get("code") == 0:
                return payload.get("data", payload)
            message = str(payload.get("msg") or "AdsPower 请求失败")
            if "Too many request" in message and attempt + 1 < retries:
                time.sleep(2.0 + attempt)
                continue
            raise AdsPowerError(message, payload.get("code"))
        raise AdsPowerError(last_error or "AdsPower 请求失败")

    # ── public calls ──────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        data = self._request("GET", "/status", interval=0.2)
        return data if isinstance(data, dict) else {}

    def list_profiles(
        self, page: int = 1, limit: int = PAGE_LIMIT, **filters: Any
    ) -> Dict[str, Any]:
        body = {"page": int(page), "limit": max(1, min(PAGE_LIMIT, int(limit))), **filters}
        data = self._request(
            "POST", "/api/v2/browser-profile/list", body=body, interval=LIST_INTERVAL
        )
        return data if isinstance(data, dict) else {}

    def list_all_profiles(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            data = self.list_profiles(page=page, limit=PAGE_LIMIT)
            chunk = data.get("list") if isinstance(data, dict) else None
            if not isinstance(chunk, list) or not chunk:
                break
            rows.extend(item for item in chunk if isinstance(item, dict))
            if len(chunk) < PAGE_LIMIT:
                break
            page += 1
        return rows

    def active(self, profile_id: str) -> Dict[str, Any]:
        if not is_env_id(profile_id):
            raise AdsPowerError("环境 ID 无效")
        data = self._request(
            "GET", "/api/v2/browser-profile/active", params={"profile_id": profile_id}
        )
        return data if isinstance(data, dict) else {}

    def profile(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """Return one raw profile row (contains proxy credentials; keep private)."""
        if not is_env_id(profile_id):
            raise AdsPowerError("环境 ID 无效")
        # AdsPower 有每秒请求上限：同一环境短时间内复用结果（跨实例共享）。
        cache_key = f"{self.base_url}|{profile_id}"
        cached = _PROFILE_CACHE.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < 180:
            return cached[1]
        data = self._request(
            "POST",
            "/api/v2/browser-profile/list",
            body={"profile_id": [profile_id], "page": 1, "limit": 1},
            interval=LIST_INTERVAL,
        )
        rows = data.get("list") if isinstance(data, dict) else None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            _PROFILE_CACHE[cache_key] = (now, rows[0])
            return rows[0]
        return None

    def cookies(self, profile_id: str) -> List[Dict[str, Any]]:
        """Read the environment's cookie list without starting the browser."""
        if not is_env_id(profile_id):
            raise AdsPowerError("环境 ID 无效")
        data = self._request(
            "GET",
            "/api/v2/browser-profile/cookies",
            params={"profile_id": profile_id},
            interval=LIST_INTERVAL,
        )
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            raw = data.get("cookies")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = []
            if isinstance(raw, list):
                return [item for item in raw if isinstance(item, dict)]
            rows = data.get("list")
            return [item for item in rows or [] if isinstance(item, dict)]
        return []

    def proxy(self, profile_id: str) -> Dict[str, str]:
        """Return the environment's SOCKS5 proxy settings, or an empty dict."""
        row = self.profile(profile_id)
        config = (row or {}).get("user_proxy_config") if row else None
        if not isinstance(config, dict):
            return {}
        host = str(config.get("proxy_host") or "").strip()
        if not host:
            return {}
        return {
            "type": str(config.get("proxy_type") or "").strip().lower(),
            "host": host,
            "port": str(config.get("proxy_port") or "443").strip(),
            "username": str(config.get("proxy_user") or ""),
            "password": str(config.get("proxy_password") or ""),
        }

    def start(self, profile_id: str, **options: Any) -> Dict[str, Any]:
        if not is_env_id(profile_id):
            raise AdsPowerError("环境 ID 无效")
        data = self._request(
            "POST",
            "/api/v2/browser-profile/start",
            body={"profile_id": profile_id, **options},
        )
        return data if isinstance(data, dict) else {}

    def stop(self, profile_id: str) -> Dict[str, Any]:
        if not is_env_id(profile_id):
            raise AdsPowerError("环境 ID 无效")
        data = self._request(
            "POST", "/api/v2/browser-profile/stop", body={"profile_id": profile_id}
        )
        return data if isinstance(data, dict) else {}
