"""Cross-book fixture matching between current PS3838 and Crown feeds.

Crown and PS3838 use separate ID spaces and different local times.  A link is
only made when league, team names and kickoff all agree; confirmed links are
stored in a small SQLite file and never written into either provider's data.

Suggestions are review aids. Confirmed league bindings and explicit source
refreshes also apply the conservative two-team automatic matching rules.
"""

from __future__ import annotations

import csv
import hashlib
import http.client
import json
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

from . import store as order_store
from .fixture_identity import fixture_identity_score, confirmed_aliases

DISPLAY_TZ = timezone(timedelta(hours=8))
DEFAULT_CROWN_OFFSET_MINUTES = -240

# Auto-confirm rules.  A high name score must never buy a wrong match a place
# in the mapping: the kickoff gate is applied first, then uniqueness.
AUTO_LEAGUE_SCORE = 85.0
AUTO_FIXTURE_SCORE = 85.0
AUTO_TIME_MINUTES = 15.0
AUTO_TEAM_FLOOR = 0.15
PAIR_TIME_TOLERANCE_MINUTES = 60.0
LIVE_MAX_HOURS = 4.0
FEED_MAX_AGE_SECONDS = 120.0

_TEAM_SUFFIXES = re.compile(
    r"\b(?:association football club|football club|soccer club|club|fc|sc|afc|cf)\b",
    re.IGNORECASE,
)
_CN_TEAM_SUFFIXES = (
    "足球俱乐部", "体育俱乐部", "足球会", "体育会", "俱乐部",
    "预备队", "后备队", "青年队", "女足", "女子",
)
_STATE_CODES = frozenset({
    "rj", "rs", "pa", "sp", "mg", "ba", "ce", "go", "pr", "sc", "pe", "es",
    "mt", "ms", "df", "am", "ro", "ac", "ap", "rr", "to", "ma", "pi", "rn",
    "pb", "al", "se",
})
_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
_MATCH_SPLIT = re.compile(r"\s*-\s*vs\s*-\s*", re.IGNORECASE)
_LEAGUE_SYNONYMS = {
    "甲组": "甲级", "乙组": "乙级", "丙组": "丙级",
    "杯赛": "杯", "足球": "", "足联": "", "职业": "",
    "解放者": "自由", "全国": "国家", "女足": "女子", "欧足联": "欧洲",
}
_EN_WORDS = {
    "brazil": "巴西", "india": "印度", "italy": "意大利", "spain": "西班牙",
    "england": "英格兰", "germany": "德国", "france": "法国", "portugal": "葡萄牙",
    "netherlands": "荷兰", "belgium": "比利时", "scotland": "苏格兰",
    "argentina": "阿根廷", "colombia": "哥伦比亚", "mexico": "墨西哥",
    "uruguay": "乌拉圭", "paraguay": "巴拉圭", "chile": "智利", "peru": "秘鲁",
    "ecuador": "厄瓜多尔", "venezuela": "委内瑞拉", "united states": "美国",
    "usa": "美国", "women": "女子", "womens": "女子", "cup": "杯",
    "league": "联赛", "super": "超级", "first": "甲级", "second": "乙级",
    "third": "丙级", "youth": "青年", "reserve": "预备", "reserves": "预备",
    "division": "", "primera": "甲级",
    "austria": "奥地利", "el salvador": "萨尔瓦多", "jordan": "约旦",
    "kosovo": "科索沃", "czech republic": "捷克", "czechia": "捷克",
    "greece": "希腊", "finland": "芬兰", "poland": "波兰", "sweden": "瑞典",
    "turkey": "土耳其", "saudi arabia": "沙特阿拉伯", "united arab emirates": "阿联酋",
    "mongolia": "蒙古", "australia": "澳大利亚", "denmark": "丹麦",
}
_GENDER_MARKERS = re.compile(r"女子|女足|women'?s?|ladies|w-league|\(女\)|（女）", re.IGNORECASE)
_YOUTH_MARKERS = re.compile(r"(?<![a-z])u\s?-?\s?(?:1[5-9]|2[0-4])(?!\d)|青年|青少年", re.IGNORECASE)
_RESERVE_MARKERS = re.compile(r"预备|后备|储备|reserve|reserves", re.IGNORECASE)
_CROWN_DATE = re.compile(
    r"(?P<month>\d{1,2})[-/](?P<day>\d{1,2})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?:\s*(?P<ampm>[ap])\.?m?\.?)?",
    re.IGNORECASE,
)
_LEAGUE_COUNTRY_SPLIT = re.compile(r"\s*[-–—]\s+")


def crown_timezone(offset_minutes: Any = DEFAULT_CROWN_OFFSET_MINUTES) -> timezone:
    try:
        minutes = int(offset_minutes)
    except (TypeError, ValueError):
        minutes = DEFAULT_CROWN_OFFSET_MINUTES
    minutes = max(-12 * 60, min(14 * 60, minutes))
    return timezone(timedelta(minutes=minutes))


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_team(value: Any) -> str:
    """Normalize a club label for comparison (case, accents, common suffixes)."""
    return _normalize_team_text(str(value or ""))


@lru_cache(maxsize=8192)
def _normalize_team_text(value: str) -> str:
    text = _strip_accents(value).casefold()
    text = _TEAM_SUFFIXES.sub(" ", text)
    for suffix in _CN_TEAM_SUFFIXES:
        text = text.replace(suffix, "")
    if text.endswith("队"):
        text = text[:-1]
    tokens = _TOKEN.findall(text)
    if len(tokens) > 1 and tokens[-1] in _STATE_CODES:
        tokens.pop()
    return "".join(tokens)


def normalize_league(value: Any) -> str:
    """Normalize a competition label, including translation synonyms."""
    return _normalize_league_text(str(value or ""))


@lru_cache(maxsize=4096)
def _normalize_league_text(value: str) -> str:
    text = _strip_accents(value).casefold()
    for english, chinese in sorted(_EN_WORDS.items(), key=lambda item: -len(item[0])):
        if chinese:
            text = re.sub(rf"\b{re.escape(english)}\b", chinese, text)
    tokens = _TOKEN.findall(text)
    if len(tokens) >= 2 and len(tokens[0]) >= 2 and tokens[1].startswith(tokens[0]):
        tokens[1] = tokens[1][len(tokens[0]):]
    collapsed: List[str] = []
    for token in tokens:
        if not collapsed or collapsed[-1] != token:
            collapsed.append(token)
    text = "".join(collapsed)
    for source, target in _LEAGUE_SYNONYMS.items():
        text = text.replace(source, target)
    return text


