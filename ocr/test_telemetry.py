# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error
import ci_client as client
from test_ci_client import EVENT, ENV, IDENTITY, reply


class TelemetryTests(unittest.TestCase):
    def run_client(self, result=None, error=None):
        with tempfile.TemporaryDirectory() as root:
            event = Path(root) / 'event.json'
            event.write_text(json.dumps(EVENT))
            env = {**ENV, 'GITHUB_EVENT_PATH': str(event), 'OCR_CI_TOKEN': 'must-not-leak',
                   'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_REPOSITORY_ID': '42',
                   'GITHUB_JOB': 'ocr-review', 'GITHUB_WORKFLOW_REF': 'lexmount/github-policy/workflow@main'}
            with patch.dict(os.environ, env, clear=True), patch.object(client, 'post', return_value=result, side_effect=error), contextlib.redirect_stdout(io.StringIO()) as output:
                if error:
                    with self.assertRaises(client.ReviewError): client.main()
                else:
                    self.assertEqual(client.main(), 0)
            self.assertNotIn('must-not-leak', output.getvalue())
            return [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]

    def test_one_final_result_with_snapshot_and_run_attempt(self):
        for state, reason, outcome in [('accepted', None, 'accepted'), ('failed', None, 'accepted'),
                                       ('skipped', 'repository_not_enabled', 'skipped')]:
            with self.subTest(state=state):
                ack = reply(state, durable=True, contract_version=2, acceptance='accepted', scheduler='backend', task_id='task', reason=reason)
                rows = self.run_client(ack)
                self.assertEqual([r['sequence'] for r in rows], [1, 2])
                final = rows[-1]
                self.assertEqual(final['event'], 'ocr_client_result')
                self.assertEqual(final['outcome'], outcome)
                self.assertEqual(final['request_id'], client.request_id(IDENTITY))
                self.assertEqual(final['run_attempt'], '2')
                self.assertEqual(final['schema'], 'ocr-policy-telemetry/v1')
                self.assertNotIn('task_id', final)

    def test_deadline_is_separate_from_retry_exhaustion_and_http_errors(self):
        for code, outcome in [('ack_deadline_exceeded', 'timeout'), ('gateway_retries_exhausted', 'failed'), ('http_error', 'failed')]:
            rows = self.run_client(error=client.ReviewError('test', code))
            self.assertEqual(rows[-1]['outcome'], outcome)
            self.assertEqual(rows[-1]['reason'], code)
            self.assertEqual(len([r for r in rows if r['event'] == 'ocr_client_result']), 1)

    def test_url_wrapped_socket_timeout_is_classified_without_changing_retries(self):
        with patch.object(client.urllib.request, 'build_opener') as opener, patch.object(client.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
            opener.return_value.open.side_effect = urllib.error.URLError(TimeoutError('socket timeout'))
            with self.assertRaises(client.ReviewError) as caught:
                client.post('ocr-ci-submit', {}, 'must-not-leak', client.time.monotonic()+90)
            self.assertEqual(caught.exception.code, 'gateway_retries_exhausted')
            self.assertEqual(opener.return_value.open.call_count, 4)
        self.assertTrue(all(json.loads(line)['error_code'] == 'http_timeout' for line in output.getvalue().splitlines()))

    def test_logging_io_failure_does_not_change_result(self):
        telemetry = client.Telemetry()
        with patch('builtins.print', side_effect=BrokenPipeError):
            telemetry.finish('accepted')
            telemetry.finish('failed')
        self.assertTrue(telemetry.finished)
        self.assertEqual(telemetry.sequence, 1)


if __name__ == '__main__': unittest.main()
