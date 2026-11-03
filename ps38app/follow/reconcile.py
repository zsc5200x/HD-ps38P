"""今日注单的保守只读核对，不把缺失、空列表或最近一票当作成交依据。

当前契约只接受显式成功且完整的 JSON 注单集合。站点缺少完整性标记、
只返回展示文本或使用未经确认的字段时保持 UNKNOWN，等待脱敏样本补充契约。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, DecimalException

from .crown_markets import LIMIT, decimal, line_number, text_number


class ReconcileInvalid(ValueError):
    pass


def _field(row, *keys):
    values = [row[key] for key in keys if key in row and row[key] not in (None, "")]
    if not values:
        return None
    if any(str(value) != str(values[0]) for value in values[1:]):
        raise ReconcileInvalid("字段回显冲突")
    return values[0]


def _row(value):
    if not isinstance(value, dict):
        raise ReconcileInvalid("注单格式无效")
    lowered = {}
    for key, item in value.items():
        key = str(key).lower()
        if key in lowered:
            raise ReconcileInvalid("重复字段")
        lowered[key] = item
    return lowered


def _json_object(pairs):
    result, keys = {}, set()
    for key, value in pairs:
        lowered = str(key).lower()
        if lowered in keys:
            raise ReconcileInvalid("响应包含重复字段")
        keys.add(lowered)
        result[key] = value
    return result


def _time(value):
    if value is None or isinstance(value, bool):
        raise ReconcileInvalid("缺少明确时间")
    try:
        stamp = decimal(value, positive=True)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise ReconcileInvalid("时间格式无效") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ReconcileInvalid("时间缺少时区")
        stamp = Decimal(str(parsed.timestamp()))
        if not 0 < stamp <= Decimal("32503680000"):
            raise ReconcileInvalid("时间超出范围")
        return stamp
    if stamp > Decimal("32503680000000"):
        raise ReconcileInvalid("时间超出范围")
    if stamp > Decimal("100000000000"):
        stamp /= 1000
    if stamp > Decimal("32503680000"):
        raise ReconcileInvalid("时间超出范围")
    return stamp


def _complete_rows(payload):
    outer = _row(payload)
    container = outer
    nested = [outer[key] for key in ("data", "response") if isinstance(outer.get(key), dict)]
    if len(nested) > 1:
        raise ReconcileInvalid("响应封装不唯一")
    if nested:
        container = _row(nested[0])
    envelopes = (outer,) if container is outer else (outer, container)
    success = False
    for envelope in envelopes:
        if envelope.get("ok") is False or envelope.get("success") is False:
            raise ReconcileInvalid("查询未成功")
        if envelope.get("error") or envelope.get("errormsg"):
            raise ReconcileInvalid("查询返回错误")
        if "status" in envelope and str(envelope["status"]).lower() not in {
            "0",
            "200",
            "success",
            "ok",
        }:
            raise ReconcileInvalid("查询状态冲突")
        if envelope.get("ok") is True or envelope.get("success") is True:
            success = True
        if "code" in envelope:
            if str(envelope["code"]).lower() not in {"0", "200", "success", "ok"}:
                raise ReconcileInvalid("查询状态未知")
            success = True
    if not success:
        raise ReconcileInvalid("查询缺少明确成功标记")
    collections = [container[key] for key in ("wagers", "records", "rows") if key in container]
    if len(collections) != 1 or not isinstance(collections[0], list):
        raise ReconcileInvalid("注单集合无效")
    records = collections[0]
    if len(records) > 10000 or any(not isinstance(item, dict) for item in records):
        raise ReconcileInvalid("注单集合无效")
    complete = False
    for envelope in envelopes:
        if "complete" in envelope and not isinstance(envelope["complete"], bool):
            raise ReconcileInvalid("查询完整性标记无效")
        if "has_more" in envelope and not isinstance(envelope["has_more"], bool):
            raise ReconcileInvalid("查询分页标记无效")
        if envelope.get("complete") is False or envelope.get("has_more") is True:
            raise ReconcileInvalid("查询不完整")
        if envelope.get("next_cursor") not in (None, ""):
            raise ReconcileInvalid("查询尚有后续页")
        if "offset" in envelope and envelope["offset"] != 0:
            raise ReconcileInvalid("查询不是完整集合")
        if "page" in envelope and envelope["page"] not in (0, 1):
            raise ReconcileInvalid("查询不是完整集合")
        if "total" in envelope:
            count = envelope["total"]
            if isinstance(count, bool) or not str(count).isdigit() or int(count) != len(records):
                raise ReconcileInvalid("查询总数不一致")
            complete = True
        complete = complete or envelope.get("complete") is True
    if not complete:
        raise ReconcileInvalid("查询缺少完整性依据")
    owners = [
        value
        for value in (
            _field(container, "account_id", "member_id"),
            _field(outer, "account_id", "member_id"),
        )
        if value is not None
    ]
    if owners and any(str(owner) != str(owners[0]) for owner in owners[1:]):
        raise ReconcileInvalid("响应账号归属冲突")
    return records, owners[0] if owners else None


def _expected(attempt, account_id):
    if not isinstance(attempt, dict) or attempt.get("account_id") != account_id:
        raise ReconcileInvalid("账号不一致")
    quote = attempt.get("submitted_quote") or attempt.get("quote")
    target = attempt.get("target")
    if not isinstance(quote, dict) or not isinstance(target, dict):
        raise ReconcileInvalid("缺少提交意图")
    if quote.get("account_id") not in (None, account_id):
        raise ReconcileInvalid("报价账号不一致")
    for key in ("period", "market_type", "side"):
        if quote.get(key) != target.get(key) or not quote.get(key):
            raise ReconcileInvalid("提交意图不一致")
    line = line_number(quote.get("line"))
    if line != line_number(target.get("line")):
        raise ReconcileInvalid("提交盘口不一致")
    if target.get("is_live") is not True:
        raise ReconcileInvalid("缺少滚球身份")
    if quote.get("period") not in {"FT", "HT"} or quote.get("market_type") not in {
        "HANDICAP",
        "TOTAL",
    }:
        raise ReconcileInvalid("提交玩法不支持")
    valid_sides = {"HANDICAP": {"HOME", "AWAY"}, "TOTAL": {"OVER", "UNDER"}}
    if quote["side"] not in valid_sides[quote["market_type"]]:
        raise ReconcileInvalid("提交方向无效")
    if quote.get("currency") != "CNY" or not quote.get("gid") or not quote.get("master_gid"):
        raise ReconcileInvalid("提交身份不完整")
    stake = decimal(attempt.get("stake"), positive=True)
    if stake > 1_000_000_000 or stake != stake.to_integral_value():
        raise ReconcileInvalid("提交金额无效")
    return {
        **quote,
        "line": line,
        "stake": stake,
        "started": _time(attempt.get("started_at")),
    }


def _match_record(raw, expected, account_id, account_uid, envelope_account):
    row = _row(raw)
    gid = str(_field(row, "gid", "game_id") or "")
    market_gid = str(_field(row, "market_gid", "line_gid") or "")
    if gid not in {str(expected["gid"]), str(expected["master_gid"])} and market_gid != str(
        expected["gid"]
    ):
        if not gid and not market_gid:
            raise ReconcileInvalid("注单缺少赛事身份")
        return None
    if gid == str(expected["gid"]):
        if market_gid and market_gid != str(expected["gid"]):
            raise ReconcileInvalid("盘口身份冲突")
    elif gid != str(expected["master_gid"]) or market_gid != str(expected["gid"]):
        raise ReconcileInvalid("主赛事与盘口关联不完整")
    master_gid = _field(row, "master_gid")
    if master_gid is not None and str(master_gid) != str(expected["master_gid"]):
        raise ReconcileInvalid("主赛事冲突")
    owner = _field(row, "account_id", "member_id")
    if owner is None:
        owner = envelope_account
    uid = _field(row, "uid")
    if owner is not None and str(owner) != account_id:
        raise ReconcileInvalid("注单账号冲突")
    if uid is not None and (not account_uid or str(uid) != str(account_uid)):
        raise ReconcileInvalid("注单会话账号冲突")
    if owner is None and uid is None:
        raise ReconcileInvalid("注单账号缺失")
    if str(_field(row, "gtype", "sport") or "").upper() != "FT":
        raise ReconcileInvalid("注单球种不完整")
    wtype = str(_field(row, "wtype") or "").upper()
    inferred = {
        "RE": ("FT", "HANDICAP"),
        "ROU": ("FT", "TOTAL"),
        "HRE": ("HT", "HANDICAP"),
        "HROU": ("HT", "TOTAL"),
    }.get(wtype)
    if wtype and inferred is None:
        raise ReconcileInvalid("注单玩法编码未经确认")
    period = str(_field(row, "period") or (inferred[0] if inferred else "")).upper()
    market_type = str(_field(row, "market_type") or (inferred[1] if inferred else "")).upper()
    if inferred and (period, market_type) != inferred:
        raise ReconcileInvalid("玩法回显冲突")
    if not period or not market_type:
        raise ReconcileInvalid("注单玩法缺失")
    live = _field(row, "is_live", "isrb")
    if live is not None and str(live).lower() not in {"true", "1", "y"}:
        return None
    if live is None and not inferred:
        raise ReconcileInvalid("注单缺少滚球身份")
    if (period, market_type) != (expected["period"], expected["market_type"]):
        return None
    choice = str(_field(row, "chose_team", "choseteam") or "").upper()
    side_map = {"HANDICAP": {"H": "HOME", "C": "AWAY"}, "TOTAL": {"C": "OVER", "H": "UNDER"}}
    side = str(_field(row, "side") or side_map[market_type].get(choice, "")).upper()
    if not side or choice and side_map[market_type].get(choice) != side:
        raise ReconcileInvalid("注单方向缺失或冲突")
    rtype = str(_field(row, "rtype") or "").upper()
    if rtype and (not wtype or not choice or rtype != wtype + choice):
        raise ReconcileInvalid("方向编码冲突")
    if side != expected["side"]:
        return None
    # line / selected_line 必须是投注方视角的带符号线，不猜展示字段的让球方。
    line = _field(row, "line", "selected_line")
    con, ratio = _field(row, "con", "concede"), _field(row, "ratio")
    decoded = None
    if (con is None) != (ratio is None):
        raise ReconcileInvalid("注单盘口编码不完整")
    if con is not None and ratio is not None:
        if abs(decimal(con)) > 1000 or abs(decimal(ratio)) > 10000:
            raise ReconcileInvalid("注单盘口编码超出范围")
        decoded = (
            -decimal(con) + decimal(ratio) / 200
            if market_type == "HANDICAP"
            else abs(decimal(con) - decimal(ratio) / 200)
        )
    if line is None and decoded is None:
        raise ReconcileInvalid("注单缺少带符号盘口")
    actual = line_number(line) if line is not None else line_number(decoded)
    if abs(actual) > 100:
        raise ReconcileInvalid("注单盘口超出范围")
    if decoded is not None and actual != decoded:
        raise ReconcileInvalid("注单盘口编码冲突")
    if actual != expected["line"]:
        return None
    currency = str(_field(row, "currency", "currencycode") or "").upper()
    if currency not in {"CNY", "RMB"}:
        raise ReconcileInvalid("注单币种未确认")
    stake = decimal(_field(row, "stake", "golds", "gold", "amount"), positive=True)
    if stake != expected["stake"]:
        return None
    stamp = _time(
        _field(row, "bet_ts", "ts", "submitted_at", "created_at", "wager_time", "timestamp")
    )
    if not expected["started"] - 5 <= stamp <= expected["started"] + 90:
        return None
    state = str(_field(row, "status", "wager_status") or "").upper()
    accepted_states = {
        "OPEN",
        "SETTLED",
        "ACCEPTED",
        "SUCCESS",
        "SUCCEEDED",
        "CONFIRMED",
    }
    if "accepted" in row and row["accepted"] is not True:
        raise ReconcileInvalid("注单未明确接收")
    if state and state not in accepted_states:
        raise ReconcileInvalid("注单接收状态冲突")
    if row.get("accepted") is not True and state not in accepted_states:
        raise ReconcileInvalid("注单缺少已接收状态")
    ticket = str(_field(row, "ticket_id", "ticketid", "w_id", "tid", "wager_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", ticket):
        raise ReconcileInvalid("票号无效")
    odds = None
    if str(_field(row, "odds_format", "odd_f_type") or "").upper() in {"HK", "H"}:
        raw_odds = _field(row, "ioratio", "odds")
        if raw_odds is not None:
            parsed_odds = decimal(raw_odds, positive=True)
            if not Decimal("0.00000001") <= parsed_odds <= 10000:
                raise ReconcileInvalid("注单水位超出范围")
            odds = text_number(parsed_odds.normalize())
    return {
        "receipt_id": ticket,
        "accepted_stake": text_number(stake.normalize()),
        "accepted_odds": odds,
    }


def reconcile_response(body, attempt, account_id, *, account_uid=None):
    """只对完整成功查询中的唯一、完整匹配返回 SUCCEEDED。"""
    unknown = {
        "status": "UNKNOWN",
        "message": "平台回执尚未唯一核实，继续保留待核对",
        "category": "RECONCILE_UNCONFIRMED",
        "error_code": "RECONCILE_UNCONFIRMED",
        "retryable": False,
        "request_sent": False,
    }
    try:
        if isinstance(body, (bytes, bytearray, str)):
            if len(body) > LIMIT:
                raise ReconcileInvalid("核对响应过大")
            payload = json.loads(body, object_pairs_hook=_json_object)
        else:
            payload = body
        expected = _expected(attempt, account_id)
        records, envelope_account = _complete_rows(payload)
        matches = []
        for record in records:
            match = _match_record(record, expected, account_id, account_uid, envelope_account)
            if match is not None:
                matches.append(match)
        if len(matches) != 1:
            return {
                **unknown,
                "category": "RECONCILE_AMBIGUOUS" if matches else "RECONCILE_NOT_FOUND",
                "error_code": "RECONCILE_AMBIGUOUS" if matches else "RECONCILE_NOT_FOUND",
            }
        return {
            **matches[0],
            "status": "SUCCEEDED",
            "message": "已通过原账号注单唯一核实成功回执",
            "category": "RECONCILED",
            "error_code": "",
            "retryable": False,
            "request_sent": False,
        }
    except (ValueError, TypeError, KeyError, OverflowError, DecimalException, RecursionError):
        return {
            **unknown,
            "category": "RECONCILE_INVALID",
            "error_code": "RECONCILE_INVALID",
            "message": "平台查询或注单字段缺少可靠核对依据，继续保留待核对",
        }