def competition_profile(value: Any) -> Dict[str, str]:
    """Classify a league by gender, age group and squad level."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return {
        "gender": "women" if _GENDER_MARKERS.search(text) else "men",
        "age": "youth" if _YOUTH_MARKERS.search(text) else "senior",
        "squad": "reserve" if _RESERVE_MARKERS.search(text) else "senior",
    }


def competition_conflict(left: Any, right: Any) -> bool:
    return competition_profile(left) != competition_profile(right)


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = SequenceMatcher(None, left, right, autojunk=False).ratio()
    if left in right or right in left:
        ratio = max(ratio, min(len(left), len(right)) / max(len(left), len(right)) * 0.92)
    return ratio


def _team_similarity(left: Any, right: Any, aliases: Optional[set] = None) -> float:
    if aliases:
        pair = (normalize_team(left), normalize_team(right))
        if pair in aliases or pair[::-1] in aliases:
            return 1.0
    return _ratio(normalize_team(left), normalize_team(right))


def _league_similarity(left: Any, right: Any, aliases: Optional[set] = None) -> float:
    if aliases:
        pair = (normalize_league(left), normalize_league(right))
        if pair in aliases:
            return 1.0
    return _ratio(normalize_league(left), normalize_league(right))


def build_aliases(league_pairs: Iterable[Tuple[Any, Any]] = (),
                  team_pairs: Iterable[Tuple[Any, Any]] = ()) -> Dict[str, set]:
    """Exact normalized aliases learned from already-confirmed mappings."""
    leagues = set()
    teams = set()
    for left, right in league_pairs:
        a, b = normalize_league(left), normalize_league(right)
        if a and b:
            leagues.add((a, b))
    for left, right in team_pairs:
        a, b = normalize_team(left), normalize_team(right)
        if a and b:
            teams.add((a, b))
    return {"leagues": leagues, "teams": teams}


def _translate_country(value: str) -> str:
    text = str(value or "").strip()
    for english, chinese in sorted(_EN_WORDS.items(), key=lambda item: -len(item[0])):
        if chinese:
            text = re.sub(rf"\b{re.escape(english)}\b", chinese, text, flags=re.IGNORECASE)
    return text


def ps38_league_country(name: Any) -> str:
    parts = _LEAGUE_COUNTRY_SPLIT.split(str(name or "").strip(), maxsplit=1)
    if len(parts) != 2:
        return ""
    return _translate_country(parts[0].strip())


def crown_league_country(name: Any, countries: Iterable[str]) -> str:
    text = str(name or "").strip()
    for country in sorted({item for item in countries if item}, key=len, reverse=True):
        if text.startswith(country):
            return country
    return ""


def parse_crown_start(value: Any, offset_minutes: Any = DEFAULT_CROWN_OFFSET_MINUTES,
                      reference: Optional[datetime] = None) -> Optional[datetime]:
    """Parse Crown's ``11-02 04:00p`` label (Crown local time) into UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    zone = crown_timezone(offset_minutes)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.astimezone(timezone.utc)

    match = _CROWN_DATE.search(text)
    if not match:
        return None
    month, day = int(match.group("month")), int(match.group("day"))
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    ampm = (match.group("ampm") or "").lower()
    if ampm:
        if hour == 0:
            hour = 12
        if not 1 <= hour <= 12:
            return None
        if ampm == "a" and hour == 12:
            hour = 0
        elif ampm == "p" and hour != 12:
            hour += 12
    elif not 0 <= hour <= 23:
        return None

    ref = reference or datetime.now(timezone.utc)
    ref_local = ref.astimezone(zone)
    options: List[datetime] = []
    for year in (ref_local.year - 1, ref_local.year, ref_local.year + 1):
        try:
            options.append(datetime(year, month, day, hour, minute, tzinfo=zone))
        except ValueError:
            continue
    if not options:
        return None
    selected = min(options, key=lambda item: abs((item - ref_local).total_seconds()))
    return selected.astimezone(timezone.utc)


def _iso_utc(value: Optional[datetime]) -> str:
    return value.astimezone(timezone.utc).isoformat() if value else ""


def _display_time(value: Optional[datetime]) -> str:
    return value.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M") if value else ""


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=DISPLAY_TZ)
        return stamp.astimezone(timezone.utc)
    except ValueError:
        return None


