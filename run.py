#!/usr/bin/env python3
"""PS38 赛事独立版 — 启动、补历史、导入旧库。

用法：
    python run.py                # 启动看板与后台订单采集（默认）
    python run.py serve          # 同上
    python run.py login          # 只登录并验证会话
    python run.py backfill       # 登录后补采 history_from 至今的已结算订单
    python run.py status         # 查看本地订单库概况（不启动浏览器）
    python run.py import-db 路径  # 导入旧 partner_orders.sqlite3
    python run.py doctor         # 检查运行环境
    python run.py install-browser # 安装 Playwright Chromium
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from ps38app.config import PROJECT_DIR, config_path, load_config
from ps38app.store import OrderStore, open_readonly, query_summary

VERSION = "1.0.0"


def _ensure_config_file() -> None:
    """Create config.json from the example on first run (not an explicit env path)."""
    if os.environ.get("PS38_CONFIG"):
        return
    path = config_path()
    if path.exists():
        return
    example = PROJECT_DIR / "config.example.json"
    if example.is_file():
        try:
            shutil.copyfile(example, path)
            print(f"[config] 已生成 {path}（可编辑账号、代理等）", flush=True)
        except OSError as exc:
            print(f"[warn] 无法生成 config.json：{exc}；将使用内置默认值", flush=True)


def _print_config(config: Dict[str, Any]) -> None:
    print(f"[config] {config_path()}", flush=True)
    print(f"[config] 数据库：{config['db_path']}", flush=True)
    print(f"[config] 浏览器目录：{config['profile_dir']}", flush=True)
    print(f"[config] 代理：{config['agent_codes'] or '自动发现在线代理'}", flush=True)
    print(f"[config] 号池库：{config.get('pool_db_path', '')}", flush=True)


def cmd_serve(args: argparse.Namespace) -> int:
    from ps38app.account_pool.service import AccountPoolService
    from ps38app.poller import Poller
    from ps38app.server import build_server

    config = load_config()
    config["verbose"] = bool(args.verbose)
    _print_config(config)
    if config["headless"] and not config.get("username"):
        print("[warn] headless=true 且没有配置账号密码：会话失效后无法在窗口里手动登录", flush=True)

    poller = Poller(config, debug=bool(args.debug))

    pool = None
    try:
        pool = AccountPoolService(config, collector=poller)
    except Exception as exc:  # noqa: BLE001 - the collector must still start
        print(f"[warn] 账号池初始化失败：{exc}；号池页面不可用", flush=True)

    # Bind first so a busy port fails before opening a browser window.
    try:
        server = build_server(config, poller, pool)
    except OSError as exc:
        print(f"[fatal] 无法监听 {config['host']}:{config['port']}：{exc}", flush=True)
        print("[hint] 端口被占用时可用 PS38_PORT=8898 python run.py 换一个端口", flush=True)
        return 1
    poller.start()
    if pool is not None:
        pool.start()

    stopping = False

    def shutdown(signum=None, frame=None):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print("\n[app] 正在停止…", flush=True)
        if pool is not None:
            pool.stop()
        poller.stop()
        # shutdown() must run outside the serve_forever thread.
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print(f"[app] 打开 http://{config['host']}:{config['port']}/", flush=True)
    print("[app] 首次启动会打开浏览器；请完成登录/2FA，之后会话由本程序自行保持", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        shutdown()
        server.server_close()
        if pool is not None:
            pool.stop()
        poller.join(timeout=8)
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from ps38app.session import PartnerSession

    config = load_config()
    session = PartnerSession(config, debug=bool(args.debug))
    try:
        profile = session.login(
            force=bool(args.force),
            on_status=lambda message: print(f"[login] {message}", flush=True),
        )
        print(f"[login] 会话有效：{profile['user_code']}（userId={profile['user_id']}）", flush=True)
        return 0
    finally:
        session.close()


def cmd_backfill(args: argparse.Namespace) -> int:
    from ps38app.poller import Poller

    config = load_config()
    poller = Poller(config, debug=bool(args.debug))
    try:
        result = poller.backfill_blocking(
            on_progress=lambda state: print(
                f"[backfill] {state['done']}/{state['total']} {state['message']}", flush=True
            )
        )
        print(f"[backfill] 完成：{result.get('message') or '无'}；共 {result.get('done', 0)} 批", flush=True)
        return 0
    finally:
        poller._close_session()  # noqa: SLF001 - deliberate one-shot shutdown
        if poller.store is not None:
            poller.store.close()


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    path = Path(config["db_path"])
    print(f"[status] 版本 {VERSION}", flush=True)
    print(f"[status] 数据库：{path}（{'存在' if path.exists() else '尚未创建'}）", flush=True)
    connection = open_readonly(path)
    if connection is None:
        print("[status] 还没有订单数据；启动程序并完成登录后会自动创建", flush=True)
        return 0
    try:
        summary = query_summary(connection)
        agents = [dict(row) for row in connection.execute(
            "SELECT code, user_id, name, resolved_at FROM partner_agents ORDER BY code"
        )]
        states = [dict(row) for row in connection.execute(
            "SELECT agent_code, snapshot_type, last_success_at, record_count "
            "FROM partner_poll_state ORDER BY agent_code, snapshot_type"
        )]
    finally:
        connection.close()
    print(
        f"[status] 订单 {summary['orders']} 单：未结算 {summary['open']}，已结算 {summary['settled']}；"
        f"投注总量 {summary['total_stake']:.2f}，已结算输赢 {summary['net']:+.2f}",
        flush=True,
    )
    print(f"[status] 代理：{', '.join(item['code'] for item in agents) or '无'}", flush=True)
    for state in states:
        print(
            f"[status]   {state['agent_code']} {state['snapshot_type']} "
            f"最近成功 {state['last_success_at']}（{state['record_count']} 条）",
            flush=True,
        )
    return 0


def cmd_import_db(args: argparse.Namespace) -> int:
    config = load_config()
    source = Path(args.path).expanduser()
    if not source.is_file():
        print(f"[import] 找不到文件：{source}", flush=True)
        return 1
    target = Path(config["db_path"])
    if source.resolve() == target.resolve():
        print("[import] 源文件和目标库是同一个文件，无需导入", flush=True)
        return 1
    if target.exists() and target.stat().st_size > 0 and not args.force:
        print(f"[import] 目标库已存在：{target}", flush=True)
        print("[import] 如需覆盖请加 --force；导入前建议先备份", flush=True)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    for sidecar in (Path(str(target) + "-wal"), Path(str(target) + "-shm")):
        try:
            sidecar.unlink()
        except OSError:
            pass
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
    except sqlite3.Error as exc:
        print(f"[import] 复制失败：{exc}", flush=True)
        return 1
    store = OrderStore(target)  # ensure schema / migrations
    count = store.counts()
    store.close()
    print(f"[import] 已导入 {count['total']} 单（未结算 {count['open']}，已结算 {count['settled']}）→ {target}", flush=True)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(f"[doctor] Python {sys.version.split()[0]}", flush=True)
    print(f"[doctor] 项目目录：{PROJECT_DIR}", flush=True)
    try:
        import playwright  # noqa: F401
        print("[doctor] playwright 已安装", flush=True)
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[doctor] playwright 未安装：{exc}", flush=True)
        print("[doctor] 请运行：python -m pip install -r requirements.txt", flush=True)
    try:
        config = load_config()
        print(f"[doctor] 配置：{config_path()}", flush=True)
        print(f"[doctor] 数据库：{config['db_path']}", flush=True)
        print(f"[doctor] 端口：{config['host']}:{config['port']}", flush=True)
        print(f"[doctor] 账号密码：{'已配置' if config.get('username') and config.get('password') else '未配置（浏览器手动登录）'}", flush=True)
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[doctor] 配置错误：{exc}", flush=True)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
            exists = Path(path).exists()
            print(f"[doctor] Chromium：{path}（{'可用' if exists else '未下载'}）", flush=True)
            if not exists:
                ok = False
                print("[doctor] 请运行：python run.py install-browser", flush=True)
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[doctor] 浏览器检查失败：{exc}", flush=True)
    print("[doctor] 结果：" + ("通过" if ok else "需要处理上面的项目"), flush=True)
    return 0 if ok else 1


def cmd_install_browser(args: argparse.Namespace) -> int:
    print("[install] 正在安装 Playwright Chromium…", flush=True)
    result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PS38 赛事独立版（仅后台订单拉取，无跟单）")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "login", "backfill", "status", "import-db", "doctor", "install-browser"])
    parser.add_argument("path", nargs="?", help="import-db 的源数据库路径")
    parser.add_argument("--force", action="store_true", help="login 强制清 Cookie / import-db 覆盖目标")
    parser.add_argument("--debug", action="store_true", help="输出详细错误")
    parser.add_argument("--verbose", action="store_true", help="打印每个 HTTP 请求")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    _ensure_config_file()
    handlers = {
        "serve": cmd_serve,
        "login": cmd_login,
        "backfill": cmd_backfill,
        "status": cmd_status,
        "import-db": cmd_import_db,
        "doctor": cmd_doctor,
        "install-browser": cmd_install_browser,
    }
    handler = handlers[args.command]
    if args.command == "import-db" and not args.path:
        print("用法：python run.py import-db <旧 partner_orders.sqlite3 路径> [--force]", flush=True)
        return 2
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI should print, not traceback
        if args.debug:
            raise
        print(f"[fatal] {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
