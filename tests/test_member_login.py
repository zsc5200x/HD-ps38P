import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import URLError

from ps38app.account_pool.crown_member import _http_post, ping_member
from ps38app.account_pool.member_capture import MemberCapture
from ps38app.account_pool.providers import CrownProvider, LoginResult, ProviderError
from ps38app.account_pool.service import AccountPoolService, AccountPoolError
from ps38app.account_pool.store import AccountStore
from tests.test_account_pool import pool_config, FakeProvider


ONLINE = b'<serverresponse><enable>Y</enable><currency>CNY</currency></serverresponse>'
STORAGE = {'cookies': [{'name': 'sid', 'value': 'saved', 'domain': 'member.example.com',
                        'path': '/', 'expires': -1}], 'origins': []}
META = {'uid': 'member-uid', 'ver': 'version1', 'origin': 'https://member.example.com',
        'langx': 'zh-cn', 'user_agent': 'browser-test-agent'}


def response(body, operation='chk_login_v3', uid='member-uid'):
    return SimpleNamespace(
        url='https://member.example.com/transform.php?ver=version1', status=200,
        request=SimpleNamespace(post_data=f'p={operation}&uid={uid}&langx=zh-cn'),
        text=lambda: body,
    )


class CaptureTests(unittest.TestCase):
    def test_login_uid_comes_from_response_not_username(self):
        capture = MemberCapture()
        capture.response(response('<serverresponse><status>200</status><uid>real-uid</uid></serverresponse>'))
        self.assertTrue(capture.authenticated)
        self.assertTrue(capture.fresh_login)
        self.assertEqual(capture.meta['uid'], 'real-uid')
        self.assertEqual(capture.meta['ver'], 'version1')

    def test_captcha_and_failed_login_do_not_replace_valid_identity(self):
        capture = MemberCapture()
        capture.response(response(ONLINE.decode(), 'get_member_data'))
        saved = dict(capture.meta)
        for text in ('{"code":201,"msg":"checkNeed"}',
                     '<serverresponse><status>401</status><uid>bad</uid></serverresponse>',
                     '<!DOCTYPE html><html>login</html>'):
            capture.response(response(text))
        self.assertEqual(capture.meta, saved)
        self.assertFalse(capture.fresh_login)

    def test_uid_and_ver_restored_from_iframe_without_network_login(self):
        frame = mock.Mock(url='https://member.example.com/app?uid=frame-uid&ver=version2')
        frame.evaluate.return_value = {'user_agent': 'agent'}
        page = mock.Mock(url='https://member.example.com/')
        page.frames = [frame]
        page.context.cookies.return_value = []
        meta = MemberCapture().collect(page)
        self.assertEqual(meta['uid'], 'frame-uid')
        self.assertEqual(meta['ver'], 'version2')
        self.assertEqual(meta['origin'], META['origin'])

    def test_login_handoff_is_captured_before_delivery_once(self):
        capture = MemberCapture()
        route = mock.Mock()
        route.request = SimpleNamespace(url='https://member.example.com/transform.php?ver=version1',
                                        post_data='p=chk_login_v3&langx=zh-cn')
        reply = response('<serverresponse><status>200</status><uid>real-uid</uid></serverresponse>')
        reply.dispose = mock.Mock()
        route.fetch.return_value = reply
        route.fulfill.side_effect = lambda **kwargs: self.assertTrue(capture.authenticated)
        capture.login_route(route)
        route.fetch.assert_called_once_with(max_redirects=0, max_retries=0, timeout=30000)
        route.fulfill.assert_called_once_with(response=reply)
        route.continue_.assert_not_called()
        reply.dispose.assert_called_once()

    def test_failed_login_request_is_never_replayed(self):
        route = mock.Mock()
        route.request = SimpleNamespace(url='https://member.example.com/transform.php?ver=version1',
                                        post_data='p=chk_login_v3')
        route.fetch.side_effect = TimeoutError()
        capture = MemberCapture()
        capture.login_route(route)
        self.assertFalse(capture.authenticated)
        route.fetch.assert_called_once()
        route.continue_.assert_not_called()
        route.abort.assert_called_once_with('failed')


