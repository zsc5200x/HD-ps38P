"""Account pool supervisor: login jobs, heartbeat checks and public snapshots."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..monitor import fastest_endpoint
from .providers import ProviderError, provider_for
from .store import (
    AccountStore,
    MANAGED_BY_COLLECTOR,
    PRIMARY_ID,
    now_iso,
)

_STATUS_FROM_COLLECTOR = {
    "online": "online",
    "login": "awaiting_login",
    "error": "error",
    "stopped": "disabled",
    "starting": "idle",
}


class AccountPoolError(RuntimeError):
    pass


class AccountPoolService:
    def __init__(self, config: Dict[str, Any], collector=None) -> None:
        self.config = config
        self.collector = collector
        self.store = AccountStore(Path(config["pool_db_path"]), config)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._busy: set[str] = set()
        self._cancel: Dict[str, threading.Event] = {}
        self._login_threads: Dict[str, threading.Thread] = {}
        self._thread: Optional[threading.Thread] = None
        self._check_all_thread: Optional[threading.Thread] = None
        self._primary_beat_at = 0.0
        self._primary_status = ""

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="account-pool-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            for event in self._cancel.values():
                event.set()
            threads = list(self._login_threads.values())
        if self._thread and threading.current_thread() is not self._thread:
            self._thread.join(timeout=4)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)

    close = stop

    # ── public snapshot ───────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        accounts = self.store.accounts()
        for account in accounts:
            if account.get("managed_by") == MANAGED_BY_COLLECTOR:
                self._overlay_collector(account)
        summary = {
            "total": len(accounts),
            "online": sum(1 for item in accounts if item["status"] == "online"),
            "pending": sum(1 for item in accounts if item["status"] in {"awaiting_login", "logging_in", "checking"}),
            "error": sum(1 for item in accounts if item["status"] in {"expired", "error"}),
            "with_session": sum(1 for item in accounts if item["has_session"]),
        }
        return {
            "ok": True,
            "accounts": accounts,
            "logs": self.store.logs(30),
            "summary": summary,
            "check_interval": int(self.config.get("pool_check_interval_seconds", 60)),
            "login_timeout": int(self.config.get("pool_login_timeout_seconds", 300)),
        }

    def _overlay_collector(self, account: Dict[str, Any]) -> Dict[str, Any]:
        if self.collector is None:
            account["status"] = "idle"
            account["status_message"] = "采集器尚未启动"
            return {}
        try:
            state = self.collector.snapshot()
        except Exception:
            account["status"] = "error"
            account["status_message"] = "无法读取采集器状态"
            return {}
        status = _STATUS_FROM_COLLECTOR.get(str(state.get("status") or ""), "idle")
        account["status"] = status
        account["status_message"] = str(state.get("message") or "")
        account["last_error"] = str(state.get("last_error") or "")
        account["last_check"] = now_iso()
        profile = state.get("profile") if isinstance(state.get("profile"), dict) else {}
        account["has_session"] = bool(profile.get("user_code"))
        if profile.get("user_code"):
            account["username"] = str(profile.get("user_code"))
        return state

    # ── CRUD ──────────────────────────────────────────────────────────────

    def save_account(self, payload: Dict[str, Any], account_id: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if account_id:
                self._ensure_not_busy(account_id)
            payload = dict(payload)
            if str(payload.get("platform") or "") == "crown_member" and not str(
                payload.get("base_url") or ""
            ).strip():
                fastest = fastest_endpoint(self.config)
                if fastest:
                    payload["base_url"] = fastest
            return self.store.save_account(payload, account_id)

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            self._ensure_not_busy(account_id)
            self.store.delete_account(account_id)

    def clear_session(self, account_id: str) -> Dict[str, Any]:
        with self._lock:
            self._ensure_not_busy(account_id)
            return self.store.clear_session(account_id)

    def toggle(self, account_id: str, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            self._ensure_not_busy(account_id)
            return self.store.set_enabled(account_id, enabled)

    # ── login / check ─────────────────────────────────────────────────────

    def login(self, account_id: str, *, force: bool = False) -> Dict[str, Any]:
        account = self.store.get(account_id)
        if account["managed_by"] == MANAGED_BY_COLLECTOR:
            if self.collector is None:
                raise AccountPoolError("采集器尚未启动")
            action = "重新登录" if force else "打开登录窗口"
            if force:
                self.collector.request_relogin()
            else:
                self.collector.request_show_window()
            self.store.set_status(account_id, "awaiting_login",
                                  f"已请求{action}，请在后台浏览器完成登录", last_check=now_iso())
            self.store.log(account_id, "login", "pending", f"已请求{action}")
            return {"message": f"已请求{action}，请在浏览器完成登录"}
        if not account["enabled"]:
            raise AccountPoolError("账号已停用，请先启用")
        if account["platform"] == "crown_member":
            fastest = fastest_endpoint(self.config)
            if fastest and fastest != account.get("base_url"):
                try:
                    self.store.set_base_url(account_id, fastest)
                    account["base_url"] = fastest
                    self.store.log(account_id, "login", "success", f"已切换入口：{fastest}")
                except ValueError:
                    pass
        with self._lock:
            self._ensure_not_busy(account_id)
            self._busy.add(account_id)
            self._cancel[account_id] = threading.Event()
            self._store_status(account_id, "logging_in", "正在打开登录窗口…")
            self.store.log(account_id, "login", "pending", "登录任务已开始")
            thread = threading.Thread(
                target=self._login_worker, args=(account_id, bool(force)),
                name=f"pool-login-{account_id[:8]}", daemon=True,
            )
            self._login_threads[account_id] = thread
        thread.start()
        return {"message": "已开始登录，请在浏览器完成登录/二次验证"}

    def check(self, account_id: str) -> Dict[str, Any]:
        account = self.store.get(account_id)
        if account["managed_by"] == MANAGED_BY_COLLECTOR:
            self._overlay_collector(account)
            return {"message": "采集器主账号状态已刷新"}
        if not account["enabled"]:
            raise AccountPoolError("账号已停用，请先启用")
        with self._lock:
            self._ensure_not_busy(account_id)
            self._busy.add(account_id)
            self._store_status(account_id, "checking", "正在检测会话…")
        thread = threading.Thread(
            target=self._check_account, args=(account_id, True),
            name=f"pool-check-{account_id[:8]}", daemon=True,
        )
        thread.start()
        return {"message": "已开始检测会话"}

    def check_all(self) -> Dict[str, Any]:
        if self._check_all_thread and self._check_all_thread.is_alive():
            return {"message": "批量检测已在进行中"}
        self._check_all_thread = threading.Thread(
            target=self._check_all_worker, name="pool-check-all", daemon=True
        )
        self._check_all_thread.start()
        return {"message": "已开始检测全部账号"}

    def cancel(self, account_id: str) -> Dict[str, Any]:
        with self._lock:
            event = self._cancel.get(account_id)
            if event:
                event.set()
        return {"message": "已请求取消当前任务"}

    # ── workers ───────────────────────────────────────────────────────────

    def _login_worker(self, account_id: str, force: bool) -> None:
        with self._lock:
            cancel = self._cancel.setdefault(account_id, threading.Event())
        timeout = int(self.config.get("pool_login_timeout_seconds", 300))
        timer = threading.Timer(max(60, timeout + 15), cancel.set)
        timer.daemon = True
        timer.start()
        try:
            account = self.store.get(account_id)
            provider = provider_for(account, self.config, self.store)

            def on_status(message: str) -> None:
                if not cancel.is_set():
                    self._store_status(account_id, "logging_in", message)

            result = provider.login(
                account, force=force, should_stop=cancel.is_set, on_status=on_status,
            )
            if cancel.is_set():
                raise ProviderError("登录超时或已取消")
            if result.status != "online":
                raise ProviderError(result.message or "登录未通过会话验证")
            if result.meta:
                self.store.save_meta(account_id, result.meta)
            self.store.set_status(
                account_id, "online", result.message or "登录成功，会话已保存",
                last_check=now_iso(), last_login=now_iso(),
            )
            self.store.add_heartbeat(account_id, "online", 0, result.message or "登录成功")
            self.store.log(account_id, "login", "success", result.message or "登录成功，会话已保存")
        except ProviderError as exc:
            message = "登录超时或已取消" if cancel.is_set() else str(exc)
            self.store.set_status(account_id, "error", message[:300], last_check=now_iso())
            self.store.add_heartbeat(account_id, "error", 0, message[:240])
            self.store.log(account_id, "login", "error", message[:300])
        except Exception as exc:  # noqa: BLE001
            message = f"登录失败：{' '.join(str(exc).split())[:240]}"
            self.store.set_status(account_id, "error", message, last_check=now_iso())
            self.store.add_heartbeat(account_id, "error", 0, message[:240])
            self.store.log(account_id, "login", "error", message)
        finally:
            timer.cancel()
            with self._lock:
                self._busy.discard(account_id)
                self._cancel.pop(account_id, None)
                self._login_threads.pop(account_id, None)

    def _check_all_worker(self) -> None:
        for account in self.store.accounts():
            if self._stop.is_set():
                break
            if account["managed_by"] == MANAGED_BY_COLLECTOR or not account["enabled"]:
                continue
            if self._is_busy(account["id"]):
                continue
            self._check_account(account["id"], False)

    def _check_account(self, account_id: str, standalone: bool) -> None:
        with self._lock:
            if not standalone and account_id in self._busy:
                return
            self._busy.add(account_id)
        started = time.monotonic()
        try:
            account = self.store.get(account_id)
            if not account["enabled"]:
                return
            provider = provider_for(account, self.config, self.store)
            result = provider.check(account)
            latency = int((time.monotonic() - started) * 1000)
            if result.meta:
                self.store.save_meta(account_id, result.meta)
            status = result.status if result.status in {"online", "expired", "error"} else "error"
            self.store.set_status(account_id, status, result.message[:300], last_check=now_iso())
            self.store.add_heartbeat(account_id, status, latency, result.message[:240])
            if status == "online":
                self.store.log(account_id, "check", "success", result.message[:300])
            else:
                self.store.log(account_id, "check", status, result.message[:300])
        except Exception as exc:  # noqa: BLE001
            latency = int((time.monotonic() - started) * 1000)
            message = f"检测失败：{' '.join(str(exc).split())[:240]}"
            self.store.set_status(account_id, "error", message, last_check=now_iso())
            self.store.add_heartbeat(account_id, "error", latency, message[:240])
            self.store.log(account_id, "check", "error", message)
        finally:
            with self._lock:
                self._busy.discard(account_id)

    # ── heartbeat loop ────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sync_primary()
                for account in self.store.accounts():
                    if self._stop.is_set():
                        break
                    if account["managed_by"] == MANAGED_BY_COLLECTOR or not account["enabled"]:
                        continue
                    if self._is_busy(account["id"]):
                        continue
                    if self._due(account):
                        self._check_account(account["id"], False)
            except Exception:
                pass
            self._wake.wait(5)
            self._wake.clear()

    def _sync_primary(self) -> None:
        if self.collector is None:
            return
        try:
            account = self.store.get(PRIMARY_ID)
        except KeyError:
            return
        state = self._overlay_collector(account)
        try:
            self.store.set_status(
                PRIMARY_ID, account["status"], account.get("status_message") or "",
                last_check=account.get("last_check") or now_iso(),
            )
        except Exception:
            pass
        now = time.monotonic()
        interval = max(15, int(account.get("check_interval") or 60))
        if (account["status"] != self._primary_status
                or now - self._primary_beat_at >= interval):
            latency = 0
            try:
                latency = int(float(state.get("last_cycle_seconds") or 0) * 1000)
            except (TypeError, ValueError):
                latency = 0
            try:
                self.store.add_heartbeat(
                    PRIMARY_ID, account["status"], latency,
                    (account.get("status_message") or "")[:240],
                )
            except Exception:
                pass
            self._primary_beat_at = now
            self._primary_status = account["status"]

    @staticmethod
    def _due(account: Dict[str, Any]) -> bool:
        last = str(account.get("last_check") or "")
        if not last:
            return True
        try:
            checked = datetime.fromisoformat(last).timestamp()
        except ValueError:
            return True
        interval = max(15, int(account.get("check_interval") or 60))
        return time.time() - checked >= interval

    # ── helpers ───────────────────────────────────────────────────────────

    def _is_busy(self, account_id: str) -> bool:
        with self._lock:
            return account_id in self._busy

    def _ensure_not_busy(self, account_id: str) -> None:
        if self._is_busy(account_id):
            raise AccountPoolError("该账号正在执行登录或检测，请等待完成")

    def _store_status(self, account_id: str, status: str, message: str) -> None:
        try:
            self.store.set_status(account_id, status, message)
        except Exception:
            pass
