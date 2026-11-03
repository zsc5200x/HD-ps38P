"""Opt-in real Chromium regression; uses a local fake member site only.

PS38_TEST_BROWSER=1 PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers \
    .venv/bin/python -m unittest tests.test_member_login_browser -v
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from unittest import mock

from ps38app.account_pool.providers import CrownProvider
from ps38app.account_pool.store import AccountStore
from tests.test_account_pool import pool_config


class LocalMemberSite(BaseHTTPRequestHandler):
    operations = []
    logins = 0

    def log_message(self, *args):
        pass

    def send(self, body, content_type='text/html; charset=utf-8', cookie=None):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path == '/frame':
            self.send('''<input name="username"><input type="password" name="pwd">
                <button id="loginBtn">登录</button><script>
                document.querySelector('button').onclick = async () => {
                    const r = await fetch('/transform.php?ver=local-v1', {method:'POST',
                        body:new URLSearchParams({p:'chk_login_v3', langx:'zh-cn',
                            username:document.querySelector('[name=username]').value,
                            pwd:document.querySelector('[name=pwd]').value})});
                    const text = await r.text();
                    if (text.includes('checkNeed')) {
                        // Simulate the operator solving a captcha, not provider resubmission.
                        setTimeout(() => document.querySelector('button').click(), 300);
                    } else { window.top.location = '/app'; }
                };</script>''')
        elif self.path == '/app':
            # No UI selectors/globals from the real site: uid exists only in the login response.
            self.send('''已连接<script>
                localStorage.setItem('fixture', 'restorable');
                setTimeout(() => { document.cookie = 'sid=stable; path=/'; }, 350);
                </script>''')
        else:
            self.send('<iframe src="/frame"></iframe>')

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        p = form.get('p', [''])[0]
        type(self).operations.append(p)
        if p.startswith('chk_login'):
            type(self).logins += 1
            if type(self).logins == 1:
                self.send('{"code":201,"msg":"checkNeed"}', 'application/json')
            else:
                self.send('<serverresponse><status>200</status><uid>real-local-uid</uid></serverresponse>',
                          'text/xml', 'sid=initial; path=/')
        elif p == 'get_member_data':
            self.send('<serverresponse><enable>Y</enable><currency>CNY</currency></serverresponse>'
                      if 'sid=stable' in self.headers.get('Cookie', '')
                      and form.get('uid') == ['real-local-uid'] else
                      '<serverresponse><code>error</code><msg>doubleLogin</msg></serverresponse>', 'text/xml')
        else:
            self.send('<serverresponse><status>200</status></serverresponse>', 'text/xml')


@unittest.skipUnless(os.environ.get('PS38_TEST_BROWSER') == '1', 'set PS38_TEST_BROWSER=1 to launch Chromium')
class BrowserMemberLoginTests(unittest.TestCase):
    def test_iframe_captcha_login_initialization_save_and_restart_restore(self):
        LocalMemberSite.operations = []
        LocalMemberSite.logins = 0
        server = ThreadingHTTPServer(('127.0.0.1', 0), LocalMemberSite)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = pool_config(Path(tmp))
                store = AccountStore(config['pool_db_path'], config)
                account = store.save_account({
                    'platform': 'crown_member', 'username': 'fixture-member', 'password': 'fixture-password',
                    'base_url': f'http://127.0.0.1:{server.server_port}',
                })
                provider = CrownProvider(config, store, 'crown_member')
                launch = provider._launch_profile
                with mock.patch.object(provider, '_launch_profile',
                                       side_effect=lambda path, headless: launch(path, headless=True)):
                    result = provider.login(account)
                self.assertEqual(result.status, 'online')
                meta = store.load_meta(account['id'])
                state = store._read_json(store.paths(account['id'])['storage'])
                self.assertEqual(meta['uid'], 'real-local-uid')
                self.assertEqual(meta['ver'], 'local-v1')
                self.assertTrue(any(c['name'] == 'sid' and c['value'] == 'stable' for c in state['cookies']))
                self.assertEqual(LocalMemberSite.logins, 2)
                operations = LocalMemberSite.operations
                self.assertLess(operations.index('memSet'), operations.index('service_mainget'))
                self.assertLess(operations.index('service_mainget'), operations.index('get_version'))
                self.assertLess(operations.index('get_version'), operations.index('get_member_data'))

                # A new store/provider simulates restarting the app; no login/browser is needed.
                restored = CrownProvider(config, AccountStore(config['pool_db_path'], config), 'crown_member')
                with mock.patch.object(restored, '_launch_profile') as browser:
                    self.assertEqual(restored.login(account).status, 'online')
                    self.assertEqual(restored.check(account).status, 'online')
                    browser.assert_not_called()
                self.assertEqual(LocalMemberSite.logins, 2)
                self.assertNotIn('fixture-password', json.dumps(store.accounts()))
                self.assertNotIn('real-local-uid', json.dumps(store.accounts()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
