"""只在已确认映射的平博侧识别来单；不即时猜测跨平台队名。

映射本身就是"这场比赛 ↔ 那场比赛"的确认结果，运行期不再拿开赛时间做淘汰：
时间只影响绑定阶段的审批，跟单执行只认联赛、主客队与唯一性。
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ..fixture_merge import competition_conflict, normalize_league, normalize_team


def resolve_fixture(path, intent):
    path = Path(path)
    if not path.is_file():
        raise ValueError("尚未建立赛事映射库")
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        ) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM fixture_mappings").fetchall()
            leagues = {
                tuple(row)
                for row in connection.execute(
                    "SELECT ps38_league_id,crown_league_id FROM league_mappings"
                )
            }
    except sqlite3.Error:
        raise ValueError("赛事映射暂不可读取") from None
    # 合并表已经有唯一事件 ID：能命中就直接用，不再靠队名相似度反推。
    source_id = str(intent.get("source_event_id") or "").strip()
    direct_ids = {
        str(row["ps38_event_id"])
        for row in rows
        if source_id
        and str(row["ps38_event_id"] or "").removeprefix("pin:") == source_id.removeprefix("pin:")
    }
    if len(direct_ids) > 1:
        raise ValueError("赛事映射不唯一，需核对")

    matches = []
    for row in rows:
        if direct_ids and str(row["ps38_event_id"]) not in direct_ids:
            continue
        try:
            pinnacle, crown = json.loads(row["ps38_snapshot"]), json.loads(row["crown_snapshot"])
        except (ValueError, TypeError):
            continue
        if not isinstance(pinnacle, dict) or not isinstance(crown, dict):
            continue
        if (str(pinnacle.get("leagueId") or ""), str(crown.get("leagueId") or "")) not in leagues:
            continue
        if not direct_ids and normalize_league(intent["league"]) != normalize_league(
            pinnacle.get("league")
        ):
            continue
        if competition_conflict(intent["league"], pinnacle.get("league")):
            continue
        if not direct_ids and any(
            normalize_team(intent[side]) != normalize_team(pinnacle.get(side))
            for side in ("home", "away")
        ):
            continue
        source_id = intent.get("source_event_id")
        if source_id:
            ids = {str(pinnacle.get(key) or "") for key in ("pinnacleId", "catalogId", "id")}
            ids.add(str(row["ps38_event_id"]))
            ids |= {value[4:] for value in ids if value.startswith("pin:")}
            if source_id not in ids:
                continue
        if not crown.get("home") or not crown.get("away") or crown.get("isFinished"):
            continue
        if crown.get("reversedOrder"):
            raise ValueError("皇冠主客队顺序颠倒，需核对方向后处理")
        if competition_conflict(pinnacle.get("league"), crown.get("league")):
            continue
        fingerprint = hashlib.sha256(
            json.dumps(
                [row["ps38_event_id"], row["crown_event_id"], pinnacle, crown],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        matches.append(
            {
                "ps38Id": row["ps38_event_id"],
                "crownId": row["crown_event_id"],
                "ps38Snapshot": pinnacle,
                "crownSnapshot": crown,
                "version": fingerprint,
                "updatedAt": row["updated_at"],
            }
        )
    if len(matches) != 1:
        raise ValueError("赛事映射不唯一，需核对" if matches else "未找到已绑定的对应赛事或联赛")
    return matches[0]
