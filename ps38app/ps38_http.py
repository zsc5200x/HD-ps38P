"""Headless PS38 partner session over HTTP (no browser).

Mirrors the account-env ``ps38ag_login``/``ps38ag_keeper`` flow:

* the AdsPower environment is only used for its SOCKS5 proxy (same exit IP);
* login uses ``/partner/login/authentication`` + captcha + TOTP;
* the resulting ``u_partner`` cookies are stored locally and reused;
* profile/orders/hierarchy are plain JSON requests.

The public surface matches :class:`ps38app.session.PartnerSession` where the
poller needs it, so the collector can switch backends without changing its
polling logic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from .adspower import AdsPowerClient
from .errors import LoginRejected
from .net import HttpClient, HttpTransportError, parse_set_cookies
from .store import PollError, SessionExpired
from .totp import remaining_seconds, totp

PROFILE_PATH = "/partner-account/ddd/user-info/login-user/profile"
BET_LIST_PATH = "/partner-reporting/ddd/common/bet-list/v2"
HIERARCHY_PATH = "/partner-reporting/ddd/report/win-loss-detail"
AUTH_PATH = "/partner/login/authentication"
CAPTCHA_PATH = "/partner/captcha/key"
OTP_PATH = "/partner/otp/validate"


def _text(value: Any) -> str:
    return str(value or "").strip()


class Ps38HttpSession:
    http_mode = True

    def __init__(self, config: Dict[str, Any], *, debug: bool = False) -> None:
        self.config = dict(config)
        self.debug = debug
        self.base_url = str(config.get("base_url") or "").rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise RuntimeError("PS38 base_url 无效")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.login_url = self.base_url + str(config.get("login_path") or "/partner/zh-cn/login")
        self.dashboard_url = self.base_url + str(
            config.get("dashboard_path") or "/partner-ui-v2/report-outstanding-v2"
        )
        self.username = _text(config.get("username"))
        self.password = str(config.get("password") or "")
        self.totp_secret = str(config.get("totp_secret") or "").strip()
        self.captcha_command = str(config.get("ps38_captcha_command") or "").strip()
        self.captcha_timeout = int(config.get("ps38_captcha_timeout") or 30)
        self.profile_dir = Path(config["profile_dir"])
        self.storage_path = self.profile_dir / "storage_state.json"
        self.captcha_dir = Path(
            config.get("ps38_captcha_dir") or (self.profile_dir.parent / "captcha")
        )
        self.env_id = _text(config.get("adspower_env_id"))
        self.timeout = float(config.get("ps38_http_timeout") or 30)
        self.client: Optional[HttpClient] = None
        self.cookies: Dict[str, str] = {}
        self.session_ready = False
        self.profile: Dict[str, Any] = {}
        self._proxy: Dict[str, str] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.client is not None:
            return
        if self.env_id:
            try:
                self._proxy = AdsPowerClient(self.config).proxy(self.env_id)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"读取 AdsPower 环境代理失败：{exc}") from exc
        self.client = HttpClient(
            self.host,
            scheme=self.scheme,
            port=self.port,
            proxy=self._proxy,
            timeout=self.timeout,
            user_agent=str(self.config.get("ps38_http_user_agent") or ""),
        )
        self._load_cookies()

    def close(self) -> None:
        try:
            self.save_storage_state()
        finally:
            if self.client is not None:
                self.client.close()
            self.client = None
            self.session_ready = False
            self.profile = {}

    def save_storage_state(self) -> None:
        if not self.cookies:
            return
        payload = {
            "cookies": [
                {"name": name, "value": value, "domain": self.host, "path": "/"}
                for name, value in self.cookies.items()
            ],
            "origins": [],
        }
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.storage_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.storage_path.chmod(0o600)
        except OSError:
            pass

    def _load_cookies(self) -> None:
        self.cookies = {}
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("cookies"), list):
            return
        for cookie in payload.get("cookies") or []:
            if not isinstance(cookie, dict):
                continue
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            if domain and self.host != domain and not self.host.endswith("." + domain):
                continue
            expires = cookie.get("expires")
            if isinstance(expires, (int, float)) and 0 <= expires <= time.time():
                continue
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if name:
                self.cookies[name] = value

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        retry: bool = True,
    ):
        self.start()
        assert self.client is not None
        for attempt in range(2 if retry else 1):
            try:
                response = self.client.request(
                    method, path, headers=headers, body=body, cookies=self.cookies
                )
                for name, value, _domain in parse_set_cookies(response.headers):
                    self.cookies[name] = value
                return response
            except (HttpTransportError, OSError):
                self.client.close()
                if attempt or not retry:
                    raise
        raise HttpTransportError("PS38 HTTP 请求失败")

    def _json(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        retry: bool = True,
    ) -> Dict[str, Any]:
        data = None
        request_headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.base_url,
            "Referer": self.dashboard_url,
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json;charset=UTF-8"
        request_headers.update(headers or {})
        response = self._request(method, path, headers=request_headers, body=data, retry=retry)
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise PollError("PS38 接口返回非 JSON") from exc
        if not isinstance(payload, dict):
            raise PollError("PS38 接口返回格式无效")
        return payload

    def _probe(self) -> Optional[Dict[str, Any]]:
        try:
            return self.fetch_profile()
        except SessionExpired:
            return None

    # ── login / 2FA ───────────────────────────────────────────────────────

    def can_auto_login(self) -> bool:
        return bool(self.username and self.password and self.totp_secret)

    def login(
        self,
        *,
        force: bool = False,
        on_status: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        on_tick: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        self.start()
        if force:
            self.cookies = {}
            try:
                self.storage_path.unlink()
            except OSError:
                pass
        profile = self._probe()
        if profile is not None:
            self.session_ready = True
            if on_status:
                on_status(f"会话有效，账号 {profile['user_code']}")
            self.save_storage_state()
            return profile
        if not self.username or not self.password:
            raise LoginRejected("PS38 缺少登录账号或密码")
        if on_status:
            on_status("正在 HTTP 登录 PS38（账号密码 + 验证码 + TOTP）…")
        self._login_flow(on_status=on_status, should_stop=should_stop)
        profile = self._probe()
        if profile is None:
            raise LoginRejected("PS38 登录后接口仍未返回有效 profile")
        self.session_ready = True
        self.save_storage_state()
        if on_status:
            on_status(f"登录成功，账号 {profile['user_code']}")
        return profile

    def _login_flow(
        self,
        *,
        on_status: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> None:
        if should_stop is not None and should_stop():
            raise LoginRejected("已请求停止")
        # Seed JSESSIONID / pctag cookies from the login page.
        self._request(
            "GET",
            self.login_url[len(self.base_url) :] or "/",
            headers={"Accept": "text/html,application/xhtml+xml", "Referer": self.base_url + "/"},
            retry=True,
        )
        captcha = ""
        for _attempt in range(3):
            if should_stop is not None and should_stop():
                raise LoginRejected("已请求停止")
            payload = {
                "username": self.username,
                "password": self.password,
                "captcha": captcha,
                "fingerprint": None,
            }
            data = self._json(
                "POST",
                AUTH_PATH,
                body=payload,
                headers={"Referer": self.login_url},
                retry=False,
            )
            status = str(data.get("status") or "")
            if data.get("required_captcha") or status == "INVALID_CAPTCHA":
                if not self.captcha_command:
                    raise LoginRejected("PS38 需要图形验证码，但未配置 ps38_captcha_command")
                image = self._fetch_captcha()
                captcha = self._solve_captcha(image)
                if not captcha:
                    raise LoginRejected("PS38 验证码识别失败")
                if on_status:
                    on_status(f"已识别验证码 {captcha}，正在提交…")
                continue
            if status != "OK":
                raise LoginRejected(self._login_error(status, data))
            if str(data.get("state") or "") == "otp":
                self._submit_totp(on_status=on_status)
            return
        raise LoginRejected("PS38 验证码连续失败，请稍后重试")

    @staticmethod
    def _login_error(status: str, data: Dict[str, Any]) -> str:
        mapping = {
            "INVALID_USER": "账号或密码错误",
            "LOCKED_ACCOUNT": "账号已锁定",
            "ACCOUNT_TEMPORARY_DISABLED": "账号临时禁用",
            "IP_NOT_ALLOWED": "出口 IP 不在白名单",
            "INVALID_UPLINE": "上级/代理关系异常",
        }
        message = mapping.get(status, status or "未知登录状态")
        if status == "ACCOUNT_TEMPORARY_DISABLED" and data.get("failedAttemptCount"):
            message += f"（失败次数 {data['failedAttemptCount']}）"
        return f"PS38 登录失败：{message}"

    def _fetch_captcha(self) -> bytes:
        response = self._request(
            "GET",
            CAPTCHA_PATH,
            headers={"Accept": "image/*", "Referer": self.login_url},
            retry=True,
        )
        if response.status != 200 or not response.body:
            raise LoginRejected(f"PS38 验证码获取失败（HTTP {response.status}）")
        return response.body

    def _solve_captcha(self, image: bytes) -> str:
        if not image or not self.captcha_command:
            return ""
        try:
            self.captcha_dir.mkdir(parents=True, exist_ok=True)
            target = self.captcha_dir / f"ps38-{time.time_ns()}.jpg"
            target.write_bytes(image)
            target.chmod(0o600)
        except OSError:
            return ""
        command = self.captcha_command.replace("{image}", str(target))
        try:
            import subprocess

            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=max(5, self.captcha_timeout),
            )
        except Exception:  # noqa: BLE001
            return ""
        digits = "".join(char for char in (completed.stdout or "") if char.isdigit())
        return digits[-4:] if len(digits) >= 4 else ""

    def _submit_totp(self, *, on_status: Optional[Callable[[str], None]] = None) -> None:
        try:
            code = totp(self.totp_secret)
        except ValueError as exc:
            raise LoginRejected("PS38 TOTP 密钥为空或格式无效，请检查已保存的凭据") from exc
        left = remaining_seconds()
        if left < 5:
            time.sleep(left + 1)
            code = totp(self.totp_secret)
        if on_status:
            on_status("账号密码已通过，正在提交 TOTP…")
        response = self._request(
            "POST",
            OTP_PATH,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base_url,
                "Referer": self.base_url + "/partner/en/otp",
            },
            body=json.dumps({"otpType": "TOTP_APP", "otpCode": code}).encode("utf-8"),
            retry=False,
        )
        text = response.text()
        if "OTP_SUCCESS_VALID" not in text:
            try:
                data = response.json() or {}
            except ValueError:
                data = {}
            status = str(data.get("status") or "") if isinstance(data, dict) else ""
            if status.upper() not in {"OK", "OTP_SUCCESS_VALID"}:
                raise LoginRejected("PS38 TOTP 校验失败")

    # ── reporting APIs ────────────────────────────────────────────────────

    def fetch_profile(self) -> Dict[str, Any]:
        response = self._request(
            "GET",
            PROFILE_PATH,
            headers={"Accept": "application/json, text/plain, */*", "Referer": self.dashboard_url},
            retry=True,
        )
        if response.status in (401, 403):
            raise SessionExpired(f"PS38 会话已失效（HTTP {response.status}）")
        if response.status != 200:
            raise PollError(f"PS38 身份接口 HTTP {response.status}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SessionExpired("PS38 身份接口未返回 JSON") from exc
        profile = payload.get("profile") if isinstance(payload, dict) else None
        if not isinstance(profile, dict):
            raise SessionExpired("PS38 身份接口缺少 profile")
        user_id = profile.get("userId")
        user_code = profile.get("userCode")
        if (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id <= 0
            or not isinstance(user_code, str)
            or not user_code.strip()
        ):
            raise SessionExpired("PS38 身份接口缺少有效 userId/userCode")
        self.profile = {
            "user_id": user_id,
            "user_code": user_code.strip(),
            "login_id": profile.get("loginId") or user_code,
        }
        return self.profile

    def fetch_orders(
        self,
        owner_user_id: int,
        agent: Dict[str, Any],
        status: str,
        page: int,
        page_size: int,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Dict[str, Any]:
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
            body.update(
                {
                    "dateBy": "GRADED_DATE",
                    "timezone": "+08:00",
                    "fromDate": from_date,
                    "toDate": to_date,
                }
            )
        response = self._request(
            "POST",
            BET_LIST_PATH,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base_url,
                "Referer": self.dashboard_url,
            },
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            retry=False,
        )
        if response.status in (401, 403):
            raise SessionExpired(f"PS38 订单接口 HTTP {response.status}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return {"httpStatus": response.status, "json": payload}

    def fetch_hierarchy(self, owner_user_id: int, from_date: str, to_date: str) -> Dict[str, Any]:
        body = {
            "timezone": "+08:00",
            "productCodes": ["SB"],
            "fromDate": from_date,
            "toDate": to_date,
            "ownerUserId": str(owner_user_id),
            "drillDownUserId": owner_user_id,
            "orderBy": {"colName": "user_code", "sort": "ASC"},
            "excludeTest": False,
            "dateBy": "GRADED_DATE",
            "searchOption": "USERNAME",
            "searchByText": "",
            "searchUserIds": "",
            "requestSearchByUser": {"searchByText": "", "selectedLevels": ["ALL"]},
            "groupType": "OWN_DOWNLINE",
            "groupId": 0,
            "userIds": None,
        }
        response = self._request(
            "POST",
            HIERARCHY_PATH,
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base_url,
                "Referer": self.dashboard_url,
            },
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            retry=False,
        )
        if response.status in (401, 403):
            raise SessionExpired(f"PS38 身份树接口 HTTP {response.status}")
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return {"httpStatus": response.status, "json": payload}

    # ── poller-compatible helpers ─────────────────────────────────────────

    def present_window(self) -> bool:
        """HTTP mode has no window; returning True keeps the collector stable."""
        return True

    def open_login_page(self) -> None:
        return None

    def wait_for_login(self, **_kwargs: Any) -> Dict[str, Any]:
        return self.login()

    def try_auto_login(self, **kwargs: Any) -> bool:
        try:
            self.login(**kwargs)
            return True
        except Exception:  # noqa: BLE001
            return False
