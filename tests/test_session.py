import unittest
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from unittest.mock import Mock, patch

from ps38app.session import PartnerSession
from ps38app.env_browser import EnvBrowser
from ps38app.store import SessionExpired


def make_session():
    config = {
        "base_url": "https://www.ps38ag.com",
        "login_path": "/partner/zh-cn/login",
        "profile_dir": str(Path(gettempdir()) / "ps38-test-profile"),
        "username": "",
        "password": "",
        "headless": True,
        "browser_channel": "",
        "minimize_after_login": False,
    }
    session = PartnerSession(config)
    page = Mock()
    page.is_closed.return_value = False
    session.page = page
    return session, page


class SessionRequestTests(unittest.TestCase):
    def test_profile_parsing(self):
        session, page = make_session()
        page.evaluate.return_value = {
            "httpStatus": 200,
            "json": {"profile": {"userId": 7, "userCode": "PARENT", "loginId": "p"}},
        }
        profile = session.fetch_profile()
        self.assertEqual(profile["user_id"], 7)
        self.assertEqual(profile["user_code"], "PARENT")
        args = page.evaluate.call_args.args[1]
        self.assertEqual(args["method"], "GET")
        self.assertTrue(args["url"].endswith("/partner-account/ddd/user-info/login-user/profile"))

    def test_profile_rejects_unauthorized(self):
        session, page = make_session()
        page.evaluate.return_value = {"httpStatus": 401, "json": None}
        with self.assertRaises(SessionExpired):
            session.fetch_profile()

    def test_profile_rejects_html(self):
        session, page = make_session()
        page.evaluate.return_value = {"httpStatus": 200, "json": None}
        with self.assertRaises(SessionExpired):
            session.fetch_profile()

    def test_open_orders_body(self):
        session, page = make_session()
        page.evaluate.return_value = {"httpStatus": 200, "json": {"body": {}}}
        session.fetch_orders(7, {"code": "A1", "user_id": 11}, "OPEN", 2, 200)
        args = page.evaluate.call_args.args[1]
        self.assertEqual(args["method"], "POST")
        self.assertTrue(args["url"].endswith("/partner-reporting/ddd/common/bet-list/v2"))
        body = args["body"]
        self.assertEqual(body["betListType"], "DETAIL")
        self.assertEqual(body["ownerUserId"], "7")
        self.assertEqual(body["playerUserId"], 11)
        self.assertEqual(body["selectedUserCode"], "A1")
        self.assertEqual(body["status"], "OPEN")
        self.assertEqual(body["paging"], {"pageNumber": 2, "rowsPerPage": 200})
        self.assertNotIn("dateBy", body)

    def test_settled_orders_body_includes_dates(self):
        session, page = make_session()
        page.evaluate.return_value = {"httpStatus": 200, "json": {"body": {}}}
        session.fetch_orders(
            7, {"code": "A1", "user_id": 11}, "SETTLED", 1, 200, "2026-10-01", "2026-10-31"
        )
        body = page.evaluate.call_args.args[1]["body"]
        self.assertEqual(body["dateBy"], "GRADED_DATE")
        self.assertEqual(body["timezone"], "+08:00")
        self.assertEqual(body["fromDate"], "2026-10-01")
        self.assertEqual(body["toDate"], "2026-10-31")

    def test_hierarchy_body(self):
        session, page = make_session()
        page.evaluate.return_value = {"httpStatus": 200, "json": {"body": {"data": []}}}
        session.fetch_hierarchy(7, "2026-07-01", "2026-07-31")
        body = page.evaluate.call_args.args[1]["body"]
        self.assertEqual(body["ownerUserId"], "7")
        self.assertEqual(body["drillDownUserId"], 7)
        self.assertEqual(body["groupType"], "OWN_DOWNLINE")
        self.assertEqual(body["timezone"], "+08:00")


