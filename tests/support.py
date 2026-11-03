"""Test configuration that never reads personal configuration or runtime data."""

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from ps38app.config import DEFAULTS


def isolated_config(root: Path) -> Dict[str, Any]:
    config = deepcopy(DEFAULTS)
    config.update(
        host="127.0.0.1",
        port=0,
        db_path=root / "orders.sqlite3",
        profile_dir=root / "profile",
        pool_db_path=root / "accounts.sqlite3",
        pool_dir=root / "accounts",
        merge_db_path=root / "fixture_merge.sqlite3",
        merge_seed_path="",
        pinnacle_snapshot_url="",
        verbose=False,
    )
    return config
