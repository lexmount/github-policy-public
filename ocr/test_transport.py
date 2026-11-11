# SPDX-License-Identifier: Apache-2.0
import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import ci_client as client


IDENTITY = {'repository': 'lexmount/example', 'pull_number': 12,
            'head_sha': 'a' * 40, 'base_sha': 'b' * 40, 'base_ref': 'main'}
RECEIPT = {'schema': client.SCHEMA, 'request_id': client.request_id(IDENTITY), **IDENTITY}
ACK = {**RECEIPT, 'state': 'accepted', 'contract_version': 2, 'durable': True,
       'acceptance': 'accepted', 'scheduler': 'backend', 'task_id': RECEIPT['request_id']}


class TransportTests(unittest.TestCase):
    @contextlib.contextmanager
    def gateway(self, responses):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                requests.append({'path': self.path, 'body': raw})
                behavior = responses[min(len(requests) - 1, len(responses) - 1)]
                if behavior == 'disconnect':
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if behavior == 'chunked_disconnect':
                    self.send_response(200)
                    self.send_header('Transfer-Encoding', 'chunked')
                    self.end_headers()
                    self.wfile.write(b'40\r\n{"durable":')
                    self.wfile.flush()
                    self.close_connection = True
                    return
                body = json.dumps(ACK).encode()
                status = 200
                length = str(len(body))
                if behavior == 'truncated':
                    body = body[:20]
                elif behavior == 'short_valid_json':
                    length = str(len(body) + 20)
                elif behavior == 'invalid_json':
                    body = b'{"durable":'
                    length = str(len(body))
                elif behavior == 'invalid_utf8':
                    body = b'\xff'
                    length = '1'
                elif behavior == 'oversized':
                    body = b' ' * (client.MAX_RESPONSE_BYTES + 1)
                    length = str(len(body))
                elif behavior == 'oversized_without_length':
                    body = b' ' * (client.MAX_RESPONSE_BYTES + 1)
                    length = None
                elif behavior == 'invalid_length':
                    length = 'invalid'
                elif isinstance(behavior, int):
                    status = behavior
                self.send_response(status)
                if length is not None:
                    self.send_header('Content-Length', length)
                self.end_headers()
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except ConnectionError:
                    pass
                self.close_connection = True

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        worker.start()
        try:
            with patch.object(client, 'BASE_URL', f'http://127.0.0.1:{server.server_port}/'):
                yield requests
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def send(self):
        return client.post('ocr-ci-submit', RECEIPT, 'secret-not-for-logs', time.monotonic() + 30)

    def records(self, output):
        return [json.loads(line) for line in output.getvalue().splitlines()
                if line.startswith('{')]

    def test_lost_ack_and_incomplete_bodies_retry_the_same_submission(self):
        cases = {'disconnect': 'RemoteDisconnected', 'truncated': 'IncompleteRead',
                 'short_valid_json': 'IncompleteRead',
                 'chunked_disconnect': 'IncompleteRead', 'invalid_json': 'JSONDecodeError',
                 'invalid_utf8': 'UnicodeDecodeError'}
        for behavior, error in cases.items():
            with self.subTest(behavior=behavior), self.gateway([behavior, 'ok']) as requests:
                output = io.StringIO()
                with contextlib.redirect_stdout(output), patch.object(client.time, 'sleep') as sleep:
                    self.assertEqual(self.send(), ACK)
                self.assertEqual(len(requests), 2)
                self.assertEqual(requests[0], requests[1])
                self.assertEqual(requests[0]['path'], '/ocr-ci-submit')
                records = self.records(output)
                self.assertEqual([r['attempt'] for r in records], [1, 2])
                self.assertEqual(records[0]['error'], error)
                self.assertIsNone(records[1]['error'])
                self.assertTrue(all(r['elapsed_ms'] >= 0 for r in records))
                self.assertTrue(all(r['request_id'] == RECEIPT['request_id'] for r in records))
                self.assertEqual(records[1]['http_status'], 200)
                self.assertEqual(sleep.call_count, 1)
                self.assertNotIn('secret-not-for-logs', output.getvalue())
                self.assertNotIn('durable', output.getvalue())

    def test_repeated_disconnects_have_four_attempts_and_bounded_backoff(self):
        with self.gateway(['disconnect']) as requests, contextlib.redirect_stdout(io.StringIO()) as output:
            with patch.object(client.time, 'sleep') as sleep, patch.object(client.random, 'uniform', side_effect=lambda low, high: high):
                with self.assertRaises(client.ReviewError):
                    self.send()
            self.assertEqual(len(requests), 4)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])
            self.assertIsNone(self.records(output)[-1]['retry_delay_seconds'])

    def test_transient_http_statuses_retry_and_permanent_statuses_stop(self):
        for status in [408, 429, 500, 502, 503, 504, 401, 403, 404, 409, 302]:
            retryable = status in [408, 429, 500, 502, 503, 504]
            with self.subTest(status=status), self.gateway([status, 'ok']) as requests:
                with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(client.time, 'sleep'):
                    if retryable:
                        self.assertEqual(self.send(), ACK)
                    else:
                        with self.assertRaises(client.ReviewError):
                            self.send()
                self.assertEqual(len(requests), 2 if retryable else 1)
                self.assertEqual(self.records(output)[0]['http_status'], status)
                self.assertEqual(self.records(output)[0]['error'], 'HTTPError')

    def test_invalid_length_and_oversized_responses_stop_without_retry(self):
        for behavior in ['invalid_length', 'oversized', 'oversized_without_length']:
            with self.subTest(behavior=behavior), self.gateway([behavior]) as requests:
                with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(client.time, 'sleep') as sleep:
                    with self.assertRaises(client.ReviewError):
                        self.send()
                self.assertEqual(len(requests), 1)
                sleep.assert_not_called()
                self.assertEqual(self.records(output)[0]['error'], 'ReviewError')

    def test_socket_and_tls_disconnects_are_retried(self):
        for error in [ConnectionResetError('connection reset'), BrokenPipeError('broken pipe'),
                      TimeoutError('timed out'), ssl.SSLEOFError('TLS EOF'),
                      http.client.IncompleteRead(b'partial'), http.client.BadStatusLine('HTTP/')]:
            with self.subTest(error=type(error).__name__), patch.object(client.urllib.request, 'build_opener') as opener:
                response = opener.return_value.open.return_value.__enter__.return_value
                response.status = 200
                response.headers = {}
                response.read.side_effect = [error, json.dumps(ACK).encode()]
                with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(client.time, 'sleep'):
                    self.assertEqual(self.send(), ACK)
                self.assertEqual(opener.return_value.open.call_count, 2)
                self.assertEqual(self.records(output)[0]['error'], type(error).__name__)

    def test_retry_sleep_and_next_attempt_obey_the_deadline(self):
        with self.gateway(['disconnect']) as requests, contextlib.redirect_stdout(io.StringIO()):
            with patch.object(client.time, 'monotonic', side_effect=[10, 10.5, 10.5, 11]), patch.object(client.time, 'sleep') as sleep:
                with self.assertRaises(client.ReviewError):
                    client.post('ocr-ci-submit', RECEIPT, 'secret', 11)
            self.assertEqual(len(requests), 1)
            self.assertEqual(sleep.call_args.args[0], 0.5)

    def test_hard_timeout_is_logged_and_never_retried(self):
        with patch.object(client.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = client.ReviewError('OCR acceptance exceeded 90 seconds')
            with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(client.time, 'sleep') as sleep:
                with self.assertRaises(client.ReviewError):
                    self.send()
            sleep.assert_not_called()
            self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertEqual(self.records(output)[0]['error'], 'ReviewError')

    def test_main_exits_after_durable_ack_without_a_status_query(self):
        event = {'pull_request': {'number': IDENTITY['pull_number'], 'state': 'open',
                 'head': {'sha': IDENTITY['head_sha']}, 'base': {'sha': IDENTITY['base_sha'],
                 'ref': IDENTITY['base_ref'], 'repo': {'full_name': IDENTITY['repository']}}}}
        with tempfile.TemporaryDirectory() as directory, self.gateway(['disconnect', 'ok']) as requests:
            event_file = Path(directory) / 'event.json'
            event_file.write_text(json.dumps(event))
            env = {'GITHUB_EVENT_NAME': 'pull_request_target', 'GITHUB_REPOSITORY': IDENTITY['repository'],
                   'GITHUB_EVENT_PATH': str(event_file), 'OCR_CI_TOKEN': 'secret-not-for-logs',
                   'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
            with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()), patch.object(client.time, 'sleep'):
                self.assertEqual(client.main(), 0)
            self.assertEqual([r['path'] for r in requests], ['/ocr-ci-submit', '/ocr-ci-submit'])
            payloads = [json.loads(r['body']) for r in requests]
            self.assertEqual(payloads[0], payloads[1])
            self.assertEqual(payloads[0]['retry_key'], '123:2')


if __name__ == '__main__':
    unittest.main()
