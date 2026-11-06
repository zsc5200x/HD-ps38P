"""皇冠盘口纯解析；不读取会话，也不发出请求。"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
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


def optional_score(home, away=None):
    """盘口行不一定回显比分；解析不了就返回 None，交给确认接口核对。"""
    try:
        return score_text(home, away)
    except ValueError:
        return None


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
        row["master"] = node.get("master", "").upper()
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


def market_fields(market_type, *, half=False, live=True):
    """返回皇冠盘口字段名：赛前与滚球的字段编码不同。"""
    if market_type == "HANDICAP":
        if live:
            wtype = "HRE" if half else "RE"
            return {
                "wtype": wtype,
                "sw": f"sw_{wtype.lower()}",
                "line": f"ratio_{wtype.lower()}",
                "strong": "hstrong" if half else "strong",
                "odds_h": f"ior_{wtype.lower()}h",
                "odds_c": f"ior_{wtype.lower()}c",
            }
        wtype = "HR" if half else "R"
        return {
            "wtype": wtype,
            "sw": f"sw_{wtype.lower()}",
            "line": "hratio" if half else "ratio",
            "strong": "strong",
            "odds_h": f"ior_{wtype.lower()}h",
            "odds_c": f"ior_{wtype.lower()}c",
        }
    if live:
        wtype = "HROU" if half else "ROU"
        base = wtype.lower()
        return {
            "wtype": wtype,
            "sw": f"sw_{base}",
            "line_o": f"ratio_{base}o",
            "line_u": f"ratio_{base}u",
            "odds_h": f"ior_{base}h",
            "odds_c": f"ior_{base}c",
        }
    wtype = "HOU" if half else "OU"
    base = wtype.lower()
    return {
        "wtype": wtype,
        "sw": f"sw_{base}",
        # 赛前大小盘线在 ratio_o / ratio_u（半场 ratio_ho / ratio_hu）。
        "line_o": f"ratio_{'h' if half else ''}o",
        "line_u": f"ratio_{'h' if half else ''}u",
        "odds_h": f"ior_{base}h",
        "odds_c": f"ior_{base}c",
    }


def select_market(rows, game, intent):
    if intent.get("period") not in {"FT", "HT"}:
        raise ValueError("仅支持足球全场和上半场")
    market_type, side = intent.get("market_type"), intent.get("side")
    valid_sides = {"HANDICAP": {"HOME", "AWAY"}, "TOTAL": {"OVER", "UNDER"}}
    if side not in valid_sides.get(market_type, set()):
        raise ValueError("首版仅支持让球和大小盘口")
    half = intent["period"] == "HT"
    live = intent.get("is_live") is True
    fields = market_fields(market_type, half=half, live=live)
    wtype = fields["wtype"]
    choice = {"HOME": "H", "AWAY": "C", "OVER": "C", "UNDER": "H"}[side]
    wanted = line_number(intent["line"])
    matches = []
    score_changed = 0
    for row in rows:
        if row.get("mode", "").upper() != "FT" or row.get(fields["sw"]) != "Y":
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
        row_score = optional_score(row.get("score_h"), row.get("score_c"))
        if row_score is not None and row_score != score_text(intent["score"]):
            score_changed += 1
            continue
        try:
            if market_type == "HANDICAP":
                magnitude = abs(line_number(row.get(fields["line"])))
                strong = row.get(fields["strong"], "").upper()
                if magnitude and strong not in {"H", "C"}:
                    continue
                actual = -magnitude if choice == strong else magnitude
            else:
                suffix = "o" if side == "OVER" else "u"
                actual = line_number(row.get(fields[f"line_{suffix}"]))
                if actual < 0:
                    continue
            if actual != wanted:
                continue
            odd = decimal(row.get(fields[f"odds_{choice.lower()}"]), positive=True)
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
                "master": str(row.get("master") or "Y").upper(),
                "team_h_id": str(game.get("team_h_id") or ""),
                "team_c_id": str(game.get("team_c_id") or ""),
                "score": score_text(intent["score"]) if intent.get("score") else "",
                "home": game["team_h"],
                "away": game["team_c"],
            }
        )
    if len(matches) > 1:
        # 主盘和附加盘可能报同一条线：优先主盘，附加盘只在没有主盘时使用。
        primary = [item for item in matches if str(item.get("master") or "Y").upper() != "N"]
        if len(primary) == 1:
            matches = primary
        elif primary:
            matches = primary
    if len(matches) != 1:
        if not matches and score_changed:
            # 比分已经变了：这注的语义不再成立，必须停止而不是反复重查。
            raise FollowPreparationError("比分已变化，停止跟单", "SCORE_CHANGED", retryable=False)
        # 多条同线通常是盘口抖动，等一两秒再查一次，不要立刻判死。
        raise FollowPreparationError(
            "目标盘口缺失、封盘或存在多个匹配",
            "MARKET_UNAVAILABLE" if not matches else "MARKET_AMBIGUOUS",
            retryable=True,
        )
    return matches[0]


def _name_key(value):
    """队名兜底比对：去掉空白和括号注释，繁简差异按字形归一。"""
    text = re.sub(r"\s+", "", str(value or ""))
    text = re.sub(r"[（(].*?[)）]", "", text)
    for old, new in (("內", "内"), ("爾", "尔"), ("斯", "斯"), ("隊", "队")):
        text = text.replace(old, new)
    return text.casefold()


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
    rtype_ok = checks["rtype"] is None or checks["rtype"] == quote["rtype"]
    if not rtype_ok or any(decimal(checks[key]) != decimal(quote[key]) for key in ("con", "ratio")):
        raise FollowPreparationError("皇冠确认盘口已发生变化", "MARKET_CHANGED", retryable=True)
    for actual, expected in (
        (fields.get("gid") or fields.get("game_gid"), quote["gid"]),
        (fields.get("chose_team") or fields.get("choseteam"), quote["chose_team"]),
    ):
        if actual and actual != expected:
            raise ValueError("皇冠确认返回了不同盘口")
    for key, expected in (("wtype", quote["wtype"]), ("gtype", "FT")):
        if fields.get(key) and fields[key] != expected:
            raise ValueError("皇冠确认返回了不同赛事或玩法")
    # 队名只用于展示，皇冠译名会变（內/内）；有队伍 ID 时以 ID 为准。
    for side, expected in (
        ("h", quote.get("team_h_id") or ""),
        ("c", quote.get("team_c_id") or ""),
    ):
        actual = str(fields.get(f"team_id_{side}") or "").strip()
        if expected and actual and actual != str(expected).strip():
            raise ValueError("皇冠确认返回了不同赛事或玩法")
    for side, name in (("h", "home"), ("c", "away")):
        # 只有响应也回显该队 ID 时才能省略队名校验，报价单方持有 ID 不足为据。
        if quote.get(f"team_{side}_id") and fields.get(f"team_id_{side}"):
            continue
        expected = quote.get(name)
        actual = str(fields.get(f"team_name_{side}") or "").strip()
        if expected and actual and _name_key(actual) != _name_key(expected):
            raise ValueError("皇冠确认返回了不同赛事或玩法")
    if re.search(
        r"角球|corner",
        " ".join(fields.get(key, "") for key in ("ptype", "team_name_h", "team_name_c")),
        re.I,
    ):
        raise ValueError("皇冠确认返回了不同比赛类别")
    confirmed_score = optional_score(fields.get("score")) or optional_score(
        fields.get("score_h"), fields.get("score_c")
    )
    if confirmed_score is not None and confirmed_score != quote["score"]:
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


# 皇冠 FT_bet 错误码 → (中文提示, 分类, 是否可重试)
# 分类：MARKET_REJECTED=盘口问题可重查；ACCOUNT_CREDIT=账号额度问题换号；
#       AMOUNT_REJECTED=金额/限额问题；PLATFORM_REJECTED=不可重试的明确拒绝。
CROWN_ERRORS = {
    # 网站/系统类：换号 + 换域名重试，最多 3 次
    "0X001": ("网站流量较高，请重试", "retry_system"),
    "0X003": ("系统忙碌中，请稍后再试", "retry_system"),
    "0X006": ("系统正在优化中，请稍候", "stop"),
    # 盘口类
    "1X000": ("此选项不再开放投注，请从交易单移除", "stop"),
    "1X001": ("此选项目前不开放投注（盘口封盘）", "wait_1x001"),
    "1X002": ("已超过停止交易时间", "stop"),
    "1X003": ("本场已转至走地盘口", "stop"),
    "1X004": ("最小投注金额为 100", "stop"),
    "1X005": ("让球数、赔率或比分已更新", "retry_market_90"),
    "1X006": ("让球数、赔率或比分已更新", "retry_market_90"),
    "1X008": ("交易金额超过股东单场总信用额度", "switch_account"),
    "1X009": ("暂时停止交易", "retry_market_90"),
    "1X010": ("暂时停止交易", "retry_market_90"),
    "1X011": ("此选项不再开放投注，请从交易单移除", "retry_market_90"),
    "1X012": ("您的总投注金额已超过余额", "switch_account"),
    "1X013": ("赔率错误，请重新交易", "retry_market_90"),
    "1X015": ("让球数、赔率或比分已更新", "retry_market_90"),
    "1X016": ("让球数、赔率或比分已更新", "retry_market_90"),
    "1X017": ("已超过该场次过关投注限额", "switch_account"),
    "1X019": ("同一组合可赢金额超过上限", "stop"),
    "1X020": ("单注最高可赢金额超过上限", "split_half"),
    "1X026": ("所选赛程跨天，无法过关", "stop"),
    "1X032": ("综合过关选项不足", "stop"),
    # 回执类（不带 XML 错误码时也会出现）
    "order_failed": ("投注失败", "retry_3x10"),
    "connectfail": ("系统目前无法回应，请稍后再试", "stop"),
    "bet_pending": ("注单正在处理中，请稍候", "pending"),
    "bet_rejected": ("您的注单已被拒收", "stop"),
    "user_stop": ("您的账户已被停用，请联系上线", "disable_account"),
    "user_forbid": ("您的账户已被禁止登录，请联系上线", "disable_account"),
}

# 这些动作在重试后仍可能成功；其余一律终态。
RETRYABLE_ACTIONS = {
    "retry_system",
    "wait_1x001",
    "retry_market_90",
    "switch_account",
    "split_half",
    "retry_3x10",
    "pending",
    "disable_account",
    "retry_bet_switch",
}


def describe_crown_error(code):
    """把皇冠错误码翻成 (中文, 动作)；未知码返回空。"""
    entry = CROWN_ERRORS.get(str(code or "").strip())
    if entry is None:
        entry = CROWN_ERRORS.get(str(code or "").strip().lower())
    return entry


def parse_receipt(body):
    """仅返回明确的业务回执；没有回显的金额和水位保持为空。"""
    try:
        fields = scalar_fields(xml_root(body))
    except ValueError:
        return {
            "status": "UNKNOWN",
            "message": "提交响应无法识别，按失败处理",
            "category": "UNRECOGNIZED_RESPONSE",
            "error_code": "UNRECOGNIZED_RESPONSE",
            "retryable": False,
            "request_sent": True,
        }
    code = fields.get("code", "")
    ticket = fields.get("ticket_id") or fields.get("ticketid") or ""
    lowered_code = str(code).strip().lower()
    lowered_error = str(fields.get("errormsg") or "").strip().lower()
    # 被拒收的注单可能带票号，必须最先判失败，绝不能当成功。
    if lowered_code == "bet_rejected" or lowered_error == "bet_rejected":
        return {
            "status": "REJECTED",
            "message": "您的注单已被拒收",
            "category": "PLATFORM_REJECTED",
            "error_code": "bet_rejected",
            "action": "stop",
            "retryable": False,
            "request_sent": True,
        }
    if (
        code == "560"
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", ticket)
        and not fields.get("errormsg")
    ):
        result = {
            "status": "SUCCEEDED",
            "receipt_id": ticket,
            "action": "success",
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
            "message": "提交回执字段不一致，按失败处理",
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
        # betError000 是通用拒绝：先换号重试，仍失败再保留订单等盘口恢复。
        if str(raw_error).strip().lower() == "beterror000":
            return {
                "status": "REJECTED",
                "message": "皇冠拒绝本次请求（通用拒绝）",
                "category": "MARKET_REJECTED",
                "error_code": raw_error,
                "action": "retry_bet_switch",
                "retryable": True,
                "request_sent": True,
            }
        # 已知错误码：翻成中文，并给出后续动作。
        described = describe_crown_error(raw_error) or describe_crown_error(code)
        if described:
            label, action = described
            return {
                "status": "REJECTED",
                "message": label,
                "category": "MARKET_REJECTED" if action != "switch_account" else "ACCOUNT_CREDIT",
                "error_code": raw_error or code,
                "action": action,
                "retryable": action in RETRYABLE_ACTIONS,
                "request_sent": True,
            }
        session_error = code.lower() == "error" and fields.get("msg", "").lower() == "doublelogin"
        # betError* 是盘口/金额类的临时拒绝：换最新盘口可以再试（对齐参考项目）。
        market_error = raw_error in {"1X001", "1X003", "1X015", "1X016"} or raw_error.startswith(
            "betError"
        )
        # 555 且没有具体错误码时是会员侧问题，换一个会员可以再试。
        member_error = code == "555" and not raw_error
        retryable = market_error or session_error or member_error
        category = (
            "ACCOUNT_SESSION"
            if session_error or member_error
            else "MARKET_REJECTED"
            if market_error
            else "PLATFORM_REJECTED"
        )
        error_code = (
            "doubleLogin" if session_error else raw_error[:60] if raw_error else "PLATFORM_REJECTED"
        )
        return {
            "status": "REJECTED",
            "message": "皇冠明确拒绝本次请求，将重新查盘再试"
            if retryable
            else "皇冠明确拒绝本次请求，请检查账号或盘口",
            "category": category,
            "error_code": error_code,
            "retryable": retryable,
            "request_sent": True,
        }
    return {
        "status": "UNKNOWN",
        "message": "提交结果不明确，按失败处理",
        "category": "UNCERTAIN_RESULT",
        "error_code": "UNCERTAIN_RESULT",
        "retryable": False,
        "request_sent": True,
    }
