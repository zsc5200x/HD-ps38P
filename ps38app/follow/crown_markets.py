"""皇冠盘口纯解析；不读取会话，也不发出请求。"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

LIMIT = 8 * 1024 * 1024


class FollowPreparationError(ValueError):
    """可供调度器判断的准备错误；消息只包含脱敏业务原因。"""

    def __init__(self, message, category, *, retryable=False, error_code=""):
        super().__init__(message)
        self.category = category
        self.retryable = bool(retryable)
        self.error_code = error_code or category
        self.request_sent = False


def decimal(value, *, positive=False):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("皇冠数字字段无效") from None
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError("皇冠数字字段无效")
    return number


def text_number(value):
    return format(decimal(value), "f")


def line_number(value):
    raw = str(value).strip().replace("－", "-")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?", raw):
        raise ValueError("皇冠盘口线格式无效")
    sign = -1 if raw.startswith("-") else 1
    parts = [decimal(part) for part in raw.lstrip("+-").split("/")]
    if len(parts) == 2 and abs(parts[0] - parts[1]) != Decimal("0.5"):
        raise ValueError("皇冠分段盘口线无效")
    number = sign * sum(parts) / len(parts)
    if number * 4 != (number * 4).to_integral_value():
        raise ValueError("皇冠盘口不是四分之一单位")
    return number


def encode_line(value, *, market_type, side):
    """独立的 con/ratio 编码，保持投注方视角的让球符号。"""
    number = line_number(value)
    magnitude = abs(number)
    base = int(magnitude)
    fraction = magnitude - base
    if fraction == 0:
        con, ratio = base, 0
    elif fraction == Decimal("0.25"):
        con, ratio = base, 50
    elif fraction == Decimal("0.5"):
        con, ratio = base + 1, -100
    else:
        con, ratio = base + 1, -50
    if market_type == "HANDICAP":
        sign = -1 if number < 0 else 1
        return str(-sign * con), str(sign * ratio)
    if market_type != "TOTAL" or number < 0 or side not in {"OVER", "UNDER"}:
        raise ValueError("皇冠盘口意图无效")
    sign = 1 if side == "OVER" else -1
    return str(sign * con), str(-sign * ratio)


def xml_root(body):
    if not isinstance(body, (bytes, bytearray)) or len(body) > LIMIT:
        raise ValueError("皇冠响应过大或格式无效")
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("皇冠响应格式无效")
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError):
        raise FollowPreparationError(
            "皇冠未返回有效 XML", "RESPONSE_INVALID", retryable=True
        ) from None
    if root.tag.lower() != "serverresponse":
        raise FollowPreparationError("皇冠响应类型无效", "RESPONSE_INVALID", retryable=True)
    return root


def scalar_fields(root):
    fields = {}
    for child in root:
        if len(child):
            continue
        key = child.tag.lower()
        if key in fields:
            raise ValueError("皇冠响应包含重复字段")
        fields[key] = (child.text or "").strip()
    return fields


def game_rows(body):
    root = xml_root(body)
    top = scalar_fields(root)
    if top.get("code", "").lower() == "error":
        raise FollowPreparationError(
            "皇冠会话已失效，请在号池检查", "ACCOUNT_SESSION", retryable=True
        )
    rows = []
    for node in root.iter("game"):
        row = scalar_fields(node)
        row["mode"] = node.get("mode", "").upper()
        rows.append(row)
    if not rows and top.get("original"):
        try:
            original = json.loads(top["original"])
            if not isinstance(original, dict):
                raise ValueError()
            for entry in original.values():
                if not isinstance(entry, dict):
                    raise ValueError()
                rows.append({str(key).lower(): str(value) for key, value in entry.items()})
        except (ValueError, TypeError):
            raise FollowPreparationError(
                "皇冠赛事列表格式无效", "RESPONSE_INVALID", retryable=True
            ) from None
    return rows


def score_text(home, away=None):
    raw = str(home) if away is None else f"{home}:{away}"
    match = re.fullmatch(r"\s*(\d+)\s*[:-]\s*(\d+)\s*", raw)
    if not match:
        raise ValueError("皇冠比分缺失或无效")
    return f"{int(match[1])}:{int(match[2])}"


def timestamp(value, offset_minutes=-240):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ValueError("皇冠赛事缺少有效开赛时间") from None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
    return stamp.timestamp()


def mapped_game(mapping):
    """Build the live-query identity from a confirmed Crown fixture snapshot."""
    snapshot = mapping.get("crownSnapshot") or {}
    lid = str(snapshot.get("providerLeagueId") or snapshot.get("leagueId") or "")
    ecid = str(snapshot.get("ecid") or "")
    home_id = str(snapshot.get("homeId") or "")
    away_id = str(snapshot.get("awayId") or "")
    home = str(snapshot.get("home") or "")
    away = str(snapshot.get("away") or "")
    crown_id = str(mapping.get("crownId") or "")
    if not all((crown_id, lid, ecid, home_id, away_id, home, away)):
        return None
    return {
        "gid": crown_id,
        "lid": lid,
        "ecid": ecid,
        "team_h_id": home_id,
        "team_c_id": away_id,
        "team_h": home,
        "team_c": away,
    }


def resolve_game(rows, mapping, intent, *, offset_minutes=-240):
    snapshot = mapping.get("crownSnapshot") or {}
    crown_id = str(mapping.get("crownId") or "")
    lid = str(snapshot.get("providerLeagueId") or snapshot.get("leagueId") or "")
    home_id = str(snapshot.get("homeId") or "")
    away_id = str(snapshot.get("awayId") or "")
    expected_time = timestamp(snapshot.get("kickoffUtc"), offset_minutes)
    if not crown_id or not lid or not snapshot.get("home") or not snapshot.get("away"):
        raise FollowPreparationError("已合并赛事的皇冠身份不完整", "IDENTITY_INCOMPLETE")
    candidates = []
    for row in rows:
        if row.get("lid") != lid:
            continue
        if home_id and away_id:
            if row.get("team_h_id") != home_id or row.get("team_c_id") != away_id:
                continue
        elif row.get("gid") != crown_id:
            continue
        if row.get("team_h") != snapshot["home"] or row.get("team_c") != snapshot["away"]:
            continue
        if abs(timestamp(row.get("game_date_time"), offset_minutes) - expected_time) > 60:
            continue
        if row.get("is_finished", "").lower() in {"1", "true", "y"}:
            continue
        if re.search(
            r"角球|corner",
            " ".join(row.get(key, "") for key in ("league", "team_h", "team_c")),
            re.I,
        ):
            continue
        if not all(row.get(key) for key in ("gid", "ecid", "team_h_id", "team_c_id")):
            continue
        candidates.append(row)
    if len(candidates) != 1:
        raise FollowPreparationError(
            "皇冠比赛未唯一匹配，请刷新已合并赛事",
            "MARKET_UNAVAILABLE" if not candidates else "IDENTITY_CONFLICT",
            retryable=not candidates,
        )
    game = candidates[0]
    if score_text(game.get("score_h"), game.get("score_c")) != score_text(intent.get("score")):
        raise FollowPreparationError("皇冠当前比分与原单比分不同", "SCORE_CHANGED")
    return game


def select_market(rows, game, intent):
    if intent.get("is_live") is not True or intent.get("period") not in {"FT", "HT"}:
        raise ValueError("首版仅支持滚球足球全场和上半场")
    market_type, side = intent.get("market_type"), intent.get("side")
    valid_sides = {"HANDICAP": {"HOME", "AWAY"}, "TOTAL": {"OVER", "UNDER"}}
    if side not in valid_sides.get(market_type, set()):
        raise ValueError("首版仅支持让球和大小盘口")
    half = intent["period"] == "HT"
    wtype = ("HRE" if half else "RE") if market_type == "HANDICAP" else ("HROU" if half else "ROU")
    choice = {"HOME": "H", "AWAY": "C", "OVER": "C", "UNDER": "H"}[side]
    wanted = line_number(intent["line"])
    matches = []
    for row in rows:
        if row.get("mode", "").upper() != "FT" or row.get(f"sw_{wtype.lower()}") != "Y":
            continue
        # 皇冠同一行的全场／半场可能有不同盘口 ID；半场缺省兼容同 ID，仍须确认上下文。
        gid = (row.get("hgid") or row.get("gid")) if half else row.get("gid")
        if not gid:
            continue
        # 扩展盘口在同一个 ECID 请求下返回；显式回显的身份不能冲突。
        if any(
            row.get(key) and row[key] != game.get(key)
            for key in ("ecid", "lid", "team_h_id", "team_c_id", "team_h", "team_c")
        ):
            continue
        if any(key in row for key in ("score_h", "score_c")):
            if score_text(row.get("score_h"), row.get("score_c")) != score_text(intent["score"]):
                continue
        try:
            if market_type == "HANDICAP":
                magnitude = abs(line_number(row.get(f"ratio_{wtype.lower()}")))
                strong = row.get("hstrong" if half else "strong", "").upper()
                if magnitude and strong not in {"H", "C"}:
                    continue
                actual = -magnitude if choice == strong else magnitude
            else:
                suffix = "o" if side == "OVER" else "u"
                raw = row.get(f"ratio_{wtype.lower()}{suffix}")
                if raw is None:
                    raw = row.get(f"ratio_{wtype.lower()}o")
                actual = line_number(raw)
                if actual < 0:
                    continue
            if actual != wanted:
                continue
            odd = decimal(row.get(f"ior_{wtype.lower()}{choice.lower()}"), positive=True)
        except ValueError:
            continue
        con, ratio = encode_line(actual, market_type=market_type, side=side)
        matches.append(
            {
                "gid": gid,
                "master_gid": game["gid"],
                "gtype": "FT",
                "wtype": wtype,
                "rtype": wtype + choice,
                "chose_team": choice,
                "period": intent["period"],
                "market_type": market_type,
                "side": side,
                "line": text_number(actual),
                "con": con,
                "ratio": ratio,
                "odds": text_number(odd),
                "odds_format": "HK",
                "score": score_text(intent["score"]),
                "home": game["team_h"],
                "away": game["team_c"],
            }
        )
    if len(matches) != 1:
        raise FollowPreparationError(
            "目标盘口缺失、封盘或存在多个匹配",
            "MARKET_UNAVAILABLE" if not matches else "MARKET_AMBIGUOUS",
            retryable=not matches,
        )
    return matches[0]


def verify_context(body, quote):
    fields = scalar_fields(xml_root(body))
    if fields.get("code") != "501":
        if fields.get("code", "").lower() == "error":
            raise FollowPreparationError(
                "皇冠盘口确认会话当前不可用", "ACCOUNT_SESSION", retryable=True
            )
        raise FollowPreparationError("皇冠盘口确认暂不可用", "MARKET_UNAVAILABLE", retryable=True)
    checks = {
        "rtype": fields.get("rtype") or fields.get("fast_check"),
        "con": fields.get("con") if fields.get("con") is not None else fields.get("concede"),
        "ratio": fields.get("ratio"),
    }
    if checks["rtype"] != quote["rtype"] or any(
        decimal(checks[key]) != decimal(quote[key]) for key in ("con", "ratio")
    ):
        raise FollowPreparationError("皇冠确认盘口已发生变化", "MARKET_CHANGED", retryable=True)
    for actual, expected in (
        (fields.get("gid") or fields.get("game_gid"), quote["gid"]),
        (fields.get("chose_team") or fields.get("choseteam"), quote["chose_team"]),
    ):
        if actual and actual != expected:
            raise ValueError("皇冠确认返回了不同盘口")
    for key, expected in (
        ("wtype", quote["wtype"]),
        ("gtype", "FT"),
        ("team_name_h", quote.get("home")),
        ("team_name_c", quote.get("away")),
    ):
        if fields.get(key) and fields[key] != expected:
            raise ValueError("皇冠确认返回了不同赛事或玩法")
    if re.search(
        r"角球|corner",
        " ".join(fields.get(key, "") for key in ("ptype", "team_name_h", "team_name_c")),
        re.I,
    ):
        raise ValueError("皇冠确认返回了不同比赛类别")
    if any(key in fields for key in ("score_h", "score_c")):
        if score_text(fields.get("score_h"), fields.get("score_c")) != quote["score"]:
            raise ValueError("皇冠确认时比分已变化")
    confirmed = {**quote, "odds": text_number(decimal(fields.get("ioratio"), positive=True))}
    for field, key in (("gold_gmin", "min_stake"), ("gold_gmax", "max_stake")):
        if field in fields:
            confirmed[key] = text_number(decimal(fields[field], positive=True))
    if (
        confirmed.get("min_stake") is not None
        and confirmed.get("max_stake") is not None
        and decimal(confirmed["min_stake"]) > decimal(confirmed["max_stake"])
    ):
        raise FollowPreparationError("皇冠确认金额范围无效", "PREPARE_INVALID")
    return confirmed


def parse_receipt(body):
    """仅返回明确的业务回执；没有回显的金额和水位保持为空。"""
    try:
        fields = scalar_fields(xml_root(body))
    except ValueError:
        return {
            "status": "UNKNOWN",
            "message": "提交响应无法识别，等待核对",
            "category": "UNRECOGNIZED_RESPONSE",
            "error_code": "UNRECOGNIZED_RESPONSE",
            "retryable": False,
            "request_sent": True,
        }
    code = fields.get("code", "")
    ticket = fields.get("ticket_id") or fields.get("ticketid") or ""
    if (
        code == "560"
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", ticket)
        and not fields.get("errormsg")
    ):
        result = {
            "status": "SUCCEEDED",
            "receipt_id": ticket,
            "accepted_stake": None,
            "accepted_odds": None,
            "message": "皇冠已返回成功回执",
            "category": "SUCCESS",
            "error_code": "",
            "retryable": False,
            "request_sent": True,
        }
        for key, raw in (
            ("accepted_stake", fields.get("golds") or fields.get("gold")),
            ("accepted_odds", fields.get("ioratio")),
        ):
            if raw is not None:
                try:
                    result[key] = text_number(decimal(raw, positive=True))
                except ValueError:
                    pass
        return result
    if ticket or code == "560":
        return {
            "status": "UNKNOWN",
            "message": "提交回执字段不一致，等待核对",
            "category": "INCONSISTENT_RECEIPT",
            "error_code": "INCONSISTENT_RECEIPT",
            "retryable": False,
            "request_sent": True,
        }
    if (
        code == "555"
        or (code.lower() == "error" and fields.get("msg", "").lower() == "doublelogin")
        or fields.get("errormsg")
    ):
        raw_error = fields.get("errormsg", "")
        error_code = raw_error if re.fullmatch(r"1X\d{3}", raw_error) else "PLATFORM_REJECTED"
        session_error = code.lower() == "error" and fields.get("msg", "").lower() == "doublelogin"
        retryable = raw_error in {"1X001", "1X003", "1X015", "1X016"} or session_error
        category = (
            "ACCOUNT_SESSION"
            if session_error
            else "MARKET_REJECTED"
            if retryable
            else "PLATFORM_REJECTED"
        )
        return {
            "status": "REJECTED",
            "message": "皇冠明确拒绝本次请求，请检查账号或盘口",
            "category": category,
            "error_code": "doubleLogin" if session_error else error_code,
            "retryable": retryable,
            "request_sent": True,
        }
    return {
        "status": "UNKNOWN",
        "message": "提交结果不明确，等待核对",
        "category": "UNCERTAIN_RESULT",
        "error_code": "UNCERTAIN_RESULT",
        "retryable": False,
        "request_sent": True,
    }
