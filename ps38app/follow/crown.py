"""独立皇冠适配器：现有号池会话、严格查盘以及单次提交。"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import ssl
import sqlite3
import threading
from typing import Dict
import time
import uuid
from contextlib import closing
from email.message import Message
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, ProxyHandler, Request, build_opener
from urllib.response import addinfourl

from ..adspower import AdsPowerClient
from ..account_pool.crown_member import _NoRedirects, cookie_header, preferred_origin
from ..net import HttpClient, HttpTransportError, socks5_connect, socks5_connect_host
from .crown_markets import (
    LIMIT,
    FollowPreparationError,
    decimal,
    game_rows,
    mapped_game,
    parse_receipt,
    scalar_fields,
    select_market,
    verify_context,
    xml_root,
)


# 皇冠高峰期单次请求可能到 4-6 秒：单请求上限 10 秒，查盘总预算 30 秒。
REQUEST_TIMEOUT_SECONDS = 10
PREPARE_TIMEOUT_SECONDS = 30


class _SocksConnection(http.client.HTTPConnection):
    """由账号 SOCKS5 代理建立连接，保留标准库单次 request/getresponse 语义。"""

    def __init__(self, host, port, *, proxy, timeout, context=None):
        super().__init__(host, port, timeout=timeout)
        self._proxy = proxy
        self._tls_context = context

    def connect(self):
        sock = socks5_connect(
            self._proxy["host"],
            int(self._proxy["port"]),
            self._proxy.get("username"),
            self._proxy.get("password"),
            timeout=self.timeout,
        )
        try:
            socks5_connect_host(sock, self.host, self.port)
            if self._tls_context is not None:
                sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class CrownAdapter:
    def __init__(
        self,
        config,
        *,
        transport=None,
        session_loader=None,
        clock=None,
        monotonic=None,
        should_stop=None,
        account_provider=None,
    ):
        self.config = config
        self._transport = transport
        self._loader = session_loader or self._load_session
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._should_stop = should_stop or (lambda: False)
        self._account_provider = account_provider
        self._prepared = {}
        # 会员校验结果短时复用：同账号 30 秒内不重复请求 get_member_data。
        self._member_cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def _load_session(self, account_id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", str(account_id)):
            raise ValueError("皇冠账号标识无效")
        try:
            database = Path(self.config["pool_db_path"])
            with closing(
                sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
            ) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT id,name,base_url,platform,enabled,status,env_id FROM accounts WHERE id=?",
                    (account_id,),
                ).fetchone()
            if (
                not row
                or row["platform"] != "crown_member"
                or not row["enabled"]
                or row["status"] != "online"
            ):
                raise ValueError("请选择已启用且在线的皇冠会员账号")
            folder = Path(self.config["pool_dir"]) / account_id
            meta = json.loads((folder / "session_meta.json").read_text(encoding="utf-8"))
            state = json.loads((folder / "profile/storage_state.json").read_text(encoding="utf-8"))
            origin = preferred_origin(meta, state, row["base_url"])
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("皇冠会员站点地址无效")
            cookie = cookie_header(state, origin)
            uid, ver = str(meta.get("uid") or ""), str(meta.get("ver") or "")
            if not cookie or not uid:
                raise ValueError("皇冠会员会话不完整或已过期")
            proxy = None
            if row["env_id"]:
                try:
                    proxy = AdsPowerClient(self.config).proxy(str(row["env_id"]))
                except Exception:
                    raise ValueError("皇冠环境代理无法读取，请恢复 AdsPower 后重试") from None
            return {
                "id": account_id,
                "name": row["name"],
                "origin": origin,
                "uid": uid,
                "ver": ver,
                "cookie": cookie,
                "user_agent": str(meta.get("user_agent") or "Mozilla/5.0"),
                "proxy": proxy,
            }
        except (OSError, sqlite3.Error, KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("皇冠会员会话无法读取，请在号池检查") from None

    def validate_account(self, account_id, currency=""):
        if currency and not re.fullmatch(r"[A-Z]{3}", str(currency)):
            raise ValueError("币种必须为三位大写代码")
        session = self._loader(account_id)
        return {"id": account_id, "name": session.get("name", account_id)}

    def account_balances(self):
        """返回各皇冠会员的可用额度（号池心跳写入的余额快照）。"""
        try:
            folder = Path(self.config["pool_dir"])
        except KeyError:
            return {}
        balances = {}
        for path in folder.glob("*/session_meta.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            raw = str(meta.get("balance") or "").replace(",", "").strip()
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            if value >= 0:
                balances[path.parent.name] = value
        return balances

    def available_accounts(self):
        """Return enabled, online Crown accounts whose project-owned sessions load locally."""
        if self._account_provider is not None:
            try:
                values = self._account_provider()
            except Exception:
                return []
            candidates = values if isinstance(values, (list, tuple)) else []
            return list(
                dict.fromkeys(str(value).strip() for value in candidates if str(value).strip())
            )
        try:
            database = Path(self.config["pool_db_path"])
            with closing(
                sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
            ) as connection:
                rows = connection.execute(
                    "SELECT id FROM accounts WHERE platform='crown_member' AND enabled=1 "
                    "AND status='online' ORDER BY created_at,id"
                ).fetchall()
        except (OSError, KeyError, sqlite3.Error):
            return []
        available = []
        for (account_id,) in rows:
            try:
                self._loader(account_id)
            except Exception:
                continue
            available.append(str(account_id))
        return available

    def _new_opener(self, session):
        """一次处理复用内存 CookieJar，承接只读确认返回的会话更新。"""
        if self._transport is not None:
            return None
        parsed = urlsplit(session["origin"])
        jar = CookieJar()
        initial = SimpleCookie()
        initial.load(session["cookie"])
        for name, morsel in initial.items():
            jar.set_cookie(
                Cookie(
                    version=0,
                    name=name,
                    value=morsel.value,
                    port=None,
                    port_specified=False,
                    domain=parsed.hostname,
                    domain_specified=False,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=parsed.scheme == "https",
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                )
            )
        context = ssl.create_default_context()
        if self.config.get("pool_crown_ignore_https_errors", True):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return build_opener(
            ProxyHandler({}),
            _NoRedirects(),
            HTTPCookieProcessor(jar),
            HTTPSHandler(context=context),
        )

    def _remaining(self, deadline):
        if self._should_stop():
            raise FollowPreparationError("皇冠处理已停止", "CANCELLED")
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise FollowPreparationError("皇冠处理超过本次时限", "PREPARE_TIMEOUT", retryable=True)
        return remaining

    def _read_limited(self, response, deadline):
        # read1 每次至多进行一次底层读取；每块更新剩余期限，防止响应体慢速滴流。
        chunks, size = [], 0
        while size <= LIMIT:
            remaining = self._remaining(deadline)
            connection = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if connection is not None:
                connection.settimeout(remaining)
            chunk = response.read1(min(65536, LIMIT + 1 - size))
            self._remaining(deadline)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > LIMIT:
            raise ValueError("皇冠响应过大")
        return b"".join(chunks)

    def _post(self, session, fields, opener=None, *, deadline=None):
        request_deadline = self._monotonic() + REQUEST_TIMEOUT_SECONDS
        if deadline is not None:
            request_deadline = min(request_deadline, deadline)
        timeout = self._remaining(request_deadline)
        form = {"uid": session["uid"], "ver": session["ver"], "langx": "zh-cn", **fields}
        origin = session["origin"]
        url = origin + "/transform.php?" + urlencode({"ver": session["ver"]})
        headers = {
            "Cookie": session["cookie"],
            "Origin": origin,
            "Referer": origin + "/",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": session.get("user_agent") or "Mozilla/5.0",
        }
        try:
            if session.get("proxy") and self._transport is None:
                parsed = urlsplit(url)
                if not parsed.hostname:
                    raise ValueError("皇冠会员站点地址无效")
                cookie_request = Request(url, method="POST")
                headers["Cookie"] = self._opener_cookie(opener, cookie_request) or session["cookie"]
                client = HttpClient(
                    parsed.hostname,
                    scheme=parsed.scheme or "https",
                    port=parsed.port,
                    proxy=session["proxy"],
                    timeout=max(5, int(timeout)),
                    user_agent=session.get("user_agent") or "",
                    verify=not bool(self.config.get("pool_crown_ignore_https_errors", True)),
                )
                try:
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query
                    response = client.request(
                        "POST",
                        path,
                        headers=headers,
                        body=urlencode(form).encode("utf-8"),
                    )
                    status, body = response.status, response.body
                    cookie_headers = Message()
                    for name, values in response.headers.items():
                        for value in values:
                            cookie_headers.add_header(name, value)
                    with addinfourl(BytesIO(b""), cookie_headers, url, status) as cookie_response:
                        self._store_response_cookies(opener, cookie_request, cookie_response)
                finally:
                    client.close()
                self._remaining(request_deadline)
            elif self._transport is not None:
                status, body = self._transport(url, form, headers, timeout)
                self._remaining(request_deadline)
            else:
                # CookieJar 添加当前 Cookie；手动 Cookie 头会屏蔽服务端更新。
                headers.pop("Cookie")
                request = Request(
                    url, data=urlencode(form).encode("utf-8"), headers=headers, method="POST"
                )
                with opener.open(request, timeout=timeout) as response:
                    status, body = response.status, self._read_limited(response, request_deadline)
        except HTTPError as error:
            error.close()
            raise FollowPreparationError(
                f"皇冠接口暂不可用（HTTP {error.code}）", "TRANSPORT", retryable=True
            ) from None
        except HttpTransportError as exc:
            raise FollowPreparationError(
                f"皇冠请求暂时失败（{type(exc).__name__}）", "TRANSPORT", retryable=True
            ) from None
        except FollowPreparationError:
            raise
        except Exception as exc:
            reason = type(exc).__name__
            raise FollowPreparationError(
                f"皇冠请求暂时失败（{reason}）", "TRANSPORT", retryable=True
            ) from None
        if status != 200:
            raise FollowPreparationError(
                f"皇冠接口暂不可用（HTTP {status}）", "TRANSPORT", retryable=True
            )
        return body

    def _bet_context(self, session, timeout):
        """下注请求专用连接；证书校验与会话来源保持一致（本机号池）。"""
        parsed = urlsplit(session["origin"])
        context = None
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            if self.config.get("pool_crown_ignore_https_errors", True):
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        if session.get("proxy"):
            return _SocksConnection(
                parsed.hostname,
                parsed.port or (443 if context else 80),
                proxy=session["proxy"],
                timeout=timeout,
                context=context,
            )
        if context is not None:
            return http.client.HTTPSConnection(
                parsed.hostname, parsed.port or 443, timeout=timeout, context=context
            )
        return http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)

    @staticmethod
    def _opener_cookie(opener, request):
        """沿用本次处理的内存 CookieJar，保留只读确认返回的会话更新。"""
        if opener is None:
            return ""
        processor = next(
            (item for item in opener.handlers if isinstance(item, HTTPCookieProcessor)), None
        )
        if processor is None:
            return ""
        processor.http_request(request)
        return request.get_header("Cookie") or ""

    @staticmethod
    def _store_response_cookies(opener, request, response):
        if opener is None:
            return
        for handler in opener.handlers:
            if isinstance(handler, HTTPCookieProcessor):
                handler.cookiejar.extract_cookies(response, request)
                break

    WARMUP_ATTEMPTS = 2

    def _warmup(self, connection, path, session, cookie, deadline, opener=None):
        """在正式下注前用一条只读请求确认通道可用。

        皇冠 TLS 连接会随机中途断开；先跑一次无害查询，断了就换连接重来，
        避免把坏连接用在真正的下注请求上。
        """
        body = urlencode(
            {
                "uid": session["uid"],
                "ver": session["ver"],
                "langx": "zh-cn",
                "p": "get_member_data",
                "change": "all",
            }
        ).encode("utf-8")
        self._remaining(deadline)
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Origin": session["origin"],
                "Referer": session["origin"] + "/",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": session.get("user_agent") or "Mozilla/5.0",
                "Cookie": cookie,
            },
        )
        response = connection.getresponse()
        response.read(256 * 1024)
        if response.status != 200:
            raise ValueError(f"预热返回 HTTP {response.status}")
        if opener is not None:
            self._store_response_cookies(
                opener, Request(session["origin"] + path, method="POST"), response
            )

    def _post_bet(self, session, fields, opener, deadline):
        """预热连接后发送一次下注写请求。

        连接/预热阶段失败时请求体没有发出去，可以安全地重新查盘；
        正式请求一旦发出就只能按结果不明处理。
        """
        if self._transport is not None:
            try:
                return self._post(session, fields, None, deadline=deadline), None
            except Exception as exc:
                return None, {"reason": f"发送后中断（{type(exc).__name__}）", "maybe_sent": True}
        form = {"uid": session["uid"], "ver": session["ver"], "langx": "zh-cn", **fields}
        body = urlencode(form).encode("utf-8")
        path = "/transform.php?" + urlencode({"ver": session["ver"]})
        request = Request(
            session["origin"] + path,
            data=body,
            method="POST",
            headers={
                "Origin": session["origin"],
                "Referer": session["origin"] + "/",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": session.get("user_agent") or "Mozilla/5.0",
            },
        )
        cookie = self._opener_cookie(opener, request) or session["cookie"]

        connection = None
        warmed = False
        last_reason = ""
        for _attempt in range(self.WARMUP_ATTEMPTS):
            try:
                connection = self._bet_context(session, self._remaining(deadline))
                connection.connect()
                self._warmup(connection, path, session, cookie, deadline, opener)
                request.remove_header("Cookie")
                cookie = self._opener_cookie(opener, request) or session["cookie"]
                warmed = True
                break
            except FollowPreparationError as exc:
                # 停止信号要传出去；预算耗尽按“未发出”处理，可安全重试。
                if getattr(exc, "category", "") == "CANCELLED":
                    raise
                last_reason = str(exc)[:60]
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                connection = None
                break
            except Exception as exc:
                last_reason = type(exc).__name__
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                connection = None
        if not warmed or connection is None:
            return None, {"reason": f"连接预热失败（{last_reason or 'UNKNOWN'}）"}

        try:
            try:
                connection.request(
                    "POST",
                    path,
                    body=body,
                    headers={
                        "Origin": session["origin"],
                        "Referer": session["origin"] + "/",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": session.get("user_agent") or "Mozilla/5.0",
                        "Cookie": cookie,
                    },
                )
                response = connection.getresponse()
                data = response.read(2 * 1024 * 1024 + 1)
                status = response.status
            except Exception as exc:  # 已进入发送/读取：不能证明未成交
                return None, {"reason": f"发送后中断（{type(exc).__name__}）", "maybe_sent": True}
        finally:
            try:
                connection.close()
            except Exception:
                pass
        if status != 200:
            return None, {"reason": f"接口返回 HTTP {status}", "maybe_sent": True}
        return data, None

    @staticmethod
    def _signature(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _plain_tag(body):
        """识别皇冠返回的短纯文本（如 VariableStandard / CheckEMNU）。"""
        if not isinstance(body, (bytes, bytearray)):
            return ""
        text = bytes(body).strip()
        if not text or len(text) > 64 or b"<" in text:
            return ""
        try:
            return text.decode("ascii")
        except UnicodeDecodeError:
            return ""

    def _menu_warmup(self, session, opener, deadline, quote):
        """模拟浏览器绑定菜单上下文，降低 VariableStandard / CheckEMNU。"""
        live = quote.get("is_live", True) is True
        forms = (
            {"p": "right_menu"},
            {
                "p": "sport_menu",
                "ts": str(int(self._clock() * 1000)),
                "gtype": "ft",
                "showtype": "live" if live else "today",
                "rtype": str(quote.get("rtype") or "").lower(),
            },
            {"p": "order"},
        )
        for form in forms:
            try:
                self._remaining(deadline)
                self._post(session, form, opener, deadline=deadline)
            except FollowPreparationError as exc:
                if exc.category == "CANCELLED":
                    raise
                return
            except Exception:
                continue

    def _verify_order_view(self, session, opener, order_view, quote, deadline):
        """遇到变盘文本先预热菜单再复核；仍变盘则交给服务重试，不提交。"""
        body = self._post(session, order_view, opener, deadline=deadline)
        tag = self._plain_tag(body)
        try:
            return verify_context(body, quote)
        except FollowPreparationError as exc:
            if exc.category != "RESPONSE_INVALID" or not tag:
                raise

        self._menu_warmup(session, opener, deadline, quote)
        body = self._post(session, order_view, opener, deadline=deadline)
        tag = self._plain_tag(body)
        try:
            return verify_context(body, quote)
        except FollowPreparationError:
            if tag and tag.lower() == "variablestandard":
                # 变盘时皇冠会拒绝未复核的 FT_bet（betError000），
                # 因此不盲目提交，交给服务在时效内继续重试核验。
                raise FollowPreparationError(
                    "皇冠盘口变盘（VariableStandard），暂不提交，稍后重新核验",
                    "VARIABLE_STANDARD",
                    retryable=True,
                ) from None
            if tag:
                raise FollowPreparationError(
                    f"皇冠盘口复核返回 {tag}", "CONTEXT_BLOCKED", retryable=True
                ) from None
            raise
        except ValueError:
            raise

    def prepare(self, mapping, intent, account_id):
        live = intent.get("is_live") is True
        try:
            deadline = self._monotonic() + PREPARE_TIMEOUT_SECONDS
            self._remaining(deadline)
            try:
                session = self._loader(account_id)
                endpoint = str(intent.get("_endpoint") or "").strip()
                if endpoint:
                    # 系统类错误重试时换域名：同一后端，会话靠 uid/ver 识别。
                    session = {**session, "origin": endpoint.rstrip("/")}
            except Exception:
                raise FollowPreparationError(
                    "皇冠会员会话当前不可用，请在号池检查", "ACCOUNT_SESSION", retryable=True
                ) from None
            self._remaining(deadline)
            opener = self._new_opener(session)
            cached = self._member_cache.get(account_id)
            if cached and self._clock() - cached[0] <= 30:
                currency = cached[1]
            else:
                member = scalar_fields(
                    xml_root(
                        self._post(
                            session,
                            {"p": "get_member_data", "change": "all"},
                            opener,
                            deadline=deadline,
                        )
                    )
                )
                if (
                    member.get("code", "").lower() == "error"
                    or member.get("enable", "").upper() != "Y"
                ):
                    raise FollowPreparationError(
                        "皇冠会员当前不可用", "ACCOUNT_SESSION", retryable=True
                    )
                currency = member.get("currency", "").upper()
                currency = "CNY" if currency == "RMB" else currency
                if not re.fullmatch(r"[A-Z]{3}", currency):
                    raise ValueError("皇冠未返回明确账号币种")
                if len(self._member_cache) > 64:
                    self._member_cache.clear()
                self._member_cache[account_id] = (self._clock(), currency)
            game = mapped_game(mapping)
            if game is None:
                # 比赛身份只能来自已确认映射；缺 ecid/队 ID 时不再去拉整份滚球列表。
                raise FollowPreparationError(
                    "赛事映射缺少皇冠盘口 ID，请在「赛事合并」刷新后重新确认该场",
                    "IDENTITY_INCOMPLETE",
                )
            boards = game_rows(
                self._post(
                    session,
                    {
                        "p": "get_game_more",
                        "gtype": "ft",
                        "showtype": "live" if live else "today",
                        "ltype": "3",
                        "isRB": "Y" if live else "N",
                        "lid": game["lid"],
                        "ecid": game["ecid"],
                        "mode": "NORMAL",
                        "from": "game_more",
                        "filter": "All",
                        "ts": str(int(self._clock() * 1000)),
                    },
                    opener,
                    deadline=deadline,
                )
            )
            quote = select_market(boards, game, intent)
            if not live:
                # 赛前盘口没有比分与开赛时钟；提交时 isRB 必须是 N。
                quote["is_live"] = False
                quote["score"] = ""
            # 盘口复核不能带 rtype：滚球和赛前带上一律返回 VariableStandard。
            order_view = {
                "p": "FT_order_view",
                "odd_f_type": "H",
                "gid": quote["gid"],
                "gtype": "FT",
                "wtype": quote["wtype"],
                "chose_team": quote["chose_team"],
            }
            quote = self._verify_order_view(session, opener, order_view, quote, deadline)
            # 赛前和滚球都必须通过复核，不能用未确认的盘口报价直接提交。
            self._remaining(deadline)
            quote.update(
                account_id=account_id,
                currency=currency,
                obtained_at=self._clock(),
                quote_id=uuid.uuid4().hex,
            )
            with self._lock:
                self._prepared = {
                    key: value
                    for key, value in self._prepared.items()
                    if self._clock() - value[0] <= 5
                }
                self._prepared[quote["quote_id"]] = (
                    quote["obtained_at"],
                    self._signature(quote),
                    self._signature(session),
                    opener,
                )
            return quote
        except FollowPreparationError:
            raise
        except ValueError as exc:
            raise FollowPreparationError(str(exc), "PREPARE_INVALID") from None
        except Exception:
            raise FollowPreparationError(
                "皇冠查盘暂时失败，请稍后重试", "PREPARE_UNAVAILABLE", retryable=True
            ) from None

    def find_history_wager(self, account_id, ticket):
        """按票号在皇冠会员历史里查结算（比分、水位、盈利）。"""
        digits = re.sub(r"\D", "", str(ticket or ""))
        if not digits:
            return None
        session = self._loader(account_id)
        opener = self._new_opener(session)
        deadline = self._monotonic() + PREPARE_TIMEOUT_SECONDS
        try:
            body = self._post(
                session,
                {
                    "p": "get_history_data",
                    "gtype": "ALL",
                    "isAll": "Y",
                    "startdate": "",
                    "enddate": "",
                    "filter": "N",
                },
                opener,
                deadline=deadline,
            )
            root = xml_root(body)
        except Exception:  # noqa: BLE001 - 历史接口异常时退回旧的输赢展示
            return None
        dates = [node.text.strip() for node in root.iter("date") if node.text and node.text.strip()]
        if not dates:
            raw = root.findtext("allDateValue") or ""
            dates = [item for item in raw.split("|") if item]
        seen = set()
        for day in dates[:6]:
            if day in seen:
                continue
            seen.add(day)
            try:
                body = self._post(
                    session,
                    {
                        "p": "history_switch",
                        "LS": "c",
                        "today_gmt": day,
                        "gtype": "ALL",
                        "tmp_flag": "Y",
                    },
                    opener,
                    deadline=deadline,
                )
                root = xml_root(body)
            except Exception:  # noqa: BLE001 - 跳过异常日期
                continue
            for wager in root.iter("wagers"):
                tid = re.sub(r"\D", "", str(wager.get("tid") or ""))
                wid = re.sub(r"\D", "", str(wager.findtext("w_id") or ""))
                if digits not in {tid, wid}:
                    continue
                fields = {child.tag: (child.text or "").strip() for child in wager}
                fields["date"] = day
                return fields
        return None

    def _today_wagers(self, account_id):
        """读取完整今日注单；无效响应不能当作空列表建立发送前基线。"""
        session = self._loader(account_id)
        opener = self._new_opener(session)
        body = self._post(
            session,
            {
                "p": "get_today_wagers",
                "LS": "g",
                "selGtype": "ALL",
                "chk_cw": "N",
                "ts": str(int(self._clock() * 1000)),
                "format": "json",
                "db_slow": "N",
            },
            opener,
            deadline=self._monotonic() + PREPARE_TIMEOUT_SECONDS,
        )
        data = json.loads(body.decode("utf-8", "replace"))
        if not isinstance(data, dict) or not isinstance(data.get("wagers"), list):
            raise ValueError("皇冠今日注单响应不完整")
        wagers = data["wagers"]
        if any(
            not isinstance(wager, dict) or not str(wager.get("w_id") or "").strip()
            for wager in wagers
        ):
            raise ValueError("皇冠今日注单缺少票号")
        if data.get("count") is not None and int(data["count"]) != len(wagers):
            raise ValueError("皇冠今日注单列表不完整")
        return wagers

    def today_wager_ids(self, account_id):
        """发送前只读记录现有票号，核对时禁止把历史注单认成本次提交。"""
        return {str(wager["w_id"]).strip() for wager in self._today_wagers(account_id)}

    def find_today_wager(self, account_id, gid, stake, *, excluded_tickets=None):
        """只接受发送前基线之外、gid 和精确金额唯一匹配的新票。"""
        if excluded_tickets is None:
            return None
        try:
            wanted_gid = str(gid or "").strip()
            wanted_stake = decimal(stake, positive=True)
        except (ValueError, TypeError):
            return None
        if not wanted_gid:
            return None
        matches = []
        for wager in self._today_wagers(account_id):
            if str(wager.get("gid") or "") != wanted_gid:
                continue
            try:
                amount = decimal(wager.get("gold"), positive=True)
            except (ValueError, TypeError):
                continue
            ticket = str(wager.get("w_id") or "").strip()
            if amount == wanted_stake and ticket and ticket not in excluded_tickets:
                matches.append(wager)
        return matches[0] if len(matches) == 1 else None

    def submit(self, quote, stake, account_id):
        rejected = {
            "status": "REJECTED",
            "message": "盘口确认已失效或金额不符合要求，请重新查盘",
            "category": "SUBMIT_INVALID",
            "error_code": "SUBMIT_INVALID",
            "retryable": False,
            "request_sent": False,
        }
        try:
            deadline = self._monotonic() + 5
            self._remaining(deadline)
            amount = decimal(stake, positive=True)
            if amount != amount.to_integral_value() or quote.get("account_id") != account_id:
                return rejected
            with self._lock:
                prepared = self._prepared.pop(quote.get("quote_id"), None)
            if not prepared or self._signature(quote) != prepared[1]:
                return rejected
            if not 0 <= self._clock() - prepared[0] <= 5:
                return {
                    **rejected,
                    "category": "QUOTE_EXPIRED",
                    "error_code": "QUOTE_EXPIRED",
                    "retryable": True,
                }
            try:
                session = self._loader(account_id)
            except Exception:
                return {
                    **rejected,
                    "category": "ACCOUNT_SESSION",
                    "error_code": "ACCOUNT_SESSION",
                    "retryable": True,
                }
            if self._signature(session) != prepared[2]:
                return {
                    **rejected,
                    "message": "会员会话已变化，请重新查盘",
                    "category": "ACCOUNT_SESSION",
                    "error_code": "ACCOUNT_SESSION",
                    "retryable": True,
                }
            fields = {
                "p": "FT_bet",
                "odd_f_type": "H",
                "golds": str(int(amount)),
                "gid": quote["gid"],
                "gtype": "FT",
                "wtype": quote["wtype"],
                "rtype": quote["rtype"],
                "chose_team": quote["chose_team"],
                "ioratio": quote["odds"],
                "con": quote["con"],
                "ratio": quote["ratio"],
                "autoOdd": "Y",
                "timestamp": str(int(self._clock() * 1000)),
                "timestamp2": "",
                "isRB": "Y" if quote.get("is_live", True) else "N",
                # 皇冠的下注来源标记按玩法区分：让球用 1M，大小用 1R。
                "f": "1M" if quote.get("wtype") in {"R", "HR"} else "1R",
                "imp": "N",
                "ptype": "",
                "isYesterday": "N",
            }
            self._remaining(deadline)
            if not 0 <= self._clock() - prepared[0] <= 5:
                return {
                    **rejected,
                    "category": "QUOTE_EXPIRED",
                    "error_code": "QUOTE_EXPIRED",
                    "retryable": True,
                }
        except FollowPreparationError as exc:
            return {
                **rejected,
                "message": str(exc),
                "category": exc.category,
                "error_code": exc.error_code,
                "retryable": exc.retryable,
            }
        except Exception:
            return rejected
        body, failure = self._post_bet(session, fields, prepared[3], deadline)
        if failure is not None:
            reason = str(failure.get("reason") or "")[:120]
            print(
                f"[crown] 提交失败 account={account_id} gid={quote.get('gid')} "
                f"rtype={quote.get('rtype')} err={reason}",
                flush=True,
            )
            if not failure.get("maybe_sent"):
                # 连接都没建立：请求体没有发出，可以安全重来。
                return {
                    "status": "REJECTED",
                    "message": f"未连上皇冠，本次未发出（{reason}）",
                    "category": "TRANSPORT_CONNECT",
                    "error_code": "TRANSPORT_CONNECT",
                    "retryable": True,
                    "request_sent": False,
                }
            return {
                "status": "UNKNOWN",
                "message": f"提交响应中断（{reason}），已按失败记录",
                "category": "UNCERTAIN_RESULT",
                "error_code": "UNCERTAIN_RESULT",
                "retryable": False,
                "request_sent": True,
            }
        try:
            return parse_receipt(body)
        except Exception as exc:
            reason = str(exc)[:120] or type(exc).__name__
            return {
                "status": "UNKNOWN",
                "message": f"回执无法解析（{reason}），已按失败记录",
                "category": "UNCERTAIN_RESULT",
                "error_code": "UNCERTAIN_RESULT",
                "retryable": False,
                "request_sent": True,
            }
