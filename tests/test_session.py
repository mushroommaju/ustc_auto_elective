import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from autoelective import session as main

URL = 'https://jw.ustc.edu.cn/for-std/course-select/12345/turn/678/select'


class LoginTests(unittest.TestCase):
    def test_auto_login_waits_through_official_passport_and_verification_race(self):
        context = Mock()
        page = context.new_page.return_value
        page.url = 'https://passport.ustc.edu.cn/login'
        page.wait_for_timeout.side_effect = lambda _: setattr(page, 'url', URL)
        with patch.object(main, 'verify_session', side_effect=[False, True]) as verify, \
                patch.object(main, 'write_session') as save, \
                patch.object(main, 'submit_credentials_once') as submit:
            self.assertTrue(main.auto_login(context, URL, main.Credentials('synthetic-user', 'synthetic-password')))
            self.assertEqual(verify.call_count, 2)
            submit.assert_not_called()
            save.assert_called_once()

    def test_course_url_boundary(self):
        self.assertEqual(main.course_context(URL), {'studentId': '12345', 'turnId': '678'})
        self.assertEqual(main.clean_course_url(URL + '?ustcTarget=TEST.01#all-lessons'), URL)
        for bad in [URL.replace('https:', 'http:'), URL.replace('jw.ustc.edu.cn', 'jw.ustc.edu.cn.evil.test'),
                    URL + '/wrong', 'https://jw.ustc.edu.cn/home']:
            with self.assertRaises(ValueError):
                main.course_context(bad)

    def test_only_selected_lessons_and_no_redirect(self):
        request = Mock()
        response = request.post.return_value
        response.status = 200
        response.headers = {'content-type': 'application/json;charset=UTF-8'}
        response.json.return_value = [{'id': 1, 'code': 'TEST.01'}]
        self.assertTrue(main.verify_session(request, URL))
        args, kwargs = request.post.call_args
        self.assertEqual(args, (main.SELECTED_URL,))
        self.assertEqual(kwargs['max_redirects'], 0)
        self.assertEqual(kwargs['form'], {'studentId': '12345', 'turnId': '678'})
        response.dispose.assert_called_once()
        for status, content_type, body in [(302, 'text/html', []), (401, 'application/json', []),
                                          (200, 'text/html', []), (200, 'application/json', {'error': 'login'}),
                                          (200, 'application/json', [{'password': 'not a lesson'}])]:
            response.status, response.headers = status, {'content-type': content_type}
            response.json.return_value = body
            self.assertFalse(main.verify_session(request, URL))

    def test_invalid_url_no_request(self):
        request = Mock()
        with self.assertRaises(ValueError):
            main.verify_session(request, 'https://evil.test/')
        request.post.assert_not_called()

    def test_save_filtered_session_and_atomic_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'auth' / 'session.json'
            cookies = [{'domain': 'jw.ustc.edu.cn', 'name': 'SESSION', 'value': 'synthetic'},
                       {'domain': 'evil.test', 'name': 'OTHER', 'value': 'must-not-save'}]
            main.write_session(URL, cookies, path)
            saved = main.read_session(path)
            self.assertEqual(saved['storage_state']['cookies'], cookies[:1])
            self.assertEqual(saved['storage_state']['origins'], [])
            self.assertNotIn('must-not-save', path.read_text())
            main.write_session(URL, [], path)
            self.assertEqual(main.read_session(path)['storage_state']['cookies'], [])
            self.assertEqual(list(path.parent.glob('*.tmp')), [])
            saved['storage_state']['cookies'] = cookies
            path.write_text(json.dumps(saved))
            with self.assertRaises(ValueError):
                main.read_session(path)

    def test_credentials_offsite_rejected(self):
        page = Mock(url='https://evil.test/cas/login')
        with self.assertRaises(ValueError):
            main.submit_credentials_once(page, 'synthetic-user', 'synthetic-password')
        page.get_by_placeholder.assert_not_called()

    def test_credentials_are_opt_in_and_literal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.ini'
            path.write_text('[auth]\nauto_login = true\nusername = synthetic-user\npassword = %literal-password\n',
                            encoding='utf-8')
            credentials = main.read_credentials(path)
            self.assertEqual(credentials.username, 'synthetic-user')
            self.assertEqual(credentials.password, '%literal-password')
            self.assertNotIn('synthetic-user', repr(credentials))
            self.assertNotIn('literal-password', repr(credentials))
            path.write_text('[auth]\nauto_login = false\n', encoding='utf-8')
            self.assertIsNone(main.read_credentials(path))
            path.write_text('[auth]\nauto_login = true\nusername = synthetic-user\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                main.read_credentials(path)

    def test_submit_once_to_real_form(self):
        page = Mock(url='https://id.ustc.edu.cn/cas/login?service=test')
        main.submit_credentials_once(page, 'synthetic-user', 'synthetic-password')
        self.assertEqual(page.get_by_placeholder.call_count, 2)
        page.get_by_role.return_value.click.assert_called_once()

    def test_auto_login_submits_once_then_saves_only_after_verification(self):
        context = Mock()
        page = context.new_page.return_value
        page.url = 'https://id.ustc.edu.cn/cas/login'
        page.wait_for_timeout.side_effect = lambda _: setattr(page, 'url', URL)
        context.cookies.return_value = []
        credentials = main.Credentials('synthetic-user', 'synthetic-password')
        with patch.object(main, 'verify_session', return_value=True) as verify, \
                patch.object(main, 'write_session') as save:
            self.assertTrue(main.auto_login(context, URL, credentials, timeout=2))
        page.get_by_role.return_value.click.assert_called_once()
        verify.assert_called_once_with(context.request, URL)
        save.assert_called_once_with(URL, [])
        page.close.assert_called_once()

    def test_auto_login_rejects_offsite_without_submitting(self):
        context = Mock()
        page = context.new_page.return_value
        page.url = 'https://evil.test/cas/login'
        credentials = main.Credentials('synthetic-user', 'synthetic-password')
        self.assertFalse(main.auto_login(context, URL, credentials, timeout=2))
        page.get_by_placeholder.assert_not_called()
        context.request.post.assert_not_called()
        page.close.assert_called_once()

    def test_auto_login_does_not_retry_a_failed_submission(self):
        context = Mock()
        page = context.new_page.return_value
        page.url = 'https://id.ustc.edu.cn/cas/login'
        credentials = main.Credentials('synthetic-user', 'synthetic-password')
        with patch.object(main.time, 'monotonic', side_effect=[0, 0, 2]):
            self.assertFalse(main.auto_login(context, URL, credentials, timeout=1))
        page.get_by_role.return_value.click.assert_called_once()
        page.close.assert_called_once()

    def test_login_uses_configured_auto_credentials(self):
        runtime = Mock()
        browser = runtime.chromium.launch.return_value
        context = browser.new_context.return_value
        manager = Mock(__enter__=Mock(return_value=runtime), __exit__=Mock(return_value=False))
        credentials = main.Credentials('synthetic-user', 'synthetic-password')
        args = SimpleNamespace(config=Path('unused.ini'), prompt_credentials=False, timeout=2)
        with patch('playwright.sync_api.sync_playwright', return_value=manager), \
                patch.object(main, 'configured_url', return_value=URL), \
                patch.object(main, 'read_credentials', return_value=credentials), \
                patch.object(main, 'read_session', return_value=None), \
                patch.object(main, 'auto_login', return_value=True) as automatic, patch('builtins.print'):
            self.assertEqual(main.login(args), 0)
        automatic.assert_called_once_with(context, URL, credentials, timeout=2)
        browser.close.assert_called_once()

    def test_login_persists_only_after_api_verification(self):
        browser = Mock()
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.url = URL
        page.is_closed.return_value = False
        home = Mock(url='https://jw.ustc.edu.cn/home')
        home.is_closed.return_value = False
        context.pages = [home, page]  # 教务可能在新标签页打开选课。
        context.cookies.return_value = []
        runtime = Mock()
        runtime.chromium.launch.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=runtime)
        manager.__exit__ = Mock(return_value=False)
        args = SimpleNamespace(config=Path('unused.ini'), prompt_credentials=False, timeout=2)
        with patch('playwright.sync_api.sync_playwright', return_value=manager), \
                patch.object(main, 'configured_url', return_value=''), \
                patch.object(main, 'read_credentials', return_value=None), \
                patch.object(main, 'read_session', return_value=None), \
                patch.object(main, 'verify_session', return_value=True) as verify, \
                patch.object(main, 'write_session') as save, patch('builtins.print'):
            self.assertEqual(main.login(args), 0)
            verify.assert_called_once_with(context.request, URL)
            save.assert_called_once_with(URL, [])
        browser.close.assert_called_once()

    def test_reused_session_skips_login_page(self):
        browser = Mock()
        context = browser.new_context.return_value
        runtime = Mock()
        runtime.chromium.launch.return_value = browser
        manager = Mock()
        manager.__enter__ = Mock(return_value=runtime)
        manager.__exit__ = Mock(return_value=False)
        saved = {'course_select_url': URL, 'storage_state': {'cookies': [], 'origins': []}}
        args = SimpleNamespace(config=Path('unused.ini'), prompt_credentials=True, timeout=2)
        with patch('playwright.sync_api.sync_playwright', return_value=manager), \
                patch.object(main, 'configured_url', return_value=''), \
                patch.object(main, 'read_credentials', return_value=None), \
                patch.object(main, 'read_session', return_value=saved), \
                patch.object(main, 'verify_session', return_value=True), \
                patch.object(main, 'write_session'), patch('builtins.input') as ask, patch('builtins.print'):
            self.assertEqual(main.login(args), 0)
            context.new_page.assert_not_called()
            ask.assert_not_called()
        browser.close.assert_called_once()

    def test_check_uses_saved_state_in_headless_edge_and_always_closes(self):
        saved = {'course_select_url': URL, 'storage_state': {'cookies': [], 'origins': []}}
        args = SimpleNamespace(config=Path('unused.ini'))
        for result in (True, False, RuntimeError('synthetic failure')):
            with self.subTest(result=result):
                runtime = Mock()
                browser = runtime.chromium.launch.return_value
                context = browser.new_context.return_value
                manager = Mock(__enter__=Mock(return_value=runtime), __exit__=Mock(return_value=False))
                with patch('playwright.sync_api.sync_playwright', return_value=manager), \
                        patch.object(main, 'configured_url', return_value=''), \
                        patch.object(main, 'read_session', return_value=saved), \
                        patch.object(main, 'verify_session', side_effect=[result]) as verify, \
                        patch.object(main, 'write_session') as save, patch('builtins.print'):
                    if isinstance(result, Exception):
                        with self.assertRaises(RuntimeError):
                            main.check(args)
                    else:
                        self.assertEqual(main.check(args), 0 if result else 2)
                    runtime.chromium.launch.assert_called_once_with(channel='msedge', headless=True)
                    browser.new_context.assert_called_once_with(storage_state=saved['storage_state'],
                                                                accept_downloads=False)
                    verify.assert_called_once_with(context.request, URL)
                    runtime.request.new_context.assert_not_called()
                    context.new_page.assert_not_called()
                    save.assert_not_called()
                    browser.close.assert_called_once()

    def test_navigation_events_are_pumped_while_waiting(self):
        browser = Mock()
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.url = 'https://id.ustc.edu.cn/cas/login'
        page.is_closed.return_value = False
        context.pages = [page]
        context.cookies.return_value = []
        page.wait_for_timeout.side_effect = lambda _: setattr(page, 'url', URL)
        runtime = Mock()
        runtime.chromium.launch.return_value = browser
        manager = Mock(__enter__=Mock(return_value=runtime), __exit__=Mock(return_value=False))
        args = SimpleNamespace(config=Path('unused.ini'), prompt_credentials=False, timeout=2)
        with patch('playwright.sync_api.sync_playwright', return_value=manager), \
                patch.object(main, 'configured_url', return_value=''), \
                patch.object(main, 'read_credentials', return_value=None), \
                patch.object(main, 'read_session', return_value=None), \
                patch.object(main, 'verify_session', return_value=True), \
                patch.object(main, 'write_session') as save, patch('builtins.print'), \
                patch.object(main.time, 'sleep', side_effect=AssertionError('Blocking sleep starves browser events')):
            self.assertEqual(main.login(args), 0)
            page.wait_for_timeout.assert_called_once_with(1000)
            save.assert_called_once_with(URL, [])


if __name__ == '__main__':
    unittest.main()