class TransportTests(unittest.TestCase):
    def test_tls_error_is_reported_without_exposing_private_url(self):
        with mock.patch('ps38app.account_pool.crown_member._http_post',
                        side_effect=URLError(ssl.SSLCertVerificationError('mismatch'))):
            result = ping_member(META['origin'], STORAGE, META)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('证书', result['message'])
        self.assertNotIn(META['uid'], result['message'])

    def test_tls_compatibility_is_scoped_to_explicit_call(self):
        for compatibility in (False, True):
            with mock.patch('ps38app.account_pool.crown_member.build_opener') as opener:
                opener.return_value.open.return_value.__enter__.return_value.status = 200
                _http_post('https://member.example.com', b'', {}, 1,
                           ignore_https_errors=compatibility)
                handler = opener.call_args.args[2]
                self.assertEqual(handler._context.verify_mode, ssl.CERT_NONE if compatibility else ssl.CERT_REQUIRED)
        self.assertEqual(ssl.create_default_context().verify_mode, ssl.CERT_REQUIRED)

    def test_transient_failure_is_not_session_expiry(self):
        def failed(*args):
            raise TimeoutError()
        self.assertEqual(ping_member(META['origin'], STORAGE, META, transport=failed)['status'], 'unavailable')


class MemberProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = pool_config(Path(self.tmp.name))
        self.store = AccountStore(self.config['pool_db_path'], self.config)
        self.account = self.store.save_account({
            'platform': 'crown_member', 'base_url': META['origin'], 'username': 'member',
        })
        self.provider = CrownProvider(self.config, self.store, 'crown_member')

    def tearDown(self):
        self.tmp.cleanup()

    def save_session(self):
        self.store._write_private(self.store.paths(self.account['id'])['storage'], STORAGE)
        self.store.save_meta(self.account['id'], META)

    def test_saved_session_is_verified_and_reused_without_new_login(self):
        self.save_session()
        with mock.patch.object(self.provider, '_member_ping', return_value={'status': 'online'}), \
                mock.patch.object(self.provider, '_launch_profile') as launch:
            result = self.provider.login(self.account)
        self.assertEqual(result.status, 'online')
        launch.assert_not_called()
        self.assertEqual(self.store.load_meta(self.account['id'])['user_agent'], META['user_agent'])

    def test_cookie_snapshot_without_uid_ver_cannot_be_online(self):
        self.store._write_private(self.store.paths(self.account['id'])['storage'], STORAGE)
        with mock.patch.object(self.provider, '_check_context') as browser:
            result = self.provider.check(self.account)
        self.assertEqual(result.status, 'expired')
        browser.assert_not_called()

    def test_conflict_and_network_failure_preserve_saved_session(self):
        self.save_session()
        for status, expected in [('conflict', 'expired'), ('unavailable', 'error')]:
            with mock.patch.object(self.provider, '_member_ping', return_value={'status': status, 'message': status}):
                self.assertEqual(self.provider.check(self.account).status, expected)
            self.assertTrue(self.store.get(self.account['id'])['has_session'])

    def test_stable_capture_waits_for_rotating_cookies_and_cancellation(self):
        context = mock.Mock()
        changed = {'cookies': [{**STORAGE['cookies'][0], 'value': 'new'}]}
        context.storage_state.side_effect = [STORAGE, changed, changed, changed]
        capture = mock.Mock()
        capture.collect.return_value = META
        meta, state = self.provider._stable_member(context, mock.Mock(), capture, None)
        self.assertEqual(state, changed)
        self.assertEqual(context.storage_state.call_count, 4)
        with self.assertRaises(ProviderError):
            self.provider._stable_member(context, mock.Mock(), capture, lambda: True)

    def test_initialization_matches_reference_protocol(self):
        context = mock.Mock()
        self.provider._initialize_member(context, META, None)
        forms = [call.kwargs['form'] for call in context.request.post.call_args_list]
        self.assertEqual([form['p'] for form in forms], ['memSet', 'service_mainget', 'get_version'])
        self.assertEqual(forms[0]['action'], 'check')
        self.assertEqual(forms[1]['login'], 'Y')

    def test_cancelled_and_unverified_results_cannot_be_reported_online(self):
        service = AccountPoolService(self.config)
        fake = FakeProvider()
        for cancel in (False, True):
            fake.login = mock.Mock(return_value=LoginResult('expired', 'unverified'))
            if cancel:
                import threading
                event = threading.Event()
                event.set()
                service._cancel[self.account['id']] = event
                fake.login.return_value = LoginResult('online', 'late success')
            with mock.patch('ps38app.account_pool.service.provider_for', return_value=fake):
                service._login_worker(self.account['id'], False)
            self.assertEqual(service.store.get(self.account['id'])['status'], 'error')
        service._busy.add(self.account['id'])
        with self.assertRaises(AccountPoolError):
            service.save_account({'username': 'other'}, self.account['id'])


if __name__ == '__main__':
    unittest.main()
