import unittest
from unittest.mock import Mock

from autoelective.client import APIError, Client, _retry_after, classify_outcome

URL = 'https://jw.ustc.edu.cn/for-std/course-select/12345/turn/678/select'
UUID = '01234567-89ab-cdef-0123-456789abcdef'


def lesson(lesson_id=1, code='TEST.01', credits=2):
    return {'id': lesson_id, 'code': code, 'course': {'credits': credits}}


def response(status=200, content_type='application/json', body=None, url='https://jw.ustc.edu.cn/ws/for-std/course-select/x'):
    value = Mock(status=status, headers={'content-type': content_type}, url=url)
    value.json.return_value = body
    value.text.return_value = body if isinstance(body, str) else ''
    return value


class ClientTests(unittest.TestCase):
    def test_rate_limit_respects_long_server_delay_and_rejects_nonfinite(self):
        self.assertEqual(_retry_after({'retry-after': '7200'}), 7200)
        self.assertEqual(_retry_after({'retry-after': 'nan'}), 60)
        self.assertEqual(_retry_after({'retry-after': '-5'}), 5)
        self.assertEqual(_retry_after({'retry-after': 'invalid'}), 5)

    def test_outcome_does_not_retain_arbitrary_server_content(self):
        result = classify_outcome({'success': False, 'errorMessage': '人数已满 synthetic-private-data'})
        self.assertEqual(result['code'], 'FULL')
        self.assertNotIn('synthetic-private-data', str(result))
        self.assertNotIn('payload', result)
        self.assertEqual(classify_outcome(None)['code'], 'PENDING')

    def setUp(self):
        self.request = Mock()
        self.client = Client(self.request, URL)

    def test_batch_counts_is_one_repeated_form_post(self):
        reply = response(body={'1': 4, '2': 5})
        self.request.post.return_value = reply
        self.assertEqual(self.client.counts([1, 2]), {1: 4, 2: 5})
        _, kwargs = self.request.post.call_args
        self.assertIn('lessonIds%5B%5D=1&lessonIds%5B%5D=2', kwargs['data'])
        self.assertEqual(kwargs['max_redirects'], 0)
        self.assertEqual(kwargs['timeout'], 15000)
        reply.dispose.assert_called_once()

    def test_selected_and_lessons_validate_payload(self):
        self.request.post.return_value = response(body=[lesson()])
        self.assertEqual(self.client.selected()[0]['code'], 'TEST.01')
        self.request.post.return_value = response(body=[lesson(), lesson(1, 'OTHER.01')])
        with self.assertRaisesRegex(APIError, 'SCHEMA'):
            self.client.lessons()

    def test_submit_has_exact_forms_then_one_outcome_query(self):
        first = response(content_type='text/plain', body=UUID)
        second = response(body={'success': True})
        self.request.post.side_effect = [first, second]
        self.assertEqual(self.client.submit('add', 9), UUID)
        self.assertTrue(self.client.outcome(UUID)['success'])
        add = self.request.post.call_args_list[0].kwargs['data']
        self.assertIn('studentAssoc=12345', add)
        self.assertIn('lessonAssoc=9', add)
        self.assertIn('courseSelectTurnAssoc=678', add)
        self.assertIn('scheduleGroupAssoc=', add)
        self.assertIn('virtualCost=0', add)
        self.assertEqual(self.request.post.call_count, 2)

    def test_drop_and_dry_run_guard(self):
        self.request.post.return_value = response(content_type='text/plain', body=UUID)
        self.assertEqual(self.client.submit('drop', 9), UUID)
        dry = Client(self.request, URL, read_only=True)
        with self.assertRaisesRegex(APIError, 'WRITE_BLOCKED'):
            dry.submit('drop', 9)

    def test_auth_html_http_and_rate_limit_are_sanitized_and_disposed(self):
        for reply, code in [
            (response(status=403), 'AUTH'),
            (response(content_type='text/html'), 'AUTH'),
            (response(status=500), 'HTTP'),
            (response(status=429, body={}, url='https://jw.ustc.edu.cn/x'), 'RATE_LIMIT'),
        ]:
            with self.subTest(code=code):
                self.request.post.return_value = reply
                with self.assertRaisesRegex(APIError, code):
                    self.client.selected()
                reply.dispose.assert_called_once()

    def test_timeout_is_not_retried_and_invalid_responses_fail_closed(self):
        self.request.post.side_effect = RuntimeError('network details must stay private')
        with self.assertRaisesRegex(APIError, 'NETWORK'):
            self.client.selected()
        self.request.post.assert_called_once()
        self.request.post.reset_mock(side_effect=True)
        self.request.post.return_value = response(body={'1': -1})
        with self.assertRaisesRegex(APIError, 'SCHEMA'):
            self.client.counts([1])
        self.request.post.return_value = response(content_type='text/plain', body='not-a-uuid')
        with self.assertRaisesRegex(APIError, 'SCHEMA'):
            self.client.submit('add', 1)


if __name__ == '__main__':
    unittest.main()
