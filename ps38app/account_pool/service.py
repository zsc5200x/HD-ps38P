"""Account pool supervisor: login jobs, heartbeat checks and public snapshots."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..adspower import AdsPowerClient, detect_platform, environment_projection, is_env_id
from ..monitor import fastest_endpoint
from .providers import ProviderError, provider_for
from .store import (
    AccountStore,
    MANAGED_BY_COLLECTOR,
    PRIMARY_ID,
    normalize_url,
    now_iso,
)

_STATUS_FROM_COLLECTOR = {
    "online": "online",
    "login": "awaiting_login",
    "error": "error",
    "stopped": "disabled",
    "starting": "idle",
}

# AdsPower 导入时按平台补默认路径，避免要求页面再填一遍。
_PLATFORM_PATHS = {
    "ps38_backend": ("/partner/zh-cn/login", "/partner-ui-v2/report-outstanding-v2"),
    "crown_backend": ("/", ""),
    "crown_member": ("/", ""),
}


class AccountPoolError(RuntimeError):
    pass


class AccountPoolService:
    def __init__(self, config: Dict[str, Any], collector=None) -> None:
        self.config = config
        self.collector = collector
        self.store = AccountStore(Path(config["pool_db_path"]), config)
        self.adspower = AdsPowerClient(config)
        self._env_cache: Optional[tuple] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._busy: set[str] = set()
        self._cancel: Dict[str, threading.Event] = {}
        self._login_threads: Dict[str, threading.Thread] = {}
        self._auto_timers: Dict[str, threading.Timer] = {}
        self._auto_relogin_at: Dict[str, float] = {}
        self._thread: Optional[threading.Thread] = None
        self._check_all_thread: Optional[threading.Thread] = None
        self._primary_beat_at = 0.0
        self._primary_status = ""

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="account-pool-heartbeat", daemon=True
        )
        self._thread.start()

    def _stop_auto_timers(self) -> None:
        with self._lock:
            timers = list(self._auto_timers.values())
            self._auto_timers.clear()
        for timer in timers:
            timer.cancel()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._stop_auto_timers()
        with self._lock:
            for event in self._cancel.values():
                event.set()
            threads = list(self._login_threads.values())
            self._auto_relogin_at.clear()
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
            "pending": sum(
                1
                for item in accounts
                if item["status"] in {"awaiting_login", "logging_in", "checking"}
            ),
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
        account["login_id"] = str(profile.get("login_id") or profile.get("user_code") or "")
        return state

    # ── AdsPower environments ─────────────────────────────────────────────

    def environments(self, *, refresh: bool = False) -> Dict[str, Any]:
        """List local AdsPower profiles with account/expiry parsing and bindings."""
        now = time.monotonic()
        cached = self._env_cache
        if not refresh and cached is not None and now - cached[0] < 10:
            return cached[1]
        rows = self.adspower.list_all_profiles()
        bound = self.store.env_bindings()
        items = [
            environment_projection(row, bound.get(str(row.get("profile_id") or "")))
            for row in rows
            if isinstance(row, dict)
        ]

        def sort_key(item: Dict[str, Any]):
            raw = str(item.get("profile_no") or "")
            try:
                return (0, -int(raw))
            except ValueError:
                return (1, 0)

        items.sort(key=sort_key)
        payload = {
            "ok": True,
            "base_url": self.adspower.base_url,
            "items": items,
            "summary": {
                "total": len(items),
                "bound": sum(1 for item in items if item["bound_account"]),
                "expired": sum(1 for item in items if item["expired"]),
                "need_account": sum(
                    1 for item in items if not item["bound_account"] and not item["account"]
                ),
                "available": sum(
                    1
                    for item in items
                    if not item["bound_account"] and not item["expired"] and not item["stopped"]
                ),
            },
        }
        self._env_cache = (now, payload)
        return payload

    def import_environments(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create pool accounts from selected AdsPower profiles (no login started)."""
        payload = dict(payload or {})
        platform = str(payload.get("platform") or "auto").strip() or "auto"
        if platform != "auto" and platform not in _PLATFORM_PATHS:
            raise ValueError("请选择有效的账号类型")
        raw_ids = payload.get("env_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("请至少选择一个环境")
        env_ids = []
        for value in raw_ids:
            env_id = str(value or "").strip()
            if not is_env_id(env_id):
                raise ValueError("环境 ID 无效")
            if env_id not in env_ids:
                env_ids.append(env_id)
        if len(env_ids) > 100:
            raise ValueError("单次最多导入 100 个环境")

        raw_base = str(payload.get("base_url") or "").strip()
        if raw_base:
            crown_base = normalize_url(raw_base)
        else:
            fallback = fastest_endpoint(self.config) or ""
            crown_base = normalize_url(fallback) if fallback else ""
        ps38_base = normalize_url(self.config.get("base_url"))
        if platform in {"crown_backend", "crown_member"} and not crown_base:
            raise ValueError("请填写站点地址（当前没有可用的皇冠入口）")

        def platform_paths(target: str) -> tuple:
            login, dashboard = _PLATFORM_PATHS[target]
            login_path = str(payload.get("login_path") or login).strip() or login
            dashboard_path = str(payload.get("dashboard_path", dashboard) or "").strip()
            return login_path, dashboard_path

        usernames = payload.get("usernames") if isinstance(payload.get("usernames"), dict) else {}
        try:
            interval = int(
                payload.get("check_interval") or self.config.get("pool_check_interval_seconds", 60)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("检测间隔必须是整数秒") from exc
        interval = max(15, min(3600, interval))
        enabled_by_default = self._truthy(payload.get("enabled", True))

        rows = self.adspower.list_all_profiles()
        by_id = {
            str(row.get("profile_id") or ""): row
            for row in rows
            if isinstance(row, dict) and row.get("profile_id")
        }
        bound = self.store.env_bindings()
        existing = {str(account["username"]).lower() for account in self.store.accounts()}
        created = []
        skipped = []
        for env_id in env_ids:
            row = by_id.get(env_id)
            if row is None:
                skipped.append({"env_id": env_id, "reason": "环境不存在或已删除"})
                continue
            if env_id in bound:
                skipped.append(
                    {
                        "env_id": env_id,
                        "reason": f"已绑定账号 {bound[env_id]['name']}",
                    }
                )
                continue
            env = environment_projection(row)
            target = platform
            if target == "auto":
                target = detect_platform(row.get("name")) or "crown_member"
            env_base = ps38_base if target == "ps38_backend" else crown_base
            if not env_base:
                skipped.append({"env_id": env_id, "reason": "缺少站点地址"})
                continue
            login_path, dashboard_path = platform_paths(target)
            username = str(usernames.get(env_id) or env["account"] or "").strip()[:128]
            if not username:
                skipped.append({"env_id": env_id, "reason": "无法从环境名识别账号，请手工填写"})
                continue
            if username.lower() in existing:
                skipped.append({"env_id": env_id, "reason": f"账号 {username} 已存在"})
                continue
            enabled = enabled_by_default and not env["expired"] and not env["stopped"]
            try:
                account = self.store.save_account(
                    {
                        "platform": target,
                        "name": env["name"] or username,
                        "base_url": env_base,
                        "login_path": login_path,
                        "dashboard_path": dashboard_path,
                        "username": username,
                        "enabled": enabled,
                        "check_interval": interval,
                        "env_id": env_id,
                        "env_no": env["profile_no"],
                        "env_name": env["name"],
                        "session_backend": "adspower",
                    }
                )
            except ValueError as exc:
                skipped.append({"env_id": env_id, "reason": str(exc)})
                continue
            existing.add(username.lower())
            bound[env_id] = {"id": account["id"], "name": account["name"], "platform": target}
            created.append(
                {
                    "id": account["id"],
                    "env_id": env_id,
                    "env_no": env["profile_no"],
                    "name": account["name"],
                    "username": username,
                    "platform": target,
                    "enabled": account["enabled"],
                    "expire_date": env["expire_date"],
                }
            )
        if created:
            self._env_cache = None
            self.store.log(None, "ads_import", "success", f"从 AdsPower 导入 {len(created)} 个账号")
            for item in created:
                self.store.set_status(
                    item["id"],
                    "idle" if item["enabled"] else "disabled",
                    (
                        "已绑定 ADS 环境，等待登录"
                        if item["platform"] in {"ps38_backend", "crown_member"}
                        else "已绑定 ADS 环境；环境登录将在下一阶段接入"
                    )
                    if item["enabled"]
                    else "环境已过期或已停用",
                )
        if not created and skipped:
            reason = skipped[0]["reason"]
            raise ValueError(f"没有可导入的账号：{reason}")
        return {
            "ok": True,
            "created": created,
            "skipped": skipped,
            "message": f"已导入 {len(created)} 个账号"
            + (f"，跳过 {len(skipped)} 个" if skipped else ""),
        }

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def bind_environment(self, account_id: str, env_id: str) -> Dict[str, Any]:
        """Bind a local AdsPower profile to an existing account.

        Used for the PS38 primary: the collector must keep the same account and
        only swap its browser backend, so this bypasses the normal edit guard.
        """
        env_id = str(env_id or "").strip()
        if not is_env_id(env_id):
            raise ValueError("环境 ID 无效")
        account = self.store.get(account_id)
        rows = self.adspower.list_all_profiles()
        row = next(
            (
                item
                for item in rows
                if isinstance(item, dict) and str(item.get("profile_id") or "") == env_id
            ),
            None,
        )
        if row is None:
            raise ValueError("环境不存在或已删除")
        env = environment_projection(row)
        updated = self.store.bind_environment(account_id, env_id, env["profile_no"], env["name"])
        if account["managed_by"] != MANAGED_BY_COLLECTOR:
            self.store.set_status(
                account_id, "idle", "已绑定 ADS 环境，等待登录", last_check=now_iso()
            )
            updated = self.store.get(account_id)
        self._env_cache = None
        return updated

    # ── CRUD ──────────────────────────────────────────────────────────────

    def save_account(
        self, payload: Dict[str, Any], account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        with self._lock:
            if account_id:
                self._ensure_not_busy(account_id)
            payload = dict(payload)
            if (
                str(payload.get("platform") or "") == "crown_member"
                and not str(payload.get("base_url") or "").strip()
            ):
                fastest = fastest_endpoint(self.config)
                if fastest:
                    payload["base_url"] = fastest
            return self.store.save_account(payload, account_id)

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            timer = self._auto_timers.pop(account_id, None)
            self._auto_relogin_at.pop(account_id, None)
            self._ensure_not_busy(account_id)
            self.store.delete_account(account_id)
        if timer is not None:
            timer.cancel()

    def save_credentials(self, account_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Save login credentials and an optional TOTP secret (0600 file)."""
        from ..totp import totp

        account = self.store.get(account_id)
        existing = self.store.credentials(account_id)
        username = str(payload.get("username") or "").strip()[:128]
        if not username:
            username = existing.get("username") or ""
        if not username and account.get("username") not in ("", "手动登录"):
            username = str(account["username"])
        if not username:
            raise ValueError("请填写登录账号")
        password = str(payload.get("password") or "")
        if not password and not existing.get("password"):
            raise ValueError("请填写密码")
        raw_totp = payload.get("totp_secret")
        secret = None
        if raw_totp is not None and str(raw_totp).strip():
            secret = str(raw_totp).strip()
            try:
                totp(secret)
            except Exception as exc:  # noqa: BLE001
                raise ValueError("TOTP 密钥格式无效（需要 Base32）") from exc
        self.store.save_credentials(account_id, username, password, totp_secret=secret)
        self.store.log(account_id, "credentials", "success", "账号凭据已保存")
        if account.get("managed_by") == MANAGED_BY_COLLECTOR and self.collector is not None:
            refresh = getattr(self.collector, "refresh_credentials", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    pass
        with self._lock:
            self._auto_relogin_at.pop(account_id, None)
        return self.store.get(account_id)

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
        if account["session_backend"] == "adspower" and account["platform"] == "crown_backend":
            raise AccountPoolError("该账号已绑定 ADS 环境；环境登录将在下一阶段接入")
        if account["managed_by"] == MANAGED_BY_COLLECTOR:
            if self.collector is None:
                raise AccountPoolError("采集器尚未启动")
            action = "重新登录" if force else "打开登录窗口"
            if force:
                self.collector.request_relogin()
            else:
                self.collector.request_show_window()
            self.store.set_status(
                account_id,
                "awaiting_login",
                f"已请求{action}，请在后台浏览器完成登录",
                last_check=now_iso(),
            )
            self.store.log(account_id, "login", "pending", f"已请求{action}")
            return {"message": f"已请求{action}，请在浏览器完成登录"}
        if not account["enabled"]:
            raise AccountPoolError("账号已停用，请先启用")
        if account["platform"] == "crown_member" and not account.get("env_id"):
            # 本地会员沿用“延迟最低入口”；绑定 ADS 环境的账号必须固定环境对应站点，
            # 否则会把 hga038 的会话切到 hga039，造成域/会话不一致。
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
                target=self._login_worker,
                args=(account_id, bool(force)),
                name=f"pool-login-{account_id[:8]}",
                daemon=True,
            )
            self._login_threads[account_id] = thread
        thread.start()
        return {"message": "已开始登录，请在浏览器完成登录/二次验证"}

    def check(self, account_id: str) -> Dict[str, Any]:
        account = self.store.get(account_id)
        if account["session_backend"] == "adspower" and account["platform"] == "crown_backend":
            raise AccountPoolError("该账号已绑定 ADS 环境；环境登录将在下一阶段接入")
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
            target=self._check_account,
            args=(account_id, True),
            name=f"pool-check-{account_id[:8]}",
            daemon=True,
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
                account,
                force=force,
                should_stop=cancel.is_set,
                on_status=on_status,
            )
            if cancel.is_set():
                raise ProviderError("登录超时或已取消")
            if result.status != "online":
                raise ProviderError(result.message or "登录未通过会话验证")
            if result.meta:
                self.store.save_meta(account_id, result.meta)
            self.store.set_status(
                account_id,
                "online",
                result.message or "登录成功，会话已保存",
                last_check=now_iso(),
                last_login=now_iso(),
            )
            self.store.add_heartbeat(account_id, "online", 0, result.message or "登录成功")
            self.store.log(account_id, "login", "success", result.message or "登录成功，会话已保存")
            # 登录成功后清掉自动重登冷却，下次掉线可以立刻恢复。
            with self._lock:
                self._auto_relogin_at.pop(account_id, None)
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
            if account["session_backend"] == "adspower" and account["platform"] == "crown_backend":
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
        final_status = ""
        final_message = ""
        try:
            account = self.store.get(account_id)
            if not account["enabled"]:
                return
            if account["session_backend"] == "adspower" and account["platform"] == "crown_backend":
                return
            provider = provider_for(account, self.config, self.store)
            result = provider.check(account)
            final_status = result.status
            final_message = result.message
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
            final_status = "error"
            final_message = message
            self.store.set_status(account_id, "error", message, last_check=now_iso())
            self.store.add_heartbeat(account_id, "error", latency, message[:240])
            self.store.log(account_id, "check", "error", message)
        finally:
            with self._lock:
                self._busy.discard(account_id)
        self._maybe_auto_relogin(account_id, final_status, final_message)

    def _maybe_auto_relogin(self, account_id: str, status: str, message: str) -> None:
        """Crown member only: schedule a login after a confirmed expiry."""
        if self._stop.is_set():
            return
        if status != "expired" and not (status == "error" and "尚未稳定" in (message or "")):
            return
        if not self._truthy(self.config.get("pool_auto_relogin", True)):
            return
        try:
            account = self.store.get(account_id)
        except KeyError:
            return
        if not account["enabled"] or account["platform"] != "crown_member":
            return
        if not self.store.credentials(account_id).get("password"):
            return
        now = time.monotonic()
        with self._lock:
            if now < self._auto_relogin_at.get(account_id, 0):
                return
            cooldown = max(5, int(self.config.get("pool_relogin_cooldown_seconds", 60)))
            self._auto_relogin_at[account_id] = now + cooldown
            existing = self._auto_timers.get(account_id)
            if existing is not None and existing.is_alive():
                return
            wait = 2
            if "重复登录" in message or "doublelogin" in message.lower():
                # 被顶掉时对端会话通常几秒内失效，短延迟后直接重登最有效。
                wait = max(0.05, float(self.config.get("pool_double_login_wait_seconds", 8)))
            timer = threading.Timer(wait, self._auto_relogin_worker, args=(account_id,))
            timer.daemon = True
            self._auto_timers[account_id] = timer
        timer.start()
        self.store.log(
            account_id,
            "auto_relogin",
            "pending",
            f"检测到会话失效，{wait} 秒后自动重新登录",
        )

    def _auto_relogin_worker(self, account_id: str) -> None:
        with self._lock:
            self._auto_timers.pop(account_id, None)
        if self._stop.is_set():
            return
        try:
            account = self.store.get(account_id)
            if not account["enabled"]:
                return
            if not self.store.credentials(account_id).get("password"):
                self.store.log(account_id, "auto_relogin", "error", "缺少登录密码，无法自动重登")
                return
            self.store.log(account_id, "auto_relogin", "pending", "开始自动重新登录")
            self.login(account_id, force=False)
        except Exception:
            pass

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
                    if (
                        account["session_backend"] == "adspower"
                        and account["platform"] == "crown_backend"
                    ):
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
                PRIMARY_ID,
                account["status"],
                account.get("status_message") or "",
                last_check=account.get("last_check") or now_iso(),
            )
        except Exception:
            pass
        now = time.monotonic()
        interval = max(15, int(account.get("check_interval") or 60))
        if account["status"] != self._primary_status or now - self._primary_beat_at >= interval:
            latency = 0
            try:
                latency = int(float(state.get("last_cycle_seconds") or 0) * 1000)
            except (TypeError, ValueError):
                latency = 0
            try:
                self.store.add_heartbeat(
                    PRIMARY_ID,
                    account["status"],
                    latency,
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
