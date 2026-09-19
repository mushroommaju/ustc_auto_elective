"""Build a public source ZIP from an explicit allowlist, never from runtime data."""
from pathlib import Path
import configparser
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    'README.md', 'SECURITY.md', 'LICENSE', '.gitignore', 'requirements.txt',
    'config.sample.ini', 'main.py', '.github/workflows/tests.yml',
    'autoelective/__init__.py', 'autoelective/__main__.py', 'autoelective/cli.py',
    'autoelective/session.py', 'autoelective/client.py', 'autoelective/config.py',
    'autoelective/engine.py', 'autoelective/monitor.py',
    'tests/__init__.py', 'tests/test_client.py', 'tests/test_config.py',
    'tests/test_engine.py', 'tests/test_monitor.py', 'tests/test_session.py',
    'tests/test_release.py', 'tools/build_release.py',
)


def public_sources(root=ROOT):
    """Fail on missing files, escaping symlinks, and obvious credential/path patterns."""
    root = root.resolve()
    patterns = (
        re.compile(r'eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}'),
        re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
        re.compile(r'[A-Za-z]:[\\/](?:Users|research|TEMP)[\\/]', re.I),
        re.compile(r'https://jw\.ustc\.edu\.cn/for-std/course-select/(\d+)/turn/(\d+)/select'),
    )
    sources = {}
    for relative in PUBLIC_FILES:
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError(f'Invalid public file: {relative}')
        data = path.read_bytes()
        content = data.decode('utf-8')
        for index, pattern in enumerate(patterns):
            for match in pattern.finditer(content):
                # Only this clearly documented synthetic account/batch fixture is allowed.
                if index == 3 and match.groups() == ('12345', '678'):
                    continue
                raise ValueError(f'Privacy review required: {relative}; pattern {index + 1}')
        sources[relative] = data
    sample = configparser.ConfigParser(interpolation=None)
    sample.read_string(sources['config.sample.ini'].decode('utf-8'))
    if (sample.getboolean('auth', 'auto_login', fallback=False) or
            sample.get('auth', 'username', fallback='').strip() or
            sample.get('auth', 'password', fallback='')):
        raise ValueError('Public sample must have auto_login disabled and blank credentials')
    return sources


def build(root=ROOT):
    sources = public_sources(root)
    destination = root / 'dist' / 'USTCAutoElective.zip'
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, content in sources.items():
            info = zipfile.ZipInfo('USTCAutoElective/' + relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    print(f'Built {destination.name}: {len(sources)} allowlisted files; manually review before publishing.')
    return destination


if __name__ == '__main__':
    build()
