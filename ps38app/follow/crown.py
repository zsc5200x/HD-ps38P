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
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, ProxyHandler, Request, build_opener

from ..account_pool.crown_member import _NoRedirects, cookie_header, preferred_origin
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
                    "SELECT id,name,base_url,platform,enabled,status FROM accounts WHERE id=?",
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
            if not cookie or not uid or not ver:
                raise ValueError("皇冠会员会话不完整或已过期")
            return {
                "id": account_id,
                "name": row["name"],
                "origin": origin,
                "uid": uid,
                "ver": ver,
                "cookie": cookie,
                "user_agent": str(meta.get("user_agent") or "Mozilla/5.0"),
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
        request_deadline = self._monotonic() + 5
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
            if self._transport is not None:
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
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            if self.config.get("pool_crown_ignore_https_errors", True):
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
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

    WARMUP_ATTEMPTS = 2

    def _warmup(self, connection, path, session, cookie, deadline):
        """在正式下注前用一条只读请求确认通道可用。

        皇冠 TLS 连接会随机中途断开；先跑一次无害查询，断了就换连接重来，
        避免把坏连接用在真正的下注请求上。
        """
        body = urlencode(
            {"uid": session["uid"], "ver": session["ver"], "langx": "zh-cn",
             "p": "get_member_data", "change": "all"}
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
                self._warmup(connection, path, session, cookie, deadline)
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

    def prepare(self, mapping, intent, account_id):
        live = intent.get("is_live") is True
        try:
            deadline = self._monotonic() + 10
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
                if member.get("code", "").lower() == "error" or member.get("enable", "").upper() != "Y":
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
            # 赛前与滚球都能用 FT_order_view 复核水位；赛前不能带 rtype。
            order_view = {
                "p": "FT_order_view",
                "odd_f_type": "H",
                "gid": quote["gid"],
                "gtype": "FT",
                "wtype": quote["wtype"],
                "chose_team": quote["chose_team"],
            }
            if live:
                order_view["rtype"] = quote["rtype"]
            quote = verify_context(
                self._post(session, order_view, opener, deadline=deadline),
                quote,
            )
            # 赛前盘口不支持 FT_order_view（返回 VariableStandard），
            # 报价直接来自 get_game_more，水位由 autoOdd=Y 在提交时对齐。
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
