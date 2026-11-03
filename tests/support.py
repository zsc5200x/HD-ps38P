"""Test configuration that never reads personal configuration or runtime data."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from ps38app.config import DEFAULTS


def upcoming_today(now=None):
    """Keep test fixtures ahead of now without crossing Beijing midnight."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(timezone(timedelta(hours=8)))
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return now + min(timedelta(hours=2), (midnight - now) / 2)


def isolated_config(root: Path) -> Dict[str, Any]:
    config = deepcopy(DEFAULTS)
    config.update(
        host="127.0.0.1",
        port=0,
        db_path=root / "orders.sqlite3",
        follow_db_path=root / "follow.sqlite3",
        profile_dir=root / "profile",
        pool_db_path=root / "accounts.sqlite3",
        pool_dir=root / "accounts",
        merge_db_path=root / "fixture_merge.sqlite3",
        merge_seed_path="",
        pinnacle_snapshot_url="",
        pinnacle_catalog_url="",
        verbose=False,
    )
    return config
