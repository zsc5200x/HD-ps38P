"""Resident order collector.

One background thread owns the browser session; the HTTP server only reads the
SQLite database. The loop polls every configured agent's OPEN bets on a short
cadence and reconciles SETTLED bets on a slower cadence. A session expiry keeps
the same browser window open and waits for the operator to finish login/2FA,
then continues without restarting the app.
"""

from __future__ import annotations

import threading
import time
import traceback
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .session import LoginWindowClosed, PartnerSession
from .store import (
    IncompletePage,
    OrderStore,
    PollError,
    SessionExpired,
    count_live_fixtures,
    now_iso,
    open_readonly,
    order_identity,
    parse_page,
)

RETRY_SECONDS = 15


class ReloginRequested(Exception):
    """The operator asked for a fresh login from the dashboard."""


def _short_error(exc: Exception, limit: int = 220) -> str:
    """Collapse multiline library errors into one readable status line."""
    text = " ".join(str(exc).split())
    return text[:limit] + ("…" if len(text) > limit else "")


def month_chunks(start: date, end: date) -> List[Tuple[date, date]]:
    """Split a range into calendar-month chunks (the API rejects long spans)."""
    chunks: List[Tuple[date, date]] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        chunk_end = min(next_month - timedelta(days=1), end)
        chunks.append((cursor, chunk_end))
        cursor = next_month
    return chunks


