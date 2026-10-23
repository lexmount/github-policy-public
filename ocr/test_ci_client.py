# SPDX-License-Identifier: Apache-2.0
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import ci_client as client
import render_workflow

IDENTITY = {'repository': 'lexmount/example', 'pull_number': 12, 'head_sha': 'a' * 40,
            'base_sha': 'b' * 40, 'base_ref': 'release/company'}
EVENT = {'pull_request': {'number': 12, 'state': 'open', 'draft': True,
                         'head': {'sha': IDENTITY['head_sha']},
                         'base': {'sha': IDENTITY['base_sha'], 'ref': IDENTITY['base_ref'],
                                  'repo': {'full_name': IDENTITY['repository']}}}}
ENV = {'GITHUB_EVENT_NAME': 'pull_request_target', 'GITHUB_REPOSITORY': IDENTITY['repository']}


def reply(state='complete', **extra):
    return {'schema': client.SCHEMA, 'request_id': client.request_id(IDENTITY), **IDENTITY,
            'state': state, 'coverage_complete': True, 'publication_complete': True,
            'findings': dict(critical=0, high=0, medium=0, low=0), **extra}


class WorkflowTests(unittest.TestCase):
    def test_embedded_workflow_matches_tested_client(self):
        self.assertEqual((render_workflow.ROOT / '.github/workflows/codex-code-review.yml').read_text(),
                         render_workflow.rendered())

    def test_base_branch_and_drafts_are_not_filtered(self):
        self.assertEqual(client.coordinates(EVENT, ENV), IDENTITY)

    def test_uses_pr_head_not_github_merge_sha(self):
        self.assertEqual(client.coordinates(EVENT, {**ENV, 'GITHUB_SHA': 'c' * 40}), IDENTITY)

    def test_rejects_wrong_repository_or_merge_group(self):
        for env in [{**ENV, 'GITHUB_REPOSITORY': 'lexmount/another'},
                    {**ENV, 'GITHUB_EVENT_NAME': 'merge_group'},
                    {**ENV, 'GITHUB_REPOSITORY': 'outside/example'}]:
            with self.assertRaises(client.ReviewError): client.coordinates(EVENT, env)

    def test_idempotency_changes_only_for_snapshot(self):
        self.assertEqual(client.request_id(IDENTITY), client.request_id(dict(reversed(list(IDENTITY.items())))))
        for key in ['head_sha', 'base_sha', 'base_ref']:
            self.assertNotEqual(client.request_id(IDENTITY), client.request_id({**IDENTITY, key: 'changed'}))

    def test_receipt_fences_stale_and_cross_repo_results(self):
        for patch_data in [{'head_sha': 'c' * 40}, {'base_sha': 'c' * 40}, {'repository': 'lexmount/other'},
                           {'request_id': 'unrelated'}, {'schema': 'v0'}, {'pull_number': True}]:
            with self.assertRaises(client.ReviewError): client.check_identity(reply(**patch_data), IDENTITY)
        client.check_identity(reply(), IDENTITY)

    def test_receipt_requires_persistence_and_published_status(self):
        for state in client.WAIT_STATES:
            self.assertIsNone(client.verdict(reply(state)))
            self.assertEqual(client.verdict(reply(state, durable=True, status_published=True,
                                                 task_id='task-1', commit_status='pending')), 0)

    def test_failed_review_can_ack_but_never_pretends_review_success(self):
        self.assertEqual(client.verdict(reply('complete', durable=True, status_published=True,
                                             task_id='task-1', commit_status='failure')), 0)

    def test_invalid_receipts_fail(self):
        for state in ['failed', 'cancelled', 'superseded', 'skipped', 'unknown']:
            with self.assertRaises(client.ReviewError): client.verdict(reply(state))
        for extra in [{'task_id': ''}, {'commit_status': 'success'}]:
            data = dict(durable=True, status_published=True, task_id='task-1', commit_status='pending')
            data.update(extra)
            with self.assertRaises(client.ReviewError): client.verdict(reply('queued', **data))

    def test_deadline_expires_before_another_request(self):
        with patch.object(client.urllib.request, 'build_opener') as opener:
            with self.assertRaises(client.ReviewError):
                client.post('ocr-ci-status', {}, 'test', client.time.monotonic() - 1)
            opener.assert_not_called()
        with self.assertRaises(client.ReviewError): client.timeout_handler(None, None)
        self.assertEqual(client.TIMEOUT, 90)

    def test_redirect_is_not_followed(self):
        self.assertIsNone(client.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere/'))

    def test_missing_secret_fails_before_submission(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(client, 'post') as post:
            with self.assertRaises(client.ReviewError): client.main()
            post.assert_not_called()

    def test_complete_submit_and_poll_flow_without_route(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / 'event.json'; event.write_text(json.dumps(EVENT))
            env = {**ENV, 'GITHUB_EVENT_PATH': str(event), 'OCR_CI_TOKEN': 'test-value',
                   'GITHUB_STEP_SUMMARY': str(Path(directory) / 'summary.md')}
            with patch.dict(os.environ, env, clear=True), patch.object(client, 'post', side_effect=[reply('accepted'), reply('queued', durable=True, status_published=True, task_id='task-1', commit_status='pending')]) as post, patch.object(client.time, 'sleep'):
                self.assertEqual(client.main(), 0)
                self.assertEqual([x.args[0] for x in post.call_args_list], ['ocr-ci-submit', 'ocr-ci-status'])
                for call in post.call_args_list:
                    self.assertNotIn('route', call.args[1])
                self.assertIn('separate ocr-review', (Path(directory) / 'summary.md').read_text())

    def test_gateway_401_is_not_retried(self):
        error = urllib.error.HTTPError('https://example.invalid', 401, 'No', {}, None)
        with patch.object(client.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(client.ReviewError): client.post('ocr-ci-submit', {}, 'test', client.time.monotonic() + 60)
            self.assertEqual(opener.return_value.open.call_count, 1)

    def test_gateway_503_has_bounded_retries(self):
        error = urllib.error.HTTPError('https://example.invalid', 503, 'Unavailable', {}, None)
        with patch.object(client.urllib.request, 'build_opener') as opener, patch.object(client.time, 'sleep'):
            opener.return_value.open.side_effect = error
            with self.assertRaises(client.ReviewError): client.post('ocr-ci-submit', {}, 'test', client.time.monotonic() + 60)
            self.assertEqual(opener.return_value.open.call_count, 4)

    def test_bad_json_is_not_success(self):
        with patch.object(client.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'<html>proxy error</html>'
            with self.assertRaises(client.ReviewError): client.post('ocr-ci-status', {}, 'test', client.time.monotonic() + 60)


if __name__ == '__main__': unittest.main()
