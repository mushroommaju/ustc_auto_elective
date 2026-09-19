"""Independent login, session verification and course-monitor CLI."""
from __future__ import annotations

import configparser
from dataclasses import dataclass, field
import getpass
import json
import os
from pathlib import Path
import re
import tempfile
import time
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
SESSION_FILE = ROOT / '.local' / 'auth' / 'session.json'
COURSE_PATH = re.compile(r'/for-std/course-select/(\d+)/turn/(\d+)/select/?')
JW = 'https://jw.ustc.edu.cn'
LOGIN_ORIGIN = 'https://id.ustc.edu.cn'
SELECTED_URL = JW + '/ws/for-std/course-select/selected-lessons'


@dataclass(frozen=True)
class Credentials:
    """Local-only login data.  Its values are intentionally absent from repr()."""
    username: str = field(repr=False)
    password: str = field(repr=False)


def course_context(url):
    """Only an exact official selection URL may determine API parameters."""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'jw.ustc.edu.cn':
        raise ValueError('必须使用科大教务 HTTPS 选课页面 URL')
    match = COURSE_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError('请进入具体选课页面，而非主页或批次列表')
    return {'studentId': match[1], 'turnId': match[2]}


def clean_course_url(url):
    course_context(url)
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def configured_url(path):
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(path, encoding='utf-8-sig'):
        raise ValueError('找不到配置文件；请复制 config.sample.ini 为 config.ini')
    url = config.get('page', 'course_select_url', fallback='').strip()
    return clean_course_url(url) if url else ''


def read_credentials(path):
    """Return opt-in local credentials, never falling back to interpolation."""
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(path, encoding='utf-8-sig'):
        raise ValueError('找不到配置文件；请复制 config.sample.ini 为 config.ini')
    if not config.getboolean('auth', 'auto_login', fallback=False):
        return None
    username = config.get('auth', 'username', fallback='').strip()
    password = config.get('auth', 'password', fallback='')
    if not username or not password:
        raise ValueError('已启用自动登录，但 [auth] 的 username 或 password 为空')
    return Credentials(username, password)


def read_session(path=SESSION_FILE):
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    clean_course_url(data['course_select_url'])
    state = data['storage_state']
    if not isinstance(state.get('cookies'), list) or state.get('origins') != []:
        raise ValueError('会话文件格式无效，请重新运行 login')
    if any(not allowed_cookie(cookie) for cookie in state['cookies']):
        raise ValueError('会话文件包含非认证范围的 Cookie')
    return data


def allowed_cookie(cookie):
    return isinstance(cookie, dict) and cookie.get('domain', '').lstrip('.') in {
        'jw.ustc.edu.cn', 'passport.ustc.edu.cn', 'id.ustc.edu.cn', 'ustc.edu.cn',
    }


def write_session(course_url, cookies, path=SESSION_FILE):
    # Cookies only: do not persist form fields or localStorage remembered passwords.
    data = {'course_select_url': clean_course_url(course_url),
            'storage_state': {'cookies': [c for c in cookies if allowed_cookie(c)], 'origins': []}}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='session-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify_session(request, course_url):
    """Read-only endpoint observed in HAR. A rendered home page is not proof."""
    response = request.post(SELECTED_URL, form=course_context(course_url),
                            headers={'X-Requested-With': 'XMLHttpRequest'},
                            max_redirects=0, timeout=20000)
    try:
        if response.status != 200 or 'application/json' not in response.headers.get('content-type', ''):
            return False
        lessons = response.json()
        return isinstance(lessons, list) and all(
            isinstance(item, dict) and isinstance(item.get('code'), str)
            and isinstance(item.get('id'), int) and not isinstance(item.get('id'), bool)
            for item in lessons)
    finally:
        response.dispose()


def submit_credentials_once(page, username, password):
    """The real page constructs crypto and challenge fields; never forge them."""
    parsed = urlsplit(page.url)
    if parsed.scheme + '://' + parsed.netloc != LOGIN_ORIGIN or parsed.path != '/cas/login':
        raise ValueError('当前不是官方认证登录页，拒绝填入凭据')
    page.get_by_placeholder('请输入学工号/GID', exact=True).fill(username)
    page.get_by_placeholder('请输入密码', exact=True).fill(password)
    page.get_by_role('button', name='立即登录', exact=True).click()


def wait_for_browser_events(page):
    # Sync Playwright only dispatches navigation/popup events during API calls.
    # time.sleep leaves page.url/context.pages stale after a manual login.
    try:
        page.wait_for_timeout(1000)
    except Exception:
        if not page.is_closed():
            raise


