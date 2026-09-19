from pathlib import Path
import tempfile
import unittest

from autoelective.config import load_settings


BASE = '''[helper]
mode = monitor
max_credits = 20
[course:primary]
code = DEMO1001P.01
'''


class ConfigTests(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.ini'
            path.write_text(text, encoding='utf-8')
            return load_settings(path)

    def test_defaults_safe_and_arbitrary_cap_not_personal_limit(self):
        settings = self.read(BASE.replace('20', '27.5'))
        self.assertEqual(settings.mode, 'monitor')
        self.assertEqual(settings.interval, 30)
        self.assertEqual(settings.max_credits, 27.5)
        self.assertEqual(settings.swaps, {})
        self.assertFalse(settings.prevent_idle_sleep)
        self.assertEqual(settings.unscheduled_selected_codes, ())

    def test_unknown_keys_and_nonfinite_numbers_rejected(self):
        for text in (BASE.replace('mode =', 'mod ='), BASE.replace('20', 'nan'),
                     BASE.replace('20', 'inf'), BASE.replace('20', '-1'),
                     BASE.replace('[helper]', '[DEFAULT]\nmode=auto\n[helper]')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.read(text)

    def test_groups_and_prerequisites(self):
        settings = self.read(BASE + '''[course:second]
code = DEMO2001P.01
group = alternatives
requires_selected = DEMO1001P.01
requires_absent = OLD1001P.01, BACK1001P.01
''')
        self.assertEqual(settings.prerequisites['DEMO2001P.01']['selected'], ('DEMO1001P.01',))
        self.assertEqual(settings.groups, {'DEMO2001P.01': 'alternatives'})

    def test_self_reference_and_cycles_rejected(self):
        for text in (BASE + 'requires_selected = DEMO1001P.01\n', BASE + '''requires_selected = DEMO2001P.01
[course:second]
code = DEMO2001P.01
requires_selected = DEMO1001P.01
'''):
            with self.assertRaises(ValueError):
                self.read(text)

    def test_cross_target_drop_prerequisite_rejected(self):
        with self.assertRaises(ValueError):
            self.read(BASE + '''[course:dependent]
code = DEMO2001P.01
requires_selected = OLD1001P.01
[after-select:primary]
enabled = true
trigger_course_code = DEMO1001P.01
drop_course_code = OLD1001P.01
acknowledgement = DROP_AFTER_SUCCESS
''')

    def test_shipped_sample_valid_and_safe(self):
        sample = Path(__file__).resolve().parents[1] / 'config.sample.ini'
        settings = load_settings(sample)
        self.assertEqual(settings.mode, 'monitor')
        self.assertEqual(settings.swaps, {})
        self.assertEqual(settings.after_select, {})
        self.assertEqual(settings.course_url, '')


if __name__ == '__main__':
    unittest.main()
