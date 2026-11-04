"""主页「跟单视角」合并单的 8 位单号。

一个合并批次（同场、同盘口、同线、同比分、同一个 20 秒窗口）首次被展示时
分配一个 8 位号码，之后无论刷新多少次都返回同一个号。号码由 SQLite 的
唯一约束保证不重复，``group_key`` 则是这批源单的可追溯键：拿号码回查
``copy_order_ids`` 就能定位到具体批次。

号码与批次绑定写在本表里，不用「每次随机」的写法，避免同一批订单在页面上
来回变成不同的号。分配失败时返回空字符串，页面按「—」展示，不阻断列表。
"""

from __future__ import annotations

import random
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Dict

BEIJING = timezone(timedelta(hours=8))
MIN_NO = 10_000_000
MAX_NO = 99_999_999
MAX_ATTEMPTS = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS copy_order_ids (
    group_key TEXT PRIMARY KEY,
    order_no TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
"""


def _stamp() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def _allocate(connection: sqlite3.Connection, group_key: str) -> str:
    """给一个批次分配未占用的号码；并发时返回别人已登记的号。"""
    created_at = _stamp()
    for _ in range(MAX_ATTEMPTS):
        candidate = str(random.randint(MIN_NO, MAX_NO))
        try:
            connection.execute(
                "INSERT INTO copy_order_ids(group_key, order_no, created_at) VALUES (?,?,?)",
                (group_key, candidate, created_at),
            )
            return candidate
        except sqlite3.IntegrityError:
            # 主键冲突说明别的请求刚登记了这个批次；唯一冲突说明号码撞了。
            existing = connection.execute(
                "SELECT order_no FROM copy_order_ids WHERE group_key=?", (group_key,)
            ).fetchone()
            if existing:
                return str(existing[0])
    return ""


def assign_numbers(path: str | Path, group_keys: Iterable[str]) -> Dict[str, str]:
    """把批次键映射到 8 位单号；没登记过的即时分配并持久化。"""
    keys = [str(key) for key in group_keys if key]
    if not keys:
        return {}
    unique = list(dict.fromkeys(keys))
    numbers: Dict[str, str] = {}
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(target, timeout=5)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            marks = ",".join("?" for _ in unique)
            for group_key, order_no in connection.execute(
                f"SELECT group_key, order_no FROM copy_order_ids WHERE group_key IN ({marks})",
                unique,
            ):
                numbers[str(group_key)] = str(order_no)
            for group_key in unique:
                if group_key in numbers:
                    continue
                allocated = _allocate(connection, group_key)
                if allocated:
                    numbers[group_key] = allocated
            connection.commit()
    except (sqlite3.Error, OSError):
        # 号码只是展示用的标识，登记库不可用时不影响列表本身。
        return numbers
    return numbers


def lookup(path: str | Path, order_no: str) -> str | None:
    """按 8 位单号回查批次键，用于追溯合并来源。"""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        with closing(sqlite3.connect(target, timeout=5)) as connection:
            row = connection.execute(
                "SELECT group_key FROM copy_order_ids WHERE order_no=?", (str(order_no),)
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None
