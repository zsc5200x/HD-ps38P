"""HTTP server for the standalone PS38 events dashboard.

Serves the static page and a read-only REST API over the order database. The
collector runs in its own thread; this server never touches the browser.
"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from . import store as order_store
from .config import PROJECT_DIR, public_config
from .fixture_merge import CrownUnavailable, MappingConflict, MergeService
from .store import now_iso

WEB_DIR = PROJECT_DIR / "web"
MAX_JSON_BODY_BYTES = 1024 * 1024
BEIJING = timezone(timedelta(hours=8))
VERSION = "1.0.0"


def _parse_day(value: str, *, end: bool = False, tz_minutes: int = 480):
    """Parse a YYYY-MM-DD day boundary in the given UTC offset into epoch ms."""
    text = str(value or "").strip()
    if not text:
        return None
    zone = timezone(timedelta(minutes=tz_minutes))
    day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=zone)
    if end:
        return int((day + timedelta(days=1)).timestamp() * 1000) - 1
    return int(day.timestamp() * 1000)


def _hours_bounds(query) -> Optional[tuple[int, int]]:
    """Exact rolling window (e.g. last 24h), overriding day inputs."""
    try:
        hours = float((query.get("hours") or [""])[0])
    except (TypeError, ValueError):
        return None
    if hours <= 0 or hours > 720:
        return None
    now_ms = int(time.time() * 1000)
    return now_ms - int(hours * 3600_000), now_ms


def _day_utc_bounds(from_value: str, to_value: str, *, tz_minutes: int = 480):
    """Return the UTC [start, end] strings for a local day range."""
    zone = timezone(timedelta(minutes=tz_minutes))

    def bound(value: str, end: bool):
        text = str(value or "").strip()
        if not text:
            return None
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=zone)
        if end:
            day = day + timedelta(days=1) - timedelta(seconds=1)
        return day.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    return bound(from_value, False), bound(to_value, True)


class Handler(BaseHTTPRequestHandler):
    server_version = "PS38Events/" + VERSION
    protocol_version = "HTTP/1.1"

    # ── plumbing ──────────────────────────────────────────────────────────

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep the console readable; errors are still surfaced by the poller.
        if self.server.config.get("verbose"):
            print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def address_string(self) -> str:
        if isinstance(self.client_address, tuple) and self.client_address:
            return str(self.client_address[0])
        return "local"

    def send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json_body(self) -> Dict[str, Any]:
        raw = getattr(self, "_request_body", b"") or b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是 JSON") from exc
        return payload if isinstance(payload, dict) else {}

    def same_origin(self) -> bool:
        """Reject cross-site POSTs so a random web page cannot trigger actions."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return urlsplit(origin).netloc == host

    def serve_static(self, path: str) -> None:
        decoded = unquote(path)
        if decoded == "/favicon.ico":
            self.send_response(204)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            return
        if decoded.startswith("/static/"):
            relative = decoded[len("/static/") :]
        else:
            relative = decoded.lstrip("/") or "index.html"
        candidate = (WEB_DIR / relative).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self.send_error(404)
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if candidate.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif candidate.suffix in {".html", ".css"}:
            content_type = f"{content_type}; charset=utf-8"
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)

    def _connection(self):
        connection = order_store.open_readonly(self.server.config["db_path"])
        return connection

    # ── routing ───────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if parsed.path == "/api/my-events":
                self._my_events(query)
            elif parsed.path == "/api/my-events/fixtures":
                self._my_events_fixtures(query)
            elif parsed.path == "/api/my-events/copy-groups":
                self._my_events_copy(query)
            elif parsed.path == "/api/my-events/fixture":
                self._my_events_fixture(query)
            elif parsed.path == "/api/my-events/summary":
                self._my_events_summary(query)
            elif parsed.path == "/api/my-events/events":
                self._my_events_events(query)
            elif parsed.path == "/api/merge":
                self._merge()
            elif parsed.path == "/api/follow":
                self._follow_status(query)
            elif parsed.path.startswith("/api/follow/groups/"):
                self._follow_detail(unquote(parsed.path[len("/api/follow/groups/") :]))
            elif parsed.path == "/api/pool":
                self._pool_status()
            elif parsed.path == "/api/status":
                self._status()
            elif parsed.path == "/api/health":
                self.send_json(
                    {
                        "ok": True,
                        "version": VERSION,
                        "serverTime": now_iso(),
                        "session": self.server.poller.snapshot()["status"],
                    }
                )
            else:
                self.serve_static(parsed.path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - never crash the server thread
            self.send_json({"ok": False, "error": f"服务内部错误：{exc}"}, status=500)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        # Always drain the request body: HTTP/1.1 keep-alive would otherwise
        # parse leftover bytes as the next request line. Keep the bytes so the
        # merge endpoints can parse their JSON action payload.
        self._request_body = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (ValueError, OSError):
            length = 0
        if length > MAX_JSON_BODY_BYTES:
            self.send_json({"ok": False, "error": "请求体过大"}, status=413)
            return
        if length > 0:
            try:
                self._request_body = self.rfile.read(length)
            except OSError:
                self._request_body = b""
        if not self.same_origin():
            self.send_json({"ok": False, "error": "跨站请求被拒绝"}, status=403)
            return
        if parsed.path == "/api/actions/refresh":
            self.server.poller.request_refresh()
            self.send_json({"ok": True, "message": "已请求立即拉取订单"})
            return
        if parsed.path == "/api/actions/backfill":
            started = self.server.poller.request_backfill()
            if started:
                self.send_json({"ok": True, "message": "已开始补采历史订单"})
            else:
                self.send_json({"ok": True, "message": "补采已在进行中"})
            return
        if parsed.path == "/api/actions/login":
            self.server.poller.request_relogin()
            self.send_json({"ok": True, "message": "正在重新登录，请在浏览器完成登录/2FA"})
            return
        if parsed.path == "/api/actions/open-login":
            self.server.poller.request_show_window()
            self.send_json({"ok": True, "message": "正在打开后台浏览器窗口…"})
            return
        if parsed.path == "/api/merge/refresh":
            self._merge_refresh()
            return
        if parsed.path == "/api/merge/league-mapping":
            self._merge_league_mapping()
            return
        if parsed.path == "/api/merge/mapping":
            self._merge_mapping()
            return
        if parsed.path == "/api/merge/auto":
            self._merge_auto()
            return
        if parsed.path.startswith("/api/pool/"):
            self._pool_action(parsed.path)
            return
        if parsed.path.startswith("/api/follow/"):
            self._follow_action(parsed.path)
            return
        self.send_json({"ok": False, "error": "接口不存在"}, status=404)

    # ── follow groups and confirmed receipts ──────────────────────────────

    def _follow_service(self):
        service = getattr(self.server, "follow_service", None)
        if service is None:
            self.send_json({"ok": False, "error": "跟单服务暂不可用，请检查启动状态"}, status=503)
        return service

    def _follow_accounts(self):
        pool = getattr(self.server, "pool", None)
        if pool is None:
            return []
        accounts = pool.snapshot().get("accounts", [])
        return [
            {
                **{
                    key: account.get(key)
                    for key in ("id", "username", "status", "enabled", "platform")
                },
                "label": account.get("name") or account.get("label") or account.get("username"),
            }
            for account in accounts
            if account.get("platform") == "crown_member"
        ]

    def _follow_status(self, query) -> None:
        service = self._follow_service()
        if service is None:
            return
        try:
            status = (query.get("status") or ["SUCCEEDED"])[0].upper()
            if status not in {"SUCCEEDED", "ALL", "PROCESSING", "FAILED", "UNKNOWN"}:
                raise ValueError("跟单状态筛选无效")
            limit, offset = self._limit_offset(query)
            payload = service.snapshot(
                status=status,
                search=(query.get("search") or [""])[0][:200],
                limit=limit,
                offset=offset,
            )
            try:
                payload["accounts"] = self._follow_accounts()
            except Exception:
                payload["accounts"] = []
                payload["account_warning"] = "账号列表暂不可用，请在号池检查账号"
            self.send_json(payload)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
        except Exception:
            self.send_json({"ok": False, "error": "跟单记录读取失败，请稍后重试"}, status=500)

    def _follow_detail(self, group_id: str) -> None:
        service = self._follow_service()
        if service is None:
            return
        try:
            if not group_id or len(group_id) > 128 or "/" in group_id:
                raise ValueError("跟单组编号无效")
            self.send_json(service.detail(group_id))
        except KeyError:
            self.send_json({"ok": False, "error": "跟单记录不存在"}, status=404)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
        except Exception:
            self.send_json({"ok": False, "error": "跟单明细读取失败，请稍后重试"}, status=500)

    def _follow_action(self, path: str) -> None:
        service = self._follow_service()
        if service is None:
            return
        try:
            if path == "/api/follow/rule":
                body = self.read_json_body()
                if not body:
                    raise ValueError("请填写跟单规则")
                rule = service.save_rule(body)
                self.send_json({"ok": True, "rule": rule, "message": "跟单规则已保存"})
            elif path == "/api/follow/pause":
                service.pause()
                self.send_json({"ok": True, "message": "跟单已暂停，在途结果将继续记录"})
            elif path.startswith("/api/follow/groups/"):
                parts = path.split("/")
                if len(parts) != 6 or parts[5] not in {"reconcile", "resolution"}:
                    self.send_json({"ok": False, "error": "接口不存在"}, status=404)
                    return
                group_id = unquote(parts[4])
                if not group_id or len(group_id) > 128 or "/" in group_id:
                    raise ValueError("跟单组编号无效")
                if parts[5] == "reconcile":
                    self.send_json(service.request_reconcile(group_id))
                else:
                    self.send_json(service.resolve_unknown(group_id, self.read_json_body()))
            else:
                self.send_json({"ok": False, "error": "接口不存在"}, status=404)
        except KeyError:
            self.send_json({"ok": False, "error": "跟单记录不存在"}, status=404)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
        except Exception:
            self.send_json({"ok": False, "error": "跟单规则操作失败，请稍后重试"}, status=500)

    # ── account pool ──────────────────────────────────────────────────────

    def _pool(self):
        pool = getattr(self.server, "pool", None)
        if pool is None:
            self.send_json({"ok": False, "error": "账号池未启用"}, status=503)
            return None
        return pool

    def _pool_body(self) -> Dict[str, Any]:
        raw = getattr(self, "_request_body", b"") or b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是 JSON") from exc
        return payload if isinstance(payload, dict) else {}

    def _pool_status(self) -> None:
        pool = self._pool()
        if pool is None:
            return
        try:
            self.send_json(pool.snapshot())
        except Exception as exc:  # noqa: BLE001
            self.send_json({"ok": False, "error": f"账号池读取失败：{exc}"}, status=500)

    def _pool_action(self, path: str) -> None:
        pool = self._pool()
        if pool is None:
            return
        try:
            body = self._pool_body()
            if path == "/api/pool/save":
                account = pool.save_account(body, body.get("id") or None)
                self.send_json({"ok": True, "account": account, "message": "账号已保存"})
            elif path == "/api/pool/login":
                account_id = str(body.get("id") or "")
                if not account_id:
                    raise ValueError("缺少账号 ID")
                result = pool.login(account_id, force=bool(body.get("force")))
                self.send_json({"ok": True, **result})
            elif path == "/api/pool/check":
                account_id = str(body.get("id") or "")
                if not account_id:
                    raise ValueError("缺少账号 ID")
                result = pool.check(account_id)
                self.send_json({"ok": True, **result})
            elif path == "/api/pool/check-all":
                result = pool.check_all()
                self.send_json({"ok": True, **result})
            elif path == "/api/pool/clear":
                account_id = str(body.get("id") or "")
                account = pool.clear_session(account_id)
                self.send_json({"ok": True, "account": account, "message": "已清除会话"})
            elif path == "/api/pool/delete":
                account_id = str(body.get("id") or "")
                pool.delete_account(account_id)
                self.send_json({"ok": True, "message": "账号已删除"})
            elif path == "/api/pool/toggle":
                account_id = str(body.get("id") or "")
                account = pool.toggle(account_id, bool(body.get("enabled")))
                self.send_json(
                    {
                        "ok": True,
                        "account": account,
                        "message": "账号已启用" if account["enabled"] else "账号已停用",
                    }
                )
            elif path == "/api/pool/cancel":
                account_id = str(body.get("id") or "")
                result = pool.cancel(account_id)
                self.send_json({"ok": True, **result})
            else:
                self.send_json({"ok": False, "error": "接口不存在"}, status=404)
        except KeyError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=404)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if message.startswith(
                ("该账号", "账号已", "采集器", "请选择", "请填写", "已有账号", "登录")
            ):
                self.send_json({"ok": False, "error": message}, status=400)
            else:
                self.send_json({"ok": False, "error": f"账号池操作失败：{message}"}, status=500)

    # ── scopes / queries ──────────────────────────────────────────────────

    def _scope(self, query) -> Dict[str, Any]:
        status = (query.get("status") or ["OPEN"])[0].strip().upper()
        if status not in {"OPEN", "SETTLED", "ALL"}:
            status = "OPEN"
        agent = (query.get("agent") or [""])[0].strip() or None
        search = (query.get("search") or [""])[0].strip()
        basis = (query.get("basis") or ["created"])[0].strip().lower()
        if basis not in {"created", "settled"}:
            basis = "created"
        try:
            tz_minutes = int((query.get("tz") or ["480"])[0])
        except ValueError:
            tz_minutes = 480
        tz_minutes = max(-720, min(840, tz_minutes))
        from_value = (query.get("from") or [""])[0]
        to_value = (query.get("to") or [""])[0]
        from_ms = _parse_day(from_value, end=False, tz_minutes=tz_minutes)
        to_ms = _parse_day(to_value, end=True, tz_minutes=tz_minutes)
        from_utc, to_utc = _day_utc_bounds(from_value, to_value, tz_minutes=tz_minutes)
        hours_window = _hours_bounds(query)
        if hours_window:
            from_ms, to_ms = hours_window
            from_utc = datetime.fromtimestamp(from_ms / 1000, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            to_utc = datetime.fromtimestamp(to_ms / 1000, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        return {
            "status": status,
            "agent": agent,
            "search": search,
            "basis": basis,
            "tz": tz_minutes,
            "from_ms": from_ms,
            "to_ms": to_ms,
            "from_utc": from_utc,
            "to_utc": to_utc,
        }

    def _limit_offset(self, query) -> tuple[int, int]:
        limit = max(1, min(200, int((query.get("limit") or ["50"])[0])))
        offset = max(0, int((query.get("offset") or ["0"])[0]))
        return limit, offset

    def _agents(self, connection) -> list:
        try:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT code, level, name FROM partner_agents ORDER BY code"
                )
            ]
        except sqlite3.Error:
            return []

    def _my_events(self, query) -> None:
        try:
            scope = self._scope(query)
            limit, offset = self._limit_offset(query)
        except ValueError:
            self.send_json({"ok": False, "error": "查询参数格式不正确"}, status=400)
            return
        connection = self._connection()
        if connection is None:
            self.send_json(
                {
                    "ok": True,
                    "ready": False,
                    "total": 0,
                    "rows": [],
                    "agents": [],
                    "status": scope["status"],
                }
            )
            return
        try:
            payload = order_store.query_orders(
                connection,
                None if scope["status"] == "ALL" else scope["status"],
                scope["agent"],
                scope["search"],
                limit,
                offset,
                scope["from_ms"],
                scope["to_ms"],
                basis=scope["basis"],
                from_utc=scope["from_utc"],
                to_utc=scope["to_utc"],
            )
            payload["agents"] = self._agents(connection)
        except sqlite3.Error as exc:
            self.send_json({"ok": False, "error": f"订单库读取失败：{exc}"}, status=500)
            return
        finally:
            connection.close()
        payload.update(
            {
                "ok": True,
                "ready": True,
                "status": scope["status"],
                "basis": scope["basis"],
                "tz": scope["tz"],
            }
        )
        self.send_json(payload)

    def _my_events_fixtures(self, query) -> None:
        try:
            scope = self._scope(query)
            limit, offset = self._limit_offset(query)
        except ValueError:
            self.send_json({"ok": False, "error": "查询参数格式不正确"}, status=400)
            return
        connection = self._connection()
        if connection is None:
            self.send_json(
                {
                    "ok": True,
                    "ready": False,
                    "total": 0,
                    "rows": [],
                    "status": scope["status"],
                    "basis": scope["basis"],
                    "tz": scope["tz"],
                }
            )
            return
        try:
            payload = order_store.query_fixtures(
                connection,
                scope["status"],
                scope["agent"],
                scope["search"],
                limit,
                offset,
                scope["from_ms"],
                scope["to_ms"],
                basis=scope["basis"],
                from_utc=scope["from_utc"],
                to_utc=scope["to_utc"],
            )
            payload["agents"] = self._agents(connection)
        except sqlite3.Error as exc:
            self.send_json({"ok": False, "error": f"比赛聚合读取失败：{exc}"}, status=500)
            return
        finally:
            connection.close()
        payload.update(
            {
                "ok": True,
                "ready": True,
                "status": scope["status"],
                "basis": scope["basis"],
                "tz": scope["tz"],
            }
        )
        self.send_json(payload)

    def _my_events_copy(self, query) -> None:
        try:
            scope = self._scope(query)
            limit, offset = self._limit_offset(query)
        except ValueError:
            self.send_json({"ok": False, "error": "查询参数格式不正确"}, status=400)
            return
        connection = self._connection()
        if connection is None:
            self.send_json(
                {
                    "ok": True,
                    "ready": False,
                    "total": 0,
                    "rows": [],
                    "status": scope["status"],
                    "basis": scope["basis"],
                    "tz": scope["tz"],
                }
            )
            return
        try:
            payload = order_store.query_copy_groups(
                connection,
                scope["status"],
                scope["agent"],
                scope["search"],
                limit,
                offset,
                scope["from_ms"],
                scope["to_ms"],
                basis=scope["basis"],
                from_utc=scope["from_utc"],
                to_utc=scope["to_utc"],
            )
            payload["agents"] = self._agents(connection)
        except sqlite3.Error as exc:
            self.send_json({"ok": False, "error": f"跟单合并读取失败：{exc}"}, status=500)
            return
        finally:
            connection.close()
        payload.update(
            {
                "ok": True,
                "ready": True,
                "status": scope["status"],
                "basis": scope["basis"],
                "tz": scope["tz"],
            }
        )
        self.send_json(payload)

    def _my_events_fixture(self, query) -> None:
        teams = (query.get("teams") or [""])[0].strip()
        try:
            event_ms = int((query.get("event") or ["0"])[0])
        except ValueError:
            event_ms = 0
        if not teams or event_ms <= 0:
            self.send_json({"ok": False, "error": "teams 和 event 参数不能为空"}, status=400)
            return
        try:
            scope = self._scope(query)
        except ValueError:
            self.send_json({"ok": False, "error": "日期格式必须是 YYYY-MM-DD"}, status=400)
            return
        connection = self._connection()
        if connection is None:
            self.send_json({"ok": True, "ready": False, "total": 0, "rows": [], "crown": None})
            return
        try:
            payload = order_store.query_fixture_orders(
                connection,
                teams,
                event_ms,
                scope["status"],
                scope["agent"],
                scope["search"],
                500,
                scope["from_ms"],
                scope["to_ms"],
                basis=scope["basis"],
                from_utc=scope["from_utc"],
                to_utc=scope["to_utc"],
            )
        except sqlite3.Error as exc:
            self.send_json({"ok": False, "error": f"比赛订单读取失败：{exc}"}, status=500)
            return
        finally:
            connection.close()
        payload["crown"] = None
        payload.update({"ok": True, "ready": True})
        self.send_json(payload)

    def _my_events_summary(self, query) -> None:
        try:
            scope = self._scope(query)
        except ValueError:
            self.send_json({"ok": False, "error": "日期格式必须是 YYYY-MM-DD"}, status=400)
            return
        connection = self._connection()
        if connection is None:
            self.send_json({"ok": True, "ready": False, "open": 0, "settled": 0, "states": []})
            return
        try:
            payload = order_store.query_summary(
                connection,
                scope["agent"],
                scope["from_ms"],
                scope["to_ms"],
                basis=scope["basis"],
                from_utc=scope["from_utc"],
                to_utc=scope["to_utc"],
            )
        except sqlite3.Error as exc:
            self.send_json({"ok": False, "error": f"订单汇总读取失败：{exc}"}, status=500)
            return
        finally:
            connection.close()
        payload.update({"ok": True, "ready": True, "basis": scope["basis"], "tz": scope["tz"]})
        self.send_json(payload)

    def _my_events_events(self, query) -> None:
        raw = (query.get("cursor") or [""])[0].strip()
        if raw == "":
            cursor = None
        else:
            try:
                cursor = int(raw)
            except ValueError:
                self.send_json({"ok": False, "error": "cursor 格式不正确"}, status=400)
                return
            if cursor < 0:
                self.send_json({"ok": False, "error": "cursor 不能为负数"}, status=400)
                return
        connection = self._connection()
        if connection is None:
            self.send_json({"ok": True, "ready": False, "cursor": 0, "reset": False, "events": []})
            return
        try:
            payload = order_store.query_events(connection, cursor, 50)
        except sqlite3.Error as exc:
            self.send_json({"ok": False, "error": f"新单事件读取失败：{exc}"}, status=500)
            return
        finally:
            connection.close()
        payload.update({"ok": True, "ready": True})
        self.send_json(payload)

    # ── cross-book merge ─────────────────────────────────────────────────

    def _merge_service(self):
        service = getattr(self.server, "merge_service", None)
        if service is None:
            self.send_json(
                {
                    "ok": False,
                    "error": getattr(self.server, "merge_error", "") or "赛事合并服务未初始化。",
                },
                status=503,
            )
        return service

    def _merge(self) -> None:
        service = self._merge_service()
        if service is None:
            return
        try:
            payload = service.payload()
        except Exception as exc:  # noqa: BLE001 - surface the cause to the page
            self.send_json({"ok": False, "error": f"合并数据读取失败：{exc}"}, status=500)
            return
        self.send_json(payload)

    def _merge_error(self, exc: Exception) -> None:
        if isinstance(exc, CrownUnavailable):
            self.send_json({"ok": False, "error": str(exc)}, status=503)
        elif isinstance(exc, MappingConflict):
            self.send_json({"ok": False, "error": str(exc)}, status=409)
        elif isinstance(exc, ValueError):
            self.send_json({"ok": False, "error": str(exc)}, status=400)
        else:
            self.send_json({"ok": False, "error": f"操作失败：{exc}"}, status=500)

    def _merge_refresh(self) -> None:
        service = self._merge_service()
        if service is None:
            return
        try:
            payload = self.read_json_body()
            source = str(payload.get("source") or "").strip()
            if source == "crown":
                result = service.pull_crown()
                message = f"皇冠赛事已拉取：{result['count']} 场"
            elif source == "pinnacle":
                result = service.pull_pinnacle()
                message = f"平博赛事已拉取：{result['count']} 场"
            else:
                raise ValueError("不支持的拉取来源。")
        except (CrownUnavailable, MappingConflict, ValueError) as exc:
            self._merge_error(exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._merge_error(exc)
            return
        self.send_json(
            {
                "ok": True,
                "source": source,
                "count": result.get("count", 0),
                "refreshed": result.get("refreshed"),
                "autoBound": result.get("autoBound", 0),
                "autoError": result.get("autoError"),
                "message": message,
            }
        )

    def _merge_league_mapping(self) -> None:
        service = self._merge_service()
        if service is None:
            return
        try:
            payload = self.read_json_body()
            action = str(payload.get("action") or "").strip()
            crown_id = str(payload.get("crownLeagueId") or "").strip()
            if not crown_id or len(crown_id) > 128:
                raise ValueError("皇冠联赛 ID 无效。")
            if action == "unmap":
                service.unbind_league(crown_id)
                self.send_json({"ok": True, "message": "已解除联赛绑定"})
                return
            if action != "map":
                raise ValueError("不支持的联赛绑定操作。")
            ps_id = str(payload.get("ps38LeagueId") or "").strip()
            if not ps_id or len(ps_id) > 256:
                raise ValueError("PS38 联赛 ID 无效。")
            saved = service.bind_league(ps_id, crown_id)
        except (CrownUnavailable, MappingConflict, ValueError) as exc:
            self._merge_error(exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._merge_error(exc)
            return
        self.send_json(
            {
                "ok": True,
                "message": "联赛已绑定",
                "mapping": saved,
                "autoBound": saved.get("autoBound", 0),
                "autoError": saved.get("autoError"),
            }
        )

    def _merge_mapping(self) -> None:
        service = self._merge_service()
        if service is None:
            return
        try:
            payload = self.read_json_body()
            action = str(payload.get("action") or "").strip()
            ps_id = str(payload.get("ps38Id") or "").strip()
            if not ps_id or len(ps_id) > 128:
                raise ValueError("PS38 赛事 ID 无效。")
            if action == "unmap":
                service.unbind_fixture(ps_id)
                self.send_json({"ok": True, "message": "已解除赛事合并"})
                return
            if action != "map":
                raise ValueError("不支持的赛事合并操作。")
            crown_id = str(payload.get("crownId") or "").strip()
            if not crown_id or len(crown_id) > 128:
                raise ValueError("皇冠赛事 ID 无效。")
            saved = service.bind_fixture(ps_id, crown_id)
        except (CrownUnavailable, MappingConflict, ValueError) as exc:
            self._merge_error(exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._merge_error(exc)
            return
        self.send_json({"ok": True, "message": "赛事已合并", "mapping": saved})

    def _merge_auto(self) -> None:
        service = self._merge_service()
        if service is None:
            return
        try:
            result = service.auto_merge()
        except (CrownUnavailable, MappingConflict, ValueError) as exc:
            self._merge_error(exc)
            return
        except Exception as exc:  # noqa: BLE001
            self._merge_error(exc)
            return
        self.send_json(result)

    def _status(self) -> None:
        state = self.server.poller.snapshot()
        connection = self._connection()
        if connection is not None:
            connection.close()
        self.send_json(
            {
                "ok": True,
                "version": VERSION,
                "serverTime": now_iso(),
                "uptimeSeconds": round(time.monotonic() - self.server.started_monotonic, 1),
                "session": {
                    "status": state["status"],
                    "message": state["message"],
                    "since": state["since"],
                    "last_error": state["last_error"],
                    "profile": state["profile"],
                    "agents": state["agents"],
                    "last_open_at": state["last_open_at"],
                    "last_settled_at": state["last_settled_at"],
                    "last_cycle_seconds": state["last_cycle_seconds"],
                    "poll_count": state["poll_count"],
                    "new_orders": state["new_orders"],
                },
                "backfill": state["backfill"],
                "counts": state["counts"],
                "db": {
                    "path": str(self.server.config["db_path"]),
                    "ready": connection is not None or Path(self.server.config["db_path"]).exists(),
                },
                "config": public_config(self.server.config),
            }
        )


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, config: Dict[str, Any], poller, pool=None):
        self.config = config
        self.poller = poller
        self.pool = pool
        self.started_monotonic = time.monotonic()
        self.merge_error = ""
        self.merge_service = None
        self.follow_service = None
        self.follow_error = ""
        # 端口占用时先退出，避免第二次启动触发在途跟单恢复。
        super().__init__(address, Handler)
        try:
            self.merge_service = MergeService(config)
        except Exception as exc:  # noqa: BLE001 - keep the order dashboard usable
            self.merge_service = None
            self.merge_error = f"赛事合并存储初始化失败：{exc}"
        try:
            from .follow.service import FollowService

            self.follow_service = FollowService(config)
        except Exception:
            self.follow_error = "跟单存储初始化失败，请检查跟单数据库路径和权限"

    def server_close(self) -> None:
        if self.follow_service is not None:
            self.follow_service.stop()
        super().server_close()


def build_server(config: Dict[str, Any], poller, pool=None) -> DashboardServer:
    return DashboardServer((config["host"], int(config["port"])), config, poller, pool)


def serve(config: Dict[str, Any], poller) -> None:
    server = build_server(config, poller)
    if server.follow_service is not None:
        server.follow_service.start()
    print(f"[server] PS38 赛事看板：http://{config['host']}:{config['port']}/", flush=True)
    print("[server] 仅本机访问；浏览器完成登录/2FA 后自动开始拉取订单", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
