"""Explicit, offline-validated authorization and monitoring settings."""
from __future__ import annotations

import configparser
from dataclasses import dataclass, field
import math
import re

from .session import clean_course_url, read_credentials, Credentials

CODE = re.compile(r'[A-Z][A-Z0-9_-]*\.[A-Z0-9_-]+')


def course_code(value):
    value = value.strip()
    if not CODE.fullmatch(value):
        raise ValueError('课堂代码格式无效；使用官网带班号的完整代码')
    return value


def codes(value):
    return tuple(course_code(part) for part in value.split(',') if part.strip())


@dataclass(frozen=True)
class Settings:
    mode: str
    interval: float
    max_credits: float
    targets: list[str]
    swaps: dict[str, str]
    after_select: dict[str, str]
    course_url: str
    max_minutes: float
    prevent_idle_sleep: bool = False
    prerequisites: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)
    unscheduled_selected_codes: tuple = ()
    credentials: Credentials | None = field(default=None, repr=False)


def load_settings(path, *, dry_run=False):
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(path, encoding='utf-8-sig'):
        raise ValueError('找不到 INI 配置；请复制 config.sample.ini 为 config.ini')
    allowed_sections = {
        'page': {'course_select_url'},
        'auth': {'auto_login', 'username', 'password'},
        'helper': {'mode', 'poll_interval_seconds', 'max_credits', 'max_run_minutes',
                   'prevent_idle_sleep', 'unscheduled_selected_codes'},
        'course': {'enabled', 'code', 'group', 'requires_selected', 'requires_absent'},
        'swap': {'enabled', 'target_course_code', 'conflict_drop_course_code', 'acknowledgement'},
        'after-select': {'enabled', 'trigger_course_code', 'drop_course_code', 'acknowledgement'},
    }
    if config.defaults():
        raise ValueError('不支持 DEFAULT 隐式授权；请在各节显式填写')
    for section in config.sections():
        kind = section.split(':', 1)[0]
        if (kind not in allowed_sections or (kind in {'course', 'swap', 'after-select'} and
                (':' not in section or not section.split(':', 1)[1].strip())) or
                (kind in {'page', 'helper', 'auth'} and section != kind)):
            raise ValueError('未知配置节；请参照示例')
        if set(config[section]) - allowed_sections[kind]:
            raise ValueError('未知配置项；为避免误授权，拒绝忽略拼写错误')
    mode = config.get('helper', 'mode', fallback='monitor').strip()
    interval = config.getfloat('helper', 'poll_interval_seconds', fallback=30)
    credits = config.getfloat('helper', 'max_credits')
    minutes = config.getfloat('helper', 'max_run_minutes', fallback=0)
    if mode not in {'monitor', 'auto'} or not math.isfinite(interval) or interval < 5:
        raise ValueError('模式应为 monitor/auto；人数请求间隔不得小于 5 秒')
    if not math.isfinite(credits) or credits <= 0:
        raise ValueError('必须填写大于 0 的学分上限；以学校对本人规则为准')
    if not math.isfinite(minutes) or minutes < 0:
        raise ValueError('运行时长不能为负数；0 表示持续运行')
    targets, swaps, after, prerequisites, groups = [], {}, {}, {}, {}
    for section in config.sections():
        if section.startswith('course:') and config.getboolean(section, 'enabled', fallback=True):
            code = course_code(config.get(section, 'code'))
            if code in targets:
                raise ValueError('目标课堂代码不得重复')
            targets.append(code)
            selected = codes(config.get(section, 'requires_selected', fallback=''))
            absent = codes(config.get(section, 'requires_absent', fallback=''))
            if code in selected or code in absent or set(selected) & set(absent):
                raise ValueError('目标前置条件自引用或互相矛盾')
            prerequisites[code] = {'selected': selected, 'absent': absent}
            group = config.get(section, 'group', fallback='').strip()
            if group:
                if not re.fullmatch(r'[a-zA-Z0-9_-]+', group):
                    raise ValueError('互斥组名称只支持英文、数字、下划线、连字符')
                groups[code] = group
        elif section.startswith(('swap:', 'after-select:')) and config.getboolean(section, 'enabled', fallback=False):
            swapping = section.startswith('swap:')
            trigger = course_code(config.get(section, 'target_course_code' if swapping else 'trigger_course_code'))
            drop = course_code(config.get(section, 'conflict_drop_course_code' if swapping else 'drop_course_code'))
            mapping = swaps if swapping else after
            expected = 'DROP_THEN_ADD' if swapping else 'DROP_AFTER_SUCCESS'
            if config.get(section, 'acknowledgement', fallback='').strip() != expected:
                raise ValueError('启用退课必须填写示例中的完整风险确认字符串')
            if trigger == drop or trigger in mapping:
                raise ValueError('退课触发目标自引用或重复')
            mapping[trigger] = drop
    if not targets or any(code not in targets for code in (*swaps, *after)):
        raise ValueError('目标不能为空，退课触发课堂必须是已启用的目标')
    if set(targets) & (set(swaps.values()) | set(after.values())):
        raise ValueError('退课对象不能同时是监控目标，避免反复选退')
    required_anywhere = {required for rule in prerequisites.values() for required in rule['selected']}
    if required_anywhere & (set(swaps.values()) | set(after.values())):
        raise ValueError('退课对象不能是任何已启用目标要求保留的前置课程')
    for trigger in targets:
        removed = {swaps.get(trigger), after.get(trigger)} - {None}
        if removed & set(prerequisites[trigger]['selected']):
            raise ValueError('退课对象不能是同一目标要求保留的前置课程')
        if swaps.get(trigger) and swaps.get(trigger) == after.get(trigger):
            raise ValueError('冲突退课和成功后退课对象不得重复')
    # Fail closed on selected-prerequisite cycles between enabled targets.
    visited, active = set(), set()
    def visit(code):
        if code in active:
            raise ValueError('目标之间存在循环前置条件')
        if code in visited:
            return
        active.add(code)
        for required in prerequisites[code]['selected']:
            if required in prerequisites:
                visit(required)
        active.remove(code)
        visited.add(code)
    for code in targets:
        visit(code)
    url = config.get('page', 'course_select_url', fallback='').strip()
    return Settings('monitor' if dry_run else mode, interval, credits, targets, swaps, after,
                    clean_course_url(url) if url else '', minutes,
                    config.getboolean('helper', 'prevent_idle_sleep', fallback=False),
                    prerequisites, groups,
                    codes(config.get('helper', 'unscheduled_selected_codes', fallback='')),
                    read_credentials(path))
