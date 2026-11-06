import unittest
from unittest.mock import Mock, patch

from ps38app.poller import RETRY_SECONDS, Poller, ReloginRequested
from ps38app.store import SessionExpired


def make_poller():
    # _service_show_window touches no config values, so an empty dict is fine.
    return Poller({})


class ShowWindowTests(unittest.TestCase):
    def test_no_request_is_a_noop(self):
        poller = make_poller()
        poller.session = Mock()
        poller._service_show_window()
        poller.session.present_window.assert_not_called()

    def test_request_brings_existing_window_to_front(self):
        poller = make_poller()
        session = Mock()
        session.present_window.return_value = True
        poller.session = session
        poller.request_show_window()
        poller._service_show_window()
        session.present_window.assert_called_once_with()
        self.assertFalse(poller._show_window.is_set())
        self.assertEqual(poller.state["message"], "已打开后台浏览器窗口")

    def test_request_without_browser_rebuilds_session(self):
        poller = make_poller()
        poller.request_show_window()
        with self.assertRaises(ReloginRequested):
            poller._service_show_window()
        self.assertFalse(poller._show_window.is_set())

    def test_request_with_dead_browser_rebuilds_session(self):
        poller = make_poller()
        session = Mock()
        session.present_window.return_value = False
        poller.session = session
        poller.request_show_window()
        with self.assertRaises(ReloginRequested):
            poller._service_show_window()

    def test_relogin_interrupts_waiting_login(self):
        poller = make_poller()
        poller.session = Mock()
        poller.request_relogin()
        with self.assertRaises(ReloginRequested):
            poller._service_show_window()


class LifecycleTests(unittest.TestCase):
    def test_normal_shutdown_closes_browser_and_database(self):
        poller = make_poller()
        session = Mock()
        database = Mock()
        poller.session = session
        poller.store = database

        with patch.object(poller, "_supervise", side_effect=poller.stop):
            poller.run()

        session.close.assert_called_once_with()
        database.close.assert_called_once_with()
        self.assertIsNone(poller.session)
        self.assertEqual(poller.state["status"], "stopped")

    def test_supervisor_failure_still_closes_browser_and_database(self):
        poller = make_poller()
        session = Mock()
        database = Mock()
        poller.session = session
        poller.store = database

        with patch.object(poller, "_supervise", side_effect=RuntimeError("unexpected failure")):
            with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
                poller.run()

        session.close.assert_called_once_with()
        database.close.assert_called_once_with()
        self.assertEqual(poller.state["status"], "stopped")

    def test_stop_during_session_failure_interrupts_retry_delay(self):
        poller = make_poller()
        session = Mock()
        poller.session = session

        def failed_login():
            poller.stop()
            raise SessionExpired("login cancelled")

        with patch.object(poller, "_open_session", side_effect=failed_login) as open_session:
            with patch.object(poller._stop_event, "wait", wraps=poller._stop_event.wait) as wait:
                poller._supervise()

        open_session.assert_called_once_with()
        wait.assert_called_once_with(RETRY_SECONDS)
        session.close.assert_called_once_with()
        self.assertTrue(poller._stop_event.is_set())


class SessionRecoveryTests(unittest.TestCase):
    def test_cleared_totp_secret_does_not_keep_stale_value(self):
        poller = make_poller()
        poller.session = Mock(totp_secret="old-secret")
        with patch.object(poller, "_primary_credentials", return_value={"totp_secret": ""}):
            poller.refresh_credentials()
        self.assertEqual(poller.session.totp_secret, "")

    def test_expired_session_recovers_in_existing_window_and_resumes_polling(self):
        poller = make_poller()
        session = Mock()
        profile = {"user_code": "RECOVERED", "user_id": 7}
        session.wait_for_login.return_value = profile
        poller.session = session
        poller.agents = [{"code": "A1", "user_id": 11}]

        resumed = []

        def poll_again():
            resumed.append((poller.session, poller.state["status"], poller.state["profile"]))
            poller.stop()

        calls = 0

        def polling_loop():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise SessionExpired("expired")
            poll_again()

        with patch.object(poller, "_open_session") as open_session:
            with patch.object(poller, "_loop", side_effect=polling_loop):
                with patch.object(
                    poller._stop_event, "wait", side_effect=lambda _timeout: poller.stop() or True
                ) as retry_wait:
                    poller._supervise()

        self.assertEqual(calls, 2)
        self.assertEqual(resumed, [(session, "online", profile)])
        retry_wait.assert_not_called()
        open_session.assert_called_once_with()
        session.open_login_page.assert_called_once_with()
        session.wait_for_login.assert_called_once()
        session.close.assert_not_called()
        self.assertIs(poller.session, session)

    def test_login_recovery_receives_live_stop_signal(self):
        poller = make_poller()
        session = Mock()
        poller.session = session

        def wait_for_login(**callbacks):
            self.assertFalse(callbacks["should_stop"]())
            poller.stop()
            self.assertTrue(callbacks["should_stop"]())
            raise SessionExpired("cancelled")

        session.wait_for_login.side_effect = wait_for_login
        with self.assertRaisesRegex(SessionExpired, "cancelled"):
            poller._recover_session(SessionExpired("expired"))

        session.close.assert_not_called()
        self.assertIs(poller.session, session)


if __name__ == "__main__":
    unittest.main()
