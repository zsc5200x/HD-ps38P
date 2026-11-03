"""hdproc's conservative fixture identity rules, local to the standalone app."""
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher


def team_key(value):
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.translate(str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ð": "d", "þ": "th", "æ": "ae", "œ": "oe"}))
    text = re.sub(r"\b(?:football club|soccer club|fc|sc|afc|cf)\b", "", text)
    text = text.replace("足球俱乐部", "").replace("俱乐部", "")
    return "".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", text))


def _profile(value):
    text = str(value or "").casefold()
    ages = tuple(sorted(set(re.findall(r"(?<![a-z])u\s*-?\s*(\d{2})(?!\d)", text))))
    return (bool(re.search(r"女子|女足|women|ladies|\(女\)|（女）", text)), ages,
            bool(re.search(r"预备|后备|reserve", text)))


def kickoff(value):
    """Only parse dated kickoff evidence, never a live minute like 1H^24:06."""
    try:
        stamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone(timedelta(hours=8)))
    return stamp


def confirmed_aliases(mappings, pinnacle_league_id, crown_league_id):
    """Reuse only unambiguous team pairs in this exact confirmed league pair."""
    pairs = set()
    for p, c in mappings:
        if (str(p.get("leagueId") or ""), str(c.get("leagueId") or "")) != (
                str(pinnacle_league_id), str(crown_league_id)) or c.get("reversedOrder"):
            continue
        for side in ("home", "away"):
            a, b = team_key(p.get(side)), team_key(c.get(side))
            if a and b and _profile(p.get(side)) == _profile(c.get(side)):
                pairs.add((a, b))
    return {(a, b) for a, b in pairs
            if len({y for x, y in pairs if x == a}) == 1
            and len({x for x, y in pairs if y == b}) == 1}


def fixture_identity_score(crown, pinnacle, aliases=()):
    """Require two team identities, same orientation/class and known kickoff."""
    p_time = kickoff(pinnacle.get("kickoffUtc") or pinnacle.get("normalizedKickoff") or pinnacle.get("kickoff"))
    c_time = kickoff(crown.get("kickoffUtc") or crown.get("normalizedKickoff") or crown.get("kickoff"))
    if p_time is None or c_time is None or abs((p_time - c_time).total_seconds()) > 900:
        return 0.0
    if _profile(pinnacle.get("league")) != _profile(crown.get("league")):
        return 0.0
    scores = []
    for side in ("home", "away"):
        p_name, c_name = pinnacle.get(side), crown.get(side)
        if _profile(p_name) != _profile(c_name):
            return 0.0
        p_key, c_key = team_key(p_name), team_key(c_name)
        if not p_key or not c_key:
            return 0.0
        p_english, c_english = pinnacle.get(side + "En"), crown.get(side + "En")
        if (p_key == c_key or (p_key, c_key) in aliases
                or (team_key(p_english) and team_key(p_english) == team_key(c_english)
                    and _profile(p_english) == _profile(c_english))):
            scores.append(1.0)
        else:
            scores.append(SequenceMatcher(None, p_key, c_key, autojunk=False).ratio())
    # One similar team must never compensate for an unknown opponent.
    if min(scores) < 0.85 or sum(scores) / 2 < 0.92:
        return 0.0
    return round(sum(scores) * 50.0, 1)


def attach_english_identities(events, english_events):
    """Join within one provider by event ID AND kickoff; never by position/name."""
    by_id = {}
    for event in english_events:
        by_id.setdefault(str(event.get("id") or ""), []).append(event)
    for event in events:
        matches = by_id.get(str(event.get("id") or ""), [])
        if len(matches) != 1:
            continue
        english = matches[0]
        own_time, en_time = kickoff(event.get("kickoff")), kickoff(english.get("kickoff"))
        if own_time is None or en_time is None or abs((own_time - en_time).total_seconds()) > 60:
            continue
        if str(event.get("leagueId") or "") != str(english.get("leagueId") or ""):
            continue
        for side in ("home", "away"):
            if english.get(side):
                event[side + "En"] = str(english[side])
    return events
