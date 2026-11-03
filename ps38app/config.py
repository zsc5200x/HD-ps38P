"""Standalone configuration for the PS38 events dashboard.

Priority: environment variables > config.json > built-in defaults.
All relative paths are resolved against the project directory (the folder that
contains this package), never against the shell's current directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"

DEFAULTS: Dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8899,
    "db_path": "data/orders.sqlite3",
    "profile_dir": "data/profile",
    "base_url": "https://www.ps38ag.com",
    "login_path": "/partner/zh-cn/login",
    "dashboard_path": "/partner-ui-v2/report-outstanding-v2",
    "username": "",
    "password": "",
    "agent_codes": [],
    "history_from": "2026-07-01",
    "open_interval_seconds": 1.0,
    "settled_interval_seconds": 300.0,
    "settled_window_days": 7,
    "page_size": 200,
    "headless": False,
    "browser_channel": "",
    "minimize_after_login": False,
    "backfill_on_first_run": True,
    # Account pool: PS38 backend, Crown backend and Crown member sessions.
    "pool_db_path": "data/accounts.sqlite3",
    "pool_dir": "data/accounts",
    "pool_check_interval_seconds": 60,
    "pool_login_timeout_seconds": 300,
    "pool_check_timeout_seconds": 10,
    "pool_crown_ignore_https_errors": True,
    # Optional Crown feed used by the 赛事合并 page. The rest of the dashboard
    # keeps working when it is not running.
    "crown_host": "127.0.0.1",
    "crown_port": 9223,
    "crown_utc_offset_minutes": -240,
    "merge_db_path": "data/fixture_merge.sqlite3",
    "merge_seed_path": "ps38app/league_library_seed.tsv",
    "pinnacle_snapshot_url": "http://127.0.0.1:8787/api/snapshot",
    "merge_horizon_hours": 72.0,
    "merge_past_hours": 24.0,
    "merge_excluded_leagues": [],
}

ENV_OVERRIDES = {
    "host": "PS38_HOST",
    "port": "PS38_PORT",
    "db_path": "PS38_DB_PATH",
    "profile_dir": "PS38_PROFILE_DIR",
    "base_url": "PS38_BASE_URL",
    "dashboard_path": "PS38_DASHBOARD_PATH",
    "username": "PS38_USERNAME",
    "password": "PS38_PASSWORD",
    "agent_codes": "PS38_AGENTS",
    "history_from": "PS38_HISTORY_FROM",
    "open_interval_seconds": "PS38_OPEN_INTERVAL",
    "settled_interval_seconds": "PS38_SETTLED_INTERVAL",
    "page_size": "PS38_PAGE_SIZE",
    "pool_db_path": "PS38_POOL_DB",
    "pool_dir": "PS38_POOL_DIR",
    "pool_check_interval_seconds": "PS38_POOL_CHECK_INTERVAL",
    "pool_login_timeout_seconds": "PS38_POOL_LOGIN_TIMEOUT",
    "headless": "PS38_HEADLESS",
    "crown_host": "PS38_CROWN_HOST",
    "crown_port": "PS38_CROWN_PORT",
    "crown_utc_offset_minutes": "PS38_CROWN_OFFSET",
    "merge_db_path": "PS38_MERGE_DB",
    "merge_seed_path": "PS38_MERGE_SEED",
    "pinnacle_snapshot_url": "PS38_PINNACLE_SNAPSHOT",
    "merge_horizon_hours": "PS38_MERGE_HORIZON",
    "merge_past_hours": "PS38_MERGE_PAST",
}


def config_path() -> Path:
    raw = os.environ.get("PS38_CONFIG") or DEFAULT_CONFIG_PATH
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: Any, key: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"配置 {key} 必须是数字") from exc
    if not (number > 0):
        raise RuntimeError(f"配置 {key} 必须大于 0")
    return number


def _as_agents(value: Any) -> List[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise RuntimeError("agent_codes 必须是字符串数组")
    return [str(item).strip() for item in value if str(item).strip()]


def resolve_path(value: Any, fallback: str) -> Path:
    raw = str(value or fallback).strip()
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def load_config() -> Dict[str, Any]:
    config = dict(DEFAULTS)
    path = config_path()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"无法读取配置文件 {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError(f"配置文件 {path} 顶层必须是 JSON 对象")
        config.update(loaded)
    elif os.environ.get("PS38_CONFIG"):
        raise RuntimeError(f"指定的配置文件不存在：{path}")

    for key, env_name in ENV_OVERRIDES.items():
        if os.environ.get(env_name):
            config[key] = os.environ[env_name]

    try:
        config["port"] = int(config["port"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("配置 port 必须是 1-65535 的端口号") from exc
    if not 1 <= config["port"] <= 65535:
        raise RuntimeError("配置 port 必须是 1-65535 的端口号")

    host = str(config.get("host") or "").strip()
    if not host or any(char.isspace() for char in host) or "/" in host:
        raise RuntimeError("配置 host 必须是有效主机名或 IP 地址")
    config["host"] = host

    base = str(config.get("base_url") or "").strip().rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("配置 base_url 必须是 https 地址")
    config["base_url"] = base
    config["login_path"] = "/" + str(config.get("login_path") or "").strip().lstrip("/")
    config["dashboard_path"] = "/" + str(config.get("dashboard_path") or "").strip().lstrip("/")

    config["agent_codes"] = _as_agents(config.get("agent_codes") or [])
    config["history_from"] = str(config.get("history_from") or "2026-07-01")
    try:
        from datetime import date as _date
        _date.fromisoformat(config["history_from"])
    except ValueError as exc:
        raise RuntimeError("配置 history_from 必须是 YYYY-MM-DD 日期") from exc

    config["open_interval_seconds"] = max(1.0, _as_float(config.get("open_interval_seconds"), "open_interval_seconds"))
    config["settled_interval_seconds"] = max(30.0, _as_float(config.get("settled_interval_seconds"), "settled_interval_seconds"))
    config["settled_window_days"] = max(1, int(config.get("settled_window_days") or 7))
    config["page_size"] = max(20, min(500, int(config.get("page_size") or 200)))

    config["headless"] = _as_bool(config.get("headless", False))
    config["pool_crown_ignore_https_errors"] = _as_bool(config.get("pool_crown_ignore_https_errors", True))
    config["minimize_after_login"] = _as_bool(config.get("minimize_after_login", False))
    config["backfill_on_first_run"] = _as_bool(config.get("backfill_on_first_run", True))
    channel = str(config.get("browser_channel") or "").strip()
    if channel not in ("", "chromium", "chrome", "msedge"):
        raise RuntimeError("browser_channel 可为空或 chromium/chrome/msedge")
    config["browser_channel"] = channel

    config["db_path"] = resolve_path(config.get("db_path"), DEFAULTS["db_path"])
    config["profile_dir"] = resolve_path(config.get("profile_dir"), DEFAULTS["profile_dir"])

    config["crown_host"] = str(config.get("crown_host") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        config["crown_port"] = int(config.get("crown_port") or 9223)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("配置 crown_port 必须是端口号") from exc
    if not 1 <= config["crown_port"] <= 65535:
        raise RuntimeError("配置 crown_port 必须是 1-65535 的端口号")
    try:
        config["crown_utc_offset_minutes"] = int(config.get("crown_utc_offset_minutes", -240))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("配置 crown_utc_offset_minutes 必须是整数") from exc
    config["crown_utc_offset_minutes"] = max(-720, min(840, config["crown_utc_offset_minutes"]))
    config["merge_db_path"] = resolve_path(
        config.get("merge_db_path"), DEFAULTS["merge_db_path"]
    )
    seed_raw = str(config.get("merge_seed_path") or "").strip()
    config["merge_seed_path"] = (
        str(resolve_path(seed_raw, DEFAULTS["merge_seed_path"])) if seed_raw else ""
    )
    config["pinnacle_snapshot_url"] = str(
        config.get("pinnacle_snapshot_url") or DEFAULTS["pinnacle_snapshot_url"]
    ).strip()
    try:
        config["merge_horizon_hours"] = float(config.get("merge_horizon_hours") or 72.0)
        config["merge_past_hours"] = float(config.get("merge_past_hours") or 24.0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("merge_horizon_hours / merge_past_hours 必须是数字") from exc
    config["merge_horizon_hours"] = max(1.0, min(168.0, config["merge_horizon_hours"]))
    config["merge_past_hours"] = max(0.0, min(168.0, config["merge_past_hours"]))
    excluded = config.get("merge_excluded_leagues") or []
    if isinstance(excluded, str):
        excluded = excluded.split(",")
    config["merge_excluded_leagues"] = [
        str(item).strip() for item in excluded if str(item).strip()
    ] if isinstance(excluded, list) else []

    config["pool_db_path"] = resolve_path(config.get("pool_db_path"),
                                          DEFAULTS["pool_db_path"])
    config["pool_dir"] = resolve_path(config.get("pool_dir"), DEFAULTS["pool_dir"])
    try:
        config["pool_check_interval_seconds"] = int(config.get("pool_check_interval_seconds") or 60)
        config["pool_login_timeout_seconds"] = int(config.get("pool_login_timeout_seconds") or 300)
        config["pool_check_timeout_seconds"] = int(config.get("pool_check_timeout_seconds") or 10)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("pool 检测/登录超时必须是整数秒") from exc
    config["pool_check_interval_seconds"] = max(15, min(3600, config["pool_check_interval_seconds"]))
    config["pool_login_timeout_seconds"] = max(60, min(1800, config["pool_login_timeout_seconds"]))
    config["pool_check_timeout_seconds"] = max(3, min(60, config["pool_check_timeout_seconds"]))
    return config


def public_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Config snapshot safe to expose through the HTTP API."""
    return {
        "host": config["host"],
        "port": config["port"],
        "db_path": str(config["db_path"]),
        "base_url": config["base_url"],
        "agent_codes": list(config["agent_codes"]),
        "history_from": config["history_from"],
        "open_interval_seconds": config["open_interval_seconds"],
        "settled_interval_seconds": config["settled_interval_seconds"],
        "settled_window_days": config["settled_window_days"],
        "has_credentials": bool(config.get("username") and config.get("password")),
        "headless": config["headless"],
        "pool_db_path": str(config.get("pool_db_path", "")),
        "pool_dir": str(config.get("pool_dir", "")),
        "pool_check_interval_seconds": config.get("pool_check_interval_seconds", 60),
        "pool_login_timeout_seconds": config.get("pool_login_timeout_seconds", 300),
        "crown_host": config.get("crown_host", ""),
        "crown_port": config.get("crown_port", 9223),
        "crown_utc_offset_minutes": config.get("crown_utc_offset_minutes", -240),
        "merge_db_path": str(config.get("merge_db_path", "")),
        "merge_seed_path": str(config.get("merge_seed_path", "")),
        "pinnacle_snapshot_url": str(config.get("pinnacle_snapshot_url", "")),
        "merge_horizon_hours": config.get("merge_horizon_hours", 72.0),
        "merge_past_hours": config.get("merge_past_hours", 24.0),
        "merge_excluded_leagues": list(config.get("merge_excluded_leagues") or []),
    }
