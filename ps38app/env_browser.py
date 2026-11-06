"""Headless AdsPower browser helper for Crown member sessions.

Only the browser environment is used: real fingerprint, proxy and cookies.
The caller owns login/heartbeat logic; this class just opens a CDP connection
and closes the environment if it started it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any, Dict, Optional

try:  # Playwright is an optional runtime dependency
    from playwright.sync_api import sync_playwright  # type: ignore
except Exception:  # pragma: no cover
    sync_playwright = None  # type: ignore

from .adspower import AdsPowerClient


class EnvBrowser:
    def __init__(self, config: Dict[str, Any], env_id: str) -> None:
        self.config = config
        self.env_id = str(env_id or "").strip()
        self.client = AdsPowerClient(config)
        self.headless = bool(config.get("adspower_headless", True))
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self._owned = False

    def open(self):
        if self.context is not None:
            return self.context, self.page
        if sync_playwright is None:
            raise RuntimeError("未安装 Playwright，无法连接 AdsPower 环境")
        self._pw = sync_playwright().start()
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                active = str(self.client.active(self.env_id).get("status") or "") == "Active"
                self._owned = self._owned or not active
                data = self.client.start(
                    self.env_id,
                    headless="1" if self.headless else "0",
                    last_opened_tabs="0",
                )
                ws = str((data.get("ws") or {}).get("puppeteer") or "")
                if not ws:
                    raise RuntimeError("AdsPower 未返回 CDP 地址")
                self.browser = self._pw.chromium.connect_over_cdp(ws)
                contexts = list(self.browser.contexts)
                self.context = contexts[0] if contexts else self.browser.new_context()
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                return self.context, self.page
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if self.browser is not None:
                    try:
                        self.browser.close()
                    except Exception:
                        pass
                self.browser = None
                self.context = None
                self.page = None
                if attempt == 0:
                    # AdsPower 偶尔留下僵尸浏览器占着 profile 锁，
                    # 表现就是 "Failed to start browser"；清掉再试一次。
                    if "Failed to start browser" in str(exc):
                        self._clear_stale_browser()
                    time.sleep(0.8)
                    continue
                self.close()
                raise RuntimeError(
                    f"AdsPower 环境启动失败：{' '.join(str(exc).split())[:180]}"
                ) from exc
        raise RuntimeError("AdsPower 环境启动失败") from last_error

    def _clear_stale_browser(self) -> bool:
        """结束该环境残留的浏览器进程（否则 profile 被锁住，启动必失败）。"""
        if not self.env_id:
            return False
        try:
            found = subprocess.run(
                ["pgrep", "-f", f"source/cache/{self.env_id}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        pids = [pid for pid in found.stdout.split() if pid.strip().isdigit()]
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (OSError, ValueError):
                continue
        if pids:
            time.sleep(1.5)
        return bool(pids)

    def close(self) -> None:
        try:
            if self.browser is not None:
                self.browser.close()
        except Exception:
            pass
        finally:
            self.browser = None
            self.context = None
            self.page = None
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
                self._pw = None
            if self._owned:
                try:
                    self.client.stop(self.env_id)
                except Exception:
                    pass
            self._owned = False
