"""Headless Crown member login/heartbeat over HTTP (no browser).

Ported from the account-env ``hga038_login`` / ``hga038_keeper`` flow:

* the AdsPower environment supplies only its SOCKS5 proxy;
* login posts ``p=chk_login`` to ``transform_nl.php`` (fallback ``transform.php``);
* cookies ``login_<mid>`` + uid are saved locally;
* heartbeat posts ``p=get_member_data`` to ``transform.php``;
* the existing ``session_meta.json`` / ``storage_state.json`` contract is kept,
  so the merge page and follow engine keep working unchanged.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from ..adspower import AdsPowerClient
from ..net import HttpClient, parse_set_cookies
from .crown_member import _parse_member_body, cookie_header, ping_member

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)
LOGIN_PATHS = ("/transform_nl.php?ver=", "/transform.php?ver=")
HEARTBEAT_PATHS = ("/transform.php?ver=", "/transform_nl.php?ver=")
_PROXY_CACHE: Dict[tuple, Dict[str, str]] = {}
_PROXY_CACHE_LOCK = threading.Lock()


class CrownHttpError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


class CrownHttpSession:
    """One Crown member account backed by the environment's SOCKS5 proxy."""

    def __init__(self, account: Dict[str, Any], config: Dict[str, Any], store) -> None:
        self.account = dict(account)
        self.config = dict(config)
        self.store = store
        self.base_url = str(account.get("base_url") or "").rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise CrownHttpError("皇冠会员站点地址无效")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.env_id = _text(account.get("env_id"))
        self.paths = store.paths(account["id"])
        self.storage_path = self.paths["storage"]
        self.credentials = store.credentials(account["id"])
        self.username = _text(self.credentials.get("username") or account.get("username"))
        self.password = str(self.credentials.get("password") or "")
        self.user_agent = (
            str(self.config.get("crown_http_user_agent") or DEFAULT_UA).strip() or DEFAULT_UA
        )
        self.langx = "zh-cn"
        self.timeout = int(self.config.get("pool_check_timeout_seconds") or 10)
        self.ignore_https = bool(self.config.get("pool_crown_ignore_https_errors", True))
        self.cookies: Dict[str, str] = {}
        self.meta: Dict[str, Any] = {}
        self._proxy: Optional[Dict[str, str]] = None
        self._network: Dict[str, str] = {}
        self._load_session()

    # ── session files ─────────────────────────────────────────────────────

    def _load_session(self) -> None:
        try:
            storage = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            storage = {}
        if not isinstance(storage, dict):
            storage = {}
        for item in cookie_header(storage, self.base_url).split("; "):
            name, separator, value = item.partition("=")
            if name and separator:
                self.cookies[name] = value
        self.meta = self.store.load_meta(self.account["id"])

    def _cookie_domain(self) -> str:
        # Playwright/cookie_header expect a bare host for IP literals and a
        # leading dot for real domains.
        if re.fullmatch(r"[0-9.]+", self.host):
            return self.host
        return f".{self.host}"

    def _storage(self) -> Dict[str, Any]:
        domain = self._cookie_domain()
        return {
            "cookies": [
                {"name": name, "value": value, "domain": domain, "path": "/"}
                for name, value in self.cookies.items()
            ],
            "origins": [],
        }

    def _save_session(self) -> None:
        try:
            self.store._write_private(self.storage_path, self._storage())
        except OSError:
            pass
        if "saved_at" not in self.meta:
            self.meta["saved_at"] = str(int(time.time()))
        if self._network:
            self.meta.update(
                {
                    "net_mode": self._network.get("mode", ""),
                    "net_host": self._network.get("host", ""),
                    "net_port": self._network.get("port", ""),
                    "net_ip": self._network.get("ip", ""),
                    "net_country": self._network.get("country", ""),
                }
            )
        self.store.save_meta(self.account["id"], self.meta)

    # ── proxy / HTTP ──────────────────────────────────────────────────────

    def _proxy_config(self) -> Dict[str, str]:
        if self._proxy is not None:
            return self._proxy
        if not self.env_id:
            self._proxy = {}
            self._network = {"mode": "direct", "country": "", "ip": "", "host": "", "port": ""}
            return self._proxy
        cached_proxy = self._load_cached_proxy()
        client = AdsPowerClient(self.config)
        try:
            row = client.profile(self.env_id) or {}
            self._proxy = client.proxy(self.env_id)
            self._save_cached_proxy(self._proxy)
        except Exception as exc:  # noqa: BLE001
            # AdsPower 限流时不阻断：用上次缓存的代理继续工作。
            if cached_proxy is not None:
                self._proxy = cached_proxy
                row = {"ip": self.meta.get("net_ip"), "ip_country": self.meta.get("net_country")}
            else:
                raise CrownHttpError(f"读取 AdsPower 会员环境代理失败：{exc}") from exc
        proxy = self._proxy or {}
        self._network = {
            "mode": "env_proxy" if proxy.get("host") else "direct",
            "host": str(proxy.get("host") or ""),
            "port": str(proxy.get("port") or ""),
            "ip": str(row.get("ip") or ""),
            "country": str(row.get("ip_country") or "").upper(),
        }
        return self._proxy

    def _proxy_cache_key(self) -> tuple:
        return (
            str(self.paths["dir"].resolve()),
            str(self.config.get("adspower_base_url") or "http://localhost:50325"),
            self.env_id,
        )

    def _load_cached_proxy(self) -> Optional[Dict[str, str]]:
        """进程内复用代理配置，避免把代理用户名和密码写入本机文件。"""
        with _PROXY_CACHE_LOCK:
            proxy = _PROXY_CACHE.get(self._proxy_cache_key())
            return dict(proxy) if proxy is not None else None

    def _save_cached_proxy(self, proxy: Dict[str, str]) -> None:
        with _PROXY_CACHE_LOCK:
            _PROXY_CACHE[self._proxy_cache_key()] = dict(proxy or {})

    def _client(self, timeout: Optional[float] = None) -> HttpClient:
        return HttpClient(
            self.host,
            scheme=self.scheme,
            port=self.port,
            proxy=self._proxy_config(),
            timeout=timeout or max(10, self.timeout),
            user_agent=self.user_agent,
            verify=not self.ignore_https,
        )

    def _update_cookies(self, headers: Dict[str, list]) -> None:
        for name, value, _domain in parse_set_cookies(headers):
            self.cookies[name] = value

    def _transport(self, url: str, fields: Dict[str, str], headers: Dict[str, str], timeout: int):
        parsed = urlsplit(url)
        client = HttpClient(
            parsed.hostname or self.host,
            scheme=parsed.scheme or self.scheme,
            port=parsed.port or (443 if parsed.scheme == "https" else 80),
            proxy=self._proxy_config(),
            timeout=max(5, timeout),
            user_agent=self.user_agent,
            verify=not self.ignore_https,
        )
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            response = client.request(
                "POST",
                path,
                body=urllib.parse.urlencode(fields).encode("utf-8"),
                headers=headers,
            )
            self._update_cookies(response.headers)
            return response.status, response.body
        finally:
            client.close()

    # ── login ─────────────────────────────────────────────────────────────

    def login(
        self,
        *,
        force: bool = False,
        should_stop: Optional[Callable[[], bool]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        if should_stop is not None and should_stop():
            raise CrownHttpError("登录已取消")
        if force:
            self.cookies = {}
            self.meta = {}
        if not force and self.meta.get("uid") and self.cookies:
            result = self.ping()
            if result["status"] == "online":
                if on_status:
                    on_status("已恢复保存的皇冠会员会话")
                return self._profile(result)
        if not self.username or not self.password:
            raise CrownHttpError("皇冠会员缺少登录账号或密码")
        if on_status:
            on_status("正在 HTTP 登录皇冠会员（账号密码）…")
        self._login_flow(on_status=on_status, should_stop=should_stop)
        result = self.ping()
        if result["status"] != "online":
            raise CrownHttpError(result.get("message") or "皇冠会员登录后心跳未通过")
        self._apply_balance(result)
        self._save_session()
        if on_status:
            on_status(f"皇冠会员登录成功（uid={self.meta.get('uid')}）")
        return self._profile(result)

    def _login_flow(
        self,
        *,
        on_status: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> None:
        client = self._client()
        try:
            seed = client.request("GET", "/", headers={"Accept": "text/html"})
            self._update_cookies(seed.headers)
            form = {
                "p": "chk_login",
                "langx": self.langx,
                "ver": "",
                "username": self.username,
                "password_hex": self.password.encode().hex(),
                "app": "N",
                "auto": "GDACII",
                "userAgent": base64.b64encode(self.user_agent.encode()).decode(),
            }
            last = ""
            for path in LOGIN_PATHS:
                if should_stop is not None and should_stop():
                    raise CrownHttpError("登录已取消")
                response = client.request(
                    "POST",
                    path,
                    body=urllib.parse.urlencode(form).encode("utf-8"),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": self.base_url + "/",
                        "Origin": self.base_url,
                    },
                    cookies=self.cookies,
                )
                self._update_cookies(response.headers)
                text = response.text()
                parsed = self._parse_login(text)
                if parsed is not None:
                    self.meta.update(parsed)
                    self.meta.update(
                        {
                            "langx": self.langx,
                            "origin": self.base_url,
                            "user_agent": self.user_agent,
                            "env_id": self.env_id,
                            "saved_at": str(int(time.time())),
                        }
                    )
                    if on_status:
                        on_status("皇冠会员账号密码已通过，正在校验心跳…")
                    return
                last = text[:160]
            raise CrownHttpError(f"皇冠会员登录失败：{last or '接口未返回有效登录信息'}")
        finally:
            client.close()

    @staticmethod
    def _parse_login(text: str) -> Optional[Dict[str, str]]:
        if "serverresponse" not in text:
            return None
        fields: Dict[str, str] = {}
        for tag in ("status", "msg", "username", "passwd_safe", "mid", "uid", "currency"):
            match = re.search(rf"<{tag}>([^<]*)</{tag}>", text)
            if match:
                fields[tag] = match.group(1).strip()
        if fields.get("status") != "200" or fields.get("msg") != "100":
            return None
        if not fields.get("uid") or not fields.get("mid"):
            return None
        return {
            "uid": fields["uid"],
            "mid": fields["mid"],
            "ver": "",
            "username": fields.get("username") or fields.get("passwd_safe") or "",
            "currency": fields.get("currency") or "",
        }

    # ── heartbeat / restore ───────────────────────────────────────────────

    def ping(self) -> Dict[str, Any]:
        if not self.cookies or not self.meta.get("uid"):
            return {"status": "expired", "message": "还没有保存的会员会话，请先登录"}
        result: Dict[str, Any] = {"status": "expired", "message": "会员会话未验证"}
        # 出口网络偶发抖动，连续失败才算真失败，避免把网络问题当掉线。
        for attempt in range(3):
            result = ping_member(
                self.base_url,
                self._storage(),
                self.meta,
                timeout=self.timeout,
                ignore_https_errors=self.ignore_https,
                transport=self._transport,
                require_ver=False,
            )
            if result["status"] != "unavailable" or attempt == 2:
                break
            time.sleep(0.4)
        if result["status"] == "online":
            self._apply_balance(result)
            self._save_session()
        return result

    def _apply_balance(self, result: Dict[str, Any]) -> None:
        for key in ("balance", "balance_currency", "balance_checked_at"):
            if result.get(key):
                self.meta[key] = result[key]

    def _profile(self, result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "user_id": self.meta.get("mid") or 0,
            "user_code": self.meta.get("username") or self.username,
            "login_id": self.username,
            "balance": self.meta.get("balance"),
        }

    def check(self) -> Dict[str, Any]:
        """Return a ``ping_member``-style result and keep the snapshot fresh."""
        result = self.ping()
        if result["status"] in {"online", "conflict"}:
            self._save_session()
        return result

    def close(self) -> None:
        if self.cookies and self.meta.get("uid"):
            self._save_session()


def parse_heartbeat_text(text: str) -> Dict[str, Any]:
    """Expose the shared parser for tests/tools."""
    return _parse_member_body(text.encode("utf-8"))
