from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from .strategy import encode_handicap_line, encode_total_line


def _find_text_ci(node: ET.Element, tag: str) -> str:
    """
    ElementTree 的 tag 大小写敏感；站点返回字段在不同接口/时期可能大小写不统一。
    这里做一次“尽力而为”的大小写兜底：
    - 原样
    - 全大写
    - 全小写
    """
    k = str(tag or "").strip()
    if not k:
        return ""
    for kk in (k, k.upper(), k.lower()):
        try:
            v = node.findtext(kk)  # type: ignore[arg-type]
        except Exception:
            v = None
        if v is None:
            continue
        s = str(v).strip()
        if s and s != "0":
            return s
    return ""


def _find_text_ci_allow_zero(node: ET.Element, tag: str) -> str:
    """Case-insensitive tag lookup for values where literal zero is valid."""

    k = str(tag or "").strip()
    if not k:
        return ""
    for kk in (k, k.upper(), k.lower()):
        try:
            value = node.findtext(kk)  # type: ignore[arg-type]
        except Exception:
            value = None
        if value is None:
            continue
        text = str(value).strip()
        if text != "":
            return text
    return ""


def _parse_split_line_avg(text: Any) -> float:
    """
    盘口线可能形如：
    - "2.5"
    - "2.5 / 3"
    - "0 / 0.5"
    - "+0.5" / "-0.5"
    返回分盘线的平均值（去掉符号）。
    """
    s = str(text or "").strip()
    if not s:
        return 0.0
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"^[+-]", "", s)
    parts = [p for p in s.split("/") if p]
    if not parts:
        return 0.0
    nums: List[float] = []
    for p in parts:
        try:
            nums.append(float(p))
        except Exception:
            return 0.0
    return float(sum(nums) / float(len(nums)))


def parse_game_more_games_and_ft_gids(xml_text: str) -> Dict[str, Any]:
    """
    解析 get_game_more 的 XML：
    - games: <game> 节点列表（保留原顺序）
    - ft_gids: <FT_gid> 列表（gid + master 标记，保留原顺序）
    """
    t = str(xml_text or "").strip()
    if not t.startswith("<"):
        raise ValueError("non_xml")
    root = ET.fromstring(t)
    games = list(root.iter("game"))

    ft_gids: List[Dict[str, str]] = []
    ft = root.find("FT_gid") or root.find("FT_GID")
    if ft is not None:
        for it in list(ft):
            if str(it.tag or "").strip().lower() != "gid":
                continue
            gid = str(it.get("id") or it.get("ID") or "").strip()
            master = str(it.get("master") or it.get("MASTER") or "").strip()
            if gid:
                ft_gids.append({"gid": gid, "master": master})

    return {"games": games, "ft_gids": ft_gids}


def _reorder_games_by_ft_gid_priority(games: List[ET.Element], ft_gids: List[Dict[str, str]]) -> List[ET.Element]:
    gid_priority = [str((x or {}).get("gid") or "").strip() for x in (ft_gids or []) if isinstance(x, dict)]
    gid_priority = [g for g in gid_priority if g]

    gid_to_game: Dict[str, ET.Element] = {}
    ordered: List[ET.Element] = []
    for g in games or []:
        gid = str((_find_text_ci(g, "gid") or _find_text_ci(g, "GID") or "")).strip()
        if gid and gid not in gid_to_game:
            gid_to_game[gid] = g

    used = set()
    for gid in gid_priority:
        gg = gid_to_game.get(gid)
        if gg is None:
            continue
        ordered.append(gg)
        used.add(id(gg))

    for g in games or []:
        if id(g) in used:
            continue
        ordered.append(g)
    return ordered


def _handicap_ratio_tags(wtype_up: str) -> List[str]:
    wt = str(wtype_up or "").strip().upper()
    if not wt:
        return []
    if wt == "R":
        # 站点在 right_score/get_game_more 下有时用 ratio 或 ratio_r
        return ["ratio", "ratio_r", "ratio_R"]
    return [f"ratio_{wt.lower()}"]


def match_handicap_from_game_more_parsed(
    *,
    games: List[ET.Element],
    ft_gids: List[Dict[str, str]],
    candidates: List[str],
    want_handicap_line: float,
    side: str,
) -> Optional[Dict[str, str]]:
    side_u = str(side or "").strip().upper()
    if side_u not in {"H", "C"}:
        return None
    want = float(want_handicap_line or 0.0)

    ordered = _reorder_games_by_ft_gid_priority(games, ft_gids)

    for gm_game in ordered:
        gid = str((_find_text_ci(gm_game, "gid") or _find_text_ci(gm_game, "GID") or "")).strip()
        if not gid:
            continue

        for wt in candidates or []:
            wt_up = str(wt or "").strip().upper()
            if not wt_up:
                continue

            ratio_txt = ""
            used_ratio_tag = ""
            for rt in _handicap_ratio_tags(wt_up):
                # Handicap 0 is a real line (draw-no-bet). Generic field
                # lookup rejects literal "0" because that is correct for
                # odds, but applying that rule to ratio_* loses zero lines,
                # especially when they move from the master to master=N.
                txt = _find_text_ci_allow_zero(gm_game, rt)
                if txt != "":
                    ratio_txt = txt
                    used_ratio_tag = rt
                    break
            if ratio_txt == "":
                continue

            ratio_abs = abs(_parse_split_line_avg(ratio_txt))

            base = wt_up
            if len(wt_up) > 1 and wt_up[0] in {"A", "B", "C", "D", "E", "F"}:
                base = wt_up[1:]
            strong_tag = "hstrong" if base in {"HR", "HRE"} else "strong"
            strong = _find_text_ci(gm_game, strong_tag).upper()
            if strong not in {"H", "C"}:
                continue

            cur_line = (-ratio_abs) if side_u == strong else (ratio_abs)
            if abs(float(cur_line) - float(want)) >= 1e-6:
                continue

            ior_tag = f"ior_{wt_up}{side_u}"
            ior = _find_text_ci(gm_game, ior_tag)
            if not ior or ior == "0":
                continue

            con, ratio = encode_handicap_line(float(cur_line))
            return {
                "gid": gid,
                "wtype": wt_up,
                "ioratio": str(ior),
                "con": str(con),
                "ratio": str(ratio),
                "matched_ratio_tag": used_ratio_tag,
                "strong": strong,
            }
    return None


