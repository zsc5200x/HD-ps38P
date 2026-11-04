"""接口监控：定期探测入口域名/地址的可达性，供号池页面展示。

只做 TLS/TCP 连通性探测——不携带任何会员会话，也不会产生业务请求。
"""

from __future__ import annotations

import ipaddress
import json
import socket
import sqlite3
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

# 默认监控列表：皇冠入口（含备用域名）与 PS38 采集器后端。
DEFAULT_ENDPOINTS: List[str] = [
    "https://hga030.com",
    "https://hga038.com",
    "https://hga050.com",
    "https://hga027.com",
    "https://hga039.com",
    "https://mos011.com",
    "https://mos022.com",
    "https://mos100.com",
    "https://mos055.com",
    "https://mos077.com",
    "https://134.159.80.63",
    "https://205.201.0.120",
]

BEATS_KEPT = 24


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _port_of(parsed) -> int:
    if parsed.port:
        return int(parsed.port)
    return 443 if parsed.scheme == "https" else 80


def probe(url: str, timeout: float = 8.0) -> Dict[str, Any]:
    """探测单个入口，返回 {url, ok, latency_ms, error, checked_at}。

    判定标准是能完成 TLS 握手（https）或 TCP 连接（http）；证书不做校验，
    因为站点的真实后端地址不通配每个门面域名。
    """
    started = time.perf_counter()

    def result(ok: bool, error: str = "") -> Dict[str, Any]:
        return {
            "url": url,
            "ok": ok,
            "latency_ms": int(max(0.0, (time.perf_counter() - started) * 1000.0)),
            "error": error[:120],
            "checked_at": _now_iso(),
        }

    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").strip()
        if not host or parsed.scheme not in {"http", "https"}:
            return result(False, "地址无效")
        raw = socket.create_connection((host, _port_of(parsed)), timeout=timeout)
    except socket.timeout:
        return result(False, "连接超时")
    except OSError as exc:
        return result(False, type(exc).__name__)
    try:
        if parsed.scheme != "https":
            return result(True)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with context.wrap_socket(raw, server_hostname=None if _is_ip(host) else host):
            return result(True)
    except ssl.SSLError as exc:
        return result(False, f"TLS: {type(exc).__name__}")
    except OSError as exc:
        return result(False, type(exc).__name__)
    finally:
        try:
            raw.close()
        except OSError:
            pass


class MonitorStore:
    """接口监控的 SQLite 存储，只保留每个入口最近若干次心跳。"""

    def __init__(self, path, endpoints: Optional[List[str]] = None, keep: int = BEATS_KEPT):
        self.path = Path(path)
        self.keep = max(4, int(keep))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.ensure_endpoints(endpoints or [])

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_endpoints (
                    url TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_beats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS monitor_beats_url ON monitor_beats(url, id DESC)"
            )

    def ensure_endpoints(self, urls: List[str]) -> None:
        now = _now_iso()
        with self._connect() as connection:
            for url in urls:
                connection.execute(
                    "INSERT OR IGNORE INTO monitor_endpoints(url, added_at) VALUES (?, ?)",
                    (str(url), now),
                )

    def endpoints(self) -> List[str]:
        with self._connect() as connection:
            return [
                row["url"]
                for row in connection.execute(
                    "SELECT url FROM monitor_endpoints ORDER BY added_at, url"
                )
            ]

    def record(self, result: Dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO monitor_beats(url, checked_at, ok, latency_ms, error) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    result["url"],
                    result["checked_at"],
                    1 if result.get("ok") else 0,
                    int(result.get("latency_ms") or 0),
                    str(result.get("error") or ""),
                ),
            )
            connection.execute(
                "DELETE FROM monitor_beats WHERE url=? AND id NOT IN "
                "(SELECT id FROM monitor_beats WHERE url=? ORDER BY id DESC LIMIT ?)",
                (result["url"], result["url"], self.keep),
            )

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            endpoints = [
                row["url"]
                for row in connection.execute(
                    "SELECT url FROM monitor_endpoints ORDER BY added_at, url"
                )
            ]
            rows = connection.execute(
                "SELECT url, checked_at, ok, latency_ms, error FROM monitor_beats ORDER BY id DESC"
            ).fetchall()
        beats: Dict[str, List[Dict[str, Any]]] = {url: [] for url in endpoints}
        for row in rows:
            bucket = beats.get(row["url"])
            if bucket is None or len(bucket) >= self.keep:
                continue
            bucket.append(
                {
                    "at": row["checked_at"],
                    "ok": bool(row["ok"]),
                    "latency": int(row["latency_ms"]),
                    "error": row["error"],
                }
            )
        result = []
        for url in endpoints:
            series = beats.get(url) or []
            total = len(series)
            passed = sum(1 for beat in series if beat["ok"])
            last = series[0] if series else None
            result.append(
                {
                    "url": url,
                    "host": urlsplit(url).hostname or url,
                    "beats": series,
                    "uptime": round(passed / total * 100.0, 1) if total else 0.0,
                    "samples": total,
                    "status": "online" if last and last["ok"] else "down" if last else "unknown",
                    "latency": last["latency"] if last else None,
                    "last_checked": last["at"] if last else "",
                    "error": last["error"] if last else "",
                }
            )
        return result


