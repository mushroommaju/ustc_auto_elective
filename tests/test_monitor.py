import configparser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch, call
from types import SimpleNamespace

from autoelective import monitor as runtime
from autoelective.client import APIError


URL = 'https://jw.ustc.edu.cn/for-std/course-select/12345/turn/678/select'
PRIMARY, SECONDARY = 'DEMO1001P.01', 'DEMO3001P.01'


def write_config(path, *, mode='auto', interval=5, credits=20, swap=True, after=True,
                 swap_ack='DROP_THEN_ADD', after_ack='DROP_AFTER_SUCCESS', extra=''):
    path.write_text(f'''[page]\ncourse_select_url = {URL}\n\n[helper]\nmode = {mode}\npoll_interval_seconds = {interval}\nmax_credits = {credits}\nmax_run_minutes = 0\n\n[course:primary]\ncode = {PRIMARY}\n[course:secondary]\ncode = {SECONDARY}\n\n[swap:primary]\nenabled = {str(swap).lower()}\ntarget_course_code = {PRIMARY}\nconflict_drop_course_code = OLD1001P.01\nacknowledgement = {swap_ack}\n\n[after-select:primary]\nenabled = {str(after).lower()}\ntrigger_course_code = {PRIMARY}\ndrop_course_code = BACK1001P.01\nacknowledgement = {after_ack}\n{extra}\n''', encoding='utf-8')


def lesson(code, ident):
    return {'id': ident, 'code': code, 'limitCount': 10, 'course': {'credits': 2},
            'dateTimePlace': {'textZh': 'R: 3(1,2)'}, 'weekText': {'textZh': '2~18'},
            'scheduleGroups': []}


