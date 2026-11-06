import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ps38app.account_pool.crown_http import CrownHttpError, CrownHttpSession
from ps38app.account_pool.store import AccountStore
from tests.support import isolated_config


class _Server(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    login_posts = 0
    heartbeat_posts = 0
    fail_first_login = False
    double_login = False
    table_error = False
    rotate_cookie = False

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

    def do_GET(self):
        if self.path == "/":
            self._send(200, b"<html>ok</html>", [("Set-Cookie", "test=init; Path=/")])
        else:
            self._send(404, b"no")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        fields = urllib.parse.parse_qs(raw.decode("utf-8"))
        op = (fields.get("p") or [""])[0]
        if op == "chk_login" and self.path.startswith(("/transform_nl.php", "/transform.php")):
            type(self).login_posts += 1
            if _Server.fail_first_login and self.path.startswith("/transform_nl.php"):
                self._send(200, b"<html>no login</html>")
                return
            if (fields.get("username") or [""])[0] != "okcodex7778":
                self._send(
                    200, b"<serverresponse><status>500</status><msg>0</msg></serverresponse>"
                )
                return
            xml = (
                b'<?xml version="1.0"?><serverresponse><status>200</status>'
                b"<msg>100</msg><username>ctk11dr5b8</username><mid>46295381</mid>"
                b"<uid>uid-1</uid><passwd_safe>okcodex7778</passwd_safe>"
                b"<currency>RMB</currency></serverresponse>"
            )
            self._send(200, xml, [("Set-Cookie", "login_46295381=abc; Path=/")])
            return
        if self.path.startswith("/transform.php") and op == "get_member_data":
            type(self).heartbeat_posts += 1
            if _Server.double_login:
                self._send(
                    200,
                    b"<serverresponse><code>error</code><msg>doubleLogin</msg></serverresponse>",
                )
                return
            if _Server.table_error or (fields.get("uid") or [""])[0] != "uid-1":
                self._send(200, b"table id error")
                return
            xml = (
                b"<serverresponse><code>get_all_data</code><enable>Y</enable>"
                b"<currency>RMB</currency><maxcredit>12000</maxcredit>"
                b"<nowcredit>12000</nowcredit></serverresponse>"
            )
            self._send(
                200,
                xml,
                [("Set-Cookie", "login_46295381=rotated; Path=/")]
                if _Server.rotate_cookie
                else None,
            )
            return
        self._send(404, b"no")


class CrownHttpTests(unittest.TestCase):
    def setUp(self):
        _Server.login_posts = 0
        _Server.heartbeat_posts = 0
        _Server.fail_first_login = False
        _Server.double_login = False
        _Server.table_error = False
        _Server.rotate_cookie = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = isolated_config(Path(self.tmp.name))
        self.config["crown_http_user_agent"] = "TestAgent/1.0"
        self.store = AccountStore(self.config["pool_db_path"], self.config)
        self.account = self.store.save_account(
            {
                "platform": "crown_member",
                "name": "皇冠会员测试",
                "base_url": f"http://127.0.0.1:{self.server.server_port}",
                "login_path": "/",
                "username": "okcodex7778",
            }
        )
        self.store.save_credentials(self.account["id"], "okcodex7778", "secret")

    def make_session(self):
        return CrownHttpSession(self.store.get(self.account["id"]), self.config, self.store)

    def test_login_heartbeat_save_and_restore(self):
        session = self.make_session()
        profile = session.login()
        self.assertEqual(profile["user_code"], "ctk11dr5b8")
        self.assertEqual(session.meta["uid"], "uid-1")
        self.assertEqual(session.meta["balance"], "12000")
        self.assertEqual(_Server.login_posts, 1)
        session.close()

        storage = self.store.paths(self.account["id"])["storage"]
        self.assertIn("login_46295381", storage.read_text(encoding="utf-8"))
        meta = self.store.load_meta(self.account["id"])
        self.assertEqual(meta["uid"], "uid-1")
        self.assertEqual(meta["balance"], "12000")

        # A new session restores the saved session without another login.
        session2 = self.make_session()
        profile2 = session2.login()
        self.assertEqual(profile2["user_code"], "ctk11dr5b8")
        self.assertEqual(_Server.login_posts, 1)
        session2.close()

    def test_login_falls_back_to_transform_php(self):
        _Server.fail_first_login = True
        session = self.make_session()
        session.login()
        self.assertEqual(_Server.login_posts, 2)
        session.close()

    def test_double_login_is_conflict(self):
        session = self.make_session()
        session.login()
        _Server.double_login = True
        result = session.check()
        self.assertEqual(result["status"], "conflict")
        session.close()

    def test_table_id_error_is_expired(self):
        session = self.make_session()
        session.login()
        _Server.table_error = True
        result = session.check()
        self.assertEqual(result["status"], "expired")
        session.close()

    def test_wrong_credentials_raise(self):
        self.store.save_credentials(self.account["id"], "wrong-user", "wrong")
        session = self.make_session()
        with self.assertRaises(CrownHttpError):
            session.login()
        session.close()

    def test_heartbeat_cookie_rotation_is_saved(self):
        _Server.rotate_cookie = True
        session = self.make_session()
        session.login()
        self.assertEqual(session.cookies["login_46295381"], "rotated")
        self.assertEqual(self.make_session().cookies["login_46295381"], "rotated")

    def test_restore_filters_expired_and_other_site_cookies(self):
        self.store._write_private(
            self.store.paths(self.account["id"])["storage"],
            {
                "cookies": [
                    {"name": "valid", "value": "yes", "domain": "127.0.0.1", "path": "/"},
                    {"name": "foreign", "value": "secret", "domain": "other.example", "path": "/"},
                    {
                        "name": "expired",
                        "value": "old",
                        "domain": "127.0.0.1",
                        "path": "/",
                        "expires": 1,
                    },
                ],
                "origins": [],
            },
        )
        self.assertEqual(self.make_session().cookies, {"valid": "yes"})

    def test_proxy_credentials_are_cached_in_memory_without_files(self):
        self.store.bind_environment(self.account["id"], "test-env")
        proxy = {
            "type": "socks5",
            "host": "127.0.0.1",
            "port": "1080",
            "username": "private-user",
            "password": "private-password",
        }
        with mock.patch("ps38app.account_pool.crown_http.AdsPowerClient") as ads:
            ads.return_value.profile.return_value = {"ip": "127.0.0.1"}
            ads.return_value.proxy.return_value = proxy
            self.assertEqual(self.make_session()._proxy_config(), proxy)
            ads.return_value.profile.side_effect = RuntimeError("API 暂时不可用")
            self.assertEqual(self.make_session()._proxy_config(), proxy)
        self.assertFalse((self.store.paths(self.account["id"])["dir"] / "proxy.json").exists())


if __name__ == "__main__":
    unittest.main()