def _total_ratio_tags(wtype_up: str) -> List[str]:
    wt_up = str(wtype_up or "").strip().upper()
    if not wt_up:
        return []
    wt_low = wt_up.lower()
    ratio_tags: List[str] = [
        f"ratio_{wt_low}o",
        f"ratio_{wt_low}u",
        f"ratio_{wt_low}ho",
        f"ratio_{wt_low}hu",
        f"ratio_{wt_low}co",
        f"ratio_{wt_low}cu",
    ]
    if wt_up == "OU":
        ratio_tags = ["ratio_o", "ratio_u"] + ratio_tags
    elif wt_up == "HOU":
        ratio_tags = ["ratio_ho", "ratio_hu"] + ratio_tags
    return ratio_tags


def _find_total_ior_for_ratio_tag(
    gm_game: ET.Element,
    *,
    wtype_up: str,
    ratio_tag: str,
    is_over: bool,
) -> Tuple[str, str]:
    wt_up = str(wtype_up or "").strip().upper()
    if not wt_up:
        return "", ""
    rt = str(ratio_tag or "").strip().lower()

    wt_low = wt_up.lower()

    def find_tag(tag0: str) -> str:
        tag = str(tag0 or "").strip()
        if not tag:
            return ""
        # 生成常见大小写变体（跟随 engine 里的容错思路）
        variants: List[str] = []
        variants.append(tag)
        variants.append(tag.upper())
        variants.append(tag.lower())
        if tag.startswith("ior_"):
            variants.append("ior_" + tag[4:].upper())
            variants.append("IOR_" + tag[4:].upper())
        seen = set()
        for vv in variants:
            if not vv or vv in seen:
                continue
            seen.add(vv)
            val = _find_text_ci(gm_game, vv)
            if val:
                return val
        return ""

    prefer: List[str] = []
    if rt.endswith(("ho", "hu")):
        prefer.append(f"ior_{wt_low}{'ho' if is_over else 'hu'}")
    elif rt.endswith(("co", "cu")):
        prefer.append(f"ior_{wt_low}{'co' if is_over else 'cu'}")
    else:
        prefer.append(f"ior_{wt_low}{'c' if is_over else 'h'}")
        prefer.append(f"ior_{wt_low}{'o' if is_over else 'u'}")

    for t in prefer:
        v = find_tag(t)
        if v:
            return v, t

    # fallback：把常见字段都试一遍
    for t in [
        f"ior_{wt_low}{'c' if is_over else 'h'}",
        f"ior_{wt_low}{'o' if is_over else 'u'}",
        f"ior_{wt_low}h",
        f"ior_{wt_low}c",
        f"ior_{wt_low}o",
        f"ior_{wt_low}u",
        f"ior_{wt_low}ho",
        f"ior_{wt_low}hu",
        f"ior_{wt_low}co",
        f"ior_{wt_low}cu",
    ]:
        v = find_tag(t)
        if v:
            return v, t
    return "", ""


def match_total_from_game_more_parsed(
    *,
    games: List[ET.Element],
    ft_gids: List[Dict[str, str]],
    candidates: List[str],
    want_total_abs: float,
    is_over: bool,
) -> Optional[Dict[str, str]]:
    want = float(want_total_abs or 0.0)
    if want <= 0:
        return None

    ordered = _reorder_games_by_ft_gid_priority(games, ft_gids)
    for gm_game in ordered:
        gid = str((_find_text_ci(gm_game, "gid") or _find_text_ci(gm_game, "GID") or "")).strip()
        if not gid:
            continue

        for wt in candidates or []:
            wt_up = str(wt or "").strip().upper()
            if not wt_up:
                continue

            ratio_txt = ""
            used_ratio_tag = ""
            for rt in _total_ratio_tags(wt_up):
                txt = _find_text_ci(gm_game, rt)
                if txt:
                    ratio_txt = txt
                    used_ratio_tag = rt
                    break
            if not ratio_txt:
                continue

            cur_total = _parse_split_line_avg(ratio_txt)
            if cur_total <= 0:
                continue
            if abs(float(cur_total) - float(want)) >= 1e-6:
                continue

            ior, used_ior_tag = _find_total_ior_for_ratio_tag(
                gm_game,
                wtype_up=wt_up,
                ratio_tag=used_ratio_tag,
                is_over=bool(is_over),
            )
            if not ior:
                continue

            con, ratio = encode_total_line(float(cur_total), is_over=bool(is_over))
            return {
                "gid": gid,
                "wtype": wt_up,
                "ioratio": str(ior),
                "con": str(con),
                "ratio": str(ratio),
                "matched_ratio_tag": used_ratio_tag,
                "matched_ior_tag": used_ior_tag,
            }
    return None