class MonitorService:
    """后台定期探测；可在测试中直接调用 run_once。"""

    def __init__(self, config: Dict[str, Any]):
        endpoints = config.get("monitor_endpoints") or DEFAULT_ENDPOINTS
        self.store = MonitorStore(
            config.get("monitor_db_path") or "data/monitor.sqlite3",
            endpoints=list(endpoints),
            keep=int(config.get("monitor_beats") or BEATS_KEPT),
        )
        self.interval = max(5.0, float(config.get("monitor_interval_seconds") or 30))
        self.timeout = max(1.0, float(config.get("monitor_timeout_seconds") or 8))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._round: Optional[threading.Thread] = None

    def run_once(self) -> List[Dict[str, Any]]:
        urls = self.store.endpoints()
        if not urls:
            return self.store.snapshot()
        with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
            for result in pool.map(lambda item: probe(item, self.timeout), urls):
                try:
                    self.store.record(result)
                except sqlite3.Error:
                    continue
        return self.store.snapshot()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - 监控失败不能影响主服务
                pass
            self._stop.wait(self.interval)

    def start(self) -> None:
        with self._lock:
            if any(thread and thread.is_alive() for thread in (self._thread, self._round)):
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="ps38-monitor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            threads = (self._thread, self._round)
        deadline = time.monotonic() + 3
        for thread in threads:
            if thread and thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline - time.monotonic()))
        with self._lock:
            if self._thread is threads[0] and self._thread and not self._thread.is_alive():
                self._thread = None
            if self._round is threads[1] and self._round and not self._round.is_alive():
                self._round = None

    def refresh_async(self) -> bool:
        """手动触发一轮检测；已有检测在跑时不重复启动。"""
        with self._lock:
            if self._stop.is_set() or (self._round and self._round.is_alive()):
                return False
            self._round = threading.Thread(
                target=self.run_once, name="ps38-monitor-now", daemon=True
            )
            self._round.start()
            return True

    def close(self) -> None:
        self.stop()


def load_endpoints(config: Dict[str, Any]) -> List[str]:
    endpoints = config.get("monitor_endpoints")
    if isinstance(endpoints, str):
        try:
            parsed = json.loads(endpoints)
        except ValueError:
            parsed = [endpoints]
        endpoints = parsed if isinstance(parsed, list) else []
    if not endpoints:
        endpoints = DEFAULT_ENDPOINTS
    return [str(item).strip().rstrip("/") for item in endpoints if str(item).strip()]


def fastest_endpoint(config: Dict[str, Any]) -> str:
    """返回当前延迟最低的在线入口；没有可用数据时回落到默认列表。"""
    try:
        store = MonitorStore(config.get("monitor_db_path") or "data/monitor.sqlite3")
        items = store.snapshot()
    except Exception:  # noqa: BLE001 - 监控库不可用不应阻塞账号操作
        items = []
    online = [
        item for item in items if item["status"] == "online" and item.get("latency") is not None
    ]
    if not online:
        return ""
    # 同延迟时优先域名入口：裸 IP 直连更容易被重置。
    online.sort(key=lambda item: (item["latency"], _is_ip(item["host"])))
    return str(online[0]["url"]).rstrip("/")


def endpoints_by_latency(config: Dict[str, Any], limit: int = 5) -> List[str]:
    """按延迟从低到高返回在线入口，供失败重试时轮换域名。"""
    try:
        store = MonitorStore(config.get("monitor_db_path") or "data/monitor.sqlite3")
        items = [item for item in store.snapshot() if item["status"] == "online"]
    except Exception:  # noqa: BLE001 - 监控库不可用时不轮换域名
        return []
    items.sort(key=lambda item: item.get("latency") or 1 << 30)
    return [str(item["url"]).rstrip("/") for item in items[: max(1, limit)]]
