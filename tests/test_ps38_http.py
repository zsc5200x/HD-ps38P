import json
import shlex
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from ps38app.errors import LoginRejected
from ps38app.net import HttpClient, HttpTransportError, socks5_connect, socks5_connect_host
from ps38app.ps38_http import Ps38HttpSession
from ps38app.store import PollError, SessionExpired


class HttpTransportTests(unittest.TestCase):
    def test_socks_handshake_accepts_fragmented_replies(self):
        sock = Mock()
        sock.recv.side_effect = [bytes([byte]) for byte in b"\x05\x02\x01\x00"]
        with patch("ps38app.net.socket.create_connection", return_value=sock):
            self.assertIs(socks5_connect("proxy.test", 1080, "user", "password"), sock)
        sock.recv.side_effect = [
            bytes([byte]) for byte in b"\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb"
        ]
        socks5_connect_host(sock, "example.test", 443)

    def test_socks_handshake_closes_connection_on_disconnect(self):
        sock = Mock()
        sock.recv.side_effect = [b"\x05", b""]
        with patch("ps38app.net.socket.create_connection", return_value=sock):
            with self.assertRaises(HttpTransportError):
                socks5_connect("proxy.test", 1080)
        sock.close.assert_called_once()

    def test_truncated_responses_are_errors_and_close_connection(self):
        responses = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\ninvalid\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 2",
        ]
        for response in responses:
            with self.subTest(response=response):
                client = HttpClient("example.test", scheme="http")
                sock = Mock()
                sock.recv.side_effect = [response, b""]
                client._sock = sock
                with self.assertRaises(HttpTransportError):
                    client.request("GET", "/")
                sock.close.assert_called_once()

    def test_not_modified_does_not_read_a_representation_body(self):
        client = HttpClient("example.test", scheme="http")
        sock = Mock()
        sock.recv.side_effect = [b"HTTP/1.1 304 Not Modified\r\nContent-Length: 200\r\n\r\n"]
        client._sock = sock
        self.assertEqual(client.request("GET", "/").body, b"")
        sock.recv.assert_called_once()
        client.close()


