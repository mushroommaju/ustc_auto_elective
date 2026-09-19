from pathlib import Path
import tempfile
import unittest

from tools.build_release import PUBLIC_FILES, public_sources


class ReleaseTests(unittest.TestCase):
    def test_sample_credentials_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, content in public_sources().items():
                file = root / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(content)
            sample = root / 'config.sample.ini'
            original = sample.read_text(encoding='utf-8')
            for old, new in [('username =', 'username = synthetic-user'),
                             ('password =', 'password = synthetic-password'),
                             ('auto_login = false', 'auto_login = true')]:
                sample.write_text(original.replace(old, new), encoding='utf-8')
                with self.assertRaises(ValueError):
                    public_sources(root)

    def test_allowlist_and_privacy_scan(self):
        sources = public_sources()
        self.assertEqual(set(sources), set(PUBLIC_FILES))
        self.assertNotIn('config.ini', sources)
        self.assertFalse(any('.local' in item or item.endswith(('.har', '.log', '.pyc')) for item in sources))
        self.assertTrue(all(source.decode('utf-8') for source in sources.values()))

    def test_private_runtime_not_collected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, content in public_sources().items():
                file = root / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(content)
            marker = 'SYNTHETIC_' + 'PRIVATE_MARKER'
            (root / 'config.ini').write_text(marker)
            (root / '.local').mkdir()
            (root / '.local' / 'session.json').write_text(marker)
            self.assertFalse(marker.encode() in b''.join(public_sources(root).values()))
            # A personal-looking URL injected into an allowed file must fail closed.
            target = root / 'main.py'
            target.write_text('https://jw.ustc.edu.cn/for-std/' + 'course-select/99999/turn/999/select')
            with self.assertRaises(ValueError):
                public_sources(root)


if __name__ == '__main__':
    unittest.main()