class PresentWindowTests(unittest.TestCase):
    def test_without_browser_reports_missing(self):
        session, _page = make_session()
        session.ctx = None
        self.assertFalse(session.present_window())

    def test_brings_live_page_to_front(self):
        session, page = make_session()
        page.url = "https://www.ps38ag.com/partner/"
        page.bring_to_front = Mock()
        session.ctx = Mock()
        session.ctx.new_cdp_session.side_effect = RuntimeError("no cdp")
        self.assertTrue(session.present_window())
        page.bring_to_front.assert_called_once_with()
        page.goto.assert_not_called()

    def test_blank_window_without_session_loads_login(self):
        session, page = make_session()
        page.url = "about:blank"
        page.bring_to_front = Mock()
        session.ctx = Mock()
        session.ctx.new_cdp_session.side_effect = RuntimeError("no cdp")
        self.assertTrue(session.present_window())
        self.assertTrue(page.goto.call_args.args[0].endswith("/partner/zh-cn/login"))

    def test_blank_window_with_session_loads_dashboard(self):
        session, page = make_session()
        page.url = "about:blank"
        page.bring_to_front = Mock()
        session.session_ready = True
        session.ctx = Mock()
        session.ctx.new_cdp_session.side_effect = RuntimeError("no cdp")
        self.assertTrue(session.present_window())
        self.assertTrue(
            page.goto.call_args.args[0].endswith("/partner-ui-v2/report-outstanding-v2")
        )

    def test_closed_page_opens_a_new_one(self):
        session, page = make_session()
        page.is_closed.return_value = True
        fresh = Mock()
        fresh.url = "https://www.ps38ag.com/partner/"
        fresh.is_closed.return_value = False
        session.ctx = Mock()
        session.ctx.pages = [fresh]
        session.ctx.new_cdp_session.side_effect = RuntimeError("no cdp")
        self.assertTrue(session.present_window())
        self.assertIs(session.page, fresh)
        fresh.bring_to_front.assert_called_once_with()


class SessionProbeTests(unittest.TestCase):
    def test_dashboard_url_defaults_to_partner_ui(self):
        session, _page = make_session()
        self.assertEqual(
            session.dashboard_url,
            "https://www.ps38ag.com/partner-ui-v2/report-outstanding-v2",
        )

    def test_probe_trusts_api_even_on_login_url(self):
        # The site bounces some partner URLs to /login while the cookie is fine.
        session, page = make_session()
        page.url = "https://www.ps38ag.com/partner/zh-cn/login"
        page.evaluate.return_value = {
            "httpStatus": 200,
            "json": {"profile": {"userId": 7, "userCode": "PARENT", "loginId": "p"}},
        }
        profile = session._probe()
        self.assertIsNotNone(profile)
        self.assertEqual(profile["user_code"], "PARENT")

    def test_probe_skips_foreign_host(self):
        session, page = make_session()
        page.url = "https://example.com/partner/"
        profile = session._probe()
        self.assertIsNone(profile)
        page.evaluate.assert_not_called()

    def test_probe_handles_expired_session(self):
        session, page = make_session()
        page.url = "https://www.ps38ag.com/partner-ui-v2/report-outstanding-v2"
        page.evaluate.return_value = {"httpStatus": 401, "json": None}
        self.assertIsNone(session._probe())


