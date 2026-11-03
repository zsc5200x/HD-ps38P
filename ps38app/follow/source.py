"""把后台原始字段变为明确的市场意图，不依赖页面展示文案。"""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

from ..fixture_merge import normalize_league, normalize_team
from ..store import is_corner_order


def source_currency(value):
    """本后台 THB 只是显示标签，金额与人民币一比一，不做汇率换算。"""
    reported = str(value or "").strip().upper()
    if reported not in {"CNY", "RMB", "THB"}:
        raise ValueError("原单金额币种未确认人民币口径")
    return "CNY"


def decimal_value(value, label="数值"):
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label}缺失或无效")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}格式无效") from None
    if not result.is_finite() or abs(result) > Decimal("1000000000"):
        raise ValueError(f"{label}超出有效范围")
    return result


def number_text(value):
    value = value if isinstance(value, Decimal) and value.is_finite() else decimal_value(value)
    return "0" if value == 0 else format(value.normalize(), "f")


def parse_line(value):
    text = str(value if value is not None else "").strip().replace("－", "-")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?", text):
        raise ValueError("盘口线缺失或格式无效")
    sign = -1 if text.startswith("-") else 1
    parts = [decimal_value(part, "盘口线") for part in text.lstrip("+-").split("/")]
    if len(parts) == 2 and abs(parts[0] - parts[1]) != Decimal("0.5"):
        raise ValueError("分段盘口必须是相邻半球线")
    result = sum(parts) / len(parts) * sign
    if abs(result) > 100 or result * 4 != (result * 4).to_integral_value():
        raise ValueError("盘口线不是有效四分之一盘")
    return number_text(result)


def _timestamp_ms(value, label):
    # 毫秒时间戳大于金额/盘口的数值上限，单独进行有限整数校验。
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label}缺失")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{label}无效") from None
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise ValueError(f"{label}无效")
    if number > 32_503_680_000_000:
        raise ValueError(f"{label}超出范围")
    return int(number)


def source_intent(row):
    raw = row.get("raw_json")
    try:
        raw = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        raise ValueError("原始订单格式无效") from None
    if not isinstance(raw, dict):
        raise ValueError("缺少原始订单")
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("首版仅支持足球单关订单")
    choice = choices[0]
    if str(row.get("status") or raw.get("status") or "").upper() != "OPEN":
        raise ValueError("原单已不再是未结算订单")
    if str(raw.get("wagerStatus") or "").upper() in {
        "CANCELLED",
        "CANCELED",
        "REJECTED",
        "VOID",
        "取消",
        "拒绝",
        "作废",
    }:
        raise ValueError("原单已被取消或拒绝")
    if str(choice.get("sportName") or row.get("sport") or "").casefold() not in {
        "足球",
        "soccer",
        "football",
    }:
        raise ValueError("首版仅支持足球")
    if (
        is_corner_order(row)
        or choice.get("parentEventName")
        or choice.get("specialEventNameReversed")
    ):
        raise ValueError("首版不支持角球或特殊玩法")
    live = choice.get("inplay")
    if str(live).lower() not in {"true", "1"}:
        raise ValueError("首版仅支持滚球订单")
    period = choice.get("periodType")
    if period is None:
        period = {"全场": 0, "全場": 0, "上半场": 1, "上半場": 1}.get(choice.get("periodName"))
    if str(period) not in {"0", "1"}:
        raise ValueError("订单时段缺失或不支持")
    market = str(choice.get("btName") or "").strip().upper()
    if market in {"AH", "HDP", "让球", "讓球"}:
        market_type = "HANDICAP"
        sides = {
            "主": "HOME",
            "主队": "HOME",
            "HOME": "HOME",
            "客": "AWAY",
            "客队": "AWAY",
            "AWAY": "AWAY",
        }
    elif market in {"OU", "大小", "TOTAL"}:
        market_type = "TOTAL"
        sides = {"大": "OVER", "OVER": "OVER", "小": "UNDER", "UNDER": "UNDER"}
    else:
        raise ValueError("首版仅支持让球与大小")
    side = sides.get(str(choice.get("selection") or choice.get("selectionName") or "").upper())
    if side is None:
        raise ValueError("订单投注方向无法识别")
    line = parse_line(choice.get("handicap"))
    if market_type == "TOTAL" and Decimal(line) < 0:
        raise ValueError("大小盘口不能为负数")
    score_match = re.fullmatch(
        r"\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\s*", str(choice.get("score") or "")
    )
    if not score_match:
        raise ValueError("滚球原单缺少有效比分")
    teams = str(choice.get("eventName") or row.get("teams") or "")
    parts = re.split(r"\s*(?:-vs-|\s+vs\.?\s+|\s+v\s+| 对 )\s*", teams, maxsplit=1, flags=re.I)
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ValueError("原单缺少明确主客队")
    home, away = (part.strip() for part in parts)
    league = str(choice.get("leagueName") or row.get("league") or "").strip()
    if not league:
        raise ValueError("原单缺少联赛")
    event_ms = _timestamp_ms(choice.get("eventDate"), "开赛时间")
    created_ms = _timestamp_ms(row.get("created_ms", raw.get("createdDate")), "源单时间")
    stake = decimal_value(raw.get("stake"), "原单金额")
    if stake <= 0:
        raise ValueError("原单金额必须大于零")
    reported_currency = str(raw.get("currencyCode") or row.get("currency") or "").strip().upper()
    currency = source_currency(reported_currency)
    identity = {key: str(row.get(key) or "") for key in ("source", "member_identity", "order_id")}
    if not all(identity.values()):
        raise ValueError("原单唯一身份不完整")
    fingerprint = hashlib.sha256(
        json.dumps(
            [normalize_league(league), normalize_team(home), normalize_team(away), event_ms],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    odds = raw.get("odds", choice.get("odds"))
    if odds is not None:
        odds = number_text(decimal_value(odds, "原单赔率"))
    return {
        **identity,
        "member_code": str(row.get("member_user_code") or ""),
        "member_name": str(row.get("member_name") or ""),
        "agent_code": str(row.get("agent_code") or ""),
        "league": league,
        "home": home,
        "away": away,
        "event_ms": event_ms,
        "created_ms": created_ms,
        "source_event_id": str(choice.get("eventId") or ""),
        "fixture_key": fingerprint,
        "market_type": market_type,
        "period": "HT" if str(period) == "1" else "FT",
        "side": side,
        "line": line,
        "is_live": True,
        "score": f"{int(score_match[1])}:{int(score_match[2])}",
        "stake": number_text(stake),
        "currency": currency,
        "reported_currency": reported_currency,
        "odds": odds,
        "odds_format": str(raw.get("oddsFormat") or choice.get("oddsFormat") or "UNKNOWN"),
        "content_hash": str(row.get("content_hash") or ""),
    }