class RuntimeTests(unittest.TestCase):
    def test_auto_login_bootstrap_and_expiry_do_not_repeat_failed_password(self):
        for saved_exists in (False, True):
            for login_ok in (False, True):
                with self.subTest(saved=saved_exists, login_ok=login_ok), tempfile.TemporaryDirectory() as folder:
                    clock = [0.0]
                    def wait(seconds): clock[0] += max(0, seconds)
                    client = Mock()
                    client.lessons.side_effect = ([APIError('AUTH'), [lesson(PRIMARY, 10)]]
                                                  if login_ok else APIError('AUTH'))
                    client.counts.return_value = {10: 0}
                    engine = Mock()
                    engine.tick.return_value = {'selected_credits': 0, 'targets': {}, 'blocked': None}
                    settings = runtime.Settings('monitor', 5, 20, [PRIMARY], {}, {}, URL, 0,
                                                credentials=object())
                    saved = {'course_select_url': URL, 'storage_state': {'cookies': [], 'origins': []}}
                    with patch.object(runtime, 'SESSION_FILE', Path(folder) / 'session.json'), \
                            patch.object(runtime, 'STOP_FILE', Path(folder) / 'stop'), \
                            patch.object(runtime, 'read_session', return_value=saved if saved_exists else None), \
                            patch.object(runtime, 'Client', return_value=client), \
                            patch.object(runtime, 'Engine', return_value=engine), \
                            patch.object(runtime, 'atomic_json'), \
                            patch.object(runtime, '_renew_sso', return_value=False), \
                            patch.object(runtime, 'auto_login', return_value=login_ok) as login, \
                            patch.object(runtime, '_wait', side_effect=wait), \
                            patch.object(runtime.time, 'monotonic', side_effect=lambda: clock[0]):
                        result = runtime._run_loop(Mock(), Mock(cycles=8), settings, Mock())
                    self.assertEqual(result, 0 if login_ok else 2)
                    login.assert_called_once()
                    if not login_ok:
                        engine.tick.assert_not_called()
                    else:
                        self.assertGreater(engine.tick.call_count, 0)

    def test_settings_authorization_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.ini'
            write_config(path)
            settings = runtime.load_settings(path)
            self.assertEqual(settings.mode, 'auto')
            self.assertEqual(settings.swaps, {PRIMARY: 'OLD1001P.01'})
            self.assertEqual(settings.after_select, {PRIMARY: 'BACK1001P.01'})
            self.assertEqual(runtime.load_settings(path, dry_run=True).mode, 'monitor')
            write_config(path, extra='[course:alternative_a]\ncode = DEMO2001P.01\n[course:alternative_b]\ncode = DEMO2001P.02')
            self.assertEqual(runtime.load_settings(path).targets[-2:], ['DEMO2001P.01', 'DEMO2001P.02'])
            for change in (
                dict(swap_ack=''), dict(after_ack='wrong'), dict(interval=4.9), dict(credits=0),
            ):
                write_config(path, **change)
                with self.assertRaises(ValueError):
                    runtime.load_settings(path)
            write_config(path, extra=f'[course:duplicate]\ncode = {PRIMARY}\n')
            with self.assertRaises(ValueError):
                runtime.load_settings(path)
            write_config(path, extra='[swap:other]\nenabled = true\ntarget_course_code = DEMO1001P.01\nconflict_drop_course_code = OTHER.01\nacknowledgement = DROP_THEN_ADD\n')
            with self.assertRaises(ValueError):
                runtime.load_settings(path)

    def test_idle_sleep_request_releases_even_on_failure_and_does_not_hold_display(self):
        setter = Mock(return_value=0x80000000)
        logger = Mock()
        with patch.object(runtime, 'os', SimpleNamespace(name='nt')), \
                patch('ctypes.WinDLL', return_value=Mock(SetThreadExecutionState=setter), create=True):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                with runtime.keep_awake(True, logger) as active:
                    self.assertTrue(active)
                    raise RuntimeError('synthetic')
        self.assertEqual(setter.call_args_list, [call(0x80000001), call(0x80000000)])
        setter.reset_mock()
        setter.return_value = 0
        with patch.object(runtime, 'os', SimpleNamespace(name='nt')), \
                patch('ctypes.WinDLL', return_value=Mock(SetThreadExecutionState=setter), create=True):
            with self.assertRaises(RuntimeError):
                with runtime.keep_awake(True, logger):
                    self.fail('must not start if Windows refuses')
        setter.assert_called_once_with(0x80000001)
        with runtime.keep_awake(False, logger) as active:
            self.assertFalse(active)

    def test_atomic_status_and_second_lock_are_exclusive(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            status = base / 'status.json'
            runtime.atomic_json(status, {'state': 'ok'})
            self.assertEqual(json.loads(status.read_text(encoding='utf-8')), {'state': 'ok'})
            self.assertEqual(list(base.glob('*.tmp')), [])
            with runtime.process_lock(base / 'process.lock'):
                with self.assertRaises(OSError):
                    with runtime.process_lock(base / 'process.lock'):
                        pass

    def test_run_loop_batches_both_counts_at_five_second_cadence_readonly(self):
        class Clock:
            now = 0.0
            def wait(self, seconds): self.now += max(0, seconds)
        class SafeClient:
            def __init__(self): self.count_calls, self.submit_calls = [], 0
            def lessons(self): return [lesson(PRIMARY, 10), lesson(SECONDARY, 20)]
            def counts(self, ids): self.count_calls.append(list(ids)); return {ident: 0 for ident in ids}
            def selected(self): return []
            def submit(self, *_): self.submit_calls += 1; raise AssertionError('read-only monitor must not submit')
        class SpyEngine:
            def __init__(self, client, **_): self.client = client
            def tick(self, offered, counts, *, observed_at):
                assert observed_at == clock.now
                self.client.selected()
                return {'selected_credits': 0, 'targets': {PRIMARY: 'ok', SECONDARY: 'ok'}, 'blocked': None}
        clock, client = Clock(), SafeClient()
        context = Mock(request=object())
        browser = Mock()
        browser.new_context.return_value = context
        playwright = Mock()
        playwright.chromium.launch.return_value = browser
        settings = runtime.Settings('monitor', 5, 20, [PRIMARY, SECONDARY], {}, {}, URL, 0)
        args = Mock(cycles=3)
        logger = Mock()
        saved = {'course_select_url': URL, 'storage_state': {'cookies': [], 'origins': []}}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(runtime, 'SESSION_FILE', Path(folder) / 'session.json'), \
                patch.object(runtime, 'read_session', return_value=saved), \
                patch.object(runtime, 'Client', return_value=client) as factory, \
                patch.object(runtime, 'Engine', SpyEngine), \
                patch.object(runtime, 'atomic_json'), \
                patch.object(runtime, '_wait', side_effect=clock.wait), \
                patch.object(runtime.time, 'monotonic', side_effect=lambda: clock.now), \
                patch.object(runtime.time, 'time', side_effect=lambda: 1000 + clock.now):
            self.assertEqual(runtime._run_loop(playwright, args, settings, logger), 0)
        self.assertEqual(client.count_calls, [[10, 20], [10, 20], [10, 20]])
        self.assertEqual(client.submit_calls, 0)
        self.assertEqual([call.kwargs['read_only'] for call in factory.call_args_list], [True])
        self.assertEqual(clock.now, 10)

    def test_readonly_network_failure_backs_off_without_submit(self):
        class Clock:
            now = 0.0
            def wait(self, seconds): self.now += max(0, seconds)
        class SafeClient:
            def __init__(self): self.count_calls = 0; self.submit_calls = 0
            def lessons(self): return [lesson(PRIMARY, 10), lesson(SECONDARY, 20)]
            def counts(self, _): self.count_calls += 1; raise APIError('NETWORK')
            def submit(self, *_): self.submit_calls += 1
        clock, client = Clock(), SafeClient()
        context, browser, playwright = Mock(request=object()), Mock(), Mock()
        browser.new_context.return_value = context
        playwright.chromium.launch.return_value = browser
        settings = runtime.Settings('monitor', 5, 20, [PRIMARY, SECONDARY], {}, {}, URL, 0)
        saved = {'course_select_url': URL, 'storage_state': {'cookies': [], 'origins': []}}
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(runtime, 'SESSION_FILE', Path(folder) / 'session.json'), \
                patch.object(runtime, 'read_session', return_value=saved), \
                patch.object(runtime, 'Client', return_value=client) as factory, \
                patch.object(runtime, 'atomic_json'), \
                patch.object(runtime, '_wait', side_effect=clock.wait), \
                patch.object(runtime.time, 'monotonic', side_effect=lambda: clock.now), \
                patch.object(runtime.time, 'time', side_effect=lambda: 1000 + clock.now):
            self.assertEqual(runtime._run_loop(playwright, Mock(cycles=2), settings, Mock()), 2)
        self.assertEqual(client.count_calls, 2)
        self.assertEqual(client.submit_calls, 0)
        self.assertEqual([call.kwargs['read_only'] for call in factory.call_args_list], [True])
        self.assertGreaterEqual(clock.now, 10)


if __name__ == '__main__':
    unittest.main()