def _split_teams(value: Any) -> Tuple[str, str]:
    parts = _MATCH_SPLIT.split(str(value or "").strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return str(value or "").strip(), ""


def ps38_event_id(event_ms: int, teams: str) -> str:
    digest = hashlib.sha1(str(teams or "").encode("utf-8")).hexdigest()[:10]
    return f"p38_{int(event_ms)}_{digest}"


def ps38_league_id(name: Any) -> str:
    """Stable PS38 league key: punctuation/spacing variants collapse together."""
    normalized = normalize_league(name)
    return "p38:" + (normalized or str(name or "").strip().casefold())


def prepare_ps38_event(row: Dict[str, Any]) -> Dict[str, Any]:
    teams = str(row.get("teams") or "")
    home, away = _split_teams(teams)
    event_ms = int(row.get("event_ms") or 0)
    kickoff = (
        datetime.fromtimestamp(event_ms / 1000.0, timezone.utc) if event_ms > 0 else None
    )
    league = str(row.get("league") or "")
    return {
        "id": ps38_event_id(event_ms, teams),
        "source": "ps38",
        "name": f"{home} vs {away}".strip(),
        "home": home,
        "away": away,
        "league": league,
        "leagueId": ps38_league_id(league),
        "teamsRaw": teams,
        "kickoffUtc": _iso_utc(kickoff),
        "kickoffLocal": _display_time(kickoff),
        "isLive": bool(row.get("live")),
        "orders": int(row.get("orders") or 0),
        "members": int(row.get("members") or 0),
        "stake": float(row.get("stake") or 0),
        "lastOrderAt": str(row.get("last_order_at") or ""),
    }


def prepare_crown_event(raw: Dict[str, Any], offset_minutes: Any = DEFAULT_CROWN_OFFSET_MINUTES,
                        reference: Optional[datetime] = None) -> Dict[str, Any]:
    start_label = str(raw.get("start_label", raw.get("startLabel")) or "")
    kickoff = parse_crown_start(start_label, offset_minutes, reference)
    return {
        "id": str(raw.get("id") or ""),
        "source": "crown",
        "name": f"{str(raw.get('home') or '').strip()} vs {str(raw.get('away') or '').strip()}",
        "home": str(raw.get("home") or "").strip(),
        "away": str(raw.get("away") or "").strip(),
        "league": str(raw.get("league") or "").strip(),
        "leagueId": str(raw.get("league_id", raw.get("leagueId")) or "").strip(),
        "providerLeagueId": str(raw.get("league_id", raw.get("leagueId")) or "").strip(),
        "startLabel": start_label,
        "kickoffUtc": _iso_utc(kickoff),
        "kickoffLocal": _display_time(kickoff),
        "isLive": bool(raw.get("is_live", raw.get("isLive"))),
        "isFinished": bool(raw.get("is_finished", raw.get("isFinished"))),
        "score": str(raw.get("score") or ""),
        "minute": str(raw.get("minute") or ""),
        "homeEn": str(raw.get("homeEn") or ""),
        "awayEn": str(raw.get("awayEn") or ""),
        "matchStatus": str(raw.get("matchStatus", raw.get("status")) or ""),
    }


def event_kickoff(event: Dict[str, Any]) -> Optional[datetime]:
    return _parse_iso(event.get("kickoffUtc"))


def current_fixture(event: Dict[str, Any], now: datetime, horizon_hours: float = 72) -> bool:
    """A historical inplay order flag is never current live evidence."""
    if event.get("isFinished") or str(event.get("matchStatus") or "").upper() in {
        "FINISHED", "ENDED", "FT", "FULL_TIME", "CLOSED", "CANCELLED", "CANCELED", "SETTLED",
    }:
        return False
    if any(word in str(event.get("league") or "") for word in ("奇幻赛事", "特别投注")):
        return False
    kickoff = event_kickoff(event)
    live = bool(event.get("isLive")) and event.get("source") != "ps38"
    if kickoff is None:
        return live
    hours = (kickoff - now).total_seconds() / 3600
    return (-LIVE_MAX_HOURS <= hours <= horizon_hours) if live else (0 <= hours <= horizon_hours)


def align_league_ids(events, mappings, side):
    """Resolve name-only feed IDs against the saved league pair, unambiguously.

    Explicit numeric provider IDs win over translated league labels.
    """
    for event in events:
        raw_id = str(event.get("providerLeagueId") or event.get("leagueId") or "")
        matches = []
        for mapping in mappings:
            snapshot = mapping.get(side + "Snapshot") or {}
            canonical = str(mapping[side + "Id"])
            provider = str(snapshot.get("providerId") or canonical)
            if raw_id.isdigit() and provider.isdigit():
                matches_pair = raw_id == provider
            else:
                matches_pair = event.get("leagueId") == canonical or (
                    normalize_league(event.get("league")) == normalize_league(snapshot.get("name"))
                    and not competition_conflict(event.get("league"), snapshot.get("name")))
            if matches_pair:
                matches.append(canonical)
        if len(matches) == 1:
            event["leagueId"] = matches[0]
    return events


def summarize_leagues(events: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for event in events:
        league_id = str(event.get("leagueId") or "")
        if not league_id:
            continue
        name = str(event.get("league") or "未命名联赛")
        item = groups.setdefault(
            league_id, {"id": league_id, "name": name, "eventCount": 0}
        )
        if item["name"] in ("", "未命名联赛") and name:
            item["name"] = name
        item["eventCount"] += 1
        if event.get("providerLeagueId"):
            item["providerId"] = event["providerLeagueId"]
    return sorted(groups.values(), key=lambda item: (normalize_league(item["name"]), item["id"]))


def _fixture_candidate(ps_event: Dict[str, Any], crown_event: Dict[str, Any],
                       aliases: Optional[Dict[str, set]] = None) -> Optional[Dict[str, Any]]:
    p_home, p_away = ps_event.get("home", ""), ps_event.get("away", "")
    c_home, c_away = crown_event.get("home", ""), crown_event.get("away", "")
    if not all((p_home, p_away, c_home, c_away)):
        return None
    if competition_conflict(ps_event.get("league"), crown_event.get("league")):
        return None

    team_aliases = (aliases or {}).get("teams")
    league_aliases = (aliases or {}).get("leagues")
    direct_home = _team_similarity(p_home, c_home, team_aliases)
    direct_away = _team_similarity(p_away, c_away, team_aliases)
    reverse_home = _team_similarity(p_home, c_away, team_aliases)
    reverse_away = _team_similarity(p_away, c_home, team_aliases)
    direct = (direct_home + direct_away) / 2.0
    reverse = (reverse_home + reverse_away) / 2.0
    reversed_order = reverse > direct
    team_score = max(direct, reverse)
    alias_hit = False
    if team_aliases:
        for left, right in ((p_home, c_home), (p_away, c_away),
                            (p_home, c_away), (p_away, c_home)):
            pair = (normalize_team(left), normalize_team(right))
            if pair in team_aliases or pair[::-1] in team_aliases:
                alias_hit = True
                break

    p_kickoff = event_kickoff(ps_event)
    c_kickoff = event_kickoff(crown_event)
    if p_kickoff and c_kickoff:
        delta = round(abs((c_kickoff - p_kickoff).total_seconds()) / 60.0, 1)
        temporal = 1.0 if delta <= 10 else 0.75 if delta <= 60 else 0.40 if delta <= 180 else 0.15 if delta <= 720 else 0.0
        day_delta = abs((c_kickoff.astimezone(DISPLAY_TZ).date()
                         - p_kickoff.astimezone(DISPLAY_TZ).date()).days)
        date_score = 1.0 if day_delta == 0 else 0.55 if day_delta == 1 else 0.0
    else:
        delta = None
        temporal = 0.0
        day_delta = None
        date_score = 0.0

    league_score = _league_similarity(ps_event.get("league"), crown_event.get("league"), league_aliases)
    same_live = bool(ps_event.get("isLive")) == bool(crown_event.get("isLive"))
    score = (
        team_score * 0.76
        + league_score * 0.10
        + temporal * 0.10
        + (0.04 if same_live else 0.0)
    )
    if team_score < 0.34 and not (league_score >= 0.70 and date_score >= 0.55):
        return None
    if reversed_order and delta is None:
        # Without a kickoff to disambiguate, a reversed pairing is too weak.
        return None

    reasons: List[str] = []
    if alias_hit:
        reasons.append("命中已确认的队名别名")
    if reversed_order:
        reasons.append("主客顺序相反")
    elif min(direct_home, direct_away) >= 0.82:
        reasons.append("主客队名称高度相似")
    elif team_score >= 0.5:
        reasons.append("球队名称相似")
    if league_score >= 0.65:
        reasons.append("联赛名称相似")
    if day_delta == 0:
        reasons.append("开赛日期相同")
    elif day_delta:
        reasons.append("开赛日期相差一天")
    if delta is not None:
        reasons.append("开赛时间吻合" if delta <= AUTO_TIME_MINUTES
                       else f"开赛时间相差 {delta:g} 分钟")
    if same_live:
        reasons.append("比赛状态一致")

    time_conflict = delta is not None and delta > AUTO_TIME_MINUTES
    if time_conflict:
        reasons.append("时间不符，不能自动确认")
    confidence = (
        "high"
        if not time_conflict and (alias_hit or (team_score >= 0.86 and min(direct_home, direct_away) >= 0.82))
        else "medium" if team_score >= 0.60 else "low"
    )
    return {
        "ps38Id": ps_event["id"],
        "crownId": crown_event["id"],
        "score": round(score * 100, 1),
        "confidence": confidence,
        "reasons": reasons,
        "reversedOrder": reversed_order,
        "kickoffDeltaMinutes": delta,
        "timeConflict": time_conflict,
        "teamScore": round(team_score, 3),
        "leagueScore": round(league_score, 3),
        "aliasHit": alias_hit,
    }


def rank_fixture_candidates(ps_events: Iterable[Dict[str, Any]],
                            crown_events: Iterable[Dict[str, Any]],
                            aliases: Optional[Dict[str, set]] = None,
                            league_mapping: Optional[Dict[str, str]] = None,
                            limit: int = 3) -> List[Dict[str, Any]]:
    """Return advisory PS38 candidates for each Crown fixture.

    ``league_mapping`` maps a Crown league ID to a PS38 league ID; only Crown
    events inside an already-bound league are considered. Apply the candidate
    limit per Crown fixture so a busy Pinnacle league cannot hide Crown games.
    """
    ps_events = [event for event in ps_events if event.get("id")]
    max_candidates = max(1, min(10, int(limit)))
    rows: List[Dict[str, Any]] = []
    for crown_event in crown_events:
        if not crown_event.get("id"):
            continue
        crown_league = str(crown_event.get("leagueId") or "")
        ranked: List[Dict[str, Any]] = []
        for ps_event in ps_events:
            ps_league = str(ps_event.get("leagueId") or "")
            if league_mapping is not None and league_mapping.get(crown_league) != ps_league:
                continue
            candidate = _fixture_candidate(ps_event, crown_event, aliases)
            if candidate is not None:
                ranked.append(candidate)
        ranked.sort(key=lambda item: (-item["score"], item["ps38Id"]))
        rows.extend(ranked[:max_candidates])
    return rows


def _event_count_score(crown_count: int, ps_count: int) -> float:
    if crown_count <= 0 or ps_count <= 0:
        return 0.0
    difference = abs(int(crown_count) - int(ps_count))
    if difference == 0:
        return 1.0
    if difference == 1:
        return 0.6
    if difference == 2:
        return 0.3
    return 0.0


def rank_league_candidates(ps_events: List[Dict[str, Any]],
                           crown_events: List[Dict[str, Any]],
                           aliases: Optional[Dict[str, set]] = None,
                           limit: int = 3) -> List[Dict[str, Any]]:
    """Rank Crown leagues for every PS38 league, with fixture evidence."""
    ps_leagues = summarize_leagues(ps_events)
    crown_leagues = summarize_leagues(crown_events)
    league_aliases = (aliases or {}).get("leagues")
    max_candidates = max(1, min(10, int(limit)))

    raw_countries = {league["id"]: ps38_league_country(league["name"]) for league in ps_leagues}
    known_countries = [country for country in raw_countries.values() if country]
    ps_countries = {
        league["id"]: raw_countries[league["id"]]
        or crown_league_country(league["name"], known_countries)
        for league in ps_leagues
    }
    crown_countries = {
        league["id"]: crown_league_country(league["name"], ps_countries.values())
        for league in crown_leagues
    }

    evidence: Dict[Tuple[str, str], List[float]] = {}
    ps_country_by_id = {event.get("id"): ps_countries.get(event.get("leagueId"), "") for event in ps_events}
    crown_country_by_id = {event.get("id"): crown_countries.get(event.get("leagueId"), "") for event in crown_events}
    for ps_event in ps_events:
        ps_league_id = str(ps_event.get("leagueId") or "")
        if not ps_league_id:
            continue
        ps_country = ps_country_by_id.get(ps_event.get("id"), "")
        for crown_event in crown_events:
            crown_league_id = str(crown_event.get("leagueId") or "")
            if not crown_league_id:
                continue
            crown_country = crown_country_by_id.get(crown_event.get("id"), "")
            if ps_country and crown_country and ps_country != crown_country:
                continue
            candidate = _fixture_candidate(ps_event, crown_event, aliases)
            if candidate and candidate["score"] >= 65:
                evidence.setdefault((crown_league_id, ps_league_id), []).append(candidate["score"])

    rows: List[Dict[str, Any]] = []
    for ps_league in ps_leagues:
        ps_league_id = str(ps_league["id"])
        ranked: List[Dict[str, Any]] = []
        for crown_league in crown_leagues:
            crown_league_id = str(crown_league["id"])
            if not crown_league_id:
                continue
            if competition_conflict(ps_league["name"], crown_league["name"]):
                continue
            ps_country = ps_countries.get(ps_league_id, "")
            crown_country = crown_countries.get(crown_league_id, "")
            if ps_country and crown_country and ps_country != crown_country:
                continue
            same_country = bool(ps_country and crown_country and ps_country == crown_country)
            name_score = _league_similarity(ps_league["name"], crown_league["name"], league_aliases)
            scores = sorted(evidence.get((crown_league_id, ps_league_id), []), reverse=True)
            event_score = sum(scores[:3]) / min(len(scores), 3) if scores else 0.0
            base = name_score * 100.0 if not scores else name_score * 45.0 + event_score * 0.55
            score = base
            reasons: List[str] = []
            if name_score >= 0.65:
                reasons.append("联赛名称相似")
            elif name_score >= 0.35:
                reasons.append("联赛名称部分相似")
            if league_aliases:
                pair = (normalize_league(ps_league["name"]), normalize_league(crown_league["name"]))
                if pair in league_aliases:
                    reasons.append("命中已确认的联赛别名")
            if same_country:
                score += 8.0
                reasons.append(f"同一国家（{crown_country}）")
            count_score = _event_count_score(int(crown_league.get("eventCount") or 0),
                                             int(ps_league.get("eventCount") or 0))
            if count_score:
                score += 14.0 * count_score
                if count_score >= 0.999:
                    reasons.append("当前赛事数量一致")
                else:
                    reasons.append("当前赛事数量接近")
            if scores:
                reasons.append(f"组内 {len(scores)} 场赛事候选吻合")
            ranked.append({
                "ps38LeagueId": ps_league_id,
                "crownLeagueId": crown_league_id,
                "ps38LeagueName": ps_league["name"],
                "crownLeagueName": crown_league["name"],
                "score": round(min(100.0, max(0.0, score)), 1),
                "baseScore": round(min(100.0, max(0.0, base)), 1),
                "evidenceCount": len(scores),
                "reasons": reasons or ["可人工核对联赛和组内赛事"],
            })
        ranked.sort(key=lambda item: (-item["score"], item["crownLeagueId"]))
        rows.extend(ranked[:max_candidates])
    return rows


class MappingConflict(ValueError):
    """Raised when a provider fixture/league is already linked elsewhere."""


class CrownUnavailable(RuntimeError):
    """Raised when the local Crown feed cannot serve a merge action."""


class FixtureMergeStore:
    """Small SQLite store for confirmed Crown ↔ PS38 links."""

    def __init__(self, path: Path, seed_path: Optional[Path] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seed_path = Path(seed_path) if seed_path else None
        self._initialize()
        if self.seed_path and self.seed_path.is_file():
            self.import_seed(self.seed_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS league_mappings (
                    ps38_league_id TEXT PRIMARY KEY,
                    crown_league_id TEXT NOT NULL UNIQUE,
                    ps38_snapshot TEXT NOT NULL,
                    crown_snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fixture_mappings (
                    ps38_event_id TEXT PRIMARY KEY,
                    crown_event_id TEXT NOT NULL UNIQUE,
                    ps38_snapshot TEXT NOT NULL,
                    crown_snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS merge_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def _seed_imported(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM merge_meta WHERE key='seed_imported'"
            ).fetchone()
        return bool(row and row["value"] == "1")

    def import_seed(self, seed_path: Path) -> int:
        """Load the known Crown ↔ PS38 league pairs once.

        The import is marked in ``merge_meta`` so an operator who unbinds a
        known league does not get it back on the next restart.
        """
        if self._seed_imported():
            return 0
        try:
            with Path(seed_path).open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
        except (OSError, csv.Error):
            return 0
        now = self._now()
        imported = 0
        with self._connect() as connection:
            for row in rows:
                crown_id = str(row.get("crown_id") or "").strip()
                crown_name = str(row.get("crown_name") or "").strip()
                pinnacle_id = str(row.get("pinnacle_id") or "").strip()
                pinnacle_name = str(row.get("pinnacle_name") or "").strip()
                if not crown_id or not pinnacle_name:
                    continue
                updated = str(row.get("updated_at") or "").strip() or now
                ps_id = ps38_league_id(pinnacle_name)
                existing = connection.execute(
                    "SELECT 1 FROM league_mappings WHERE ps38_league_id=? OR crown_league_id=?",
                    (ps_id, crown_id),
                ).fetchone()
                if existing is not None:
                    continue
                ps_snapshot = {
                    "id": ps_id,
                    "name": pinnacle_name,
                    "eventCount": 0,
                    "providerId": pinnacle_id,
                }
                crown_snapshot = {
                    "id": crown_id,
                    "name": crown_name or crown_id,
                    "eventCount": 0,
                    "providerId": crown_id,
                }
                connection.execute(
                    """
                    INSERT OR IGNORE INTO league_mappings
                        (ps38_league_id, crown_league_id, ps38_snapshot, crown_snapshot, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ps_id, crown_id,
                        json.dumps(ps_snapshot, ensure_ascii=False),
                        json.dumps(crown_snapshot, ensure_ascii=False),
                        updated, updated,
                    ),
                )
                imported += 1
            connection.execute(
                "INSERT OR REPLACE INTO merge_meta (key, value) VALUES ('seed_imported', '1')"
            )
        return imported

    @staticmethod
    def _now() -> str:
        return datetime.now(DISPLAY_TZ).isoformat(timespec="seconds")

    def list_league_mappings(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM league_mappings ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "ps38Id": row["ps38_league_id"],
                "crownId": row["crown_league_id"],
                "ps38Snapshot": json.loads(row["ps38_snapshot"]),
                "crownSnapshot": json.loads(row["crown_snapshot"]),
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    def list_mappings(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fixture_mappings ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "ps38Id": row["ps38_event_id"],
                "crownId": row["crown_event_id"],
                "ps38Snapshot": json.loads(row["ps38_snapshot"]),
                "crownSnapshot": json.loads(row["crown_snapshot"]),
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in rows
        ]

    def map_leagues(self, ps_league: Dict[str, Any], crown_league: Dict[str, Any]) -> Dict[str, Any]:
        ps_id = str(ps_league["id"])
        crown_id = str(crown_league["id"])
        now = self._now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM league_mappings WHERE ps38_league_id=? OR crown_league_id=?",
                (ps_id, crown_id),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["ps38_league_id"] == ps_id
                    and existing["crown_league_id"] == crown_id
                )
                if not same:
                    raise MappingConflict("该联赛已绑定到其他皇冠联赛，请先解除原绑定。")
            connection.execute(
                """
                INSERT INTO league_mappings
                    (ps38_league_id, crown_league_id, ps38_snapshot, crown_snapshot, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ps38_league_id) DO UPDATE SET
                    crown_league_id=excluded.crown_league_id,
                    ps38_snapshot=excluded.ps38_snapshot,
                    crown_snapshot=excluded.crown_snapshot,
                    updated_at=excluded.updated_at
                """,
                (
                    ps_id, crown_id,
                    json.dumps(ps_league, ensure_ascii=False),
                    json.dumps(crown_league, ensure_ascii=False),
                    now, now,
                ),
            )
        return {
            "ps38Id": ps_id,
            "crownId": crown_id,
            "ps38Snapshot": ps_league,
            "crownSnapshot": crown_league,
            "updatedAt": now,
        }

    def unmap_league(self, crown_id: str) -> None:
        """Remove the league rule while preserving confirmed fixture history."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM league_mappings WHERE crown_league_id=?", (crown_id,)
            )

    def map_events(self, ps_event: Dict[str, Any], crown_event: Dict[str, Any]) -> Dict[str, Any]:
        ps_id = str(ps_event["id"])
        crown_id = str(crown_event["id"])
        now = self._now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM fixture_mappings WHERE ps38_event_id=? OR crown_event_id=?",
                (ps_id, crown_id),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["ps38_event_id"] == ps_id
                    and existing["crown_event_id"] == crown_id
                )
                if not same:
                    raise MappingConflict("该赛事已与其他场次绑定，请先解除原绑定。")
            connection.execute(
                """
                INSERT INTO fixture_mappings
                    (ps38_event_id, crown_event_id, ps38_snapshot, crown_snapshot, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ps38_event_id) DO UPDATE SET
                    crown_event_id=excluded.crown_event_id,
                    ps38_snapshot=excluded.ps38_snapshot,
                    crown_snapshot=excluded.crown_snapshot,
                    updated_at=excluded.updated_at
                """,
                (
                    ps_id, crown_id,
                    json.dumps(ps_event, ensure_ascii=False),
                    json.dumps(crown_event, ensure_ascii=False),
                    now, now,
                ),
            )
        return {
            "ps38Id": ps_id,
            "crownId": crown_id,
            "ps38Snapshot": ps_event,
            "crownSnapshot": crown_event,
            "updatedAt": now,
        }

    def unmap(self, ps_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM fixture_mappings WHERE ps38_event_id=?", (ps_id,))

    def counts(self) -> Tuple[int, int]:
        with self._connect() as connection:
            leagues = connection.execute("SELECT COUNT(*) FROM league_mappings").fetchone()[0]
            fixtures = connection.execute("SELECT COUNT(*) FROM fixture_mappings").fetchone()[0]
        return int(leagues), int(fixtures)


def ps38_fixtures(db_path: Path, past_hours: float, horizon_hours: float) -> List[Dict[str, Any]]:
    """Distinct PS38 fixtures derived from orders inside the merge window."""
    connection = order_store.open_readonly(Path(db_path))
    if connection is None:
        return []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - int(max(0.0, past_hours) * 3600_000)
    end_ms = now_ms + int(max(1.0, horizon_hours) * 3600_000)
    try:
        rows = connection.execute(
            """
            WITH o AS (
                SELECT teams, league, stake, created_ms,
                       COALESCE(CAST(json_extract(raw_json,'$.choices[0].eventDate') AS INTEGER), created_ms) AS ev,
                       inplay
                FROM partner_orders WHERE source=? AND status='OPEN'
            )
            SELECT teams, ev, MAX(league) AS league, COUNT(*) AS orders,
                   COALESCE(SUM(stake),0) AS stake,
                   MAX(inplay) AS live,
                   MAX(created_ms) AS last_ms
            FROM o
            WHERE ev BETWEEN ? AND ?
            GROUP BY teams, ev
            ORDER BY ev ASC
            """,
            (order_store.SOURCE, start_ms, end_ms),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        connection.close()
    events: List[Dict[str, Any]] = []
    for row in rows:
        last_ms = int(row["last_ms"] or 0)
        item = prepare_ps38_event({
            "teams": row["teams"],
            "event_ms": row["ev"],
            "league": row["league"],
            "orders": row["orders"],
            "stake": row["stake"],
            "live": row["live"],
            "last_order_at": (
                datetime.fromtimestamp(last_ms / 1000.0, DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
                if last_ms else ""
            ),
        })
        events.append(item)
    return events


def fetch_crown_events(config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str], str]:
    """Read the local Crown event feed; never raise into the HTTP layer."""
    import http.client

    host = str(config.get("crown_host") or "127.0.0.1")
    try:
        port = int(config.get("crown_port") or 9223)
    except (TypeError, ValueError):
        port = 9223
    error = "皇冠服务暂不可用；请先启动皇冠数据服务后重试。"
    try:
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("GET", "/api/crown/events?compact=1",
                               headers={"Connection": "close"})
            response = connection.getresponse()
            body = response.read(8 * 1024 * 1024 + 1)
            if response.status != 200:
                raise ValueError("皇冠服务不可用")
            if len(body) > 8 * 1024 * 1024:
                raise ValueError("皇冠赛事数据过大")
            payload = json.loads(body.decode("utf-8"))
            events = payload.get("events") if isinstance(payload, dict) else None
            if not isinstance(events, list):
                raise ValueError("皇冠赛事数据格式不正确")
            fetched = str(payload.get("updated_at") or "")
            stamp = _parse_iso(fetched)
            if (payload.get("error") or payload.get("cached") or stamp is None
                    or (datetime.now(timezone.utc) - stamp).total_seconds() > FEED_MAX_AGE_SECONDS):
                raise ValueError("皇冠服务返回旧缓存")
            return [event for event in events if isinstance(event, dict)], None, fetched
        finally:
            connection.close()
    except (OSError, http.client.HTTPException, ValueError):
        from .crown_fixtures import fetch_pool_crown_events
        events, pool_error, fetched = fetch_pool_crown_events(config)
        return events, pool_error if pool_error else (None if fetched else error), fetched


def fetch_pinnacle_events(
    config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Optional[str], str]:
    """Read the local PS3838/Pinnacle snapshot service (read-only)."""
    url = str(config.get("pinnacle_snapshot_url") or "").strip()
    if not url:
        return [], "未配置平博快照地址。", ""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return [], "平博快照地址无效。", ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
    error = "平博快照服务暂不可用；请确认 PS3838 采集已启动。"
    try:
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parsed.hostname, port, timeout=8)
        try:
            connection.request("GET", path, headers={"Connection": "close"})
            response = connection.getresponse()
            body = response.read(8 * 1024 * 1024 + 1)
            if response.status != 200:
                return [], f"平博快照服务返回 HTTP {response.status}。", ""
            if len(body) > 8 * 1024 * 1024:
                return [], "平博快照数据过大，已拒绝读取。", ""
            payload = json.loads(body.decode("utf-8"))
            raw_events = payload.get("events") if isinstance(payload, dict) else None
            if not isinstance(raw_events, list):
                return [], "平博快照数据格式不正确。", ""
            now = datetime.now(timezone.utc)
            source = payload.get("source") or {}
            captured = _parse_iso(source.get("lastCapturedAt")) if isinstance(source, dict) else None
            lag = payload.get("lagSeconds")
            if (payload.get("error") or payload.get("cached")
                    or str(payload.get("freshness") or "").lower() in {"stale", "offline", "error", "disconnected"}
                    or (isinstance(lag, (int, float)) and lag > FEED_MAX_AGE_SECONDS)
                    or (captured and (now - captured).total_seconds() > FEED_MAX_AGE_SECONDS)):
                return [], "平博实时快照已过期，请恢复采集后重新拉取。", ""
            events: List[Dict[str, Any]] = []
            for raw in raw_events:
                if not isinstance(raw, dict):
                    continue
                if str(raw.get("sportId") or "") != "29":
                    continue
                home, away = _split_pinnacle_teams(raw.get("name") or "")
                league = str(raw.get("league") or "").strip()
                kickoff = _parse_pinnacle_kickoff(raw.get("kickoff"))
                events.append({
                    "id": f"pin:{raw.get('id')}",
                    "source": "pinnacle",
                    "name": f"{home} vs {away}".strip(),
                    "home": home,
                    "away": away,
                    "league": league,
                    "leagueId": ps38_league_id(league),
                    "providerLeagueId": str(raw.get("leagueId") or ""),
                    "teamsRaw": str(raw.get("name") or ""),
                    "kickoffUtc": _iso_utc(kickoff),
                    "kickoffLocal": _display_time(kickoff),
                    "isLive": bool(raw.get("isLive")),
                    "isFinished": bool(raw.get("isFinished", raw.get("is_finished"))),
                    "matchStatus": str(raw.get("matchStatus") or ""),
                    "homeEn": str(raw.get("homeEn") or ""),
                    "awayEn": str(raw.get("awayEn") or ""),
                    "orders": 0,
                    "members": 0,
                    "stake": 0.0,
                    "lastOrderAt": "",
                    "pinnacleId": str(raw.get("id") or ""),
                })
            fetched = str(source.get("lastCapturedAt") or payload.get("serverTime") or now.isoformat()) if isinstance(source, dict) else now.isoformat()
            return events, None, fetched
        finally:
            connection.close()
    except (OSError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError):
        return [], error, ""


def _split_pinnacle_teams(value: Any) -> Tuple[str, str]:
    text = str(value or "").strip()
    parts = re.split(r"\s+VS\s+", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return text, ""


def _parse_pinnacle_kickoff(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DISPLAY_TZ)
    return parsed.astimezone(timezone.utc)


def _merge_event_sources(
    order_events: List[Dict[str, Any]], pinnacle_events: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The current provider catalog owns existence, IDs, kickoff and live state.

    Orders only enrich matching current fixtures with volume; they never add
    historical fixtures to a successfully fetched (even empty) provider list.
    """
    buckets: Dict[Tuple[str, int, str, str], Dict[str, Any]] = {}
    for event in order_events:
        kickoff = event_kickoff(event)
        if kickoff is None:
            continue
        key = (
            str(event.get("leagueId") or ""),
            int(kickoff.timestamp()) // 300,
            normalize_team(event.get("home")),
            normalize_team(event.get("away")),
        )
        buckets[key] = event
    result: List[Dict[str, Any]] = []
    for event in pinnacle_events:
        kickoff = event_kickoff(event)
        key = None
        if kickoff is not None:
            key = (
                str(event.get("leagueId") or ""),
                int(kickoff.timestamp()) // 300,
                normalize_team(event.get("home")),
                normalize_team(event.get("away")),
            )
        matched = buckets.get(key) if key else None
        if matched is not None:
            event = {**event, **{key: matched.get(key) for key in ("orders", "members", "stake", "lastOrderAt")}}
        result.append(event)
    return result


def _snapshot_league(league: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": league["id"],
        "name": league["name"],
        "eventCount": league["eventCount"],
    }


class MergeService:
    """Builds the merge payload and applies confirmed mappings."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        merge_path = config.get("merge_db_path")
        if not merge_path:
            merge_path = Path(config.get("db_path", "data/orders.sqlite3")).parent / "fixture_merge.sqlite3"
        seed_path = config.get("merge_seed_path")
        self.store = FixtureMergeStore(
            Path(merge_path), Path(seed_path) if seed_path else None
        )
        self.offset_minutes = config.get("crown_utc_offset_minutes", DEFAULT_CROWN_OFFSET_MINUTES)
        self._crown_lock = threading.Lock()
        self._crown_cache: Optional[Tuple[float, List[Dict[str, Any]], Optional[str], str]] = None
        self._pinnacle_lock = threading.Lock()
        self._pinnacle_cache: Optional[Tuple[float, List[Dict[str, Any]], Optional[str], str]] = None
        self._mapping_lock = threading.RLock()

    def _crown_source(self) -> Tuple[List[Dict[str, Any]], Optional[str], str]:
        """Reuse a successful pull through matching and the following UI read.

        The pull button always reloads. Avoid discarding a just-pulled catalog
        with a second network request during the same UI action.
        """
        with self._crown_lock:
            now = time.monotonic()
            ttl = 30.0 if self._crown_cache and not self._crown_cache[2] else 3.0
            if self._crown_cache is not None and now - self._crown_cache[0] < ttl:
                _, events, error, fetched = self._crown_cache
                return events, error, fetched
            events, error, fetched = fetch_crown_events(self.config)
            self._crown_cache = (time.monotonic(), events, error, fetched)
            return events, error, fetched

    def _pinnacle_source(self) -> Tuple[List[Dict[str, Any]], Optional[str], str]:
        with self._pinnacle_lock:
            now = time.monotonic()
            if self._pinnacle_cache is not None and now - self._pinnacle_cache[0] < 3.0:
                _, events, error, fetched = self._pinnacle_cache
                return events, error, fetched
            events, error, fetched = fetch_pinnacle_events(self.config)
            self._pinnacle_cache = (time.monotonic(), events, error, fetched)
            return events, error, fetched

    def pull_crown(self) -> Dict[str, Any]:
        """Read a fresh feed or saved pool session without triggering a login."""
        events, error, fetched = fetch_crown_events(self.config)
        with self._crown_lock:
            self._crown_cache = (time.monotonic(), events, error, fetched)
        if error and not events:
            raise CrownUnavailable(error)
        matched = self._auto_after_refresh()
        return {
            "count": len(events),
            "fetchedAt": fetched,
            "refreshed": True,
            **matched,
        }

    def pull_pinnacle(self) -> Dict[str, Any]:
        """Force a PS3838/Pinnacle snapshot read."""
        events, error, fetched = fetch_pinnacle_events(self.config)
        with self._pinnacle_lock:
            self._pinnacle_cache = (time.monotonic(), events, error, fetched)
        if error and not events:
            raise ValueError(error)
        matched = self._auto_after_refresh()
        return {"count": len(events), "fetchedAt": fetched,
                **matched}

    def _auto_after_refresh(self) -> Dict[str, Any]:
        # A successful source read/binding stays successful if the other feed
        # is unavailable; matching can be retried when both recover.
        try:
            result = self.auto_merge()
            return {"autoBound": len(result["mappedFixtures"])}
        except (CrownUnavailable, ValueError) as exc:
            return {"autoBound": 0, "autoError": str(exc)}

    def _catalog(self) -> Dict[str, Any]:
        """Current validated events; manual confirmation needs no candidate ranking."""
        past_hours = float(self.config.get("merge_past_hours") or 24.0)
        horizon_hours = float(self.config.get("merge_horizon_hours") or 72.0)
        order_events = ps38_fixtures(
            Path(self.config["db_path"]), past_hours, horizon_hours
        )
        pinnacle_events, pinnacle_error, pinnacle_fetched = self._pinnacle_source()
        ps_events = [] if pinnacle_error else _merge_event_sources(order_events, pinnacle_events)
        crown_raw, crown_error, crown_fetched = self._crown_source()
        now = datetime.now(timezone.utc)
        crown_events = [
            prepare_crown_event(event, self.offset_minutes, now) for event in crown_raw
        ]
        ps_events = [event for event in ps_events if current_fixture(event, now, horizon_hours)]
        crown_events = [event for event in crown_events if current_fixture(event, now, horizon_hours)]
        excluded = {
            str(name).strip().casefold()
            for name in (self.config.get("merge_excluded_leagues") or [])
            if str(name).strip()
        }
        if excluded:
            ps_events = [event for event in ps_events
                         if str(event.get("league") or "").casefold() not in excluded]
            crown_events = [event for event in crown_events
                            if str(event.get("league") or "").casefold() not in excluded]
        league_mappings = self.store.list_league_mappings()
        align_league_ids(ps_events, league_mappings, "ps38")
        align_league_ids(crown_events, league_mappings, "crown")
        return {
            "past_hours": past_hours,
            "horizon_hours": horizon_hours,
            "ps_events": ps_events,
            "crown_events": crown_events,
            "league_mappings": league_mappings,
            "crown_error": crown_error,
            "crown_fetched": crown_fetched,
            "pinnacle_error": pinnacle_error,
            "pinnacle_fetched": pinnacle_fetched,
        }

    def _pools(self) -> Dict[str, Any]:
        catalog = self._catalog()
        ps_events = catalog["ps_events"]
        crown_events = catalog["crown_events"]
        league_mappings = catalog["league_mappings"]
        now = datetime.now(timezone.utc)
        mappings = self.store.list_mappings()
        # As in hdproc's comparison view, retain confirmed fixtures for 48h.
        # Keep older rows on disk so a UI filter never deletes user mappings.
        mappings = [mapping for mapping in mappings
                    if (event_kickoff(mapping.get("ps38Snapshot") or {})
                        or _parse_iso(mapping.get("updatedAt")) or now) >= now - timedelta(hours=48)]
        crown_to_ps = {
            str(item["crownId"]): str(item["ps38Id"]) for item in league_mappings
        }
        aliases = build_aliases(
            [
                ((item.get("ps38Snapshot") or {}).get("name"),
                 (item.get("crownSnapshot") or {}).get("name"))
                for item in league_mappings
            ],
            [
                pair
                for item in mappings
                for pair in (
                    ((item.get("ps38Snapshot") or {}).get("home"),
                     (item.get("crownSnapshot") or {}).get("home")),
                    ((item.get("ps38Snapshot") or {}).get("away"),
                     (item.get("crownSnapshot") or {}).get("away")),
                )
            ],
        )
        ps_leagues = summarize_leagues(ps_events)
        crown_leagues = summarize_leagues(crown_events)
        league_suggestions = rank_league_candidates(ps_events, crown_events, aliases, limit=3)
        mapped_ps = {mapping["ps38Id"] for mapping in mappings}
        mapped_crown = {mapping["crownId"] for mapping in mappings}
        fixture_suggestions = rank_fixture_candidates(
            [event for event in ps_events if event["id"] not in mapped_ps],
            [event for event in crown_events if event["id"] not in mapped_crown],
            aliases, crown_to_ps, limit=3,
        )
        return {
            **catalog,
            "ps_leagues": ps_leagues,
            "crown_leagues": crown_leagues,
            "league_mappings": league_mappings,
            "mappings": mappings,
            "league_suggestions": league_suggestions,
            "fixture_suggestions": fixture_suggestions,
            "aliases": aliases,
        }

    def payload(self) -> Dict[str, Any]:
        pools = self._pools()
        bound_crown_leagues = {item["crownId"] for item in pools["league_mappings"]}
        match_events = [event for event in pools["crown_events"]
                        if event["leagueId"] in bound_crown_leagues]
        league_count, _ = self.store.counts()
        fixture_count = len(pools["mappings"])
        generated = datetime.now(DISPLAY_TZ).isoformat(timespec="seconds")
        return {
            "ok": True,
            "generatedAt": generated,
            "window": {
                "pastHours": pools["past_hours"],
                "horizonHours": pools["horizon_hours"],
            },
            "sources": {
                "ps38": {
                    "ok": pools["pinnacle_error"] is None,
                    "count": len(pools["ps_events"]),
                    "leagueCount": len(pools["ps_leagues"]),
                    "mode": "current_catalog",
                    "warning": pools["pinnacle_error"],
                    "fetchedAt": pools["pinnacle_fetched"],
                },
                "crown": {
                    "ok": pools["crown_error"] is None,
                    "count": len(pools["crown_events"]),
                    "leagueCount": len(pools["crown_leagues"]),
                    "error": pools["crown_error"],
                    "fetchedAt": pools["crown_fetched"],
                },
            },
            "counts": {
                "leagueMappings": league_count,
                "fixtureMappings": fixture_count,
            },
            "ps38Events": pools["ps_events"],
            "matchingPerspective": "crown",
            "matchEvents": match_events,
            "matchCrownEvents": match_events,
            "crownEvents": pools["crown_events"],
            "ps38Leagues": pools["ps_leagues"],
            "crownLeagues": pools["crown_leagues"],
            "leagueMappings": pools["league_mappings"],
            "mappings": pools["mappings"],
            "leagueSuggestions": pools["league_suggestions"],
            "suggestions": pools["fixture_suggestions"],
            "crownError": pools["crown_error"],
        }

    def bind_league(self, ps38_league_id: str, crown_league_id: str) -> Dict[str, Any]:
        pools = self._pools()
        if pools["crown_error"]:
            raise CrownUnavailable(pools["crown_error"])
        ps_league = next((item for item in pools["ps_leagues"]
                          if item["id"] == ps38_league_id), None)
        crown_league = next((item for item in pools["crown_leagues"]
                             if item["id"] == crown_league_id), None)
        if ps_league is None or crown_league is None:
            raise ValueError("联赛列表已更新，请刷新后重新选择。")
        if competition_conflict(ps_league.get("name"), crown_league.get("name")):
            raise ValueError("两个联赛的赛事类别不一致（青年/预备队/女子），不能绑定。")
        with self._mapping_lock:
            saved = self.store.map_leagues(ps_league, crown_league)
            return {**saved, **self._auto_after_refresh()}

    def unbind_league(self, crown_league_id: str) -> None:
        with self._mapping_lock:
            self.store.unmap_league(crown_league_id)

    def bind_fixture(self, ps38_id: str, crown_id: str) -> Dict[str, Any]:
        pools = self._catalog()
        if pools["crown_error"]:
            raise CrownUnavailable(pools["crown_error"])
        if pools["pinnacle_error"]:
            raise ValueError(pools["pinnacle_error"])
        ps_event = next((item for item in pools["ps_events"] if item["id"] == ps38_id), None)
        crown_event = next((item for item in pools["crown_events"] if item["id"] == crown_id), None)
        if ps_event is None or crown_event is None:
            raise ValueError("赛事列表已更新，请刷新后重新选择。")
        if competition_conflict(ps_event.get("league"), crown_event.get("league")):
            raise ValueError("两场赛事的联赛类别不一致（青年/预备队/女子），不能绑定。")
        candidate = _fixture_candidate(ps_event, crown_event)
        crown_snapshot = {**crown_event, "reversedOrder": bool(candidate and candidate["reversedOrder"])}
        with self._mapping_lock:
            # A league can be unbound while the current event catalog is read.
            # Validate its latest saved rule under the same lock as the write.
            pairs = {item["crownId"]: item["ps38Id"]
                     for item in self.store.list_league_mappings()}
            if pairs.get(crown_event["leagueId"]) != ps_event["leagueId"]:
                raise ValueError("请先绑定对应联赛；不能跨联赛合并比赛。")
            return self.store.map_events(ps_event, crown_snapshot)

    def unbind_fixture(self, ps38_id: str) -> None:
        self.store.unmap(ps38_id)

    def auto_merge(self) -> Dict[str, Any]:
        """Match only confirmed league pairs using hdproc's two-team identity gate.

        A unique best match is required in BOTH directions with a 3 point
        margin. Kickoff must be known and within 15 minutes, both teams must
        score >= .85 and their average >= .92. Time alone is never identity.
        """
        with self._mapping_lock:
            pools = self._pools()
            if pools["crown_error"]:
                raise CrownUnavailable(pools["crown_error"])
            if pools["pinnacle_error"]:
                raise ValueError(pools["pinnacle_error"])
            pairs = {item["crownId"]: item["ps38Id"] for item in pools["league_mappings"]}
            existing = self.store.list_mappings()
            used_ps = {item["ps38Id"] for item in existing}
            used_crown = {item["crownId"] for item in existing}
            confirmed = [(item["ps38Snapshot"], item["crownSnapshot"]) for item in existing]
            candidates = []
            for crown_league, ps_league in pairs.items():
                aliases = confirmed_aliases(confirmed, ps_league, crown_league)
                crowns = [event for event in pools["crown_events"]
                          if event["leagueId"] == crown_league and event["id"] not in used_crown]
                events = [event for event in pools["ps_events"]
                          if event["leagueId"] == ps_league and event["id"] not in used_ps]
                for crown in crowns:
                    for event in events:
                        score = fixture_identity_score(crown, event, aliases)
                        if score:
                            candidates.append((score, event, crown))
            candidates.sort(key=lambda item: (-item[0], item[1]["id"], item[2]["id"]))
            mapped = []
            skipped = []
            for score, event, crown in candidates:
                if event["id"] in used_ps or crown["id"] in used_crown:
                    continue
                competitors = [item for item in candidates
                               if (item[1]["id"] == event["id"] or item[2]["id"] == crown["id"])
                               and item[0] >= score - 3]
                if len(competitors) != 1:
                    skipped.append({"ps38Id": event["id"], "crownId": crown["id"],
                                    "reason": "候选身份接近，需人工确认"})
                    continue
                try:
                    saved = self.store.map_events(event, crown)
                except MappingConflict as exc:
                    skipped.append({"ps38Id": event["id"], "crownId": crown["id"], "reason": str(exc)})
                    continue
                used_ps.add(event["id"])
                used_crown.add(crown["id"])
                mapped.append({**saved, "score": score})
            return {
                "ok": True, "mappedLeagues": [], "mappedFixtures": mapped,
                "skippedLeagues": [], "skippedFixtures": skipped,
                "thresholds": {"fixtureTimeMinutes": 15, "eachTeamScore": 85,
                               "averageTeamScore": 92, "uniqueMargin": 3},
            }
