"""把皇冠会员历史注单同步到本地报表库。

用法：
    .venv/bin/python scripts/sync_crown_reports.py
    .venv/bin/python scripts/sync_crown_reports.py --days 8
"""

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ps38app.config import load_config  # noqa: E402
from ps38app.crown_reports import CrownReportStore, report_db_path, sync_account  # noqa: E402
from ps38app.follow.crown import CrownAdapter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="同步皇冠会员历史注单报表")
    parser.add_argument("--days", type=int, default=8, help="回填最近 N 个皇冠日期")
    parser.add_argument("--account", default="", help="只同步指定账号 ID")
    args = parser.parse_args()

    config = load_config()
    store = CrownReportStore(report_db_path(config))
    adapter = CrownAdapter(config)

    database = Path(config["pool_db_path"]).resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        accounts = [
            dict(row)
            for row in connection.execute(
                "SELECT id,name,status FROM accounts WHERE platform='crown_member' AND enabled=1"
            )
        ]

    print(f"[report] 数据库：{store.path}")
    for account in accounts:
        if args.account and account["id"] != args.account:
            continue
        print(f"[report] {account['name']} ({account['status']}) ...", flush=True)
        stats = sync_account(adapter, store, account["id"], days=max(1, args.days))
        if stats["error"]:
            print(f"  失败：{stats['error']}", flush=True)
        else:
            print(
                f"  新增 {stats['inserted']}，更新 {stats['updated']}，"
                f"未变 {stats['skipped']}，日期 {len(stats['dates'])} 天",
                flush=True,
            )

    print("[report] 汇总：")
    for row in store.summary():
        print(
            f"  {row['account_id'][:8]} {row['settle_date']} "
            f"{row['orders']} 单 本金 {row['stake']:.2f} 输赢 {row['win_loss']:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
