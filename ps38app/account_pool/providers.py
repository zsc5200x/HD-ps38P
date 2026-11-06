"""Login, session persistence and heartbeat providers for the account pool.

The three providers mirror the reference hdproc flows:

* ``ps38_backend`` reuses this project's PartnerSession (profile + storage state
  snapshot + profile API probe).
* ``crown_backend`` opens the agent backend in a persistent Chromium profile,
  lets the operator finish login/2FA, then saves Playwright storage state and
  checks the signed-in UI on a headless context.
* ``crown_member`` captures the real login response, initializes and stabilizes
  the session, then saves cookies and ``uid/ver/langx`` only after the observed
  ``get_member_data`` endpoint verifies the handoff. UI text is not proof of login.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlencode, urlsplit

from .crown_member import _parse_member_body, cookie_header, ping_member, preferred_origin
from .member_capture import MemberCapture

try:  # Playwright is an optional runtime dependency in the CLI/doctor flow.
    from playwright.sync_api import sync_playwright  # type: ignore
except Exception:  # pragma: no cover
    sync_playwright = None  # type: ignore


class ProviderError(RuntimeError):
    pass


@dataclass
class CheckResult:
    status: str
    message: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LoginResult:
    status: str
    message: str
    meta: Dict[str, Any] = field(default_factory=dict)


def _origin(value: str) -> str:
    parts = urlsplit(str(value or ""))
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _clean_meta(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, str] = {}
    for key in ("uid", "ver", "langx", "origin", "login_layer"):
        item = value.get(key)
        if isinstance(item, (str, int)):
            text = str(item).strip()
            if text and len(text) <= 2048 and not any(ord(char) < 32 for char in text):
                result[key] = text
    return result


class BaseProvider:
    platform = ""

    def __init__(self, config: Dict[str, Any], store) -> None:
        self.config = config
        self.store = store

    def login(
        self,
        account: Dict[str, Any],
        *,
        force: bool = False,
        should_stop: Optional[Callable[[], bool]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> LoginResult:
        raise NotImplementedError

    def check(self, account: Dict[str, Any]) -> CheckResult:
        raise NotImplementedError

    # ── Playwright helpers ────────────────────────────────────────────────

    def _require_playwright(self) -> None:
        if sync_playwright is None:
            raise ProviderError("未安装 Playwright，无法建立登录会话")

    def _launch_options(self, *, headless: bool) -> Dict[str, Any]:
        options: Dict[str, Any] = {
            "headless": bool(headless),
            "viewport": {"width": 1440, "height": 900},
        }
        channel = str(self.config.get("browser_channel") or "").strip()
        if channel:
            options["channel"] = channel
        return options

    def _launch_profile(self, profile_dir: Path, *, headless: bool):
        self._require_playwright()
        profile_dir.mkdir(parents=True, exist_ok=True)
        pw = sync_playwright().start()
        try:
            context = pw.chromium.launch_persistent_context(
                str(profile_dir), **self._launch_options(headless=headless)
            )
        except Exception as exc:  # noqa: BLE001
            try:
                pw.stop()
            except Exception:
                pass
            raw = " ".join(str(exc).split())
            if "Executable doesn't exist" in raw or "playwright install" in raw:
                raise ProviderError(
                    "未安装 Playwright Chromium；请运行 python run.py install-browser"
                ) from exc
            raise ProviderError(f"浏览器启动失败：{raw[:180]}") from exc
        page = context.pages[0] if context.pages else context.new_page()
        return pw, context, page

    def _close(self, pw, context) -> None:
        try:
            context.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass

    @staticmethod
    def _stop(should_stop: Optional[Callable[[], bool]]) -> bool:
        return bool(should_stop and should_stop())

    @staticmethod
    def _visible(page, selector: str):
        try:
            locator = page.locator(selector)
            count = min(int(locator.count()), 10)
            for index in range(count):
                item = locator.nth(index)
                try:
                    if item.is_visible():
                        return item
                except Exception:
                    continue
            return None
        except Exception:
            return None

    def _password_visible(self, page) -> bool:
        return any(
            self._visible(frame, 'input[type="password"]') is not None for frame in page.frames
        )

    @staticmethod
    def _body_text(page, limit: int = 1200) -> str:
        try:
            return str(page.locator("body").inner_text(timeout=1200))[:limit]
        except Exception:
            return ""

    def _signed_in_backend(self, page, login_url: str = "") -> bool:
        if page.is_closed() or self._password_visible(page):
            return False
        text = self._body_text(page).lower()
        if any(word in text for word in ("退出登录", "登出", "注销", "logout", "log out")):
            return True
        selectors = (
            '[class*="user-info"]',
            '[class*="userInfo"]',
            '[class*="account"]',
            '[class*="member"]',
            ".sidebar",
            ".side-menu",
        )
        if any(self._visible(page, selector) for selector in selectors):
            return True
        if login_url:
            try:
                current = urlsplit(str(page.url or "")).path.rstrip("/").lower()
                login_path = urlsplit(login_url).path.rstrip("/").lower()
                if current and current != login_path and "login" not in current:
                    return True
            except Exception:
                pass
        return False

    def _signed_in_member(self, page) -> bool:
        if page.is_closed() or self._password_visible(page):
            return False
        if any(
            self._visible(page, selector)
            for selector in (
                "#myAcc_page",
                ".btn_acc",
                "#user_info",
                '[aria-label="账户"]',
                "#header_todaywagers",
                "#header_historydata",
            )
        ):
            return True
        text = self._body_text(page).lower()
        if any(word in text for word in ("退出登录", "登出", "logout", "log out")):
            return True
        return bool(
            __import__("re").search(r"(?:rmb|cny|usd|hkd|余额|餘額)\s*[:：]?\s*[\d,]+", text)
        )

    @staticmethod
    def _dismiss_member_popup(page) -> None:
        for frame in page.frames:
            try:
                frame.evaluate(DISMISS_MEMBER_POPUP_JS)
            except Exception:
                continue

    def _fill_login(self, page, username: str, password: str) -> None:
        user_selectors = (
            "input[placeholder*='用户名'], input[placeholder*='账号'], "
            "input[name='username'], input[name='userName'], input[name='userid'], "
            "input[id*='user'], input[id*='account'], input[type='text']"
        )
        password_selectors = (
            "input[placeholder*='密码'], input[name='password'], input[name='pwd'], "
            "input[id*='password'], input[id*='pwd'], input[type='password']"
        )
        user = self._visible(page, user_selectors)
        pwd = self._visible(page, password_selectors)
        if not user or not pwd:
            return
        user.fill(username, timeout=8000)
        pwd.fill(password, timeout=8000)
        submit = page.locator(
            "button:has-text('Login'), button:has-text('登录'), button:has-text('登入'), "
            "button[type='submit'], input[type='submit'], #loginBtn, #btn_login, .lg_login_btn"
        ).first
        submit.click(timeout=8000)

    def _save_session(
        self, account: Dict[str, Any], context, meta: Dict[str, Any], *, storage_path: Path
    ) -> None:
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.store._write_private(storage_path, context.storage_state())
        if meta:
            self.store.save_meta(account["id"], meta)

    def _check_context(self, account: Dict[str, Any], probe_url: str, signed_in) -> CheckResult:
        paths = self.store.paths(account["id"])
        storage = paths["storage"]
        if not storage.is_file():
            return CheckResult("expired", "还没有保存的会话，请先登录")
        self._require_playwright()
        pw = sync_playwright().start()
        browser = None
        try:
            launch_options: Dict[str, Any] = {"headless": True}
            channel = str(self.config.get("browser_channel") or "").strip()
            if channel:
                launch_options["channel"] = channel
            browser = pw.chromium.launch(**launch_options)
            context = browser.new_context(
                storage_state=str(storage),
                viewport={"width": 1280, "height": 800},
                **self._context_options(),
            )
            page = context.new_page()
            try:
                page.goto(probe_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                return CheckResult("error", f"会话检测失败：{' '.join(str(exc).split())[:160]}")
            page.wait_for_timeout(1200)
            if page.is_closed():
                return CheckResult("error", "检测页面已关闭")
            try:
                cookies = context.cookies()
            except Exception:
                cookies = []
            if signed_in(page, cookies):
                try:
                    context.storage_state(path=str(storage))
                    storage.chmod(0o600)
                except Exception:
                    pass
                return CheckResult("online", "会话有效")
            if self._password_visible(page):
                return CheckResult("expired", "站点已返回登录表单，会话失效")
            return CheckResult("error", "无法确认登录状态；页面没有出现明确的登录或已登录标志")
        except Exception as exc:  # noqa: BLE001
            return CheckResult("error", f"会话检测失败：{' '.join(str(exc).split())[:160]}")
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            try:
                pw.stop()
            except Exception:
                pass

    def _wait_for_login(
        self,
        page,
        signed_in,
        *,
        timeout: int,
        should_stop: Optional[Callable[[], bool]],
        on_status: Optional[Callable[[str], None]],
    ) -> bool:
        deadline = time.monotonic() + max(30, timeout)
        last = 0.0
        while time.monotonic() < deadline:
            if self._stop(should_stop):
                raise ProviderError("登录已取消")
            if page.is_closed():
                raise ProviderError("登录窗口已关闭")
            if signed_in(page):
                return True
            now = time.monotonic()
            if on_status and now - last >= 5:
                on_status("等待你在浏览器完成登录/二次验证，窗口会保持打开")
                last = now
            page.wait_for_timeout(800)
        raise ProviderError("登录超时，未检测到有效会话")

    def _context_options(self) -> Dict[str, Any]:
        return {}


DISMISS_MEMBER_POPUP_JS = """
(() => {
  const dialog = document.querySelector('#C_alert_confirm')
    || document.querySelector('#alert_confirm');
  if (!dialog) return false;
  if (!dialog.getClientRects().length || getComputedStyle(dialog).display === 'none') return false;
  let acted = false;
  dialog.querySelectorAll('input[type=checkbox]').forEach((box) => {
    try { if (!box.checked) { box.click(); acted = true; } } catch (error) {}
  });
  const no = dialog.querySelector('#C_no_btn')
    || dialog.querySelector('#no_btn')
    || dialog.querySelector('.btn_cancel');
  if (no) { no.click(); acted = true; }
  return acted;
})()
"""


class CrownProvider(BaseProvider):
    """Crown member/agent backend login and heartbeat."""

    def __init__(self, config, store, platform: str) -> None:
        super().__init__(config, store)
        self.platform = platform

    def _context_options(self) -> Dict[str, Any]:
        return {
            "ignore_https_errors": bool(self.config.get("pool_crown_ignore_https_errors", True))
        }

    def _launch_options(self, *, headless: bool) -> Dict[str, Any]:
        return {**super()._launch_options(headless=headless), **self._context_options()}

    def _login_url(self, account: Dict[str, Any]) -> str:
        return account["base_url"].rstrip("/") + (account["login_path"] or "/")

    def _probe_url(self, account: Dict[str, Any]) -> str:
        path = account.get("dashboard_path") or "/"
        return account["base_url"].rstrip("/") + path

    def _signed_in(self, page, account: Dict[str, Any], cookies=None) -> bool:
        if self.platform == "crown_member":
            return self._signed_in_member(page)
        if not self._password_visible(page) and isinstance(cookies, list):
            # hdproc treats a live lpsid cookie (plus no login form) as the
            # backend session handoff. It is a stronger signal than menu text.
            for cookie in cookies:
                if (
                    str(cookie.get("name") or "").lower() == "lpsid"
                    and str(cookie.get("value") or "").strip()
                ):
                    return True
        return self._signed_in_backend(page, self._login_url(account))

    def _capture_meta(self, page, account: Dict[str, Any]) -> Dict[str, str]:
        try:
            values = page.evaluate(
                """() => {
                    const domain = window._CHDomain || {};
                    const pick = (key) => {
                        try { return String(window[key] || domain[key] || ""); } catch (e) { return ""; }
                    };
                    return {
                        uid: pick("uid") || pick("userid") || "",
                        ver: pick("ver") || "",
                        langx: pick("langx") || "zh-cn",
                        login_layer: pick("login_layer") || "",
                    };
                }"""
            )
        except Exception:
            values = {}
        meta = _clean_meta(values)
        try:
            origin = _origin(str(page.url or ""))
        except Exception:
            origin = ""
        if origin:
            meta["origin"] = origin
        if "langx" not in meta:
            meta["langx"] = "zh-cn"
        if not meta.get("ver"):
            try:
                from urllib.parse import parse_qs

                query = parse_qs(urlsplit(str(page.url or "")).query)
                if query.get("ver"):
                    meta["ver"] = str(query["ver"][0])[:96]
            except Exception:
                pass
        meta["saved_at"] = str(int(time.time()))
        return meta

    def login(
        self,
        account: Dict[str, Any],
        *,
        force: bool = False,
        should_stop: Optional[Callable[[], bool]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> LoginResult:
        if self._use_env_browser(account):
            return self._login_member(
                account, force=force, should_stop=should_stop, on_status=on_status
            )
        if self._use_http_session(account):
            from .crown_http import CrownHttpSession

            session = CrownHttpSession(account, self.config, self.store)
            try:
                profile = session.login(force=force, should_stop=should_stop, on_status=on_status)
                meta = self.store.load_meta(account["id"])
                return LoginResult(
                    "online",
                    f"登录成功，账号 {profile.get('user_code', '')}",
                    meta,
                )
            except Exception as exc:  # noqa: BLE001
                text = " ".join(str(exc).split())
                raise ProviderError(text[:220] or "皇冠会员登录失败") from exc
            finally:
                session.close()
        if self.platform == "crown_member":
            return self._login_member(
                account, force=force, should_stop=should_stop, on_status=on_status
            )
        paths = self.store.paths(account["id"])
        if force:
            self.store._clear_session_files(account["id"])  # noqa: SLF001 - intentional reset
        pw, context, page = self._launch_profile(paths["profile_dir"], headless=False)
        try:
            login_url = self._login_url(account)
            if on_status:
                on_status(f"正在打开登录页：{login_url}")
            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"无法打开登录页：{' '.join(str(exc).split())[:180]}") from exc
            try:
                page.bring_to_front()
            except Exception:
                pass

            credentials = self.store.credentials(account["id"])
            username = credentials.get("username") or account.get("username") or ""
            password = credentials.get("password") or ""
            if username and password and self._password_visible(page):
                try:
                    self._fill_login(page, username, password)
                    if on_status:
                        on_status("已自动填入账号密码，请在浏览器完成验证码/二次验证")
                except Exception:
                    if on_status:
                        on_status("自动填充未完成，请在浏览器手动登录")

            def ready(current):
                try:
                    cookies = context.cookies()
                except Exception:
                    cookies = []
                return self._signed_in(current, account, cookies)

            if not ready(page):
                if on_status:
                    on_status("请在浏览器完成登录；程序只保存会话，不会重复提交登录")
                self._wait_for_login(
                    page,
                    ready,
                    timeout=int(self.config.get("pool_login_timeout_seconds", 300)),
                    should_stop=should_stop,
                    on_status=on_status,
                )
            meta = self._capture_meta(page, account)
            self._save_session(account, context, meta, storage_path=paths["storage"])
            return LoginResult("online", "登录成功，会话已保存", meta)
        finally:
            self._close(pw, context)

    def _member_ping(self, account, storage, meta, *, transport=None):
        return ping_member(
            account["base_url"],
            storage,
            meta,
            timeout=int(self.config.get("pool_check_timeout_seconds", 10)),
            ignore_https_errors=self._context_options()["ignore_https_errors"],
            transport=transport,
            require_ver=False,
        )

    @staticmethod
    def _member_snapshot(meta, result):
        snapshot = dict(meta or {})
        for key in ("balance", "balance_currency", "balance_checked_at"):
            if key in result:
                snapshot[key] = result[key]
        return snapshot

    def _restore_member(self, context, storage) -> None:
        # Persistent Chromium does not always retain session cookies on exit.
        # Restore the verified snapshot before navigating, including localStorage.
        if storage.get("cookies"):
            context.add_cookies(storage["cookies"])
        origins = storage.get("origins") or []
        if origins:
            context.add_init_script(
                """(() => {
                const states = """
                + json.dumps(origins)
                + """;
                for (const state of states) {
                    if (location.origin !== state.origin) continue;
                    for (const item of state.localStorage || []) {
                        try {
                            if (localStorage.getItem(item.name) === null) localStorage.setItem(item.name, item.value);
                        } catch (e) {}
                    }
                }
            })();"""
            )

    def _initialize_member(self, context, meta, should_stop, *, page=None) -> None:
        from urllib.parse import urlencode

        ver = str(meta.get("ver") or "")
        url = meta["origin"] + "/transform.php?" + urlencode({"ver": ver})
        for operation, extra in (
            ("memSet", {"action": "check"}),
            ("service_mainget", {"login": "Y"}),
            ("get_version", {}),
        ):
            if self._stop(should_stop):
                raise ProviderError("登录已取消")
            try:
                fields = {
                    "p": operation,
                    "uid": meta["uid"],
                    "ver": ver,
                    "langx": meta.get("langx", "zh-cn"),
                    **extra,
                }
                if page is not None:
                    self._browser_post(page, url, fields, 10000)
                else:
                    response = context.request.post(
                        url, form=fields, timeout=10000, max_redirects=0
                    )
                    response.dispose()
            except Exception as exc:
                raise ProviderError(
                    "会员登录后初始化请求失败，原保存会话未覆盖，请稍后重试"
                ) from exc

    def _use_http_session(self, account: Dict[str, Any]) -> bool:
        """皇冠会员是否用纯 HTTP 会话（账号级 http 或全局开关）。"""
        if self.platform != "crown_member":
            return False
        if str(account.get("session_backend") or "").strip().lower() == "http":
            return True
        return str(self.config.get("crown_transport") or "browser").lower() == "http"

    def _use_env_browser(self, account: Dict[str, Any]) -> bool:
        if self.platform != "crown_member" or not account.get("env_id"):
            return False
        # 账号级开关优先：session_backend=http 表示只用环境网络走接口，不开浏览器。
        backend = str(account.get("session_backend") or "").strip().lower()
        if backend == "http":
            return False
        return str(self.config.get("crown_transport") or "browser").lower() != "http"

    def _stable_member(self, context, page, capture, should_stop):
        previous = None
        count = 0
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self._stop(should_stop):
                raise ProviderError("登录已取消")
            meta = capture.collect(page)
            storage = context.storage_state()
            cookie = cookie_header(storage, meta.get("origin", ""))
            identity = (meta.get("origin"), meta.get("uid"), meta.get("ver"), cookie)
            valid = bool(meta.get("origin") and meta.get("uid") and cookie)
            count = count + 1 if valid and identity == previous else (1 if valid else 0)
            previous = identity
            if count >= 3:
                return meta, storage
            page.wait_for_timeout(500)
        raise ProviderError("会员登录信息尚未稳定，未覆盖原会话，请稍后重试登录")

    def _login_member(
        self, account, *, force=False, should_stop=None, on_status=None
    ) -> LoginResult:
        paths = self.store.paths(account["id"])
        saved = self.store._read_json(paths["storage"])
        saved_meta = self.store.load_meta(account["id"])
        use_env = self._use_env_browser(account)
        if use_env and not saved_meta.get("browser_env"):
            saved = {}
        if not force and saved and saved_meta.get("uid"):
            if on_status:
                on_status("正在验证并恢复已保存的会员会话…")
            if use_env:
                checked = self._check_env_browser(account)
                result = {**checked.meta, "status": checked.status, "message": checked.message}
            else:
                result = self._member_ping(account, saved, saved_meta)
            if result["status"] == "online":
                snapshot = self._member_snapshot(saved_meta, result)
                self.store.save_meta(account["id"], snapshot)
                return LoginResult("online", "已恢复保存的会员会话，在线检查通过", snapshot)
        if self._stop(should_stop):
            raise ProviderError("登录已取消")
        if force:
            self.store._clear_session_files(account["id"])
            saved = {}
        env_browser = None
        if use_env:
            from ..env_browser import EnvBrowser

            env_browser = EnvBrowser(self.config, account["env_id"])
            context, page = env_browser.open()
            pw = None
            if force:
                try:
                    context.clear_cookies()
                except Exception:
                    pass
        else:
            pw, context, page = self._launch_profile(paths["profile_dir"], headless=False)
        capture = MemberCapture()
        context.on("response", capture.response)
        context.route("**/transform.php*", capture.login_route)
        context.route("**/transform_nl.php*", capture.login_route)
        try:
            if saved:
                self._restore_member(context, saved)
            if on_status:
                on_status("正在打开会员登录页，请完成登录/验证码…")
            try:
                page.goto(self._login_url(account), wait_until="domcontentloaded", timeout=45000)
            except Exception:
                # A route may still be redirecting when domcontentloaded times out.
                if page.is_closed() or str(page.url) == "about:blank":
                    raise ProviderError("无法打开会员登录页，请检查站点地址和网络")
            page.bring_to_front()
            deadline = time.monotonic() + int(self.config.get("pool_login_timeout_seconds", 300))
            credentials = self.store.credentials(account["id"])
            filled = False
            initialized_uid = ""
            last_probe = 0.0
            last_hint = 0.0
            while time.monotonic() < deadline:
                if self._stop(should_stop):
                    raise ProviderError("登录已取消")
                pages = [p for p in context.pages if not p.is_closed()]
                if not pages:
                    raise ProviderError("登录窗口已关闭")
                # Prefer the page which issued the authenticated handoff (popups).
                page = next(
                    (p for p in reversed(pages) if _origin(p.url) == capture.meta.get("origin")),
                    pages[-1],
                )
                if not filled and credentials.get("password"):
                    for frame in page.frames:
                        if self._visible(frame, 'input[type="password"]'):
                            filled = True  # Never repeatedly submit credentials/captcha.
                            try:
                                self._fill_login(
                                    frame,
                                    credentials.get("username") or account["username"],
                                    credentials["password"],
                                )
                            except Exception:
                                pass
                            break
                self._dismiss_member_popup(page)
                meta = capture.collect(page)
                if (
                    meta.get("uid")
                    and not self._password_visible(page)
                    and time.monotonic() - last_probe >= 3
                ):
                    last_probe = time.monotonic()
                    if capture.fresh_login and initialized_uid != meta["uid"]:
                        if on_status:
                            on_status("已收到会员登录响应，正在初始化并等待会话稳定…")
                        self._initialize_member(
                            context, meta, should_stop, page=page if use_env else None
                        )
                        initialized_uid = meta["uid"]
                    meta, storage = self._stable_member(context, page, capture, should_stop)

                    def transport(url, fields, headers, timeout):
                        response = context.request.post(
                            url,
                            form=fields,
                            headers=headers,
                            timeout=timeout * 1000,
                            max_redirects=0,
                        )
                        try:
                            return response.status, response.body()
                        finally:
                            response.dispose()

                    result = (
                        self._browser_member_ping(page, meta, meta["origin"])
                        if use_env
                        else self._member_ping(account, storage, meta, transport=transport)
                    )
                    if result["status"] == "online":
                        if self._stop(should_stop):
                            raise ProviderError("登录已取消")
                        meta["saved_at"] = str(int(time.time()))
                        if use_env:
                            meta["browser_env"] = "yes"
                        meta = self._member_snapshot(meta, result)
                        self._save_session(account, context, meta, storage_path=paths["storage"])
                        return LoginResult(
                            "online", "会员登录成功，会话已稳定保存且在线检查通过", meta
                        )
                    if on_status:
                        on_status(result["message"] + "；等待在浏览器完成登录")
                if on_status and time.monotonic() - last_hint >= 10:
                    on_status("等待在浏览器完成会员登录/验证码；验证通过后才保存会话")
                    last_hint = time.monotonic()
                page.wait_for_timeout(800)
            raise ProviderError("会员登录超时，未获得可验证的会话，原保存会话未覆盖")
        finally:
            if env_browser is not None:
                env_browser.close()
            else:
                self._close(pw, context)

    @staticmethod
    def _browser_post(page, url, fields, timeout_ms):
        return page.evaluate(
            """async ({url, fields, timeout}) => {
              const controller = new AbortController();
              const timer = setTimeout(() => controller.abort(), timeout);
              try {
                const res = await fetch(url, {
                  method: 'POST', credentials: 'include',
                  headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                  body: new URLSearchParams(fields).toString(), signal: controller.signal,
                });
                return {status: res.status, text: await res.text()};
              } finally { clearTimeout(timer); }
            }""",
            {"url": url, "fields": fields, "timeout": timeout_ms},
        )

    def _browser_member_ping(self, page, meta: Dict[str, Any], origin: str) -> Dict[str, Any]:
        uid = str(meta.get("uid") or "").strip()
        ver = str(meta.get("ver") or "").strip()
        langx = str(meta.get("langx") or "zh-cn").strip() or "zh-cn"
        if not uid:
            return {"status": "expired", "message": "会话缺少 uid，请重新登录"}
        url = origin.rstrip("/") + "/transform.php?" + urlencode({"ver": ver})
        fields = {"ver": ver, "p": "get_member_data", "uid": uid, "langx": langx, "change": "all"}
        last_error = None
        for attempt in range(2):
            try:
                payload = self._browser_post(
                    page,
                    url,
                    fields,
                    max(1, int(self.config.get("pool_check_timeout_seconds", 10))) * 1000,
                )
                status = int(payload.get("status") or 0)
                text = str(payload.get("text") or "")
                if status in (401, 440):
                    return {"status": "expired", "message": "皇冠会员明确拒绝当前会话"}
                if status != 200:
                    return {
                        "status": "unavailable",
                        "message": f"会员在线检查失败（HTTP {status}）",
                    }
                return _parse_member_body(text.encode("utf-8"))
            except Exception as exc:  # page may still be redirecting
                last_error = exc
                try:
                    page.wait_for_timeout(600)
                except Exception:
                    break
        return {
            "status": "unavailable",
            "message": f"会员浏览器心跳失败：{type(last_error).__name__}",
        }

    def _check_env_browser(self, account: Dict[str, Any]) -> CheckResult:
        from ..env_browser import EnvBrowser

        paths = self.store.paths(account["id"])
        meta = self.store.load_meta(account["id"])
        saved = self.store._read_json(paths["storage"])
        if not saved or not meta.get("uid"):
            return CheckResult("expired", "还没有保存的会员会话，请先登录", meta)
        if meta.get("browser_env") != "yes":
            # An HTTP-created cookie must be rebound by a real browser login
            # before we claim the environment fingerprint.
            return CheckResult("expired", "需要切换到 AdsPower 浏览器会话", meta)
        env_browser = EnvBrowser(self.config, account["env_id"])
        try:
            context, page = env_browser.open()
            self._restore_member(context, saved)
            # 优先用域名入口：裸 IP 的 TLS 不稳定，心跳会随机失败。
            origin = preferred_origin(meta, saved, account.get("base_url") or "")
            try:
                page.goto(origin, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(1_500)
                self._dismiss_member_popup(page)
            except Exception:
                pass
            page_origin = _origin(str(page.url or "")) or origin
            result = self._browser_member_ping(page, meta, page_origin)
            status = str(result.get("status") or "unavailable")
            message = str(result.get("message") or "")
            if status == "online":
                meta = self._member_snapshot(meta, result)
                self._save_session(account, context, meta, storage_path=paths["storage"])
                return CheckResult("online", message or "会员在线检查通过", meta)
            if status in {"expired", "conflict"}:
                return CheckResult("expired", message or "会员会话已失效", meta)
            return CheckResult("error", message or "会员在线检查暂时失败", meta)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                "error",
                f"会员浏览器心跳失败：{' '.join(str(exc).split())[:160]}",
                meta,
            )
        finally:
            env_browser.close()

    def check(self, account: Dict[str, Any]) -> CheckResult:
        paths = self.store.paths(account["id"])
        meta = self.store.load_meta(account["id"])
        if self._use_env_browser(account):
            return self._check_env_browser(account)
        if self._use_http_session(account):
            from .crown_http import CrownHttpSession

            session = CrownHttpSession(account, self.config, self.store)
            try:
                result = session.check()
                meta = self.store.load_meta(account["id"])
                status = str(result.get("status") or "unavailable")
                message = str(result.get("message") or "")
                if status == "online":
                    return CheckResult("online", message or "会员在线检查通过", meta)
                if status in {"expired", "conflict"}:
                    return CheckResult("expired", message or "会员会话已失效", meta)
                return CheckResult("error", message or "会员在线检查暂时失败", meta)
            except Exception as exc:  # noqa: BLE001
                return CheckResult(
                    "error", f"会员 HTTP 检查失败：{' '.join(str(exc).split())[:160]}", meta
                )
            finally:
                session.close()
        if self.platform == "crown_member" and meta.get("uid"):
            result = self._member_ping(account, self.store._read_json(paths["storage"]), meta)
            status = result.get("status", "unavailable")
            message = str(result.get("message") or "")
            if status == "online":
                return CheckResult(
                    "online", message or "会员在线检查通过", self._member_snapshot(meta, result)
                )
            if status == "expired":
                return CheckResult("expired", message or "会员会话已失效", meta)
            if status == "conflict":
                return CheckResult("expired", message or "皇冠提示重复登录", meta)
            # Network/format problems are not proof of expiry; keep the session.
            return CheckResult("error", message or "会员在线检查暂时失败", meta)
        if self.platform == "crown_member":
            return CheckResult(
                "expired", "会员会话缺少 uid，请点击登录补全；仅有页面标志不能确认在线"
            )
        return self._check_context(
            account,
            self._probe_url(account),
            lambda page, cookies: self._signed_in(page, account, cookies),
        )


class PS38Provider(BaseProvider):
    platform = "ps38_backend"

    def _session_config(self, account: Dict[str, Any], *, headless: bool) -> Dict[str, Any]:
        paths = self.store.paths(account["id"])
        credentials = self.store.credentials(account["id"])
        return {
            "base_url": account["base_url"],
            "login_path": account["login_path"],
            "dashboard_path": account.get("dashboard_path")
            or "/partner-ui-v2/report-outstanding-v2",
            "username": credentials.get("username") or account.get("username") or "",
            "password": credentials.get("password") or "",
            "totp_secret": credentials.get("totp_secret") or "",
            "ps38_captcha_command": self.config.get("ps38_captcha_command") or "",
            "ps38_captcha_timeout": self.config.get("ps38_captcha_timeout") or 30,
            "ps38_captcha_dir": self.config.get("ps38_captcha_dir") or "",
            "profile_dir": str(paths["profile_dir"]),
            "headless": bool(headless),
            "browser_channel": self.config.get("browser_channel") or "",
            "minimize_after_login": False,
            "adspower_env_id": str(account.get("env_id") or ""),
            "adspower_base_url": self.config.get("adspower_base_url") or "",
            "adspower_api_key": self.config.get("adspower_api_key") or "",
            "adspower_timeout_seconds": self.config.get("adspower_timeout_seconds") or 20,
            "adspower_headless": self.config.get("adspower_headless", True),
            "ps38_http_timeout": self.config.get("ps38_http_timeout") or 30,
            "ps38_http_user_agent": self.config.get("ps38_http_user_agent") or "",
        }

    def _http_mode(self, account: Dict[str, Any]) -> bool:
        if not account.get("env_id"):
            return False
        return str(self.config.get("ps38_transport") or "auto").lower() in {"auto", "http"}

    def login(
        self,
        account: Dict[str, Any],
        *,
        force: bool = False,
        should_stop: Optional[Callable[[], bool]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> LoginResult:
        from ..ps38_http import Ps38HttpSession
        from ..session import PartnerSession

        paths = self.store.paths(account["id"])
        paths["profile_dir"].mkdir(parents=True, exist_ok=True)
        config = self._session_config(account, headless=False)
        session = (
            Ps38HttpSession(config, debug=False)
            if self._http_mode(account)
            else PartnerSession(config, debug=False)
        )
        try:
            profile = session.login(force=force, on_status=on_status, should_stop=should_stop)
            meta = {
                "uid": str(profile.get("user_id") or ""),
                "ver": "",
                "langx": "",
                "origin": account["base_url"],
                "env_id": str(account.get("env_id") or ""),
                "saved_at": str(int(time.time())),
            }
            self.store.save_meta(account["id"], meta)
            return LoginResult("online", f"登录成功，账号 {profile.get('user_code', '')}", meta)
        except Exception as exc:  # noqa: BLE001
            text = " ".join(str(exc).split())
            raise ProviderError(text[:220] or "PS38 登录失败") from exc
        finally:
            session.close()

    def check(self, account: Dict[str, Any]) -> CheckResult:
        from ..ps38_http import Ps38HttpSession
        from ..session import PartnerSession
        from ..store import SessionExpired

        paths = self.store.paths(account["id"])
        if not paths["storage"].is_file() and not account.get("env_id"):
            return CheckResult("expired", "还没有保存的会话，请先登录")
        config = self._session_config(account, headless=True)
        if self._http_mode(account):
            session = Ps38HttpSession(config, debug=False)
            try:
                session.start()
                profile = session.fetch_profile()
                meta = {
                    "uid": str(profile.get("user_id") or ""),
                    "ver": "",
                    "langx": "",
                    "origin": account["base_url"],
                    "env_id": str(account.get("env_id") or ""),
                    "saved_at": str(int(time.time())),
                }
                return CheckResult("online", f"会话有效，账号 {profile.get('user_code', '')}", meta)
            except SessionExpired:
                return CheckResult("expired", "PS38 后台会话已失效，请重新登录")
            except Exception as exc:  # noqa: BLE001
                return CheckResult(
                    "error", f"PS38 会话检测失败：{' '.join(str(exc).split())[:160]}"
                )
            finally:
                session.close()
        session = PartnerSession(config, debug=False)
        try:
            session.start()
            page = session._page()  # noqa: SLF001 - provider owns this session
            page.goto(session.dashboard_url, wait_until="domcontentloaded", timeout=45000)
            profile = session.fetch_profile()
            meta = {
                "uid": str(profile.get("user_id") or ""),
                "ver": "",
                "langx": "",
                "origin": account["base_url"],
                "saved_at": str(int(time.time())),
            }
            return CheckResult("online", f"会话有效，账号 {profile.get('user_code', '')}", meta)
        except SessionExpired:
            return CheckResult("expired", "PS38 后台会话已失效，请重新登录")
        except Exception as exc:  # noqa: BLE001
            return CheckResult("error", f"PS38 会话检测失败：{' '.join(str(exc).split())[:160]}")
        finally:
            session.close()


def provider_for(account: Dict[str, Any], config: Dict[str, Any], store) -> BaseProvider:
    platform = account.get("platform")
    if platform == "ps38_backend":
        return PS38Provider(config, store)
    if platform in {"crown_backend", "crown_member"}:
        return CrownProvider(config, store, platform)
    raise ProviderError(f"不支持的账号类型：{platform}")
