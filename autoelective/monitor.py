"""INI, console/file heartbeat and single-process runtime. No browser UI scraping."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sys
import tempfile
import time

from .session import ROOT, SESSION_FILE, clean_course_url, read_session, write_session, auto_login
from .engine import Engine
from .config import Settings, load_settings
from .client import APIError, Client

STATE_DIR = ROOT / '.local' / 'monitor'
STATUS_FILE = STATE_DIR / 'status.json'
STOP_FILE = STATE_DIR / 'stop.request'
@contextmanager
def keep_awake(enabled, logger):
    """Hold a thread-scoped idle-sleep request, not a permanent power-plan change."""
    if not enabled:
        yield False
        return
    if os.name != 'nt':
        raise RuntimeError('自动防休眠目前仅支持 Windows')
    import ctypes
    setter = ctypes.WinDLL('kernel32', use_last_error=True).SetThreadExecutionState
    setter.argtypes = [ctypes.c_uint]
    setter.restype = ctypes.c_uint
    if not setter(0x80000001):  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED, deliberately not DISPLAY_REQUIRED
        logger.error('Windows 拒绝防自动睡眠请求，监控未启动。')
        raise RuntimeError('防自动睡眠请求失败')
    logger.info('已申请防自动睡眠；允许锁屏和熄屏，程序退出时释放，不更改电源方案。')
    try:
        yield True
    finally:
        setter(0x80000000)
        logger.info('已释放防自动睡眠请求。')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='status-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def process_lock(path):
    # ponytail: one project-wide process; per-account locks only if multi-account use is requested.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def get_logger():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('ustc.monitor')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        for handler in (logging.StreamHandler(sys.stdout),
                        RotatingFileHandler(STATE_DIR / 'monitor.log', maxBytes=5_000_000,
                                            backupCount=3, encoding='utf-8')):
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S'))
            logger.addHandler(handler)
    return logger


def request_stop():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()
    print('已请求安全停止：当前选退课事务完成或进入未知保护后退出，不强制中断请求。')
    return 0


def show_status():
    if not STATUS_FILE.exists():
        print('暂无 Python 监控心跳。')
        return 2
    state = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
    state['heartbeat_age_seconds'] = round(max(0, time.time() - state['timestamp']), 1)
    state['note'] = '这是最后一次心跳；心跳陈旧不代表仍在运行。'
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def _wait(seconds):
    deadline = time.monotonic() + max(0, seconds)
    while not STOP_FILE.exists() and time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def _renew_sso(context, target):
    """Reuse official SSO once; never fill credentials or loop login submissions."""
    page = context.new_page()
    try:
        page.goto(target, wait_until='domcontentloaded', timeout=20000)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            try:
                if clean_course_url(page.url) == target:
                    Client(context.request, target, read_only=True).selected()
                    write_session(target, context.cookies())
                    return True
            except (ValueError, APIError):
                continue
    except Exception:
        pass
    finally:
        try:
            page.close()
        except Exception:
            pass
    return False


def run(args):
    from playwright.sync_api import sync_playwright
    if args.cycles < 0:
        raise ValueError('cycles 不能为负数')
    settings = load_settings(args.config, dry_run=args.dry_run)
    logger = get_logger()
    try:
        with process_lock(STATE_DIR / 'process.lock'):
            # Starting explicitly supersedes an old stop request; never removes an action journal.
            STOP_FILE.unlink(missing_ok=True)
            with keep_awake(settings.prevent_idle_sleep, logger) as power_active:
                with sync_playwright() as playwright:
                    return _run_loop(playwright, args, settings, logger, power_active=power_active)
    except OSError:
        logger.error('无法获取监控进程锁或访问本地状态；请确认没有另一实例运行。')
        return 2


def _run_loop(playwright, args, settings, logger, *, power_active=False):
    browser = playwright.chromium.launch(channel='msedge', headless=True)
    context = None
    engine = None
    session_stamp = None
    target = settings.course_url
    offered, metadata_at = [], 0.0
    started = time.monotonic()
    last_count = None
    next_run = started
    last_renew = -math.inf
    credential_attempted = False
    failures = 0
    round_no = 0
    last_success = None
    state = {'mode': settings.mode, 'interval_seconds': settings.interval,
             'max_credits': settings.max_credits, 'pid': os.getpid(),
             'idle_sleep_prevention': power_active}
    logger.info('启动 Python 监控 mode=%s 人数请求间隔=%gs 学分上限=%g 目标=%s',
                settings.mode, settings.interval, settings.max_credits, ','.join(settings.targets))
    logger.info('锁屏不停止；电脑真正睡眠时无法请求，唤醒后继续；请勿同时启动扩展自动操作。')
    try:
        while not STOP_FILE.exists():
            if args.cycles and round_no >= args.cycles:
                break
            if settings.max_minutes and time.monotonic() - started >= settings.max_minutes * 60:
                break
            _wait(next_run - time.monotonic())
            if STOP_FILE.exists():
                break
            tick_start = time.monotonic()
            if last_count is not None and tick_start - last_count > max(30, settings.interval * 4) and failures == 0:
                logger.warning('检测到运行间隔延长（睡眠或调度延迟）；恢复查询，不补发积压请求。')
            round_no += 1
            state.update(round=round_no, state='checking', timestamp=time.time())
            atomic_json(STATUS_FILE, state)
            delay = settings.interval
            try:
                stamp = SESSION_FILE.stat().st_mtime_ns if SESSION_FILE.exists() else None
                if context is None or stamp != session_stamp:
                    saved = read_session()
                    if saved is None:
                        if settings.credentials is None or not target:
                            if settings.credentials is not None and not target:
                                logger.warning('首次自动登录需要配置本人当前批次 course_select_url。')
                            raise APIError('AUTH')
                        saved = {'course_select_url': target,
                                 'storage_state': {'cookies': [], 'origins': []}}
                    saved_url = clean_course_url(saved['course_select_url'])
                    if target and target != saved_url:
                        raise APIError('CONTEXT_MISMATCH')
                    target = target or saved_url
                    if context is not None:
                        context.close()
                    context = browser.new_context(storage_state=saved['storage_state'], accept_downloads=False)
                    session_stamp = stamp
                    client = Client(context.request, target, read_only=settings.mode != 'auto')
                    if engine is None:
                        engine = Engine(client, targets=settings.targets, mode=settings.mode,
                                        max_credits=settings.max_credits, swaps=settings.swaps,
                                        after_select=settings.after_select, journal_path=STATE_DIR / 'transaction.json',
                                        logger=logger, prerequisites=settings.prerequisites,
                                        groups=settings.groups, unscheduled_selected_codes=settings.unscheduled_selected_codes)
                    else:
                        engine.client = client
                    offered, metadata_at = [], 0
                    logger.info('已加载本地会话；将以官网接口核验，不输出 Cookie。')
                if not offered or tick_start - metadata_at >= 60:
                    offered = client.lessons()
                    metadata_at = time.monotonic()
                ids = [item['id'] for item in offered if item['code'] in settings.targets]
                # Missing or already-selected targets must not starve other targets.
                # Engine still requires an exact unique match before adding anything.
                # One request includes all target sections, including prerequisite-gated ones.
                last_count = time.monotonic()
                count_time = time.time()
                counts = client.counts(ids) if ids else {}
                result = engine.tick(offered, counts, observed_at=last_count)
                failures = 0
                # Only a verified working monitoring round starts a new recovery episode.
                credential_attempted = False
                state.pop('error', None)
                last_success = datetime.now().isoformat(timespec='seconds')
                state.update(result, state='blocked' if result.get('blocked') else 'monitoring',
                             count_requested_at=count_time if ids else None, last_success=last_success)
                logger.info('第%d轮 | 已选学分=%s/%g | %s%s', round_no, result.get('selected_credits'),
                            settings.max_credits, ' | '.join(f'{k} {v}' for k, v in result.get('targets', {}).items()),
                            ' | ' + result['blocked'] if result.get('blocked') else '')
            except APIError as error:
                failures += 1
                delay = max(error.retry_after, min(300, settings.interval * 2 ** min(failures, 6)))
                state.update(state='waiting_auth' if error.code == 'AUTH' else 'retrying',
                             error=error.code, last_success=last_success)
                logger.warning('第%d轮 | %s | %.0fs 后重试；本轮未继续自动操作。', round_no, error.code, delay)
                if error.code == 'AUTH':
                    delay = max(60, delay)
                    if context is not None and time.monotonic() - last_renew >= 300:
                        last_renew = time.monotonic()
                        if _renew_sso(context, target):
                            logger.info('官网 SSO 会话续期成功，下轮重新加载。')
                            delay = settings.interval
                        else:
                            if settings.credentials is not None and not credential_attempted:
                                credential_attempted = True
                                logger.info('尝试官方账号密码登录一次；不会输出登录凭据。')
                                if auto_login(context, target, settings.credentials):
                                    logger.info('自动登录经官网接口核验成功，下轮恢复查询。')
                                    delay = settings.interval
                                else:
                                    logger.warning('自动登录未成功，暂停密码提交；请核对密码或完成官网额外验证。')
                            else:
                                logger.warning('需在另一终端运行 python main.py login；保存新会话后监控会自动恢复。')
            except Exception as error:
                # Browser exceptions can contain request bodies; log the type, never raw text/traceback.
                failures += 1
                delay = min(300, settings.interval * 2 ** min(failures, 6))
                state.update(state='retrying', error=type(error).__name__, last_success=last_success)
                logger.error('第%d轮 | %s | %.0fs 后重试；保留未完成事务保护。', round_no, type(error).__name__, delay)
            state.update(timestamp=time.time(), retry_in_seconds=delay if failures else None)
            atomic_json(STATUS_FILE, state)
            # Start-to-start normal cadence; long actions/slow requests never cause a catch-up burst.
            next_run = (last_count + settings.interval if failures == 0 and last_count is not None
                        and time.monotonic() < last_count + settings.interval else time.monotonic() + delay)
    finally:
        state.update(timestamp=time.time(), state='stopped')
        atomic_json(STATUS_FILE, state)
        browser.close()
        logger.info('Python 监控已退出；未知选退课事务不会被清除。')
    return 0 if last_success else 2
