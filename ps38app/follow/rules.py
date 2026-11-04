"""规则校验与唯一方向变换入口。"""

import re
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN

from .source import decimal_value, number_text

DEFAULT_RULE = {
    "id": "default",
    "version": 1,
    "enabled": False,
    "direction": "forward",
    "amount_mode": "fixed",
    "amount_value": "",
    "currency": "CNY",
    "max_age_seconds": 120,
    "min_odds": None,
    "max_odds": None,
    "scope": "live",
    "member_codes": [],
    "window_seconds": 0,
    "rounding": "down_integer",
    "split_count": 1,
    "min_stake": 100,
    "split_delay_seconds": 0,
    "match_limit_amount": 0,
    "market_limit_amount": 0,
}


def validate_rule(payload, previous=None):
    if not isinstance(payload, dict):
        raise ValueError("规则必须是对象")
    rule = {**DEFAULT_RULE, **(previous or {})}
    for key in DEFAULT_RULE:
        if key in payload and key not in {"id", "version", "window_seconds", "rounding"}:
            rule[key] = payload[key]
    if not isinstance(rule["enabled"], bool):
        raise ValueError("启用状态必须为布尔值")
    if rule["direction"] not in {"forward", "reverse"}:
        raise ValueError("方向必须是正向或反向")
    if rule["amount_mode"] not in {"fixed", "ratio"}:
        raise ValueError("金额模式必须是固定金额或比例")
    if rule["scope"] != "live":
        raise ValueError("首版仅支持滚球")
    # 老规则可能保存了固定账号；规则现在只配置行为，执行账号由号池动态分配。
    rule.pop("account_id", None)
    # 保留旧接口字段兼容，但币种已固定为人民币，不再由表单或旧配置决定。
    rule["currency"] = "CNY"
    amount = rule["amount_value"]
    if amount not in (None, ""):
        amount = decimal_value(amount, "金额或比例")
        if amount <= 0:
            raise ValueError("金额或比例必须大于零")
        if rule["amount_mode"] == "fixed" and amount != amount.to_integral_value():
            raise ValueError("固定金额必须为整币")
        rule["amount_value"] = number_text(amount)
    else:
        rule["amount_value"] = ""
    age = decimal_value(rule["max_age_seconds"], "最长源单年龄")
    if age != age.to_integral_value() or not 21 <= age <= 600:
        raise ValueError("最长源单年龄须为 21 至 600 秒整数")
    rule["max_age_seconds"] = int(age)
    for key in ("min_odds", "max_odds"):
        if rule[key] in ("", None):
            rule[key] = None
        else:
            value = decimal_value(rule[key], "香港盘水位")
            if value <= 0:
                raise ValueError("香港盘水位必须大于零")
            rule[key] = number_text(value)
    if rule["min_odds"] is not None and rule["max_odds"] is not None:
        if Decimal(rule["min_odds"]) > Decimal(rule["max_odds"]):
            raise ValueError("最低水位不能大于最高水位")
    members = rule["member_codes"]
    if isinstance(members, str):
        members = re.split(r"[,，\s]+", members)
    if not isinstance(members, list) or len(members) > 500:
        raise ValueError("会员筛选必须是最多 500 项的列表")
    rule["member_codes"] = sorted({str(value).strip() for value in members if str(value).strip()})
    if any(len(value) > 80 for value in rule["member_codes"]):
        raise ValueError("会员标识过长")
    try:
        try:
            rule["min_stake"] = max(0, int(rule.get("min_stake") or 0))
        except (TypeError, ValueError):
            raise ValueError("最低投注金额必须是整数") from None
        split_count = int(1 if rule.get("split_count") is None else rule.get("split_count"))
    except (TypeError, ValueError):
        raise ValueError("分单数量必须是整数") from None
    if not 1 <= split_count <= 50:
        raise ValueError("分单数量必须在 1 到 50 之间")
    rule["split_count"] = split_count

    raw_delay = rule.get("split_delay_seconds")
    try:
        split_delay = float(0 if raw_delay is None else raw_delay)
    except (TypeError, ValueError):
        raise ValueError("分单间隔必须是数字") from None
    if not 0 <= split_delay <= 60:
        raise ValueError("分单间隔必须在 0 到 60 秒之间")
    rule["split_delay_seconds"] = split_delay

    for key, label in (
        ("match_limit_amount", "单场限额"),
        ("market_limit_amount", "盘口限额"),
    ):
        value = decimal_value(rule.get(key) or 0, label)
        if value < 0:
            raise ValueError(f"{label}必须是非负数字")
        rule[key] = number_text(value)

    if rule["enabled"] and not rule["amount_value"]:
        raise ValueError("启用前请设置跟单金额或比例")
    rule["id"] = "default"
    rule["window_seconds"] = 0
    rule["rounding"] = "down_integer"
    if previous and any(
        rule[key] != previous.get(key) for key in DEFAULT_RULE if key not in {"version", "enabled"}
    ):
        rule["version"] = int(previous["version"]) + 1
    return rule


def target_intent(intent, rule):
    result = dict(intent)
    if rule["direction"] == "reverse":
        result["side"] = {"HOME": "AWAY", "AWAY": "HOME", "OVER": "UNDER", "UNDER": "OVER"}[
            intent["side"]
        ]
        if intent["market_type"] == "HANDICAP":
            result["line"] = number_text(-Decimal(intent["line"]))
    return result


def group_stake(total, rule):
    value = decimal_value(rule["amount_value"], "规则金额")
    if rule["amount_mode"] == "ratio":
        value = decimal_value(total, "组金额") * value / 100
    value = value.quantize(Decimal("1"), rounding=ROUND_DOWN)
    if value < 1:
        raise ValueError("组目标金额向下取整后不足 1")
    return number_text(value)


def split_stakes(total_stake, split_count) -> list:
    """把总金额按整数尽量平均拆成 N 份，总和保持不变；每份至少 1 元。"""
    try:
        total = Decimal(str(total_stake))
    except Exception:  # noqa: BLE001 - 非法金额按 0 处理
        return []
    if total <= 0:
        return []
    total_int = int(total.to_integral_value(rounding=ROUND_CEILING))
    count = max(1, min(50, int(split_count or 1)))
    count = min(count, total_int)
    if count <= 1:
        return [total_int]
    base, remainder = divmod(total_int, count)
    parts = [base + 1] * remainder + [base] * (count - remainder)
    return [part for part in parts if part > 0]
