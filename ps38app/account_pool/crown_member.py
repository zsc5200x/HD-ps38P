"""Crown member session heartbeat, ported from the hdproc member keeper.

Only the member site's own read-only session endpoint is used. Cookies and the
member uid are never logged or returned through the HTTP API.
"""

from __future__ import annotations

import ipaddress
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlencode
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_LANG = "zh-cn"


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def cookie_header(storage_state: Dict[str, Any], base_url: str) -> str:
    host = (urlsplit(base_url).hostname or "").lower()
    if not host:
        return ""
    now = time.time()
    pairs: List[str] = []
    for cookie in (storage_state or {}).get("cookies", []) or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").lower()
        if not (host == domain or (domain.startswith(".") and
                                   (host == domain[1:] or host.endswith(domain)))):
            continue
        path = str(cookie.get("path") or "/")
        if not "/transform.php".startswith(path):
            continue
        expires = cookie.get("expires", -1)
        if isinstance(expires, (int, float)) and 0 < expires <= now:
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or not name:
            continue
        if any(char in name + value for char in "\r\n;"):
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _parse_member_body(body: bytes) -> Dict[str, Any]:
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {"status": "unavailable", "message": "会员在线检查返回格式异常"}
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        return {"status": "unavailable", "message": "会员在线检查返回 HTML，可能是登录页"}
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        return {"status": "unavailable", "message": "会员在线检查返回格式异常"}
    if root.tag != "serverresponse":
        return {"status": "unavailable", "message": "会员在线检查响应未识别"}
    values: Dict[str, str] = {}
    for child in root:
        if child.tag in values:
            return {"status": "unavailable", "message": "会员在线检查响应字段重复"}
        values[child.tag] = (child.text or "").strip()
    code = values.get("code", "")
    message = values.get("msg", "")
    if code == "error":
        if message.lower() == "doublelogin":
            return {"status": "conflict", "message": "皇冠提示重复登录，请确认该会员是否在其他地方登录"}
        return {"status": "expired", "message": "皇冠会员会话已失效，请重新登录"}
    if any(values.get(key) for key in ("enable", "currency", "maxcredit", "nowcredit", "cash")):
        balance = None
        for key in ("maxcredit", "nowcredit", "cash"):
            raw = values.get(key, "").replace(",", "").strip()
            if not raw:
                continue
            try:
                amount = Decimal(raw)
            except InvalidOperation:
                break
            if not amount.is_finite() or amount < 0:
                break
            balance = format(amount, "f")
            break
        currency = values.get("currency", "").strip().upper()
        if currency == "RMB":
            currency = "CNY"
        if currency and not (len(currency) == 3 and currency.isascii() and currency.isalpha()):
            currency = ""
        return {
            "status": "online",
            "message": "皇冠会员在线检查通过",
            "balance": balance,
            "balance_currency": currency,
            "balance_checked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
    return {"status": "unavailable", "message": "会员在线检查未返回可识别的登录信息"}


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def preferred_origin(meta: Dict[str, Any], storage_state: Dict[str, Any], base_url: str = "") -> str:
    """优先使用账号登记的域名入口，而不是登录后被跳转到的真实 IP。

    皇冠登录成功后浏览器会被跳到后端 IP，抓会话时记录的 origin 就是那个 IP。
    裸 IP 直连会绕过本机代理，实测失败率约 20%、耗时翻倍；域名入口更稳。
    """
    recorded = str(meta.get("origin") or "").rstrip("/")
    candidate = str(base_url or meta.get("base_url") or "").rstrip("/")
    try:
        host = urlsplit(candidate).hostname or ""
    except ValueError:
        host = ""
    if (
        host
        and not _is_ip_literal(host)
        and urlsplit(candidate).scheme in {"http", "https"}
        and cookie_header(storage_state, candidate)
    ):
        return candidate
    return recorded or candidate


def ping_member(
    base_url: str,
    storage_state: Dict[str, Any],
    meta: Dict[str, Any],
    *,
    timeout: int = 10,
    ignore_https_errors: bool = False,
    transport: Optional[Callable[[str, Dict[str, str], Dict[str, str], int], Tuple[int, bytes]]] = None,
) -> Dict[str, Any]:
    """POST ``get_member_data`` with the saved cookies/uid/ver.

    Returns one of ``online`` / ``expired`` / ``conflict`` / ``unavailable``.
    A transport hook is accepted so the parser can be tested without a network.
    """
    uid = str(meta.get("uid") or "").strip()
    ver = str(meta.get("ver") or "").strip()
    langx = str(meta.get("langx") or DEFAULT_LANG).strip() or DEFAULT_LANG
    origin = preferred_origin(meta, storage_state, base_url)
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return {"status": "unavailable", "message": "会员站点地址无效"}
    except ValueError:
        return {"status": "unavailable", "message": "会员站点地址无效"}
    if not uid or not ver:
        return {"status": "unavailable", "message": "会话缺少 uid/ver，需要重新登录补全"}
    cookie = cookie_header(storage_state, origin)
    if not cookie:
        return {"status": "expired", "message": "保存的会员 Cookie 已不存在，请重新登录"}

    fields = {"ver": ver, "p": "get_member_data", "uid": uid,
              "langx": langx, "change": "all"}
    url = origin + "/transform.php?" + urlencode({"ver": ver})
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "origin": origin,
        "referer": origin + "/",
        "content-type": "application/x-www-form-urlencoded",
        "user-agent": str(meta.get("user_agent") or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"),
        "cookie": cookie,
    }
    data = urlencode(fields).encode("utf-8")

    if transport is not None:
        try:
            status, body = transport(url, fields, headers, timeout)
        except Exception:  # noqa: BLE001
            return {"status": "unavailable", "message": "会员在线检查暂时失败，稍后重试"}
    else:
        try:
            status, body = _http_post(url, data, headers, timeout,
                                      ignore_https_errors=ignore_https_errors)
        except HTTPError as error:
            if error.code in (401, 440):
                return {"status": "expired", "message": "皇冠会员明确拒绝当前会话"}
            return {"status": "unavailable", "message": f"会员在线检查失败（HTTP {error.code}）"}
        except (URLError, OSError, ValueError) as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, ssl.SSLCertVerificationError):
                message = "会员站点证书校验失败，请检查皇冠证书兼容配置"
            elif isinstance(reason, (TimeoutError,)):
                message = "会员在线检查连接超时，已保留会话，稍后重试"
            else:
                message = "会员在线检查网络连接失败，已保留会话，稍后重试"
            return {"status": "unavailable", "message": message}

    if status in (401, 440):
        return {"status": "expired", "message": "皇冠会员明确拒绝当前会话"}
    if status != 200:
        return {"status": "unavailable", "message": f"会员在线检查失败（HTTP {status}）"}
    if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_RESPONSE_BYTES:
        return {"status": "unavailable", "message": "会员在线检查响应过大或格式异常"}
    return _parse_member_body(bytes(body))


def _http_post(url: str, data: bytes, headers: Dict[str, str], timeout: int,
               *, ignore_https_errors: bool = False,
               max_response_bytes: int = MAX_RESPONSE_BYTES) -> Tuple[int, bytes]:
    # Crown's member routes may redirect to IPs with expired/mismatched certs.
    # Match hdproc only when explicitly enabled by the Crown provider; never
    # change the process-wide SSL context or PS38's verification policy.
    context = ssl.create_default_context()
    if ignore_https_errors:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    opener = build_opener(ProxyHandler({}), _NoRedirects(), HTTPSHandler(context=context))
    request = Request(url, data=data, method="POST", headers=headers)
    with opener.open(request, timeout=timeout) as response:
        return int(response.status), response.read(max_response_bytes + 1)