class _Server(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    auth_bodies = []
    otp_count = 0

    def log_message(self, fmt, *args):  # noqa: A003
        return

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in headers or []:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, payload, headers=None):
        self._send(
            200,
            json.dumps(payload).encode("utf-8"),
            [("Content-Type", "application/json")] + list(headers or []),
        )

    def do_GET(self):
        if self.path == "/partner/zh-cn/login":
            self._send(200, b"<html>login</html>", [("Set-Cookie", "JSESSIONID=seed; Path=/")])
        elif self.path == "/partner/captcha/key":
            self._send(200, b"fake-image", [("Content-Type", "image/jpeg")])
        elif self.path == "/partner-account/ddd/user-info/login-user/profile":
            cookie = self.headers.get("Cookie") or ""
            if "u_partner=ok" in cookie:
                self._json({"profile": {"userId": 7, "userCode": "TLH0R", "loginId": "okcodex77."}})
            else:
                self._send(403, b"SESSION_EXPIRED")
        else:
            self._send(404, b"not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path == "/partner/login/authentication":
            body = json.loads(raw.decode("utf-8"))
            type(self).auth_bodies.append(body)
            if body.get("captcha") == "":
                self._json(
                    {"required_captcha": "REQUIRED_CAPTCHA"}, [("Set-Cookie", "pctag=1; Path=/")]
                )
            elif body.get("captcha") == "1234":
                self._json(
                    {"status": "OK", "state": "otp"},
                    [("Set-Cookie", "u_partner_login=half; Path=/")],
                )
            else:
                self._json({"status": "INVALID_CAPTCHA"})
        elif self.path == "/partner/otp/validate":
            type(self).otp_count += 1
            self._send(200, b"OTP_SUCCESS_VALID", [("Set-Cookie", "u_partner=ok; Path=/")])
        elif self.path == "/partner-reporting/ddd/common/bet-list/v2":
            self._json({"body": {"totalSize": 0, "rows": []}})
        elif self.path == "/partner-reporting/ddd/report/win-loss-detail":
            self._json({"body": {"data": []}})
        else:
            self._send(404, b"not found")


class Ps38HttpTests(unittest.TestCase):
    def setUp(self):
        _Server.auth_bodies = []
        _Server.otp_count = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        command = f"{shlex.quote(sys.executable)} -c \"print('1234')\""
        self.config = {
            "base_url": f"http://127.0.0.1:{self.server.server_port}",
            "login_path": "/partner/zh-cn/login",
            "dashboard_path": "/partner-ui-v2/report-outstanding-v2",
            "profile_dir": str(self.root / "profile"),
            "username": "okcodex77.",
            "password": "secret",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "ps38_captcha_command": command,
            "ps38_captcha_timeout": 10,
            "ps38_captcha_dir": str(self.root / "captcha"),
            "ps38_http_timeout": 5,
        }

    def test_read_request_reconnects_after_socket_disconnect(self):
        session = Ps38HttpSession(self.config)
        session.client = Mock()
        response = Mock(headers={})
        session.client.request.side_effect = [ConnectionResetError("stale keep-alive"), response]
        self.assertIs(session._request("GET", "/"), response)
        self.assertEqual(session.client.request.call_count, 2)
        session.client.close.assert_called_once()

    def test_authentication_request_is_not_replayed_after_disconnect(self):
        session = Ps38HttpSession(self.config)
        session.client = Mock()
        session.client.request.side_effect = ConnectionResetError("response lost")
        with self.assertRaises(ConnectionResetError):
            session._request("POST", "/partner/login/authentication", retry=False)
        session.client.request.assert_called_once()

    def test_invalid_totp_secret_is_a_login_rejection(self):
        session = Ps38HttpSession(self.config)
        session.totp_secret = "not-base32!"
        with self.assertRaises(LoginRejected):
            session._submit_totp()

    def test_profile_server_failure_does_not_submit_credentials(self):
        session = Ps38HttpSession(self.config)
        session.fetch_profile = Mock(side_effect=PollError("HTTP 503"))
        session._login_flow = Mock()
        with self.assertRaises(PollError):
            session.login()
        session._login_flow.assert_not_called()
        session.close()

    def test_cookie_restore_rejects_foreign_domains_and_expired_cookies(self):
        session = Ps38HttpSession(self.config)
        session.storage_path.parent.mkdir(parents=True)
        session.storage_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "u_partner", "value": "ok", "domain": "127.0.0.1"},
                        {"name": "other_secret", "value": "private", "domain": "other.test"},
                        {
                            "name": "expired",
                            "value": "old",
                            "domain": "127.0.0.1",
                            "expires": time.time() - 1,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        session.start()
        self.assertEqual(session.cookies, {"u_partner": "ok"})
        session.close()

    def test_cookie_restore_ignores_malformed_storage(self):
        session = Ps38HttpSession(self.config)
        session.storage_path.parent.mkdir(parents=True)
        session.storage_path.write_text("[]", encoding="utf-8")
        session.start()
        self.assertEqual(session.cookies, {})
        session.close()

    def test_login_captcha_totp_and_cookie_reuse(self):
        session = Ps38HttpSession(self.config)
        profile = session.login()
        self.assertEqual(profile["user_code"], "TLH0R")
        self.assertEqual([body.get("captcha") for body in _Server.auth_bodies], ["", "1234"])
        self.assertEqual(_Server.otp_count, 1)
        storage = self.root / "profile" / "storage_state.json"
        self.assertTrue(storage.is_file())
        self.assertIn("u_partner", storage.read_text(encoding="utf-8"))
        session.close()

        # A second session reuses the saved cookie without another login.
        session2 = Ps38HttpSession(self.config)
        profile2 = session2.login()
        self.assertEqual(profile2["user_code"], "TLH0R")
        self.assertEqual(len(_Server.auth_bodies), 2)
        session2.close()

    def test_wrong_password_is_not_retried(self):
        original = _Server.do_POST

        def wrong(self_handler):
            length = int(self_handler.headers.get("Content-Length") or 0)
            self_handler.rfile.read(length)
            _Server.auth_bodies.append({"captcha": ""})
            self_handler._json({"status": "INVALID_USER"})

        _Server.do_POST = wrong
        try:
            session = Ps38HttpSession(self.config)
            self.addCleanup(session.close)
            with self.assertRaises(LoginRejected):
                session.login()
        finally:
            _Server.do_POST = original
        self.assertEqual(len(_Server.auth_bodies), 1)

    def test_missing_captcha_solver_is_rejected(self):
        config = dict(self.config)
        config["ps38_captcha_command"] = ""
        session = Ps38HttpSession(config)
        self.addCleanup(session.close)
        with self.assertRaises(LoginRejected):
            session.login()

    def test_profile_expiry_raises_session_expired(self):
        session = Ps38HttpSession(self.config)
        session.start()
        with self.assertRaises(SessionExpired):
            session.fetch_profile()
        session.close()

    def test_orders_and_hierarchy(self):
        session = Ps38HttpSession(self.config)
        session.login()
        orders = session.fetch_orders(7, {"user_id": 11, "code": "A1"}, "OPEN", 1, 10)
        self.assertEqual(orders["httpStatus"], 200)
        self.assertIsInstance(orders["json"], dict)
        hierarchy = session.fetch_hierarchy(7, "2026-10-01", "2026-10-31")
        self.assertEqual(hierarchy["httpStatus"], 200)
        session.close()


if __name__ == "__main__":
    unittest.main()
