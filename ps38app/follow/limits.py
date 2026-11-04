"""按整场 / 同盘口累计的跟单额度计算。

语义与旧系统一致：
- 单场限额：同一场比赛所有已成功与在途跟单金额之和；
- 盘口限额：同一场比赛 + 同一盘口方向（大小球分大/小，让球合并）的金额之和；
- 额度 = 限额 − 已成功 − 在途预留；不足最小下注时跳过。
"""

import json
from decimal import Decimal

from .source import decimal_value, number_text
from .store import PROCESSING

MARKET_LABELS = {
    "total_over": "大球",
    "total_under": "小球",
    "handicap": "让球",
    "moneyline": "独赢",
    "other": "其他",
}
COMMITTED = {"SUCCEEDED", "PARTIAL"}


def limit_identity(group):
    """从已确认赛事映射生成稳定的比赛键和盘口键。"""
    mapping = group.get("mapping") if isinstance(group.get("mapping"), dict) else {}
    crown = mapping.get("crownSnapshot") if isinstance(mapping.get("crownSnapshot"), dict) else {}
    match_key = str(mapping.get("crownId") or mapping.get("ps38Id") or crown.get("id") or "")
    match_label = str(
        crown.get("name")
        or f"{group.get('crown_home') or group.get('home') or ''} v "
        f"{group.get('crown_away') or group.get('away') or ''}"
    ).strip(" v")
    market_type = str(group.get("market_type") or "").upper()
    side = str(group.get("target_side") or group.get("source_side") or "").upper()
    if market_type == "TOTAL":
        code = "total_under" if side == "UNDER" else "total_over"
    elif market_type == "HANDICAP":
        code = "handicap"
    elif market_type == "MONEYLINE":
        code = "moneyline"
    else:
        code = "other"
    return {
        "match_key": match_key,
        "match_label": match_label or match_key,
        "market_key": f"{match_key}|{code}" if match_key else "",
        "market_code": code,
        "market_label": MARKET_LABELS.get(code, code),
    }


def _group_amount(group):
    try:
        return decimal_value(group.get("target_stake") or 0, "计划金额")
    except ValueError:
        return Decimal("0")


def _committed_amount(group):
    try:
        value = decimal_value(group.get("accepted_stake") or 0, "成交金额")
    except ValueError:
        value = Decimal("0")
    return value if value > 0 else _group_amount(group)


def load_usage(connection, exclude_group_id=""):
    """统计已成功金额与在途预留；跳过当前组，避免重复计算。"""
    statuses = tuple(sorted(PROCESSING | COMMITTED | {"UNKNOWN"}))
    placeholders = ",".join("?" for _ in statuses)
    rows = connection.execute(
        f"SELECT id,status,payload FROM follow_groups WHERE status IN ({placeholders})",
        statuses,
    ).fetchall()
    match_committed = {}
    market_committed = {}
    match_reserved = {}
    market_reserved = {}
    for row in rows:
        group_id, status = row[0], str(row[1])
        if exclude_group_id and group_id == str(exclude_group_id):
            continue
        try:
            group = json.loads(row[2])
        except (TypeError, ValueError):
            continue
        identity = limit_identity(group)
        match_key, market_key = identity["match_key"], identity["market_key"]
        if not match_key or not market_key:
            continue
        if status in COMMITTED:
            amount = _committed_amount(group)
            if amount <= 0:
                continue
            match_committed[match_key] = match_committed.get(match_key, Decimal("0")) + amount
            market_committed[market_key] = market_committed.get(market_key, Decimal("0")) + amount
        elif status in PROCESSING or status == "UNKNOWN":
            amount = _group_amount(group)
            if amount <= 0:
                continue
            match_reserved[match_key] = match_reserved.get(match_key, Decimal("0")) + amount
            market_reserved[market_key] = market_reserved.get(market_key, Decimal("0")) + amount
    return {
        "match_committed": match_committed,
        "market_committed": market_committed,
        "match_reserved": match_reserved,
        "market_reserved": market_reserved,
    }


def apply_limits(group, rule, usage, min_stake=1):
    """把计划金额压到剩余额度内；返回 (允许金额, 跳过原因, 明细)。"""
    identity = limit_identity(group)
    requested = _group_amount(group)
    try:
        match_limit = decimal_value(rule.get("match_limit_amount") or 0, "单场限额")
        market_limit = decimal_value(rule.get("market_limit_amount") or 0, "盘口限额")
    except ValueError:
        match_limit = market_limit = Decimal("0")
    match_key, market_key = identity["match_key"], identity["market_key"]
    meta = {
        **identity,
        "requested_stake": number_text(requested) if requested > 0 else "0",
        "match_limit_amount": number_text(match_limit),
        "market_limit_amount": number_text(market_limit),
        "match_committed_amount": number_text(usage["match_committed"].get(match_key, 0)),
        "market_committed_amount": number_text(usage["market_committed"].get(market_key, 0)),
        "match_reserved_amount": number_text(usage["match_reserved"].get(match_key, 0)),
        "market_reserved_amount": number_text(usage["market_reserved"].get(market_key, 0)),
    }
    if requested <= 0:
        return 0, "跟单金额为 0。", meta
    if (match_limit > 0 or market_limit > 0) and not match_key:
        return 0, "无法识别比赛，已按累计限额保护跳过。", meta

    allowed = requested
    reached = []
    if match_limit > 0:
        remaining = match_limit - usage["match_committed"].get(match_key, Decimal("0")) - usage[
            "match_reserved"
        ].get(match_key, Decimal("0"))
        meta["match_remaining_amount"] = number_text(max(remaining, Decimal("0")))
        allowed = min(allowed, max(remaining, Decimal("0")))
        if remaining <= 0:
            reached.append("单场限额")
    if market_limit > 0:
        remaining = market_limit - usage["market_committed"].get(market_key, Decimal("0")) - usage[
            "market_reserved"
        ].get(market_key, Decimal("0"))
        meta["market_remaining_amount"] = number_text(max(remaining, Decimal("0")))
        allowed = min(allowed, max(remaining, Decimal("0")))
        if remaining <= 0:
            reached.append(f"{identity['market_label']}限额")

    allowed_int = int(allowed)
    minimum = max(1, int(min_stake or 1))
    if allowed_int < minimum:
        reason = "、".join(reached) or "累计限额剩余额度"
        return 0, f"已按{reason}跳过：剩余额度不足最小下注金额 {minimum} 元。", meta
    meta["allowed_stake"] = number_text(allowed_int)
    meta["limited"] = allowed_int < requested
    return allowed_int, "", meta
