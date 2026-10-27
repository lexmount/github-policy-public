# SPDX-License-Identifier: Apache-2.0
"""Trusted workflow client; never check out or execute pull request code."""
import hashlib
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request

BASE_URL = 'https://n8n.cross.lexmount.cn/webhook/'
SCHEMA = 'ocr-ci/v1'
WAIT_STATES = {'accepted', 'dispatching', 'queued', 'running', 'retrying'}
FAIL_STATES = {'failed', 'cancelled', 'superseded'}
TIMEOUT = 90


class ReviewError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def coordinates(event, env):
    if env.get('GITHUB_EVENT_NAME') != 'pull_request_target':
        raise ReviewError('Only pull_request_target is supported; merge queues are not supported.')
    repo = env.get('GITHUB_REPOSITORY', '')
    if not re.fullmatch(r'lexmount/[A-Za-z0-9_.-]+', repo, re.I):
        raise ReviewError('This workflow is for the lexmount organization.')
    pull = event.get('pull_request', {})
    number = pull.get('number')
    if type(number) is not int or number <= 0 or pull.get('state') != 'open':
        raise ReviewError('An open pull request is required.')
    base = pull.get('base', {})
    if base.get('repo', {}).get('full_name', '').lower() != repo.lower():
        raise ReviewError('Pull request base repository does not match the workflow repository.')
    head_sha, base_sha = pull.get('head', {}).get('sha', ''), base.get('sha', '')
    if not all(re.fullmatch('[0-9a-f]{40}', s) for s in [head_sha, base_sha]):
        raise ReviewError('Missing immutable PR head/base SHA.')
    if not isinstance(base.get('ref'), str) or not base['ref']:
        raise ReviewError('Missing PR base branch.')
    return {'repository': repo.lower(), 'pull_number': number, 'head_sha': head_sha,
            'base_sha': base_sha, 'base_ref': base['ref']}


def request_id(identity):
    payload = json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()
    return 'ocr-ci-' + hashlib.sha256(payload).hexdigest()


def check_identity(reply, identity):
    if not isinstance(reply, dict) or reply.get('schema') != SCHEMA:
        raise ReviewError('Unexpected OCR response schema.')
    for key, value in identity.items():
        if type(reply.get(key)) is not type(value) or reply[key] != value:
            raise ReviewError('OCR result does not match this PR snapshot: ' + key)
    if reply.get('request_id') != request_id(identity):
        raise ReviewError('OCR receipt does not match the submitted request.')


def verdict(reply):
    state = reply.get('state')
    if reply.get('contract_version') == 2:
        if state not in WAIT_STATES | FAIL_STATES | {'complete', 'skipped'}:
            raise ReviewError('Unknown OCR task state.')
        if reply.get('acceptance') != 'accepted' or reply.get('scheduler') != 'backend' or reply.get('durable') is not True:
            raise ReviewError('Backend did not accept responsibility for this task.')
        if not isinstance(reply.get('task_id'), str) or not reply['task_id'].strip():
            raise ReviewError('Missing durable OCR task ID.')
        return 0
    if state in FAIL_STATES:
        raise ReviewError('OCR task failed: ' + state)
    if state not in WAIT_STATES | {'complete', 'skipped'}:
        raise ReviewError('Unknown OCR task state.')
    if reply.get('durable') is not True or reply.get('status_published') is not True:
        return None
    task_id = reply.get('task_id')
    if not isinstance(task_id, str) or not task_id.strip():
        raise ReviewError('Missing durable OCR task ID.')
    expected = {'success', 'failure'} if state in {'complete', 'skipped'} else {'pending'}
    if reply.get('commit_status') not in expected:
        raise ReviewError('Missing authoritative OCR commit status.')
    # This gate confirms dispatch only. Rulesets may separately require ocr-review to enforce the verdict.
    return 0


def timeout_handler(signum, frame):
    raise ReviewError('OCR acceptance exceeded 90 seconds; re-run to reuse the task.')


def post(path, payload, token, deadline):
    data = json.dumps(payload).encode()
    for attempt in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReviewError('Timed out waiting for OCR. Re-run this workflow to reuse the task.')
        req = urllib.request.Request(BASE_URL + path, data=data, headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=min(15, remaining)) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ReviewError('OCR response exceeds the allowed size.')
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ReviewError('OCR returned an invalid JSON response.') from None
        except urllib.error.HTTPError as error:
            if error.code not in [408, 429, 500, 502, 503, 504]:
                raise ReviewError(f'OCR gateway HTTP {error.code}; check configuration and credentials.') from None
        except (urllib.error.URLError, TimeoutError):
            pass
        if attempt < 3:
            time.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
    raise ReviewError('OCR gateway remains unavailable after bounded retries; re-run this workflow.')


def summary(identity, reply):
    text = (f"## OCR dispatch accepted\n\nRepository: `{identity['repository']}`; PR #{identity['pull_number']}\n\n"
            f"Head: `{identity['head_sha']}`\n\n"
            'Task persisted. The backend owns dispatch, recovery and result publication.\n'
            'The separate ocr-review status reports the review verdict; enforcement is configured in Rulesets.\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
            output.write(text)


def main():
    token = os.environ.get('OCR_CI_TOKEN', '').strip()
    if not token:
        raise ReviewError('Missing organization secret OCR_CI_TOKEN or repository access to it.')
    with open(os.environ['GITHUB_EVENT_PATH']) as source:
        identity = coordinates(json.load(source), os.environ)
    receipt = {'schema': SCHEMA, 'request_id': request_id(identity), **identity}
    payload = {**receipt, 'source': {'run_id': os.environ.get('GITHUB_RUN_ID'),
                                   'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
                                   'workflow_ref': os.environ.get('GITHUB_WORKFLOW_REF')}}
    run_id, run_attempt = os.environ.get('GITHUB_RUN_ID', ''), os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if run_id.isdecimal() and run_attempt.isdecimal() and int(run_attempt) > 1:
        payload['retry_key'] = run_id + ':' + run_attempt
    deadline = time.monotonic() + TIMEOUT
    reply = post('ocr-ci-submit', payload, token, deadline)
    last_state = None
    while True:
        check_identity(reply, identity)
        result = verdict(reply)
        state = reply['state']
        if state != last_state:
            print('OCR state: ' + state, flush=True)
            last_state = state
        if result is not None:
            summary(identity, reply)
            print('OCR dispatch accepted; the separate ocr-review status reports the review verdict.')
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReviewError('OCR acceptance exceeded 90 seconds. Re-run to reuse the task.')
        time.sleep(min(5, remaining))
        reply = post('ocr-ci-status', receipt, token, deadline)


if __name__ == '__main__':
    try:
        # Ubuntu runner: enforce total wall time even on trickling HTTP bodies.
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, TIMEOUT)
        try:
            result = main()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        sys.exit(result)
    except ReviewError as error:
        print('::error::' + str(error), file=sys.stderr)
        sys.exit(1)