class Poller(threading.Thread):
    def __init__(self, config: Dict[str, Any], *, debug: bool = False) -> None:
        super().__init__(daemon=True, name="ps38-poller")
        self.config = config
        self.debug = debug
        # The writable connection is created inside the poller thread so
        # SQLite never sees a cross-thread handle.
        self.store: Optional[OrderStore] = None
        self.session: Optional[PartnerSession] = None
        self.agents: List[Dict[str, Any]] = []

        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._relogin = threading.Event()
        self._show_window = threading.Event()
        self._backfill_requested = threading.Event()
        self._lock = threading.Lock()

        self._backfill_plan: List[Tuple[Dict[str, Any], date, date]] = []
        self._backfill_done = 0
        self._backfill_total = 0
        self._backfill_running = False

        self.state: Dict[str, Any] = {
            "status": "starting",
            "message": "正在启动采集器…",
            "since": now_iso(),
            "last_error": "",
            "last_open_at": "",
            "last_settled_at": "",
            "last_cycle_seconds": 0.0,
            "profile": {},
            "agents": [],
            "poll_count": 0,
            "new_orders": 0,
            "backfill": {
                "running": False,
                "done": 0,
                "total": 0,
                "message": "",
            },
        }

    # ── public API used by the HTTP server ────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            state = {
                "status": self.state["status"],
                "message": self.state["message"],
                "since": self.state["since"],
                "last_error": self.state["last_error"],
                "last_open_at": self.state["last_open_at"],
                "last_settled_at": self.state["last_settled_at"],
                "last_cycle_seconds": self.state["last_cycle_seconds"],
                "profile": dict(self.state["profile"]),
                "agents": list(self.state["agents"]),
                "poll_count": self.state["poll_count"],
                "new_orders": self.state["new_orders"],
                "backfill": dict(self.state["backfill"]),
            }
        state["counts"] = self._read_counts()
        return state

    def _read_counts(self) -> Dict[str, int]:
        """Count orders through a fresh read-only connection (thread-safe)."""
        empty = {"total": 0, "open": 0, "settled": 0, "live": 0}
        connection = open_readonly(self.config["db_path"])
        if connection is None:
            return empty
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),0) AS open, "
                "COALESCE(SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END),0) AS settled "
                "FROM partner_orders WHERE source=?",
                ("ps38ag",),
            ).fetchone()
            return {
                "total": row["total"],
                "open": row["open"],
                "settled": row["settled"],
                "live": count_live_fixtures(connection),
            }
        except Exception:
            return empty
        finally:
            connection.close()

    def request_refresh(self) -> None:
        self._wake.set()

    def request_backfill(self) -> bool:
        if self._backfill_running:
            return False
        self._backfill_requested.set()
        self._wake.set()
        return True

    def request_relogin(self) -> None:
        self._relogin.set()
        self._wake.set()

    def request_show_window(self) -> None:
        """Bring the backend browser window to the front (keeps the session)."""
        self._show_window.set()
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    # ── thread entry ──────────────────────────────────────────────────────

    def _ensure_store(self) -> None:
        if self.store is None:
            self.store = OrderStore(self.config["db_path"])

    def run(self) -> None:
        self._ensure_store()
        try:
            self._supervise()
        finally:
            self._close_session()
            try:
                if self.store is not None:
                    self.store.close()
            except Exception:
                pass
            self._set_state("stopped", "采集器已停止")

    def _supervise(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._open_session()
                while not self._stop_event.is_set():
                    try:
                        self._loop()
                    except SessionExpired as exc:
                        self._recover_session(exc)
            except ReloginRequested:
                self._set_state("login", "正在重新登录…")
                self._close_session()
                time.sleep(0.5)
            except SessionExpired as exc:
                # Raised before/while opening the session (login or agent tree).
                # Keep a retry gap so a failing upstream cannot spin the browser.
                self._set_state("login", f"会话失效：{_short_error(exc)}；{RETRY_SECONDS} 秒后重试")
                self._close_session()
                if self._stop_event.wait(RETRY_SECONDS):
                    return
            except LoginWindowClosed as exc:
                self._set_state("error", f"{_short_error(exc)}；{RETRY_SECONDS} 秒后重试")
                self._close_session()
                if self._stop_event.wait(RETRY_SECONDS):
                    return
            except Exception as exc:  # noqa: BLE001 - supervisor must survive
                if self.debug:
                    traceback.print_exc()
                self._set_state("error", f"采集异常：{_short_error(exc)}；{RETRY_SECONDS} 秒后重试")
                self._close_session()
                if self._stop_event.wait(RETRY_SECONDS):
                    return

    def _service_show_window(self) -> None:
        """Open or focus the browser window; must run in the poller thread."""
        if not self._show_window.is_set():
            return
        self._show_window.clear()
        if self.session is not None and self.session.present_window():
            self._set_message("已打开后台浏览器窗口")
            return
        # No usable window (closed or crashed): rebuild it. Cookies are reused,
        # so a still-valid session comes back without another login.
        raise ReloginRequested()

    def _recover_session(self, exc: Exception) -> None:
        """Keep the same browser window and wait for a manual re-login."""
        if self.session is None:
            raise SessionExpired(str(exc))
        self._set_state("login", f"会话失效：{_short_error(exc)}；请在浏览器完成登录/2FA")
        # Bring the login form back; if the cookie is actually still valid the
        # site redirects to the dashboard and the probe below succeeds.
        self.session.open_login_page()
        try:
            profile = self.session.wait_for_login(
                on_status=lambda message: self._set_state("login", message),
                should_stop=self._stop_event.is_set,
                on_tick=self._service_show_window,
            )
        except SessionExpired:
            raise
        self._set_profile(profile)
        self._set_state("online", f"会话已恢复（{profile['user_code']}）")
        if not self.agents:
            self.agents = self._resolve_agents(profile)
            with self._lock:
                self.state["agents"] = [agent["code"] for agent in self.agents]
            self.store.save_agents(self.agents)

    # ── connection / login ────────────────────────────────────────────────

    def _open_session(self) -> None:
        self._set_state("starting", "正在连接后台会话…")
        self._close_session()
        session = PartnerSession(self.config, debug=self.debug)
        self.session = session
        profile = session.login(
            force=self._relogin.is_set(),
            on_status=lambda message: self._set_state("login", message),
            should_stop=self._stop_event.is_set,
            on_tick=self._service_show_window,
        )
        self._relogin.clear()
        self._set_profile(profile)
        self._set_state("online", f"会话在线（{profile['user_code']}）")

        agents = self._resolve_agents(profile)
        self.agents = agents
        with self._lock:
            self.state["agents"] = [agent["code"] for agent in agents]
        self.store.save_agents(agents)
        self._set_state("online", f"已连接 {profile['user_code']}，代理 {len(agents)} 个")

        if (self.config.get("backfill_on_first_run", True)
                and not self.store.has_orders()):
            self._backfill_requested.set()
            self._set_message("首次运行：准备补采历史订单…")

    def _resolve_agents(self, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Resolve configured agent codes to user ids, with a local fallback."""
        wanted = list(self.config.get("agent_codes") or [])
        saved = self.store.saved_agents()
        saved_by_code = {agent["code"]: agent for agent in saved}
        found: Dict[str, Dict[str, Any]] = {}
        try:
            start = date.fromisoformat(str(self.config["history_from"]))
            end = date.today()
            for chunk_start, chunk_end in month_chunks(start, end):
                response = self.session.fetch_hierarchy(
                    profile["user_id"], chunk_start.isoformat(), chunk_end.isoformat()
                )
                if response.get("httpStatus") in (401, 403):
                    raise SessionExpired("身份树接口返回未登录")
                if response.get("httpStatus") != 200:
                    continue
                payload = response.get("json") or {}
                body = payload.get("body") if isinstance(payload, dict) else None
                data = (body or {}).get("data") if isinstance(body, dict) else None
                if not isinstance(data, list):
                    continue
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    code = row.get("userCode")
                    user_id = row.get("userId")
                    if (not isinstance(code, str) or not code.strip()
                            or isinstance(user_id, bool) or not isinstance(user_id, int)
                            or user_id <= 0):
                        continue
                    found[code.strip()] = {
                        "code": code.strip(),
                        "user_id": user_id,
                        "level": str(row.get("userLevel") or row.get("level") or ""),
                        "name": str(row.get("firstName") or row.get("name") or ""),
                    }
        except SessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to saved agents
            self._set_message(f"代理身份树读取失败，使用本地缓存：{_short_error(exc)}")

        if wanted:
            agents = [found[code] for code in wanted if code in found]
            missing = [code for code in wanted if code not in found]
            # Keep previously resolved agents when the month sweep missed one.
            for code in missing:
                if code in saved_by_code:
                    agents.append(saved_by_code[code])
            missing = [code for code in wanted
                       if code not in {agent["code"] for agent in agents}]
            if missing:
                self._set_message(f"未找到代理：{', '.join(missing)}（已跳过）")
        else:
            agents = list(found.values()) or saved

        if not agents:
            raise PollError("没有可用的代理账号；请在 config.json 填写 agent_codes")
        agents.sort(key=lambda item: item["code"])
        return agents

    # ── main polling loop ─────────────────────────────────────────────────

    def _loop(self) -> None:
        self._prepare_backfill()
        next_settled = 0.0
        while not self._stop_event.is_set():
            self._service_show_window()
            if self._relogin.is_set():
                raise ReloginRequested()
            cycle_start = time.monotonic()
            self._wake.clear()

            self._poll_open()

            if self._backfill_requested.is_set() and not self._backfill_running:
                self._start_backfill()
            if self._backfill_running:
                self._backfill_step()

            now = time.monotonic()
            if now >= next_settled:
                self._poll_settled()
                next_settled = time.monotonic() + float(self.config["settled_interval_seconds"])

            with self._lock:
                self.state["last_cycle_seconds"] = round(time.monotonic() - cycle_start, 3)
                self.state["poll_count"] += 1

            elapsed = time.monotonic() - cycle_start
            delay = max(0.05, float(self.config["open_interval_seconds"]) - elapsed)
            # A manual refresh starts the next OPEN round immediately.
            if self._wake.wait(timeout=delay):
                self._wake.clear()
                if self._relogin.is_set():
                    raise ReloginRequested()

    def _poll_open(self) -> None:
        started = now_iso()
        begin = time.monotonic()
        for agent in self.agents:
            try:
                rows, requests = self.fetch_status(agent, "OPEN")
                result = self.store.commit_snapshot(agent["code"], "OPEN", rows, {
                    "started_at": started,
                    "finished_at": now_iso(),
                    "elapsed_seconds": time.monotonic() - begin,
                    "request_count": requests,
                })
                if result["new"] or result["events"]:
                    with self._lock:
                        self.state["new_orders"] += result["new"]
                    message = (f"新单 {result['new']} 笔 · {agent['code']} · "
                               f"未结算 {result['records']} 笔")
                    print(f"[poll] {message}", flush=True)
                    self._set_message(message)
            except SessionExpired:
                raise
            except LoginWindowClosed:
                raise
            except PollError as exc:
                self._set_message(f"{agent['code']} 本轮校验失败：{_short_error(exc)}")
            except Exception as exc:  # noqa: BLE001 - one agent must not stop the loop
                self._set_message(f"{agent['code']} 本轮请求失败：{_short_error(exc)}")
        with self._lock:
            self.state["last_open_at"] = now_iso()
            if self.state["status"] != "online":
                self.state["status"] = "online"

    def _poll_settled(self) -> None:
        end = date.today()
        begin = end - timedelta(days=int(self.config["settled_window_days"]))
        started = now_iso()
        for index, agent in enumerate(self.agents):
            if index:
                # Keep new-order detection moving between agents during the
                # slower 7-day settled sweep.
                self._poll_open()
            settle_start = time.monotonic()
            try:
                rows, requests = self.fetch_status(
                    agent, "SETTLED", begin.isoformat(), end.isoformat()
                )
                self.store.commit_snapshot(agent["code"], "SETTLED", rows, {
                    "started_at": started,
                    "finished_at": now_iso(),
                    "elapsed_seconds": time.monotonic() - settle_start,
                    "request_count": requests,
                })
            except SessionExpired:
                raise
            except LoginWindowClosed:
                raise
            except PollError as exc:
                self._set_message(f"{agent['code']} 结算同步失败：{_short_error(exc)}")
            except Exception as exc:  # noqa: BLE001
                self._set_message(f"{agent['code']} 结算同步异常：{_short_error(exc)}")
        with self._lock:
            self.state["last_settled_at"] = now_iso()

    # ── pagination ────────────────────────────────────────────────────────

    def fetch_status(self, agent: Dict[str, Any], status: str,
                     from_date: Optional[str] = None,
                     to_date: Optional[str] = None) -> Tuple[List[Dict[str, Any]], int]:
        """Read and validate every page of one status query."""
        page_size = int(self.config["page_size"])
        records: List[Dict[str, Any]] = []
        identities = set()
        first_meta = None
        page = 1
        requests = 0
        profile = self.session.profile
        while True:
            response = self.session.fetch_orders(
                profile["user_id"], agent, status, page, page_size, from_date, to_date
            )
            requests += 1
            valid, meta = parse_page(response, page, page_size, status)
            if first_meta is not None and meta != first_meta:
                raise IncompletePage("翻页期间总数发生变化；等待下一轮")
            first_meta = meta
            for row in valid:
                identity = order_identity(row)
                if identity in identities:
                    raise IncompletePage("分页存在重复订单；本轮不提交")
                identities.add(identity)
            records.extend(valid)
            total, pages, _ = meta
            if page >= max(1, pages):
                break
            if page >= 1000:
                raise IncompletePage("分页数超过上限；本轮不提交")
            page += 1
        return records, requests

    # ── historical backfill ───────────────────────────────────────────────

    def _prepare_backfill(self) -> None:
        if not self._backfill_requested.is_set():
            return
        if self._backfill_running:
            return
        self._start_backfill()

    def _start_backfill(self) -> None:
        history_from = date.fromisoformat(str(self.config["history_from"]))
        # Full range: the regular settled loop will simply reconcile recent
        # days again, which is harmless and also helps one-shot CLI backfills.
        end = date.today()
        if end < history_from:
            self._backfill_requested.clear()
            return
        plan: List[Tuple[Dict[str, Any], date, date]] = []
        for agent in self.agents:
            for chunk_start, chunk_end in month_chunks(history_from, end):
                plan.append((agent, chunk_start, chunk_end))
        if not plan:
            self._backfill_requested.clear()
            return
        self._backfill_requested.clear()
        self._backfill_plan = plan
        self._backfill_done = 0
        self._backfill_total = len(plan)
        self._backfill_running = True
        with self._lock:
            self.state["backfill"] = {
                "running": True,
                "done": 0,
                "total": len(plan),
                "message": f"准备补采 {len(plan)} 个月度批次",
            }
        self._set_message(f"补历史订单：0/{len(plan)}")
        print(f"[backfill] 开始补采 {len(plan)} 个批次", flush=True)

    def _backfill_step(self) -> None:
        if not self._backfill_plan:
            self._finish_backfill()
            return
        agent, chunk_start, chunk_end = self._backfill_plan.pop(0)
        started = now_iso()
        begin = time.monotonic()
        text = f"{agent['code']} {chunk_start.isoformat()}~{chunk_end.isoformat()}"
        try:
            rows, requests = self.fetch_status(
                agent, "SETTLED", chunk_start.isoformat(), chunk_end.isoformat()
            )
            result = self.store.commit_snapshot(agent["code"], "SETTLED", rows, {
                "started_at": started,
                "finished_at": now_iso(),
                "elapsed_seconds": time.monotonic() - begin,
                "request_count": requests,
            })
            message = f"{text} 记录 {result['records']}，新增 {result['new']}"
        except SessionExpired:
            # Push the chunk back and let the supervisor re-login.
            self._backfill_plan.insert(0, (agent, chunk_start, chunk_end))
            raise
        except LoginWindowClosed:
            self._backfill_plan.insert(0, (agent, chunk_start, chunk_end))
            raise
        except Exception as exc:  # noqa: BLE001 - skip this month and continue
            message = f"{text} 失败：{_short_error(exc)}"
        self._backfill_done += 1
        with self._lock:
            self.state["backfill"]["done"] = self._backfill_done
            self.state["backfill"]["message"] = message
        self._set_message(f"补历史订单 {self._backfill_done}/{self._backfill_total}：{message}")
        print(f"[backfill] {self._backfill_done}/{self._backfill_total} {message}", flush=True)
        if not self._backfill_plan:
            self._finish_backfill()

    def _finish_backfill(self) -> None:
        self._backfill_running = False
        self._backfill_plan = []
        with self._lock:
            self.state["backfill"]["running"] = False
            self.state["backfill"]["message"] = "历史订单补采完成"
        self._set_message("历史订单补采完成")
        print("[backfill] 完成", flush=True)

    # ── one-shot CLI backfill ────────────────────────────────────────────

    def backfill_blocking(self, on_progress=None) -> Dict[str, Any]:
        """Login and backfill history synchronously (used by `run.py backfill`)."""
        self._ensure_store()
        self._open_session()
        self._start_backfill()
        if not self._backfill_running:
            return {"done": 0, "total": 0, "message": "没有需要补采的历史区间"}
        while self._backfill_plan and not self._stop_event.is_set():
            before = self._backfill_done
            self._backfill_step()
            if on_progress is not None and self._backfill_done != before:
                with self._lock:
                    on_progress(dict(self.state["backfill"]))
        with self._lock:
            return dict(self.state["backfill"])

    # ── state helpers ─────────────────────────────────────────────────────

    def _set_state(self, status: str, message: str) -> None:
        with self._lock:
            changed = self.state["status"] != status
            self.state["status"] = status
            self.state["message"] = message
            if changed:
                self.state["since"] = now_iso()
            if status == "error":
                self.state["last_error"] = message
            if changed:
                print(f"[poller] {status}: {message}", flush=True)

    def _set_message(self, message: str) -> None:
        with self._lock:
            self.state["message"] = message

    def _set_profile(self, profile: Dict[str, Any]) -> None:
        with self._lock:
            self.state["profile"] = dict(profile)

    def _close_session(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None
