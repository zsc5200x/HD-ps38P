"""Capture Crown's real login handoff, without storing request bodies/passwords."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict
from urllib.parse import parse_qs, urlsplit

from .crown_member import MAX_RESPONSE_BYTES, _parse_member_body


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in {"http", "https"} and parts.hostname else ""


def clean_meta(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("uid", "ver", "langx", "origin", "login_layer", "user_agent"):
        item = value.get(key)
        if isinstance(item, (str, int)):
            text = str(item).strip()
            if text and len(text) <= 2048 and not any(ord(char) < 32 for char in text):
                result[key] = text
    return result


class MemberCapture:
    def __init__(self) -> None:
        self.meta: Dict[str, str] = {}
        self.authenticated = False
        self.fresh_login = False

    def response(self, response, *, request=None) -> None:
        """Only accept identity from successful Crown protocol responses.

        Watching the context also covers iframe forms, popups and chk_login2.
        Captcha/failed responses never replace a previously captured identity.
        """
        try:
            request = request or response.request
            parts = urlsplit(response.url)
            if not parts.path.endswith("/transform.php") or response.status != 200:
                return
            query = parse_qs(parts.query)
            fields = parse_qs(request.post_data or "")
            operation = (fields.get("p") or query.get("p") or [""])[0]
            if not (operation.startswith("chk_login") or operation == "get_member_data"):
                return
            text = response.text()
            if len(text) > MAX_RESPONSE_BYTES or "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
                return
            if operation.startswith("chk_login"):
                root = ET.fromstring(text)
                values = {child.tag: (child.text or "").strip() for child in root}
                if values.get("status") != "200" or not values.get("uid"):
                    return
                uid = values["uid"]
                self.fresh_login = True
            else:
                if _parse_member_body(text.encode())["status"] != "online":
                    return
                uid = (fields.get("uid") or query.get("uid") or [""])[0]
            ver = (query.get("ver") or fields.get("ver") or [""])[0]
            if not uid or not ver:
                return
            self.meta = clean_meta({
                "uid": uid, "ver": ver, "origin": origin(response.url),
                "langx": (fields.get("langx") or ["zh-cn"])[0],
            })
            self.authenticated = True
        except Exception:
            # Navigations and captcha responses may dispose the response body.
            return

    def login_route(self, route) -> None:
        """Read a login handoff before an iframe can navigate away from it.

        A response observer can lose Network.getResponseBody when the successful
        login immediately removes its iframe. Forward the original request once,
        capture only verified identity, then deliver the unchanged response.
        Never replay a login if the upstream request fails after submission.
        """
        request = route.request
        query = parse_qs(urlsplit(request.url).query)
        fields = parse_qs(request.post_data or "")
        operation = (fields.get("p") or query.get("p") or [""])[0]
        if not operation.startswith("chk_login"):
            route.continue_()
            return
        response = None
        try:
            response = route.fetch(max_redirects=0, max_retries=0, timeout=30000)
            self.response(response, request=request)
            route.fulfill(response=response)
        except Exception:
            route.abort("failed")
        finally:
            if response is not None:
                response.dispose()

    def collect(self, page) -> Dict[str, str]:
        """Restore identity from frames, globals, storage, URLs and cookies.

        These are candidates only: the provider must verify get_member_data
        before treating them as an online session.
        """
        meta = dict(self.meta)
        frames = list(page.frames)
        for frame in frames:
            try:
                values = frame.evaluate("""() => {
                    const d = window._CHDomain || {};
                    const pick = (keys) => {
                        for (const key of keys) {
                            let v = window[key] || d[key];
                            try { v = v || sessionStorage.getItem(key) || localStorage.getItem(key); } catch (e) {}
                            if (v) return String(v);
                        }
                        return '';
                    };
                    return {uid: pick(['uid','UID','userid','userId','user_id','loginUid']),
                        ver: pick(['ver','myGameVer','mygamever','gameVer','gamever']),
                        langx: pick(['langx']), login_layer: pick(['login_layer']),
                        user_agent: navigator.userAgent};
                }""")
                for key, value in clean_meta(values).items():
                    meta.setdefault(key, value)
                query = parse_qs(urlsplit(frame.url).query)
                for key in ("uid", "ver", "langx"):
                    if query.get(key):
                        meta.setdefault(key, query[key][0])
                if not meta.get("uid") or not meta.get("ver"):
                    html = frame.content()[:512 * 1024]
                    for key, aliases in (("uid", "uid|userid|user_id|loginuid"),
                                         ("ver", "ver|mygamever|gamever")):
                        match = re.search(r"\b(?:" + aliases + r")[\s\"']*[:=][\s\"']*([0-9A-Za-z._-]{4,})",
                                          html, re.IGNORECASE)
                        if match:
                            meta.setdefault(key, match.group(1))
                if meta.get("uid") and meta.get("ver") and not meta.get("origin"):
                    meta["origin"] = origin(frame.url)
            except Exception:
                continue
        meta.setdefault("origin", origin(page.url))
        try:
            for cookie in page.context.cookies([meta["origin"]]):
                key = str(cookie.get("name") or "").lower()
                if key in {"uid", "userid", "user_id", "loginuid"}:
                    meta.setdefault("uid", cookie["value"])
                if key in {"ver", "mygamever", "gamever"}:
                    meta.setdefault("ver", cookie["value"])
        except Exception:
            pass
        meta.setdefault("langx", "zh-cn")
        return clean_meta(meta)
