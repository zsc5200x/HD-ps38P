from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple


def encode_handicap_line(line_val: float) -> Tuple[str, str]:
    """
    将让球盘口线（以 0.25 为步进）编码为 con/ratio：
      line ≈ -con + ratio/200
    并尽量产出该站点常见编码：ratio ∈ {0,±50,±100,±150}
    """
    try:
        x = float(line_val)
    except Exception:
        return "0", "0"
    if abs(x) < 1e-9:
        return "0", "0"

    sign = 1 if x >= 0 else -1
    ax = abs(x)
    base = int(math.floor(ax + 1e-9))
    frac = max(0.0, min(0.99, ax - base))
    q = int(round(frac * 4))
    frac_q = max(0.0, min(0.75, q / 4.0))

    if frac_q == 0.0:
        con = sign * base
        ratio = 0
    elif frac_q == 0.25:
        con = sign * base
        ratio = sign * 50
    elif frac_q == 0.5:
        con = sign * (base + 1)
        ratio = -sign * 100
    else:  # 0.75
        con = sign * (base + 1)
        ratio = -sign * 50

    # 关键：该站点的让球盘线约为 (-con + ratio/200)，因此 con 需要与“直觉编码”取反。
    return str(int(-con)), str(int(ratio))


def decode_handicap_line(con: Any, ratio: Any) -> Optional[float]:
    """
    让球盘线解码（与 encode_handicap_line 对应）：
      line ≈ -con + ratio/200
    """
    try:
        c = float(str(con).strip())
        r = float(str(ratio).strip())
        return (-c) + (r / 200.0)
    except Exception:
        return None


def encode_total_line(line_val: float, *, is_over: bool) -> Tuple[str, str]:
    """
    大小盘盘口线编码为 con/ratio（以抓包规则为准）：
      盘口线 ≈ con - ratio/200
    其中“大小方向”通过 con/ratio 的符号区分：
      - 大：正号
      - 小：负号
    """
    try:
        x = float(line_val)
    except Exception:
        return "0", "0"
    if x <= 0:
        return "0", "0"

    base = int(math.floor(x + 1e-9))
    frac = max(0.0, min(0.99, x - base))
    q = int(round(frac * 4))
    frac_q = max(0.0, min(0.75, q / 4.0))

    if frac_q == 0.0:
        con_mag = int(round(x))
        ratio_code = 0
    elif frac_q == 0.25:
        con_mag = int(math.floor(x))
        ratio_code = -50
    elif frac_q == 0.5:
        con_mag = int(math.ceil(x))
        ratio_code = 100
    else:  # 0.75
        con_mag = int(math.ceil(x))
        ratio_code = 50

    sign = 1 if bool(is_over) else -1
    return str(int(sign * con_mag)), str(int(sign * ratio_code))


def decode_total_line(con: Any, ratio: Any) -> Optional[float]:
    """
    大小盘盘口线解码（与 encode_total_line 对应）：
      line ≈ con - ratio/200
    """
    try:
        c = float(str(con).strip())
        r = float(str(ratio).strip())
        return c - (r / 200.0)
    except Exception:
        return None


def format_handicap_line_value(line_val: float) -> str:
    """
    把盘口线格式化成常见展示：
    - 0.5 => +0.5
    - 0.25 => +0 / 0.5
    - 0.75 => +0.5 / 1
    """
    try:
        x = float(line_val)
    except Exception:
        return str(line_val)
    if abs(x) < 1e-9:
        return "0"
    sign = "+" if x > 0 else "-"
    ax = abs(x)
    base = int(math.floor(ax + 1e-9))
    frac = ax - base
    q = int(round(frac * 4))
    frac_q = max(0.0, min(0.75, q / 4.0))

    def fmt_num(v: float) -> str:
        if abs(v - round(v)) < 1e-9:
            return str(int(round(v)))
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return s or str(v)

    if frac_q == 0.0:
        return f"{sign}{fmt_num(float(base))}"
    if frac_q == 0.5:
        return f"{sign}{fmt_num(base + 0.5)}"
    if frac_q == 0.25:
        return f"{sign}{fmt_num(float(base))} / {fmt_num(base + 0.5)}"
    # 0.75
    return f"{sign}{fmt_num(base + 0.5)} / {fmt_num(base + 1.0)}"

def format_total_line_value(line_val: float) -> str:
    """
    把大小盘盘口线格式化成常见展示：
    - 2 => 2
    - 2.25 => 2 / 2.5
    - 2.5 => 2.5
    - 2.75 => 2.5 / 3
    """
    try:
        x = abs(float(line_val))
    except Exception:
        return str(line_val)
    if x <= 0:
        return "0"
    base = int(math.floor(x + 1e-9))
    frac = x - base
    q = int(round(frac * 4))
    frac_q = max(0.0, min(0.75, q / 4.0))

    def fmt_num(v: float) -> str:
        if abs(v - round(v)) < 1e-9:
            return str(int(round(v)))
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return s or str(v)

    if frac_q == 0.0:
        return fmt_num(float(base))
    if frac_q == 0.5:
        return fmt_num(base + 0.5)
    if frac_q == 0.25:
        return f"{fmt_num(float(base))} / {fmt_num(base + 0.5)}"
    return f"{fmt_num(base + 0.5)} / {fmt_num(base + 1.0)}"


def build_follow_line_display(order_line: str, params: Dict[str, Any]) -> str:
    """
    跟单展示用“玩法”：默认沿用后台 order.line，但让球/大小盘用实际 con/ratio 反推盘口，
    避免显示与实际下注不一致。
    """
    order_line = str(order_line or "").strip() or "未知"
    wtype = str(params.get("wtype") or "").strip().upper()
    con = params.get("con")
    ratio = params.get("ratio")
    if wtype.endswith("OU") and con is not None and ratio is not None:
        lv = decode_total_line(con, ratio)
        if lv is None:
            return order_line
        # 尽量沿用后台文案里的“大/小”，避免方向反推误差
        prefix = "大" if lv > 0 else "小"
        s0 = order_line.lstrip()
        if s0.startswith("大"):
            prefix = "大"
        elif s0.startswith("小"):
            prefix = "小"
        return f"{prefix} {format_total_line_value(lv)}".strip()
    if wtype in {"R", "RE", "HRE"} and con is not None and ratio is not None:
        lv = decode_handicap_line(con, ratio)
        if lv is None:
            return order_line
        # 通常 order.line 形如：“球队名 +0.5”或“球队名 -0 / 0.5”，用最后一个空格拆分更稳（球队名里可能有数字/空格）。
        if " " in order_line:
            prefix = order_line.rsplit(" ", 1)[0].strip()
            return f"{prefix} {format_handicap_line_value(lv)}".strip()
        return f"{order_line} {format_handicap_line_value(lv)}".strip()
    return order_line
