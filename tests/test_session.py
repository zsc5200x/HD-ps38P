import unittest
from pathlib import Path
from tempfile import gettempdir
from unittest.mock import Mock

from ps38app.session import PartnerSession
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
        self.assertTrue(args["url"].endswith(
            "/partner-account/ddd/user-info/login-user/profile"))

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
        session.fetch_orders(7, {"code": "A1", "user_id": 11}, "SETTLED", 1, 200,
                             "2026-10-01", "2026-10-31")
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
        self.assertTrue(page.goto.call_args.args[0].endswith("/partner-ui-v2/report-outstanding-v2"))

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


if __name__ == "__main__":
    unittest.main()
