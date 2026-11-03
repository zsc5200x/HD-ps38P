"""独立皇冠适配器：现有号池会话、严格查盘以及单次提交。"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from http.cookiejar import Cookie, CookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, ProxyHandler, Request, build_opener

from ..account_pool.crown_member import _NoRedirects, cookie_header
from .crown_markets import (
    LIMIT,
    FollowPreparationError,
    decimal,
    game_rows,
    mapped_game,
    parse_receipt,
    resolve_game,
    scalar_fields,
    select_market,
    verify_context,
    xml_root,
)
from .reconcile import reconcile_response


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
            origin = str(meta.get("origin") or row["base_url"]).rstrip("/")
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
            raise FollowPreparationError("皇冠接口暂不可用", "TRANSPORT", retryable=True) from None
        except FollowPreparationError:
            raise
        except Exception:
            raise FollowPreparationError("皇冠请求暂时失败", "TRANSPORT", retryable=True) from None
        if status != 200:
            raise FollowPreparationError("皇冠接口暂不可用", "TRANSPORT", retryable=True)
        return body

    @staticmethod
    def _signature(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _resolve_live_game(self, session, opener, deadline, mapping, intent):
        now_ms = str(int(self._clock() * 1000))
        rows = game_rows(
            self._post(
                session,
                {
                    "p": "get_game_list",
                    "gtype": "ft",
                    "showtype": "live",
                    "rtype": "rb",
                    "ltype": "3",
                    "filter": "FT",
                    "cupFantasy": "N",
                    "sorttype": "L",
                    "isFantasy": "N",
                    "ts": now_ms,
                    "chgSortTS": now_ms,
                },
                opener,
                deadline=deadline,
            )
        )
        return resolve_game(
            rows,
            mapping,
            intent,
            offset_minutes=int(self.config.get("crown_utc_offset_minutes", -240)),
        )

    def prepare(self, mapping, intent, account_id):
        if intent.get("is_live") is not True:
            raise FollowPreparationError("首版仅支持滚球足球盘口", "UNSUPPORTED")
        try:
            deadline = self._monotonic() + 10
            self._remaining(deadline)
            try:
                session = self._loader(account_id)
            except Exception:
                raise FollowPreparationError(
                    "皇冠会员会话当前不可用，请在号池检查", "ACCOUNT_SESSION", retryable=True
                ) from None
            self._remaining(deadline)
            opener = self._new_opener(session)
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
            game = mapped_game(mapping)
            direct_lookup = game is not None
            if not direct_lookup:
                # Older confirmed mappings may not have ECID/team IDs yet.
                game = self._resolve_live_game(session, opener, deadline, mapping, intent)
            boards = game_rows(
                self._post(
                    session,
                    {
                        "p": "get_game_more",
                        "gtype": "ft",
                        "showtype": "live",
                        "ltype": "3",
                        "isRB": "Y",
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
            if direct_lookup and not all("score_h" in row and "score_c" in row for row in boards):
                # 扩展盘口缺少完整比分时，保留原有的当前比分核验。
                live_game = self._resolve_live_game(session, opener, deadline, mapping, intent)
                if live_game.get("ecid") != game.get("ecid"):
                    raise FollowPreparationError(
                        "已合并赛事身份已变化，请刷新赛事映射",
                        "IDENTITY_CONFLICT",
                    )
                game = live_game
            quote = select_market(boards, game, intent)
            quote = verify_context(
                self._post(
                    session,
                    {
                        "p": "FT_order_view",
                        "odd_f_type": "H",
                        "gid": quote["gid"],
                        "gtype": "FT",
                        "wtype": quote["wtype"],
                        "chose_team": quote["chose_team"],
                    },
                    opener,
                    deadline=deadline,
                ),
                quote,
            )
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
                "autoOdd": "N",
                "timestamp": str(int(self._clock() * 1000)),
                "timestamp2": "",
                "isRB": "Y",
                "imp": "N",
                "ptype": "",
                "isYesterday": "N",
                "f": "1R",
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
        try:
            # 只发一次。连接中断、HTTP 失败与无法识别响应都不能证明请求未被接收。
            return parse_receipt(self._post(session, fields, prepared[3], deadline=deadline))
        except Exception:
            return {
                "status": "UNKNOWN",
                "message": "提交响应中断，结果待核对；不会自动重复提交",
                "category": "UNCERTAIN_RESULT",
                "error_code": "UNCERTAIN_RESULT",
                "retryable": False,
                "request_sent": True,
            }

    def reconcile(self, attempt, account_id):
        """固定原会员只读查询今日注单；未唯一核实时始终保持 UNKNOWN。"""
        unknown = {
            "status": "UNKNOWN",
            "message": "平台回执尚未唯一核实，继续保留待核对",
            "category": "RECONCILE_UNAVAILABLE",
            "error_code": "RECONCILE_UNAVAILABLE",
            "retryable": False,
            "request_sent": False,
        }
        if not isinstance(attempt, dict) or attempt.get("account_id") != account_id:
            return {
                **unknown,
                "category": "ACCOUNT_MISMATCH",
                "error_code": "ACCOUNT_MISMATCH",
                "message": "核对账号与原提交账号不一致",
            }
        try:
            deadline = self._monotonic() + 5
            self._remaining(deadline)
            session = self._loader(account_id)
            if session.get("id") != account_id:
                return {
                    **unknown,
                    "category": "ACCOUNT_MISMATCH",
                    "error_code": "ACCOUNT_MISMATCH",
                    "message": "核对会话与原提交账号不一致",
                }
            body = self._post(
                session,
                {
                    "p": "get_today_wagers",
                    "LS": "g",
                    "selGtype": "FT",
                    "chk_cw": "N",
                    "ts": str(int(self._clock() * 1000)),
                    "format": "json",
                    "db_slow": "N",
                },
                self._new_opener(session),
                deadline=deadline,
            )
            return reconcile_response(body, attempt, account_id, account_uid=session.get("uid"))
        except Exception:
            return unknown
