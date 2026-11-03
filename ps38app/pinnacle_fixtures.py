"""PS3838 官方赛事目录；滚球/今日归属由接口分类决定。"""

from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlencode, urlsplit


def catalog_path(url: str) -> str:
    """复用站点足球主盘目录参数，不添加本地日期或时区条件。"""
    parsed = urlsplit(url)
    params = {
        "btg": "1",
        "c": "",
        "cl": "5",
        "d": "",
        "ec": "",
        "ev": "",
        "g": "QQ==",
        "hle": "false",
        "ic": "false",
        "ice": "false",
        "inl": "false",
        "l": "5",
        "lang": "",
        "lg": "",
        "lv": "0",
        "me": "0",
        "me01": "",
        "mk": "1",
        "more": "false",
        "o": "1",
        "ot": "2",
        "pa": "0",
        "pimo": "0,1,8,39,2,3,6,7,4,5",
        "pn": "-1",
        "pv": "1",
        "ru": "",
        "sp": "29",
        "tm": "0",
        "v": "0",
        "locale": "zh_CN",
        "withCredentials": "true",
    }
    params.update(parse_qsl(parsed.query, keep_blank_values=True))
    return (parsed.path or "/") + "?" + urlencode(params)


def parse_catalog(payload: Any) -> List[Dict[str, Any]]:
    """保留 l=滚球、n=今日；e/e1=早盘不进入匹配目录。"""
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), list) for key in ("l", "n")
    ):
        raise ValueError("平博官方赛事清单格式不正确。")
    events: Dict[str, Dict[str, Any]] = {}
    # 同一 ID 在切盘瞬间重复出现时，滚球状态优先。
    for key, scope in (("n", "today"), ("l", "live")):
        for sport in payload[key]:
            if not isinstance(sport, list) or len(sport) < 3 or str(sport[0]) != "29":
                continue
            if not isinstance(sport[2], list):
                continue
            for league in sport[2]:
                if (
                    not isinstance(league, list)
                    or len(league) < 3
                    or not isinstance(league[2], list)
                ):
                    continue
                for row in league[2]:
                    if not isinstance(row, list) or len(row) < 5 or row[0] is None:
                        continue
                    try:
                        stamp = float(row[4])
                        if stamp > 100_000_000_000:
                            stamp /= 1000
                        kickoff = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
                    except (TypeError, ValueError, OSError, OverflowError):
                        continue
                    home, away = str(row[1] or "").strip(), str(row[2] or "").strip()
                    if not home or not away:
                        continue
                    period = str(row[16] or "") if len(row) > 16 else ""
                    status = str(row[17] or "") if len(row) > 17 else ""
                    event_id = str(row[0])
                    events[event_id] = {
                        "id": event_id,
                        "sportId": "29",
                        "name": f"{home} VS {away}",
                        "leagueId": str(league[0]),
                        "league": str(league[1] or "").strip(),
                        "kickoff": kickoff,
                        "isLive": scope == "live",
                        "scope": scope,
                        "isFinished": period.upper().startswith("END"),
                        "matchStatus": status,
                    }
    return list(events.values())
