import json
import unittest
from unittest.mock import patch

from eks_console import backend


VALID_BLOCK = """AWS_ACCESS_KEY_ID=AKIATESTVALUE
AWS_SECRET_ACCESS_KEY=test-secret-value
AWS_SESSION_TOKEN=test-session-value
AWS_REGION=us-east-1
"""


class AwsConnectionTests(unittest.TestCase):
    def setUp(self):
        self.demo = backend.DEMO
        backend.DEMO = False
        with backend.AWS_SESSION_LOCK:
            backend.AWS_SESSIONS.clear()

    def tearDown(self):
        backend.DEMO = self.demo
        with backend.AWS_SESSION_LOCK:
            backend.AWS_SESSIONS.clear()

    def test_parser_accepts_supported_shell_formats(self):
        values = backend.parse_environment_block(
            '$env:AWS_ACCESS_KEY_ID="key"\nexport AWS_SECRET_ACCESS_KEY=secret\nset AWS_SESSION_TOKEN=token',
            backend.AWS_KEYS,
        )
        self.assertEqual(values['AWS_ACCESS_KEY_ID'], 'key')
        self.assertEqual(values['AWS_SECRET_ACCESS_KEY'], 'secret')
        self.assertEqual(values['AWS_SESSION_TOKEN'], 'token')

    def test_parser_rejects_commands(self):
        with self.assertRaisesRegex(ValueError, 'asignaciones de variables'):
            backend.parse_environment_block('aws sso login', backend.AWS_KEYS)

    @patch.object(backend, 'resolve_context', return_value='qa-context')
    @patch.object(backend.shutil, 'which', return_value='aws')
    @patch.object(backend, 'run_command')
    def test_temporary_credentials_are_verified_before_success(self, run, _which, _resolve):
        run.return_value = json.dumps({
            'Account': '123456789012',
            'Arn': 'arn:aws:sts::123456789012:assumed-role/qa/user',
        })
        result = backend.aws_block_connect({'context': 'qa-context', 'block': VALID_BLOCK})
        self.assertTrue(result['status']['connected'])
        self.assertEqual(result['status']['source'], 'temporary')
        self.assertNotIn('values', result)
        self.assertIn('qa-context', backend.AWS_SESSIONS)

    @patch.object(backend, 'resolve_context', return_value='qa-context')
    @patch.object(backend.shutil, 'which', return_value='aws')
    @patch.object(backend, 'run_command', side_effect=RuntimeError('ExpiredToken'))
    def test_rejected_credentials_are_removed(self, _run, _which, _resolve):
        with self.assertRaisesRegex(ValueError, 'AWS rechazó'):
            backend.aws_block_connect({'context': 'qa-context', 'block': VALID_BLOCK})
        self.assertNotIn('qa-context', backend.AWS_SESSIONS)

    def test_demo_status_is_explicit(self):
        backend.DEMO = True
        status = backend.aws_connection_status('demo-eks-qa')
        self.assertTrue(status['connected'])
        self.assertEqual(status['source'], 'demo')

    @patch.object(backend, 'resolve_context', return_value='qa-context')
    @patch.object(backend, 'kubectl', return_value='2026-09-28T01:00:01.000Z INFO ready')
    def test_incremental_logs_use_since_time(self, kubectl, _resolve):
        since = '2026-09-28T01:00:00.000Z'
        result = backend.pod_content(
            'logs', 'qa', 'api-123', 'qa-context', 'api', False, since
        )
        self.assertIn('INFO ready', result['text'])
        args = kubectl.call_args.args
        self.assertIn('--since-time=' + since, args)
        self.assertNotIn('--tail=200', args)

    def test_incremental_logs_reject_invalid_timestamp(self):
        with self.assertRaisesRegex(ValueError, 'Marca de tiempo'):
            backend.pod_content(
                'logs', 'qa', 'api-123', 'qa-context', since='; rm -rf'
            )


if __name__ == '__main__':
    unittest.main()
