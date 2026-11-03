"""Self-contained Playwright session for the PS38 partner reporting backend.

The browser profile lives inside this folder (``data/profile``). The session is
opened with persistent cookies and an extra ``storage_state.json`` snapshot, so
restarting the app does not require a new login while the cookie is valid.

When the session expires the app keeps the same browser window open and waits
for the operator to finish the site's login/2FA. It never stores a one-time
code and never submits the login form more than once per explicit request.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from .store import PollError, SessionExpired

try:
    from playwright.sync_api import sync_playwright  # type: ignore
except Exception:  # pragma: no cover - dependency checked by doctor/CLI
    sync_playwright = None  # type: ignore

PROFILE_PATH = "/partner-account/ddd/user-info/login-user/profile"
BET_LIST_PATH = "/partner-reporting/ddd/common/bet-list/v2"
HIERARCHY_PATH = "/partner-reporting/ddd/report/win-loss-detail"

FETCH_JS = """
async ({url, method, body}) => {
  const options = {
    method,
    credentials: 'include',
    headers: {'Accept': 'application/json, text/plain, */*'},
  };
  if (body !== null) {
    options.headers['content-type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const res = await fetch(url, {...options, signal: controller.signal});
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch (error) { json = null; }
    return {httpStatus: res.status, json, text: text.slice(0, 500)};
  } finally {
    clearTimeout(timer);
  }
}
"""


class LoginWindowClosed(PollError):
    """The operator closed the login window; a new login must be requested."""


def _text(value: Any) -> str:
    return str(value or "").strip()


class PartnerSession:
    def __init__(self, config: Dict[str, Any], *, debug: bool = False) -> None:
        self.base_url = str(config["base_url"]).rstrip("/")
        self.login_url = self.base_url + str(config.get("login_path") or "/partner/zh-cn/login")
        self.dashboard_url = self.base_url + str(
            config.get("dashboard_path") or "/partner-ui-v2/report-outstanding-v2"
        )
        self.username = _text(config.get("username"))
        self.password = str(config.get("password") or "")
        self.profile_dir = Path(config["profile_dir"])
        self.storage_path = self.profile_dir / "storage_state.json"
        self.headless = bool(config.get("headless", False))
        self.channel = str(config.get("browser_channel") or "") or None
        self.minimize_after_login = bool(config.get("minimize_after_login", False))
        self.debug = debug

        self._pw = None
        self.ctx = None
        self.page = None
        self.session_ready = False
        self.browser_hidden = False
        self.profile: Dict[str, Any] = {}

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.ctx is not None:
            return
        if sync_playwright is None:
            raise RuntimeError("未安装 Playwright，无法建立 PS38 登录会话")
        self._pw = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                viewport={"width": 1440, "height": 900},
                channel=self.channel,
            )
        except Exception as exc:  # noqa: BLE001 - surface a clear message
            try:
                self._pw.stop()
            finally:
                self._pw = None
            raw = str(exc)
            if "Executable doesn't exist" in raw or "playwright install" in raw:
                raise RuntimeError(
                    "未安装 Playwright Chromium；请运行 `python run.py install-browser`"
                ) from exc
            first_line = " ".join(raw.splitlines()[:1]) or raw
            raise RuntimeError(
                f"浏览器启动失败：{first_line[:160]}（请确认同一目录没有其他进程在运行）"
            ) from exc
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self._restore_storage_state()

    def close(self) -> None:
        try:
            self.save_storage_state()
            if self.ctx is not None:
                self.ctx.close()
        finally:
            self.ctx = None
            self.page = None
            self.session_ready = False
            self.browser_hidden = False
            self.profile = {}
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None

    def _restore_storage_state(self) -> None:
        if self.ctx is None or not self.storage_path.exists():
            return
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if isinstance(cookies, list) and cookies:
            try:
                self.ctx.add_cookies(cookies)
            except Exception:
                pass

    def save_storage_state(self) -> None:
        if self.ctx is None:
            return
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.ctx.storage_state(path=str(self.storage_path))
        except Exception:
            pass

    # ── low-level requests through the logged-in page ──────────────────────

    def _page(self):
        if self.page is None or self.page.is_closed():
            raise LoginWindowClosed("登录窗口已关闭，请点击“重新登录”重新建立会话")
        return self.page

    def _fetch(self, path: str, method: str = "GET",
               body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        page = self._page()
        url = self.base_url + path
        result = page.evaluate(FETCH_JS, {"url": url, "method": method, "body": body})
        if not isinstance(result, dict):
            raise PollError("后台接口返回格式无效")
        status = int(result.get("httpStatus") or 0)
        if status in (401, 403):
            self.session_ready = False
            raise SessionExpired(f"后台会话已失效（HTTP {status}）")
        return result

    def _fetch_required(self, path: str, method: str = "GET",
                        body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = self._fetch(path, method, body)
        if result.get("httpStatus") != 200 or not isinstance(result.get("json"), (dict, list)):
            self.session_ready = False
            raise SessionExpired("后台接口未返回 JSON，可能已退出登录")
        return result["json"]

    # ── partner reporting APIs ─────────────────────────────────────────────

    def fetch_profile(self) -> Dict[str, Any]:
        payload = self._fetch_required(PROFILE_PATH)
        profile = payload.get("profile") if isinstance(payload, dict) else None
        if not isinstance(profile, dict):
            raise SessionExpired("身份接口缺少 profile")
        user_id = profile.get("userId")
        user_code = profile.get("userCode")
        if (isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
                or not isinstance(user_code, str) or not user_code.strip()):
            raise SessionExpired("身份接口缺少有效 userId/userCode")
        self.profile = {
            "user_id": user_id,
            "user_code": user_code.strip(),
            "login_id": profile.get("loginId") or user_code,
        }
        return self.profile

    def fetch_orders(self, owner_user_id: int, agent: Dict[str, Any], status: str,
                     page: int, page_size: int,
                     from_date: Optional[str] = None,
                     to_date: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "betListType": "DETAIL",
            "ownerUserId": str(owner_user_id),
            "playerUserId": agent["user_id"],
            "selectedUserCode": agent["code"],
            "product": "SB",
            "status": status,
            "paging": {"pageNumber": page, "rowsPerPage": page_size},
        }
        if status == "SETTLED":
            body.update({
                "dateBy": "GRADED_DATE",
                "timezone": "+08:00",
                "fromDate": from_date,
                "toDate": to_date,
            })
        result = self._fetch(BET_LIST_PATH, "POST", body)
        return {"httpStatus": result.get("httpStatus"), "json": result.get("json")}

    def fetch_hierarchy(self, owner_user_id: int, from_date: str, to_date: str) -> Dict[str, Any]:
        body = {
            "timezone": "+08:00", "productCodes": ["SB"],
            "fromDate": from_date, "toDate": to_date,
            "ownerUserId": str(owner_user_id), "drillDownUserId": owner_user_id,
            "orderBy": {"colName": "user_code", "sort": "ASC"},
            "excludeTest": False, "dateBy": "GRADED_DATE",
            "searchOption": "USERNAME", "searchByText": "", "searchUserIds": "",
            "requestSearchByUser": {"searchByText": "", "selectedLevels": ["ALL"]},
            "groupType": "OWN_DOWNLINE", "groupId": 0, "userIds": None,
        }
        result = self._fetch(HIERARCHY_PATH, "POST", body)
        return {"httpStatus": result.get("httpStatus"), "json": result.get("json")}

    # ── login / 2FA ────────────────────────────────────────────────────────

    def open_login_page(self) -> None:
        """Show the login form after a detected session expiry."""
        try:
            page = self._page()
            page.goto(self.login_url, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            pass

    def _fill_login(self) -> None:
        if not self.username or not self.password:
            return  # Leave the native form to the operator.
        page = self._page()
        selectors_user = (
            "input[placeholder*='用户名'], input[placeholder*='账号'], "
            "input[name='username'], input[name='userName'], input[name='userid'], "
            "input[id*='user'], input[id*='account'], input[type='text']"
        )
        selectors_pwd = (
            "input[placeholder*='密码'], input[name='password'], input[name='pwd'], "
            "input[id*='password'], input[id*='pwd'], input[type='password']"
        )
        password_input = page.locator(selectors_pwd).first
        password_input.wait_for(state="visible", timeout=10_000)
        page.locator(selectors_user).first.fill(self.username, timeout=10_000)
        password_input.fill(self.password, timeout=10_000)
        page.locator(
            "button:has-text('Login'), button:has-text('登录'), button:has-text('登入'), "
            "button[type='submit'], input[type='submit']"
        ).first.click(timeout=10_000)

    def _pending_message(self) -> str:
        page = self.page
        if page is None:
            return "登录窗口尚未建立"
        try:
            text = page.locator("body").inner_text(timeout=1_000).lower()
            if "account temporarily disabled" in text or "your account has been locked" in text:
                return "账号暂时锁定，请等待网站解除锁定；不会自动重新提交登录"
        except Exception:
            pass
        return "等待你在浏览器完成登录/2FA，窗口会保持打开"

    def _probe(self) -> Optional[Dict[str, Any]]:
        """Session check driven by the profile API, not by the visible URL.

        The site redirects some partner URLs to the login page even when the
        cookie is still valid, so the URL alone must never decide the state.
        """
        page = self.page
        if page is None:
            return None
        try:
            current = urlparse(str(page.url))
            expected = urlparse(self.base_url)
            if (current.scheme, current.netloc) != (expected.scheme, expected.netloc):
                return None
        except Exception:
            return None
        try:
            return self.fetch_profile()
        except Exception:
            return None

    def login(self, *, force: bool = False,
              on_status: Optional[Callable[[str], None]] = None,
              should_stop: Optional[Callable[[], bool]] = None,
              on_tick: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
        """Ensure an authenticated session, waiting in the same window for 2FA."""
        self.start()
        page = self._page()
        if force:
            try:
                self.ctx.clear_cookies()
            except Exception:
                pass
            try:
                self.storage_path.unlink()
            except OSError:
                pass
            self.session_ready = False
            self.profile = {}

        # A persistent profile may already hold a valid cookie. The dashboard
        # path (not /partner/) is the URL that keeps a live session instead of
        # bouncing to the login form.
        try:
            page.goto(self.dashboard_url, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            page.goto(self.base_url, wait_until="domcontentloaded", timeout=45_000)
        profile = self._probe()
        if profile is not None:
            self.session_ready = True
            self.save_storage_state()
            if on_status:
                on_status(f"会话有效，账号 {profile['user_code']}")
            if self.minimize_after_login:
                self.minimize_window()
            return profile

        page.goto(self.login_url, wait_until="domcontentloaded", timeout=60_000)
        if on_status:
            on_status("正在打开登录页，请在浏览器完成登录/2FA")
        try:
            if urlparse(str(page.url)).path.rstrip("/").lower().endswith("/login"):
                self._fill_login()
        except Exception:
            if self.debug:
                print("[login] 自动填充未完成，请在浏览器中手动登录", flush=True)

        return self.wait_for_login(
            on_status=on_status, should_stop=should_stop, on_tick=on_tick
        )

    def wait_for_login(self, *, on_status: Optional[Callable[[str], None]] = None,
                       should_stop: Optional[Callable[[], bool]] = None,
                       on_tick: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
        """Keep the original window open until the profile API confirms login."""
        self.session_ready = False
        self.restore_window()
        last_message = ""
        next_status = 0.0
        next_probe = 0.0
        while True:
            if on_tick is not None:
                # Lets the collector service "open the backend window" while it
                # is parked here waiting for login/2FA. Runs in this thread.
                on_tick()
            if should_stop is not None and should_stop():
                raise LoginWindowClosed("已请求停止")
            page = self.page
            if page is None or page.is_closed():
                raise LoginWindowClosed("登录窗口已关闭，请点击“重新登录”")
            message = self._pending_message()
            if on_status and (message != last_message or time.monotonic() >= next_status):
                on_status(message)
                last_message = message
                next_status = time.monotonic() + 5.0
            # Poll the profile API a bit less often than the page text; the
            # cookie is what actually decides whether login finished.
            if "账号暂时锁定" not in message and time.monotonic() >= next_probe:
                next_probe = time.monotonic() + 2.0
                profile = self._probe()
                if profile is not None:
                    self.session_ready = True
                    self.save_storage_state()
                    if on_status:
                        on_status(f"登录成功，账号 {profile['user_code']}")
                    if self.minimize_after_login:
                        self.minimize_window()
                    return profile
            page.wait_for_timeout(1_000)

    # ── keep the Playwright event loop alive ───────────────────────────────

    def wait(self, seconds: float) -> None:
        page = self._page()
        remaining = max(0.0, float(seconds))
        # Chunked waits let the browser process cookie refreshes promptly.
        while remaining > 0:
            step = min(remaining, 0.25)
            page.wait_for_timeout(step * 1000)
            remaining -= step

    def _set_window_state(self, page, state: str) -> bool:
        """Minimize / restore / focus the OS window through CDP."""
        if self.ctx is None:
            return False
        cdp = None
        try:
            cdp = self.ctx.new_cdp_session(page)
            window = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {
                "windowId": window["windowId"],
                "bounds": {"windowState": state},
            })
            return True
        except Exception:
            return False
        finally:
            if cdp is not None:
                try:
                    cdp.detach()
                except Exception:
                    pass

    def minimize_window(self) -> bool:
        if self.browser_hidden:
            return True
        if self.ctx is None or self.page is None:
            return False
        if not self._set_window_state(self.page, "minimized"):
            return False
        self.browser_hidden = True
        return True

    def restore_window(self) -> None:
        if not self.browser_hidden or self.ctx is None or self.page is None:
            return
        if self._set_window_state(self.page, "normal"):
            self.browser_hidden = False

    def present_window(self) -> bool:
        """Bring the backend browser window to the front without logging out.

        Called by the collector thread when the dashboard asks for the login
        window. Returns False only when the browser itself is gone, so the
        caller can rebuild the session (Cookies in the profile are kept).
        """
        if self.ctx is None:
            return False
        page = self.page
        if page is None or page.is_closed():
            opened = [item for item in self.ctx.pages if not item.is_closed()]
            if opened:
                page = opened[0]
            else:
                try:
                    page = self.ctx.new_page()
                except Exception:
                    return False
            self.page = page
        try:
            if not urlparse(str(page.url)).netloc:
                # Blank window (fresh profile / crashed renderer): give the
                # operator the backend; the login form shows if the cookie expired.
                target = self.dashboard_url if self.session_ready else self.login_url
                page.goto(target, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            pass
        self.browser_hidden = False
        self._set_window_state(page, "normal")
        try:
            page.bring_to_front()
        except Exception:
            pass
        return True


def session_summary(session: Optional[PartnerSession]) -> Dict[str, Any]:
    if session is None:
        return {"ready": False, "profile": {}}
    return {"ready": bool(session.session_ready), "profile": dict(session.profile or {})}
