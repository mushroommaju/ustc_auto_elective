"""Minimal, fail-closed client for the observed USTC course-selection API."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
from urllib.parse import urlencode

from .session import course_context

API_ROOT = 'https://jw.ustc.edu.cn/ws/for-std/course-select'
REQUEST_ID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


class APIError(Exception):
    """A sanitized API failure.  Its text never contains response bodies."""

    def __init__(self, code, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _retry_after(headers):
    value = headers.get('retry-after', '')
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value).astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = 5
    return max(5, seconds) if math.isfinite(seconds) else 60


def _login_response(response):
    url = getattr(response, 'url', '') or ''
    headers = getattr(response, 'headers', {}) or {}
    content_type = headers.get('content-type', '')
    return (response.status in (401, 403) or 300 <= response.status < 400
            or '/login' in url.lower() or 'passport' in url.lower() or 'cas/' in url.lower()
            or 'text/html' in content_type.lower())


def classify_outcome(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('success'), bool):
        return {'success': False, 'code': 'PENDING', 'message': '请求仍在处理中'}
    if payload['success']:
        return {'success': True, 'code': 'SUCCESS', 'message': '操作成功'}
    raw = payload.get('errorMessage') or payload.get('exception') or ''
    message = raw if isinstance(raw, str) else next(
        (raw.get(key, '') for key in ('textZh', 'text', 'textEn') if isinstance(raw, dict) and raw.get(key)), '')
    code = 'REJECTED'
    if re.search(r'人数已满|Lesson is full', message, re.I):
        code = 'FULL'
    elif re.search(r'时间冲突|Date time conflict', message, re.I):
        code = 'TIME_CONFLICT'
    elif re.search(r'考试.*冲突|exam.*conflict', message, re.I):
        code = 'EXAM_CONFLICT'
    # Do not retain arbitrary server text: it may include account details.
    return {'success': False, 'code': code, 'message': '教务系统拒绝了请求'}


class Client:
    def __init__(self, request, course_url, *, read_only=False):
        self.request = request
        context = course_context(course_url)
        self.student_id = context['studentId']
        self.turn_id = context['turnId']
        self.read_only = read_only

    def _post(self, endpoint, pairs, *, text=False):
        response = None
        try:
            try:
                response = self.request.post(
                    f'{API_ROOT}/{endpoint}', data=urlencode([(key, str(value)) for key, value in pairs]),
                    headers={
                        'Accept': 'text/plain, application/json, */*',
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'X-Requested-With': 'XMLHttpRequest',
                    }, max_redirects=0, timeout=15000)
            except Exception as error:
                raise APIError('NETWORK') from error
            if response.status == 429:
                raise APIError('RATE_LIMIT', _retry_after(response.headers))
            if _login_response(response):
                raise APIError('AUTH')
            if response.status != 200:
                raise APIError('HTTP')
            if text:
                try:
                    return response.text().strip()
                except Exception as error:
                    raise APIError('NETWORK') from error
            if 'application/json' not in response.headers.get('content-type', '').lower():
                raise APIError('SCHEMA')
            try:
                return response.json()
            except (ValueError, json.JSONDecodeError) as error:
                raise APIError('SCHEMA') from error
        finally:
            if response is not None:
                response.dispose()

    @staticmethod
    def _lessons(payload):
        if not isinstance(payload, list):
            raise APIError('SCHEMA')
        ids, codes = set(), set()
        for lesson in payload:
            if not isinstance(lesson, dict) or not _positive_int(lesson.get('id')):
                raise APIError('SCHEMA')
            code = lesson.get('code')
            course = lesson.get('course')
            credits = course.get('credits') if isinstance(course, dict) else None
            if not isinstance(code, str) or not code.strip() or isinstance(credits, bool) or not isinstance(credits, (int, float)) or not math.isfinite(credits) or credits < 0:
                raise APIError('SCHEMA')
            if lesson['id'] in ids or code in codes:
                raise APIError('SCHEMA')
            ids.add(lesson['id'])
            codes.add(code)
        return payload

    def selected(self):
        return self._lessons(self._post('selected-lessons', [('studentId', self.student_id), ('turnId', self.turn_id)]))

    def lessons(self):
        return self._lessons(self._post('addable-lessons', [('studentId', self.student_id), ('turnId', self.turn_id)]))

    def counts(self, lesson_ids):
        ids = list(lesson_ids)
        if not ids or any(not _positive_int(value) for value in ids) or len(set(ids)) != len(ids):
            raise APIError('INVALID_ARGUMENT')
        payload = self._post('std-count', [('lessonIds[]', value) for value in ids])
        if not isinstance(payload, dict) or set(payload) != {str(value) for value in ids}:
            raise APIError('SCHEMA')
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in payload.values()):
            raise APIError('SCHEMA')
        return {lesson_id: payload[str(lesson_id)] for lesson_id in ids}

    def submit(self, action, lesson_id):
        if self.read_only:
            raise APIError('WRITE_BLOCKED')
        if action not in {'add', 'drop'} or not _positive_int(lesson_id):
            raise APIError('INVALID_ARGUMENT')
        pairs = [('studentAssoc', self.student_id), ('lessonAssoc', lesson_id), ('courseSelectTurnAssoc', self.turn_id)]
        if action == 'add':
            pairs += [('scheduleGroupAssoc', ''), ('virtualCost', 0)]
        request_id = self._post(f'{action}-request', pairs, text=True)
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise APIError('SCHEMA')
        return request_id

    def outcome(self, request_id):
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            raise APIError('INVALID_ARGUMENT')
        return classify_outcome(self._post('add-drop-response', [('studentId', self.student_id), ('requestId', request_id)]))
