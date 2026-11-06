"""Persistent account pool for PS38 / Crown backend / Crown member sessions.

Account metadata lives in a small SQLite database. Browser profiles, storage
state snapshots, session metadata and optional credentials live in one private
directory per account (``data/accounts/<id>``). Public snapshots never include
cookies, passwords or the member uid.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from decimal import Decimal, InvalidOperation
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

PLATFORMS = {
    "ps38_backend": "PS38 后台",
    "crown_backend": "皇冠后台",
    "crown_member": "皇冠会员",
}
STATUSES = {
    "idle",
    "logging_in",
    "checking",
    "online",
    "awaiting_login",
    "expired",
    "error",
    "disabled",
}
MANAGED_BY_COLLECTOR = "collector"
PRIMARY_ID = "primary-ps38"
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _text(value: Any, limit: int = 256) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _balance_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return format(amount, "f")


def _normalize_url(value: Any) -> str:
    raw = _text(value, 512)
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("站点地址必须是完整的 http(s) 地址")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("站点地址不能包含账号、参数或锚点")
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def _normalize_path(value: Any, *, default: str = "/") -> str:
    raw = _text(value, 512) or default
    if not raw.startswith("/") or raw.startswith("//") or "?" in raw or "#" in raw:
        raise ValueError("登录路径必须是不带参数的站内路径")
    return raw


def normalize_url(value: Any) -> str:
    """Public wrapper used by the AdsPower import path."""
    return _normalize_url(value)


class AccountStore:
    def __init__(self, db_path: Path, config: Dict[str, Any]) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(config["pool_dir"])
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.config = config
        self._lock = threading.RLock()
        self._init_schema()
        self._seed_primary()

    # ── sqlite plumbing ───────────────────────────────────────────────────

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._lock, self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    login_path TEXT NOT NULL,
                    dashboard_path TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    managed_by TEXT NOT NULL DEFAULT '',
                    env_id TEXT NOT NULL DEFAULT '',
                    env_no TEXT NOT NULL DEFAULT '',
                    env_name TEXT NOT NULL DEFAULT '',
                    session_backend TEXT NOT NULL DEFAULT 'local',
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_login TEXT NOT NULL DEFAULT '',
                    last_check TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    check_interval INTEGER NOT NULL DEFAULT 60,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pool_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_heartbeats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS account_logs_created
                    ON account_logs(created_at);
                CREATE INDEX IF NOT EXISTS account_heartbeats_account
                    ON account_heartbeats(account_id, id);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(accounts)")}
            if "status_message" not in columns:
                db.execute(
                    "ALTER TABLE accounts ADD COLUMN status_message TEXT NOT NULL DEFAULT ''"
                )
            for column, ddl in (
                ("env_id", "TEXT NOT NULL DEFAULT ''"),
                ("env_no", "TEXT NOT NULL DEFAULT ''"),
                ("env_name", "TEXT NOT NULL DEFAULT ''"),
                ("session_backend", "TEXT NOT NULL DEFAULT 'local'"),
            ):
                if column not in columns:
                    db.execute(f"ALTER TABLE accounts ADD COLUMN {column} {ddl}")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS accounts_env_id "
                "ON accounts(env_id) WHERE env_id != ''"
            )
            # One-time reconstruction for databases created before heartbeat
            # history existed: reuse the check/login log as the first strip.
            if db.execute("SELECT COUNT(*) FROM account_heartbeats").fetchone()[0] == 0:
                rows = db.execute(
                    "SELECT account_id, status, message, created_at FROM account_logs "
                    "WHERE action IN ('check','login') ORDER BY id DESC LIMIT 400"
                ).fetchall()
                for row in reversed(rows):
                    mapped = {"success": "online", "error": "error", "pending": "pending"}.get(
                        str(row["status"]), "idle"
                    )
                    db.execute(
                        "INSERT INTO account_heartbeats "
                        "(account_id, status, latency_ms, message, created_at) VALUES (?,?,?,?,?)",
                        (
                            row["account_id"],
                            mapped,
                            0,
                            str(row["message"])[:240],
                            row["created_at"],
                        ),
                    )
            os.chmod(self.db_path, 0o600)

    # ── seeding ───────────────────────────────────────────────────────────

    def _setting(self, db, key: str) -> str:
        row = db.execute("SELECT value FROM pool_settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else ""

    def _set_setting(self, db, key: str, value: str) -> None:
        db.execute(
            "INSERT INTO pool_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _seed_primary(self) -> None:
        """Register the collector's PS38 login once, without touching its profile."""
        with self._lock, self._db() as db:
            if self._setting(db, "primary_seeded") == "1":
                return
            base_url = _text(self.config.get("base_url"), 512)
            if base_url:
                now = now_iso()
                db.execute(
                    """INSERT OR IGNORE INTO accounts
                    (id, platform, name, base_url, login_path, dashboard_path,
                     username, enabled, managed_by, status, check_interval,
                     created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        PRIMARY_ID,
                        "ps38_backend",
                        "PS38 主账号",
                        base_url,
                        _text(self.config.get("login_path"), 512) or "/partner/zh-cn/login",
                        _text(self.config.get("dashboard_path"), 512)
                        or "/partner-ui-v2/report-outstanding-v2",
                        _text(self.config.get("username"), 128) or "手动登录",
                        1,
                        MANAGED_BY_COLLECTOR,
                        "idle",
                        60,
                        now,
                        now,
                    ),
                )
            self._set_setting(db, "primary_seeded", "1")

    # ── paths / private files ─────────────────────────────────────────────

    def _safe_id(self, account_id: str) -> str:
        value = str(account_id or "")
        if not _SAFE_ID.fullmatch(value):
            raise KeyError("账号 ID 无效")
        return value

    def paths(self, account_id: str) -> Dict[str, Path]:
        account_dir = self.root / self._safe_id(account_id)
        profile_dir = account_dir / "profile"
        return {
            "dir": account_dir,
            "profile_dir": profile_dir,
            "storage": profile_dir / "storage_state.json",
            "meta": account_dir / "session_meta.json",
            "credentials": account_dir / "credentials.json",
        }

    @staticmethod
    def _write_private(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def load_meta(self, account_id: str) -> Dict[str, Any]:
        return self._read_json(self.paths(account_id)["meta"])

    def save_meta(self, account_id: str, meta: Dict[str, Any]) -> None:
        currency = _text(meta.get("balance_currency"), 3).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            currency = ""
        safe = {
            key: _text(meta.get(key), 2048)
            for key in (
                "uid",
                "ver",
                "langx",
                "origin",
                "saved_at",
                "user_agent",
                "login_layer",
                "env_id",
                "mid",
                "username",
                "browser_env",
                "net_mode",
                "net_host",
                "net_port",
                "net_ip",
                "net_country",
            )
        }
        safe.update(
            {
                "balance": _balance_text(meta.get("balance")),
                "balance_currency": currency,
                "balance_checked_at": _text(meta.get("balance_checked_at"), 64),
            }
        )
        self._write_private(self.paths(account_id)["meta"], safe)

    def credentials(self, account_id: str) -> Dict[str, str]:
        raw = self._read_json(self.paths(account_id)["credentials"])
        return {
            key: str(raw.get(key) or "")
            for key in ("username", "password", "security_code", "totp_secret")
        }

    def save_credentials(
        self,
        account_id: str,
        username: str,
        password: str,
        security_code: str = "",
        totp_secret: Optional[str] = None,
    ) -> None:
        """Write credentials atomically (0600).

        ``None`` keeps the existing TOTP secret, an empty string clears it.
        An empty password keeps the existing password so a user can update the
        TOTP secret without retyping the password.
        """
        existing = self.credentials(account_id)
        secret = existing.get("totp_secret", "") if totp_secret is None else _text(totp_secret, 128)
        password = str(password) if password else existing.get("password", "")
        if not password:
            return
        self._write_private(
            self.paths(account_id)["credentials"],
            {
                "username": _text(username or existing.get("username"), 128),
                "password": password,
                "security_code": _text(security_code or existing.get("security_code"), 64),
                "totp_secret": secret,
            },
        )

    # ── public projection ─────────────────────────────────────────────────

    def _public(self, row: sqlite3.Row) -> Dict[str, Any]:
        account_id = row["id"]
        paths = self.paths(account_id)
        meta = self.load_meta(account_id)
        currency = _text(meta.get("balance_currency"), 3).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            currency = ""
        credentials = self.credentials(account_id)
        network = {
            "mode": _text(meta.get("net_mode"), 16),
            "host": _text(meta.get("net_host"), 64),
            "port": _text(meta.get("net_port"), 8),
            "ip": _text(meta.get("net_ip"), 64),
            "country": _text(meta.get("net_country"), 8),
        }
        return {
            "id": account_id,
            "platform": row["platform"],
            "platform_label": PLATFORMS.get(row["platform"], row["platform"]),
            "name": row["name"],
            "base_url": row["base_url"],
            "login_path": row["login_path"],
            "dashboard_path": row["dashboard_path"],
            "username": row["username"],
            "login_id": "",
            "enabled": bool(row["enabled"]),
            "managed_by": row["managed_by"],
            "env_id": row["env_id"],
            "env_no": row["env_no"],
            "env_name": row["env_name"],
            "session_backend": row["session_backend"],
            "status": row["status"],
            "status_message": row["status_message"],
            "last_login": row["last_login"],
            "last_check": row["last_check"],
            "last_error": row["last_error"],
            "network": network,
            "check_interval": row["check_interval"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "has_session": paths["storage"].is_file(),
            "has_credentials": paths["credentials"].is_file(),
            "has_totp": bool(credentials.get("totp_secret")),
            "credential_username": credentials.get("username", ""),
            "has_member_meta": bool(meta.get("uid") and meta.get("ver")),
            "balance": _balance_text(meta.get("balance")),
            "balance_currency": currency,
            "balance_checked_at": _text(meta.get("balance_checked_at"), 64),
            "heartbeats": self.heartbeats(account_id, 24),
        }

    def get(self, account_id: str) -> Dict[str, Any]:
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise KeyError("账号不存在")
        return self._public(row)

    def accounts(self) -> List[Dict[str, Any]]:
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT * FROM accounts ORDER BY "
                "CASE WHEN managed_by='collector' THEN 0 ELSE 1 END, created_at, id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def env_bindings(self) -> Dict[str, Dict[str, str]]:
        """Map AdsPower profile_id -> bound account, for import duplicate checks."""
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT id, name, platform, env_id FROM accounts WHERE env_id != ''"
            ).fetchall()
        return {
            str(row["env_id"]): {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "platform": str(row["platform"]),
            }
            for row in rows
        }

    # ── mutations ─────────────────────────────────────────────────────────

    def set_base_url(self, account_id: str, base_url: str) -> None:
        """更新账号站点地址（皇冠会员自动选择延迟最低的入口时使用）。"""
        value = _normalize_url(base_url)
        if not value:
            raise ValueError("站点地址无效")
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE accounts SET base_url=?, updated_at=? WHERE id=?",
                (value, now_iso(), account_id),
            )

    def bind_environment(
        self, account_id: str, env_id: str, env_no: str = "", env_name: str = ""
    ) -> Dict[str, Any]:
        """Bind an AdsPower profile to any account, including the collector primary.

        Unlike ``save_account`` this deliberately allows the collector-owned
        primary row to switch its browser backend without editing config.json.
        The local profile/snapshot is kept as a rollback path.
        """
        value = _text(env_id, 80)
        if not value or not _SAFE_ID.fullmatch(value):
            raise ValueError("环境 ID 无效")
        with self._lock, self._db() as db:
            row = db.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise KeyError("账号不存在")
            duplicate = db.execute(
                "SELECT name FROM accounts WHERE env_id=? AND id<>?",
                (value, account_id),
            ).fetchone()
            if duplicate:
                raise ValueError(f"环境已绑定账号 {duplicate['name']}")
            db.execute(
                "UPDATE accounts SET env_id=?, env_no=?, env_name=?, "
                "session_backend='adspower', updated_at=? WHERE id=?",
                (value, _text(env_no, 32), _text(env_name, 256), now_iso(), account_id),
            )
            self._log(
                db,
                account_id,
                "env_bound",
                "success",
                f"已绑定 AdsPower 环境 {_text(env_no, 32) or value}",
            )
        return self.get(account_id)

    def save_account(
        self, payload: Dict[str, Any], account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        with self._lock:
            old = None
            if account_id:
                with self._db() as db:
                    row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
                if not row:
                    raise KeyError("账号不存在")
                old = dict(row)
                if old["managed_by"] == MANAGED_BY_COLLECTOR:
                    raise ValueError("采集器主账号由 config.json 管理，不能在号池里修改")

            platform = _text(payload.get("platform") or (old or {}).get("platform"), 32)
            if platform not in PLATFORMS:
                raise ValueError("请选择有效的账号类型")
            if old and platform != old["platform"]:
                raise ValueError("已有账号不能更换类型")

            base_url = _normalize_url(payload.get("base_url") or (old or {}).get("base_url"))
            login_path = _normalize_path(payload.get("login_path") or (old or {}).get("login_path"))
            dashboard_path = ""
            raw_dashboard = payload.get("dashboard_path")
            if raw_dashboard is None and old:
                dashboard_path = old.get("dashboard_path") or ""
            elif raw_dashboard:
                dashboard_path = _normalize_path(raw_dashboard)
            username = _text(payload.get("username") or (old or {}).get("username"), 128)
            if not username:
                raise ValueError("请填写登录账号")
            name = _text(payload.get("name") or (old or {}).get("name") or username, 128)
            if not name:
                raise ValueError("请填写账号名称")

            env_id = _text(payload.get("env_id", (old or {}).get("env_id", "")), 80)
            if env_id and not _SAFE_ID.fullmatch(env_id):
                raise ValueError("环境 ID 无效")
            env_no = _text(payload.get("env_no", (old or {}).get("env_no", "")), 32)
            env_name = _text(payload.get("env_name", (old or {}).get("env_name", "")), 256)
            if not env_id:
                env_no = ""
                env_name = ""
            backend = _text(
                payload.get("session_backend") or ("adspower" if env_id else "local"), 16
            )
            if backend not in {"local", "adspower"}:
                raise ValueError("会话后端无效")
            if env_id:
                backend = "adspower"

            enabled = _bool(payload.get("enabled", old["enabled"] if old else True))
            try:
                interval = int(payload.get("check_interval", old["check_interval"] if old else 60))
            except (TypeError, ValueError) as exc:
                raise ValueError("检测间隔必须是整数秒") from exc
            interval = max(15, min(3600, interval))

            now = now_iso()
            new_id = account_id or uuid.uuid4().hex
            if env_id:
                with self._db() as db:
                    duplicate = db.execute(
                        "SELECT name FROM accounts WHERE env_id=? AND id<>?",
                        (env_id, new_id),
                    ).fetchone()
                if duplicate:
                    raise ValueError(f"环境已绑定账号 {duplicate['name']}")
            invalidates = bool(
                not old
                or old["platform"] != platform
                or old["base_url"] != base_url
                or old["login_path"] != login_path
                or old["username"] != username
                or old.get("env_id", "") != env_id
            )
            status = old["status"] if old and not invalidates else "idle"
            if not enabled:
                status = "disabled"
            elif old and old["enabled"] and not enabled:
                status = "disabled"
            elif old and not old["enabled"] and enabled:
                status = "idle"
            last_error = "" if invalidates else (old or {}).get("last_error", "")

            with self._db() as db:
                if old:
                    db.execute(
                        """UPDATE accounts SET platform=?, name=?, base_url=?, login_path=?,
                        dashboard_path=?, username=?, enabled=?, status=?, status_message=?,
                        last_error=?, check_interval=?, env_id=?, env_no=?, env_name=?,
                        session_backend=?, updated_at=? WHERE id=?""",
                        (
                            platform,
                            name,
                            base_url,
                            login_path,
                            dashboard_path,
                            username,
                            int(enabled),
                            status,
                            "" if invalidates else (old or {}).get("status_message", ""),
                            last_error,
                            interval,
                            env_id,
                            env_no,
                            env_name,
                            backend,
                            now,
                            new_id,
                        ),
                    )
                else:
                    db.execute(
                        """INSERT INTO accounts
                        (id, platform, name, base_url, login_path, dashboard_path,
                         username, enabled, managed_by, status, last_error,
                         check_interval, env_id, env_no, env_name, session_backend,
                         created_at, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            new_id,
                            platform,
                            name,
                            base_url,
                            login_path,
                            dashboard_path,
                            username,
                            int(enabled),
                            "",
                            status,
                            "",
                            interval,
                            env_id,
                            env_no,
                            env_name,
                            backend,
                            now,
                            now,
                        ),
                    )
                self._log(
                    db,
                    new_id,
                    "account_saved",
                    "success",
                    "账号配置已保存" if not old else "账号配置已更新",
                )

            password = payload.get("password")
            if isinstance(password, str) and password:
                self.save_credentials(
                    new_id, username, password, _text(payload.get("security_code"), 64)
                )
            elif username and self.paths(new_id)["credentials"].is_file():
                existing = self.credentials(new_id)
                if existing.get("username") != username:
                    self.save_credentials(
                        new_id,
                        username,
                        existing.get("password", ""),
                        existing.get("security_code", ""),
                    )

            if invalidates and old:
                self._clear_session_files(new_id)
            return self.get(new_id)

    def delete_account(self, account_id: str) -> None:
        account = self.get(account_id)
        if account["managed_by"] == MANAGED_BY_COLLECTOR:
            raise ValueError("采集器主账号不能从号池删除")
        with self._lock, self._db() as db:
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            db.execute("DELETE FROM account_logs WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM account_heartbeats WHERE account_id=?", (account_id,))
            self._log(db, account_id, "account_deleted", "success", "账号已删除")
        paths = self.paths(account_id)
        self._remove_account_dir(paths["dir"])

    def _remove_account_dir(self, target: Path) -> None:
        root = self.root.resolve()
        try:
            target = target.resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            return
        shutil.rmtree(target, ignore_errors=True)

    def _clear_session_files(self, account_id: str) -> None:
        paths = self.paths(account_id)
        self._remove_account_dir(paths["profile_dir"])
        for key in ("meta",):
            try:
                paths[key].unlink()
            except OSError:
                pass

    def clear_session(self, account_id: str) -> Dict[str, Any]:
        account = self.get(account_id)
        if account["managed_by"] == MANAGED_BY_COLLECTOR:
            raise ValueError("采集器主账号请在设置页使用“重新登录”")
        self._clear_session_files(account_id)
        self.set_status(account_id, "idle", "已清除保存的会话")
        self.log(account_id, "session_cleared", "success", "已清除保存的登录会话")
        return self.get(account_id)

    def set_enabled(self, account_id: str, enabled: bool) -> Dict[str, Any]:
        account = self.get(account_id)
        if account["managed_by"] == MANAGED_BY_COLLECTOR:
            raise ValueError("采集器主账号由采集器控制，不能停用")
        status = "idle" if enabled else "disabled"
        with self._lock, self._db() as db:
            db.execute(
                "UPDATE accounts SET enabled=?, status=?, status_message=?, updated_at=? WHERE id=?",
                (int(bool(enabled)), status, "", now_iso(), account_id),
            )
            self._log(
                db,
                account_id,
                "account_toggled",
                "success",
                "账号已启用" if enabled else "账号已停用",
            )
        return self.get(account_id)

    def set_status(
        self,
        account_id: str,
        status: str,
        message: Optional[str] = None,
        *,
        last_check: Optional[str] = None,
        last_login: Optional[str] = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError("状态值无效")
        assignments = ["status=?", "status_message=?", "updated_at=?"]
        values: List[Any] = [status, _text(message, 300), now_iso()]
        if status in {"error", "expired"}:
            if message is not None:
                assignments.append("last_error=?")
                values.append(_text(message, 300))
        else:
            assignments.append("last_error=?")
            values.append("")
        if last_check is not None:
            assignments.append("last_check=?")
            values.append(last_check)
        if last_login is not None:
            assignments.append("last_login=?")
            values.append(last_login)
        with self._lock, self._db() as db:
            db.execute(
                f"UPDATE accounts SET {', '.join(assignments)} WHERE id=?",
                values + [account_id],
            )

    def log(self, account_id: Optional[str], action: str, status: str, message: str) -> None:
        with self._lock, self._db() as db:
            self._log(db, account_id, action, status, message)

    def _log(self, db, account_id: Optional[str], action: str, status: str, message: str) -> None:
        db.execute(
            "INSERT INTO account_logs (account_id, action, status, message, created_at) "
            "VALUES (?,?,?,?,?)",
            (account_id, _text(action, 60), _text(status, 40), _text(message, 300), now_iso()),
        )
        db.execute(
            "DELETE FROM account_logs WHERE id NOT IN "
            "(SELECT id FROM account_logs ORDER BY id DESC LIMIT 800)"
        )

    def add_heartbeat(
        self, account_id: str, status: str, latency_ms: int = 0, message: str = ""
    ) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO account_heartbeats "
                "(account_id, status, latency_ms, message, created_at) VALUES (?,?,?,?,?)",
                (
                    account_id,
                    _text(status, 24),
                    max(0, int(latency_ms or 0)),
                    _text(message, 240),
                    now_iso(),
                ),
            )
            db.execute(
                "DELETE FROM account_heartbeats WHERE account_id=? AND id NOT IN "
                "(SELECT id FROM account_heartbeats WHERE account_id=? ORDER BY id DESC LIMIT 60)",
                (account_id, account_id),
            )

    def heartbeats(self, account_id: str, limit: int = 24) -> List[Dict[str, Any]]:
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT status, latency_ms, message, created_at FROM account_heartbeats "
                "WHERE account_id=? ORDER BY id DESC LIMIT ?",
                (account_id, max(1, min(60, limit))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def logs(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._lock, self._db() as db:
            rows = db.execute(
                "SELECT account_id, action, status, message, created_at "
                "FROM account_logs ORDER BY id DESC LIMIT ?",
                (max(1, min(200, limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> Dict[str, int]:
        accounts = self.accounts()
        return {
            "total": len(accounts),
            "online": sum(1 for item in accounts if item["status"] == "online"),
            "pending": sum(
                1
                for item in accounts
                if item["status"] in {"awaiting_login", "logging_in", "checking"}
            ),
            "error": sum(1 for item in accounts if item["status"] in {"expired", "error"}),
            "with_session": sum(1 for item in accounts if item["has_session"]),
        }
