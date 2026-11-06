import argparse
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import run
from ps38app.poller import Poller


class LoginCommandTests(unittest.TestCase):
    def test_login_uses_poller_session_factory_and_closes_session(self):
        session = Mock()
        session.login.return_value = {"user_code": "PRIMARY", "user_id": 7}
        config = {"profile_dir": "/unused-in-test"}
        with (
            patch("ps38app.poller.Poller.create_session", return_value=session) as factory,
            patch("run.load_config", return_value=config),
        ):
            self.assertEqual(run.cmd_login(argparse.Namespace(debug=False, force=True)), 0)
        factory.assert_called_once()
        session.login.assert_called_once()
        self.assertTrue(session.login.call_args.kwargs["force"])
        session.close.assert_called_once()

    def test_poller_factory_applies_saved_primary_binding_and_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "accounts.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, env_id TEXT)")
                connection.execute("INSERT INTO accounts VALUES (?, ?)", ("primary-ps38", "env-1"))
                connection.commit()
            credential_path = root / "accounts" / "primary-ps38" / "credentials.json"
            credential_path.parent.mkdir(parents=True)
            credential_path.write_text(
                json.dumps(
                    {
                        "username": "test-user",
                        "password": "test-password",
                        "totp_secret": "JBSWY3DPEHPK3PXP",
                    }
                ),
                encoding="utf-8",
            )
            poller = Poller(
                {
                    "pool_db_path": str(database),
                    "pool_dir": str(root / "accounts"),
                    "ps38_transport": "auto",
                    "base_url": "https://example.test",
                    "profile_dir": str(root / "profile"),
                }
            )
            with patch("ps38app.poller.Ps38HttpSession") as http_session:
                created = poller.create_session()
            self.assertIs(created, http_session.return_value)
            session_config = http_session.call_args.args[0]
            self.assertEqual(session_config["adspower_env_id"], "env-1")
            self.assertEqual(session_config["username"], "test-user")
            self.assertEqual(session_config["password"], "test-password")
            self.assertEqual(session_config["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_poller_factory_respects_explicit_http_mode_without_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            poller = Poller(
                {
                    "pool_db_path": str(root / "missing.sqlite3"),
                    "pool_dir": str(root / "accounts"),
                    "ps38_transport": "http",
                    "base_url": "https://example.test",
                    "profile_dir": str(root / "profile"),
                }
            )
            with patch("ps38app.poller.Ps38HttpSession") as http_session:
                poller.create_session()
            http_session.assert_called_once()


if __name__ == "__main__":
    unittest.main()