class AdsPowerSessionTests(unittest.TestCase):
    def make_env_session(self, *, status="Inactive", reconnect=False):
        client = Mock()
        client.active.return_value = {"status": status}
        if reconnect:
            client.active.side_effect = [{"status": "Inactive"}, {"status": "Active"}]
        client.start.return_value = {"ws": {"puppeteer": "ws://127.0.0.1:1/devtools/browser/x"}}
        page = Mock()
        page.is_closed.return_value = False
        context = Mock()
        context.pages = []
        context.new_page.return_value = page
        browser = Mock()
        browser.contexts = [context]
        pw = Mock()
        pw.start.return_value = pw
        pw.chromium.connect_over_cdp.return_value = browser
        if reconnect:
            pw.chromium.connect_over_cdp.side_effect = [RuntimeError("CDP unavailable"), browser]
        playwright = Mock()
        playwright.return_value = pw
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        config = {
            "base_url": "https://www.ps38ag.com",
            "login_path": "/partner/zh-cn/login",
            "profile_dir": str(Path(root.name) / "profile"),
            "username": "",
            "password": "",
            "headless": True,
            "browser_channel": "",
            "minimize_after_login": False,
            "adspower_env_id": "k1ippneh",
        }
        with (
            patch("ps38app.session.sync_playwright", playwright),
            patch("ps38app.session.AdsPowerClient", return_value=client),
            patch("ps38app.session.time.sleep"),
        ):
            session = PartnerSession(config)
            session.start()
        return session, client, browser, context, pw

    def test_env_start_connects_and_close_stops_owned_environment(self):
        session, client, browser, context, pw = self.make_env_session()
        client.start.assert_called_once_with("k1ippneh", headless="1", last_opened_tabs="0")
        self.assertIs(session.ctx, context)
        session.close()
        browser.close.assert_called_once()
        pw.stop.assert_called_once()
        client.stop.assert_called_once_with("k1ippneh")

    def test_already_running_environment_is_not_stopped(self):
        session, client, browser, _context, _pw = self.make_env_session(status="Active")
        session.close()
        browser.close.assert_called_once()
        client.stop.assert_not_called()

    def test_reconnect_keeps_ownership_of_started_environment(self):
        session, client, _browser, _context, _pw = self.make_env_session(reconnect=True)
        session.close()
        client.stop.assert_called_once_with("k1ippneh")

    def test_member_browser_reconnect_keeps_environment_ownership(self):
        client = Mock()
        client.active.side_effect = [{"status": "Inactive"}, {"status": "Active"}]
        client.start.return_value = {"ws": {"puppeteer": "ws://127.0.0.1:1/browser"}}
        browser = Mock()
        context = Mock(pages=[])
        browser.contexts = [context]
        pw = Mock()
        pw.chromium.connect_over_cdp.side_effect = [RuntimeError("CDP unavailable"), browser]
        with (
            patch("ps38app.env_browser.AdsPowerClient", return_value=client),
            patch("ps38app.env_browser.sync_playwright") as playwright,
            patch("ps38app.env_browser.time.sleep"),
        ):
            playwright.return_value.start.return_value = pw
            session = EnvBrowser({}, "test-env")
            session.open()
            session.close()
        client.stop.assert_called_once_with("test-env")


class AutoLoginTests(unittest.TestCase):
    def make_session(self, *, captcha_command="ocr {image}"):
        config = {
            "base_url": "https://www.ps38ag.com",
            "login_path": "/partner/zh-cn/login",
            "profile_dir": str(Path(gettempdir()) / "ps38-auto-test-profile"),
            "username": "okcodex77.",
            "password": "secret",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "headless": True,
            "browser_channel": "",
            "minimize_after_login": False,
            "ps38_captcha_command": captcha_command,
            "ps38_captcha_timeout": 5,
        }
        session = PartnerSession(config)
        page = Mock()
        page.url = "https://www.ps38ag.com/partner/zh-cn/login"
        page.is_closed.return_value = False
        session.page = page
        session.ctx = Mock()
        return session

    def test_can_auto_login_requires_all_three(self):
        session = self.make_session()
        self.assertTrue(session.can_auto_login())
        session.totp_secret = ""
        self.assertFalse(session.can_auto_login())

    def test_captcha_then_totp_flow(self):
        session = self.make_session()
        session._fetch = Mock(
            side_effect=[
                {"json": {"required_captcha": "REQUIRED_CAPTCHA"}},
                {"json": {"status": "OK", "state": "otp"}},
            ]
        )
        session._fetch_captcha_image = Mock(return_value=b"image")
        session._solve_captcha = Mock(return_value="1234")
        session._submit_totp = Mock(return_value=True)
        session._probe = Mock(return_value={"user_code": "TLH0R", "user_id": 1})
        session.save_storage_state = Mock()

        self.assertTrue(session.try_auto_login())
        self.assertEqual(session._fetch.call_args_list[0].args[2]["captcha"], "")
        self.assertEqual(session._fetch.call_args_list[1].args[2]["captcha"], "1234")
        session._submit_totp.assert_called_once()
        self.assertTrue(session.session_ready)
        session.save_storage_state.assert_called_once()

    def test_required_captcha_without_command_falls_back(self):
        session = self.make_session(captcha_command="")
        session._fetch = Mock(return_value={"json": {"required_captcha": "REQUIRED_CAPTCHA"}})
        self.assertFalse(session.try_auto_login())
        self.assertEqual(session._fetch.call_count, 1)

    def test_invalid_user_is_not_retried(self):
        session = self.make_session()
        session._fetch = Mock(return_value={"json": {"status": "INVALID_USER"}})
        self.assertFalse(session.try_auto_login())
        self.assertEqual(session._fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
