"""跟单任务存储；每次访问自行打开连接并明确提交或回滚。"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

from .rules import DEFAULT_RULE
from .source import number_text

ACTIVE = {
    "WAIT_MAPPING",
    "WAIT_ACCOUNT",
    "GROUPING",
    "READY",
    "WAIT_MARKET",
    "PREPARED",
    "PREPARING",
    "RETRY_WAIT",
}
PROCESSING = ACTIVE | {"SUBMITTING"}
FAILED = {"REJECTED", "SKIPPED", "EXPIRED", "PARTIAL"}
STATES = PROCESSING | FAILED | {"SUCCEEDED", "UNKNOWN"}


def iso_time(seconds):
    return datetime.fromtimestamp(seconds, timezone(timedelta(hours=8))).isoformat(
        timespec="milliseconds"
    )


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def public_group(group):
    result = {
        key: value
        for key, value in group.items()
        if key
        not in {
            "rule",
            "intent",
            "mapping",
            "base_key",
            "quote",
            "start_ms",
            "close_ms",
            "deadline_ms",
            "_revision",
            "next_check_at",
            "reconcile_next_at",
            "reconcile_deadline",
        }
    }
    # 列表需要区分赛前/滚球；intent 不外传，只透出这一个布尔值。
    intent = group.get("intent")
    if isinstance(intent, dict) and isinstance(intent.get("is_live"), bool):
        result["is_live"] = intent["is_live"]
    return result


class FollowStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS follow_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS follow_rule (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS follow_groups (
                    id TEXT PRIMARY KEY, base_key TEXT NOT NULL, start_ms INTEGER NOT NULL,
                    close_ms INTEGER NOT NULL, status TEXT NOT NULL, account_id TEXT NOT NULL,
                    receipt_id TEXT, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS follow_groups_lookup
                    ON follow_groups(base_key,start_ms);
                CREATE INDEX IF NOT EXISTS follow_groups_status
                    ON follow_groups(status,start_ms);
                CREATE UNIQUE INDEX IF NOT EXISTS follow_groups_source_batch
                    ON follow_groups(json_extract(payload,'$.source_batch_no'))
                    WHERE json_extract(payload,'$.source_batch_no') IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS follow_groups_receipt
                    ON follow_groups(account_id,receipt_id) WHERE receipt_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS follow_members (
                    source TEXT NOT NULL, member_identity TEXT NOT NULL, order_id TEXT NOT NULL,
                    group_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(source,member_identity,order_id),
                    FOREIGN KEY(group_id) REFERENCES follow_groups(id)
                );
                CREATE INDEX IF NOT EXISTS follow_members_group ON follow_members(group_id);
                CREATE TABLE IF NOT EXISTS follow_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL,
                    FOREIGN KEY(group_id) REFERENCES follow_groups(id)
                );
                CREATE TABLE IF NOT EXISTS follow_parts (
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
                    part_index INTEGER NOT NULL, status TEXT NOT NULL,
                    stake TEXT NOT NULL, account_id TEXT NOT NULL,
                    ticket_id TEXT, payload TEXT NOT NULL,
                    UNIQUE(group_id, part_index)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS follow_parts_ticket
                    ON follow_parts(account_id,ticket_id) WHERE ticket_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS follow_claims (
                    group_id TEXT PRIMARY KEY, account_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL, owner_pid INTEGER NOT NULL,
                    phase TEXT NOT NULL, acquired_at TEXT NOT NULL,
                    FOREIGN KEY(group_id) REFERENCES follow_groups(id)
                );
                CREATE TABLE IF NOT EXISTS follow_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
                    payload TEXT NOT NULL, FOREIGN KEY(group_id) REFERENCES follow_groups(id)
                );
                CREATE INDEX IF NOT EXISTS follow_history_group ON follow_history(group_id,id);
                CREATE TABLE IF NOT EXISTS follow_reconciliations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
                    payload TEXT NOT NULL, FOREIGN KEY(group_id) REFERENCES follow_groups(id)
                );
                -- 主页 20 秒展示批次冻结前的 10 秒待跟单窗口；行本身就是倒计时状态。
                CREATE TABLE IF NOT EXISTS follow_pending (
                    id TEXT PRIMARY KEY, source_batch_key TEXT NOT NULL,
                    page_batch_no TEXT NOT NULL, freeze_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS follow_pending_open
                    ON follow_pending(status,freeze_at_ms);
                CREATE INDEX IF NOT EXISTS follow_pending_batch
                    ON follow_pending(source_batch_key,status);
                CREATE TABLE IF NOT EXISTS follow_pending_members (
                    source TEXT NOT NULL, member_identity TEXT NOT NULL, order_id TEXT NOT NULL,
                    pending_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(source,member_identity,order_id),
                    FOREIGN KEY(pending_id) REFERENCES follow_pending(id) ON DELETE CASCADE
                );
            """)
            connection.execute(
                "INSERT OR IGNORE INTO follow_rule VALUES ('default',?)", (dump(DEFAULT_RULE),)
            )

    @contextmanager
    def connect(self, write=False):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    @staticmethod
    def rule(connection):
        row = connection.execute("SELECT payload FROM follow_rule WHERE id='default'").fetchone()
        return {**DEFAULT_RULE, **json.loads(row[0]), "currency": "CNY"}

    @staticmethod
    def save_rule(connection, rule):
        connection.execute("UPDATE follow_rule SET payload=? WHERE id='default'", (dump(rule),))

    @staticmethod
    def meta(connection, key, default=None):
        row = connection.execute("SELECT value FROM follow_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def set_meta(connection, key, value):
        connection.execute(
            "INSERT INTO follow_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dump(value)),
        )

    @staticmethod
    def save_group(connection, group):
        before = connection.execute(
            "SELECT payload FROM follow_groups WHERE id=?", (group["id"],)
        ).fetchone()
        previous = json.loads(before[0]) if before else {}
        revision = group.get("_revision", 0)
        updated = {**group, "_revision": revision + 1}
        changed = connection.execute(
            "INSERT INTO follow_groups VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET base_key=excluded.base_key,status=excluded.status,account_id=excluded.account_id,receipt_id=excluded.receipt_id,payload=excluded.payload "
            "WHERE COALESCE(json_extract(follow_groups.payload,'$._revision'),0)=?",
            (
                group["id"],
                group["base_key"],
                group["start_ms"],
                group["close_ms"],
                group["status"],
                group["account_id"],
                group.get("receipt_id") or None,
                dump(updated),
                revision,
            ),
        )
        if changed.rowcount != 1:
            raise RuntimeError("订单组已更新，不能覆盖旧快照")
        group["_revision"] = revision + 1
        if any(
            previous.get(key) != group.get(key) for key in ("status", "message", "error_category")
        ):
            connection.execute(
                "INSERT INTO follow_history(group_id,payload) VALUES (?,?)",
                (
                    group["id"],
                    dump(
                        {
                            "at": group.get("updated_at") or group.get("created_at"),
                            "from_status": previous.get("status"),
                            "to_status": group["status"],
                            "message": group.get("message", ""),
                            "kind": group.get("history_kind")
                            or group.get("error_category")
                            or "STATE",
                        }
                    ),
                ),
            )

    @staticmethod
    def save_part(connection, part):
        connection.execute(
            "INSERT INTO follow_parts(id,group_id,part_index,status,stake,account_id,ticket_id,payload) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(group_id,part_index) DO UPDATE SET "
            "status=excluded.status,stake=excluded.stake,account_id=excluded.account_id,"
            "ticket_id=excluded.ticket_id,payload=excluded.payload",
            (
                part["id"],
                part["group_id"],
                int(part["part_index"]),
                part["status"],
                str(part["stake"]),
                part.get("account_id") or "",
                part.get("ticket_id") or None,
                dump(part),
            ),
        )

    @staticmethod
    def get_parts(connection, group_id):
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM follow_parts WHERE group_id=? ORDER BY part_index",
                (group_id,),
            )
        ]

    @staticmethod
    def claim(connection, group, owner_id, owner_pid, phase, now):
        return (
            connection.execute(
                "INSERT OR IGNORE INTO follow_claims VALUES (?,?,?,?,?,?)",
                (group["id"], group["account_id"], owner_id, owner_pid, phase, iso_time(now)),
            ).rowcount
            == 1
        )

    @staticmethod
    def account_inflight(connection, exclude_group_id=""):
        """按账号统计在途订单数：认领表 + 正在执行的组（同一组只算一次）。"""
        counts = {}
        seen = {}
        rows = connection.execute("SELECT group_id, account_id FROM follow_claims").fetchall()
        for group_id_value, account_id_value in rows:
            account_id = str(account_id_value or "")
            if not account_id:
                continue
            seen.setdefault(account_id, set()).add(str(group_id_value))
        rows = connection.execute(
            "SELECT id, account_id FROM follow_groups "
            "WHERE status IN ('READY','PREPARING','SUBMITTING') AND account_id<>'' AND id<>?",
            (exclude_group_id,),
        ).fetchall()
        for group_id_value, account_id_value in rows:
            account_id = str(account_id_value or "")
            if not account_id:
                continue
            seen.setdefault(account_id, set()).add(str(group_id_value))
        for account_id, group_ids in seen.items():
            counts[account_id] = len(group_ids)
        return counts

    @staticmethod
    def release_claim(connection, group_id, owner_id):
        connection.execute(
            "DELETE FROM follow_claims WHERE group_id=? AND owner_id=?", (group_id, owner_id)
        )

    @staticmethod
    def owns_claim(connection, group_id, owner_id):
        return (
            connection.execute(
                "SELECT 1 FROM follow_claims WHERE group_id=? AND owner_id=?", (group_id, owner_id)
            ).fetchone()
            is not None
        )

    @staticmethod
    def get_group(connection, group_id):
        row = connection.execute(
            "SELECT payload FROM follow_groups WHERE id=?", (group_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def group_for_source_batch(connection, order_no):
        row = connection.execute(
            "SELECT payload FROM follow_groups "
            "WHERE json_extract(payload,'$.source_batch_no')=? LIMIT 1",
            (str(order_no),),
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def get_members(connection, group_id):
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM follow_members WHERE group_id=? ORDER BY rowid", (group_id,)
            )
        ]

    @staticmethod
    def contains_member(connection, member):
        return (
            connection.execute(
                "SELECT 1 FROM follow_members WHERE source=? AND member_identity=? AND order_id=?",
                (member["source"], member["member_identity"], member["order_id"]),
            ).fetchone()
            is not None
        )

    @staticmethod
    def add_member(connection, group_id, member):
        connection.execute(
            "INSERT INTO follow_members VALUES (?,?,?,?,?)",
            (
                member["source"],
                member["member_identity"],
                member["order_id"],
                group_id,
                dump(member),
            ),
        )

    # ── 10 秒待跟单窗口：倒计时与成员都落库，重启后按原截止时间恢复 ──
    @staticmethod
    def pending_batches(connection):
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM follow_pending WHERE status='OPEN' ORDER BY freeze_at_ms,rowid"
            )
        ]

    @staticmethod
    def pending_for_key(connection, source_batch_key):
        row = connection.execute(
            "SELECT payload FROM follow_pending WHERE status='OPEN' AND source_batch_key=? "
            "ORDER BY freeze_at_ms DESC,rowid DESC LIMIT 1",
            (str(source_batch_key),),
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def save_pending(connection, pending):
        connection.execute(
            "INSERT INTO follow_pending"
            "(id,source_batch_key,page_batch_no,freeze_at_ms,status,created_at,payload) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET freeze_at_ms=excluded.freeze_at_ms,"
            "status=excluded.status,payload=excluded.payload",
            (
                pending["id"],
                pending["source_batch_key"],
                pending["page_batch_no"],
                int(pending["freeze_at_ms"]),
                pending["status"],
                pending["created_at"],
                dump(pending),
            ),
        )

    @staticmethod
    def drop_pending(connection, pending_id):
        connection.execute("DELETE FROM follow_pending WHERE id=?", (str(pending_id),))

    @staticmethod
    def pending_count(connection):
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM follow_pending WHERE status='OPEN'"
            ).fetchone()[0]
        )

    @staticmethod
    def pending_members(connection, pending_id):
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM follow_pending_members WHERE pending_id=? ORDER BY rowid",
                (str(pending_id),),
            )
        ]

    @staticmethod
    def pending_contains_member(connection, member):
        return (
            connection.execute(
                "SELECT 1 FROM follow_pending_members "
                "WHERE source=? AND member_identity=? AND order_id=?",
                (member["source"], member["member_identity"], member["order_id"]),
            ).fetchone()
            is not None
        )

    @staticmethod
    def add_pending_member(connection, pending_id, member):
        connection.execute(
            "INSERT OR IGNORE INTO follow_pending_members VALUES (?,?,?,?,?)",
            (
                member["source"],
                member["member_identity"],
                member["order_id"],
                str(pending_id),
                dump(member),
            ),
        )

    @staticmethod
    def groups_for_key(connection, base_key):
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM follow_groups WHERE base_key=? ORDER BY start_ms DESC LIMIT 30",
                (base_key,),
            )
        ]

    @staticmethod
    def active_groups(connection):
        marks = ",".join("?" for _ in ACTIVE)
        return [
            json.loads(row[0])
            for row in connection.execute(
                f"SELECT payload FROM follow_groups WHERE status IN ({marks}) ORDER BY start_ms,id",
                tuple(sorted(ACTIVE)),
            )
        ]

    def recover_inflight(self, now, owner_alive=None):
        with self.connect(write=True) as connection:
            live = set()
            for claim in connection.execute("SELECT * FROM follow_claims").fetchall():
                if owner_alive and owner_alive(claim["owner_id"], claim["owner_pid"]):
                    live.add(claim["group_id"])
                    continue
                connection.execute(
                    "DELETE FROM follow_claims WHERE group_id=?", (claim["group_id"],)
                )
            # 查盘阶段尚未产生写请求，失去占用后可重新准备。已提交阶段只可核对。
            for row in connection.execute(
                "SELECT payload FROM follow_groups WHERE status='PREPARING'"
            ).fetchall():
                group = json.loads(row[0])
                if group["id"] not in live:
                    group.update(
                        status="READY",
                        message="已恢复未提交任务，等待重新查盘",
                        updated_at=iso_time(now),
                        next_check_at=now,
                        next_action_at=iso_time(now),
                    )
                    self.save_group(connection, group)
            rows = connection.execute(
                "SELECT payload FROM follow_groups WHERE status='SUBMITTING'"
            ).fetchall()
            for row in rows:
                group = json.loads(row[0])
                if group["id"] in live:
                    continue
                group.update(
                    status="REJECTED",
                    message="程序中断时请求已在途，按失败记录",
                    error_category="SUBMIT_INTERRUPTED",
                    waiting_reason="",
                    next_action_at=None,
                    updated_at=iso_time(now),
                )
                self.save_group(connection, group)
            for row in connection.execute(
                "SELECT id,payload FROM follow_attempts WHERE status='SUBMITTING'"
            ).fetchall():
                payload = json.loads(row["payload"])
                if payload["group_id"] in live:
                    continue
                payload.update(
                    status="UNKNOWN", message="请求在途时程序中断", finished_at=iso_time(now)
                )
                connection.execute(
                    "UPDATE follow_attempts SET status='UNKNOWN',payload=? WHERE id=?",
                    (dump(payload), row["id"]),
                )

    @staticmethod
    def unknown_groups(connection):
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM follow_groups WHERE status='UNKNOWN' ORDER BY start_ms,id"
            )
        ]

    def snapshot(self, status="ALL", search="", limit=50, offset=0):
        status = str(status or "ALL").upper()
        if status not in STATES | {"ALL", "PROCESSING", "FAILED"}:
            raise ValueError("跟单状态筛选无效")
        limit, offset = max(1, min(200, int(limit))), max(0, int(offset))
        allowed = (
            PROCESSING if status == "PROCESSING" else FAILED if status == "FAILED" else {status}
        )
        query = str(search or "").strip().casefold()[:200]
        # 兼容异常旧记录：没有可关联票号的 SUCCEEDED 只能按待核对显示。
        current = """WITH current_groups AS (
            SELECT *,CASE WHEN status='SUCCEEDED' AND
              (COALESCE(receipt_id,'')='' OR
               COALESCE(json_extract(payload,'$.receipt_id'),'')<>receipt_id)
            THEN 'UNKNOWN' ELSE status END AS effective_status FROM follow_groups
        ) """
        where, params = ["1=1"], []
        if status != "ALL":
            where.append("g.effective_status IN (" + ",".join("?" for _ in allowed) + ")")
            params.extend(sorted(allowed))
        if query:
            fields = ("league", "home", "away", "crown_home", "crown_away", "message")
            haystack = " || ' ' || ".join(
                ["g.id", "g.account_id", "COALESCE(g.receipt_id,'')"]
                + [f"COALESCE(json_extract(g.payload,'$.{key}'),'')" for key in fields]
            )
            where.append(f"""(INSTR(LOWER({haystack}),?)>0 OR EXISTS (
                SELECT 1 FROM follow_members m WHERE m.group_id=g.id AND INSTR(LOWER(
                    m.order_id || ' ' || m.member_identity || ' ' ||
                    COALESCE(json_extract(m.payload,'$.member_code'),'') || ' ' ||
                    COALESCE(json_extract(m.payload,'$.member_name'),'')),?)>0))""")
            params.extend((query, query))
        clause = " AND ".join(where)
        with self.connect() as connection:
            rule = self.rule(connection)
            message = self.meta(connection, "message", "跟单尚未启用")
            total = connection.execute(
                current + f"SELECT COUNT(*) FROM current_groups g WHERE {clause}", params
            ).fetchone()[0]
            page = connection.execute(
                current
                + f"SELECT g.payload,g.effective_status FROM current_groups g WHERE {clause} ORDER BY g.start_ms DESC,g.id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            counts = connection.execute(
                current
                + "SELECT effective_status,COUNT(*) FROM current_groups GROUP BY effective_status"
            ).fetchall()
            amount_rows = connection.execute(
                current
                + """SELECT
                json_extract(payload,'$.currency') AS currency,
                ROUND(SUM(CASE WHEN json_extract(payload,'$.accepted_stake') IS NOT NULL
                    THEN CAST(json_extract(payload,'$.accepted_stake') AS REAL) ELSE 0 END),2) AS amount,
                SUM(CASE WHEN json_extract(payload,'$.accepted_stake') IS NULL THEN 1 ELSE 0 END) AS missing
                FROM current_groups WHERE effective_status='SUCCEEDED' GROUP BY currency"""
            ).fetchall()
            active_tasks = connection.execute("SELECT COUNT(*) FROM follow_claims").fetchone()[0]
            pending_tasks = connection.execute(
                "SELECT COUNT(*) FROM follow_pending WHERE status='OPEN'"
            ).fetchone()[0]
            queued_tasks = connection.execute(
                "SELECT COUNT(*) FROM follow_groups g WHERE status IN ('READY','WAIT_ACCOUNT','WAIT_MARKET','RETRY_WAIT') "
                "AND NOT EXISTS (SELECT 1 FROM follow_claims c WHERE c.group_id=g.id)"
            ).fetchone()[0]
        summary = {
            "succeeded": 0,
            "processing": 0,
            "failed": 0,
            "unknown": 0,
            "total": 0,
            "confirmed_stake": "0",
            "pending_amount_count": 0,
        }
        for state, count in counts:
            key = (
                "succeeded"
                if state == "SUCCEEDED"
                else "unknown"
                if state == "UNKNOWN"
                else "processing"
                if state in PROCESSING
                else "failed"
            )
            summary[key] += count
            summary["total"] += count
        amounts = {row["currency"]: number_text(Decimal(str(row["amount"]))) for row in amount_rows}
        summary["pending_amount_count"] = sum(row["missing"] for row in amount_rows)
        summary["confirmed_stake_by_currency"] = amounts
        summary["confirmed_stake"] = (
            next(iter(amounts.values())) if len(amounts) == 1 else ("0" if not amounts else None)
        )
        summary["currency"] = next(iter(amounts)) if len(amounts) == 1 else ""
        rows = []
        for item in page:
            group = json.loads(item["payload"])
            group["status"] = item["effective_status"]
            rows.append(public_group(group))
        return {
            "ok": True,
            "rule": rule,
            "service": {
                "enabled": rule["enabled"],
                "running": False,
                "scope": "live",
                "message": message,
                "active_tasks": active_tasks,
                "queued_tasks": queued_tasks,
                "pending_tasks": pending_tasks,
            },
            "summary": summary,
            "rows": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def recent_events(self, limit=80):
        """返回最近的状态时间线事件（跨订单），供执行日志窗口展示。"""
        limit = max(1, min(300, int(limit)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT h.group_id, h.payload, g.payload FROM follow_history h "
                "JOIN follow_groups g ON g.id = h.group_id ORDER BY h.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        events = []
        for group_id, event, group in rows:
            try:
                item = json.loads(event or "{}")
                group_payload = json.loads(group or "{}")
            except (TypeError, ValueError):
                continue
            events.append(
                {
                    "group_id": group_id,
                    "at": item.get("at"),
                    "kind": item.get("kind") or "",
                    "from_status": item.get("from_status"),
                    "to_status": item.get("to_status"),
                    "message": item.get("message") or "",
                    "batch": group_payload.get("source_batch_no"),
                    "market_type": group_payload.get("market_type"),
                    "target_line": group_payload.get("target_line"),
                    "account_id": group_payload.get("account_id") or "",
                    "stake": group_payload.get("target_stake"),
                    "error_code": group_payload.get("error_code") or "",
                }
            )
        return events

    def detail(self, group_id):
        with self.connect() as connection:
            group = self.get_group(connection, group_id)
            if group is None:
                raise KeyError("跟单记录不存在")
            if group["status"] == "SUCCEEDED" and not group.get("receipt_id"):
                group["status"] = "UNKNOWN"
            members = self.get_members(connection, group_id)
            attempts = [
                {**json.loads(row["payload"]), "id": row["id"]}
                for row in connection.execute(
                    "SELECT id,payload FROM follow_attempts WHERE group_id=? ORDER BY id",
                    (group_id,),
                )
            ]
            history = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload FROM follow_history WHERE group_id=? ORDER BY id", (group_id,)
                )
            ]
            parts = self.get_parts(connection, group_id)
            reconciliations = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload FROM follow_reconciliations WHERE group_id=? ORDER BY id",
                    (group_id,),
                )
            ]
        return {
            "ok": True,
            "group": public_group(group),
            "members": members,
            "attempts": attempts,
            "parts": parts,
            "history": history,
            "reconciliations": reconciliations,
        }