def auto_login(context, target, credentials, timeout=30):
    """Submit once through the official form, then save only a verified session."""
    target = clean_course_url(target)
    page = None
    submitted = False
    verifications = 0
    try:
        page = context.new_page()
        page.goto(target, wait_until='domcontentloaded')
        deadline = time.monotonic() + max(1, timeout)
        while time.monotonic() < deadline:
            try:
                candidate = clean_course_url(page.url)
            except ValueError:
                candidate = ''
            if candidate == target:
                verifications += 1
                if verify_session(context.request, target):
                    write_session(target, context.cookies())
                    return True
                if verifications >= 2:
                    return False
                page.wait_for_timeout(5000)
                continue
            parsed = urlsplit(page.url)
            is_login = (parsed.scheme + '://' + parsed.netloc == LOGIN_ORIGIN
                        and parsed.path == '/cas/login')
            if is_login and not submitted:
                submit_credentials_once(page, credentials.username, credentials.password)
                submitted = True
            elif parsed.scheme != 'https' or parsed.netloc not in {
                    'jw.ustc.edu.cn', 'id.ustc.edu.cn', 'passport.ustc.edu.cn'}:
                return False
            wait_for_browser_events(page)
    except Exception:
        # Login errors, challenges and wrong passwords must not trigger retries.
        return False
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
    return False


def login(args):
    from playwright.sync_api import sync_playwright
    target = configured_url(args.config)
    credentials = read_credentials(args.config)
    old = read_session()
    target = target or (old['course_select_url'] if old else '')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=False)
        try:
            context = browser.new_context(storage_state=old['storage_state'] if old else None,
                                          accept_downloads=False)
            if old and verify_session(context.request, target):
                write_session(target, context.cookies())
                print('已有会话有效，无需再次登录。')
                return 0
            if credentials:
                if not target:
                    print('自动登录需要在 config.ini 的 [page] 填写具体选课页面 URL。')
                    return 2
                if auto_login(context, target, credentials, timeout=args.timeout):
                    print('自动登录及选课接口核验成功；会话已保存至 .local/auth/session.json。')
                    return 0
                print('自动登录未完成（可能需要验证码、二次验证或密码有误）；未保存会话。')
                return 2
            page = context.new_page()
            page.goto(target or JW + '/home', wait_until='domcontentloaded')
            if args.prompt_credentials:
                print('如已自动回到教务页，无需填密码；否则本地输入一次，额外验证请在官网完成。')
                if urlsplit(page.url).hostname == 'id.ustc.edu.cn':
                    if not sys.stdin.isatty():
                        raise ValueError('密码只允许在交互终端输入')
                    username = input('学工号/GID：').strip()
                    password = getpass.getpass('密码（不回显、不保存）：')
                    try:
                        submit_credentials_once(page, username, password)
                    finally:
                        password = None
            print('请在独立 Edge 窗口完成登录，并进入具体选课页；程序只核验登录，不选退课。')
            deadline = time.monotonic() + args.timeout
            last_check = 0.0
            while time.monotonic() < deadline:
                pages = [tab for tab in context.pages if not tab.is_closed()]
                if not pages:
                    print('登录窗口已关闭，未保存新会话。')
                    return 2
                candidate = ''
                for tab in reversed(pages):
                    try:
                        candidate = clean_course_url(tab.url)
                        break
                    except ValueError:
                        continue
                if not candidate:
                    wait_for_browser_events(pages[0])
                    continue
                if target and candidate != target:
                    print('当前选课批次与配置不一致，未保存会话；请核对 config.ini。')
                    return 2
                if time.monotonic() - last_check >= 15:
                    last_check = time.monotonic()
                    if verify_session(context.request, candidate):
                        write_session(candidate, context.cookies())
                        print('登录及选课接口核验成功；会话已保存至 .local/auth/session.json。')
                        return 0
                wait_for_browser_events(pages[0])
            print('等待登录超时，未保存新会话；可以重新运行 login。')
            return 2
        finally:
            browser.close()


def check(args):
    from playwright.sync_api import sync_playwright
    saved = read_session()
    if saved is None:
        print('暂无保存会话，请先运行 python main.py login。')
        return 2
    target = configured_url(args.config) or saved['course_select_url']
    with sync_playwright() as playwright:
        # The standalone request client returned 403 with otherwise valid cookies.
        # Use the same Edge context as login, without opening a visible window.
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        try:
            context = browser.new_context(storage_state=saved['storage_state'], accept_downloads=False)
            valid = verify_session(context.request, target)
        finally:
            browser.close()
    print('会话有效；未执行选退课。' if valid else '会话未通过核验（可能失效或接口不可用）；未执行选退课。')
    return 0 if valid else 2
