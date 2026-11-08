"""Check native Codex turn receipts instead of treating requested CLI flags as effective policy."""
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid


def policy_error(actual, expected):
    if actual.get('approval_policy') != expected.get('approval_policy'):
        return 'EXECUTION_POLICY_MISMATCH: actual approval_policy differs from request'
    if (actual.get('sandbox_policy') or {}).get('type') != expected.get('expected_sandbox'):
        return 'EXECUTION_POLICY_MISMATCH: actual sandbox differs from request'
    if expected.get('expected_reviewer') and actual.get('approvals_reviewer') != expected['expected_reviewer']:
        return 'EXECUTION_POLICY_MISMATCH: automatic approval reviewer is not active'
    return ''


def latest_context(path, since, cwd, model):
    latest = None
    for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            event = json.loads(line)
            if event.get('type') != 'turn_context':
                continue
            if dt.datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp() < since:
                continue
            data = event['payload']
            if data.get('cwd') != cwd or data.get('model') != model:
                continue
            latest = {k: data.get(k) for k in ('approval_policy', 'approvals_reviewer', 'sandbox_policy')}
            latest['timestamp'] = event['timestamp']
        except (ValueError, KeyError, TypeError):
            continue
    return latest


def stop_tree(proc):
    # The outer adapter's process group remains intact so stop --now still reaches every descendant.
    def children(pid):
        try:
            direct = [int(x) for x in Path('/proc/%d/task/%d/children' % (pid, pid)).read_text().split()]
        except OSError:
            return []
        return [child for p in direct for child in children(p)] + direct
    pids = children(proc.pid) + [proc.pid]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def communicate(proc, prompt, output, request, timeout=30, sessions=None):
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
    finally:
        proc.stdin = None
    sessions = Path(sessions or (Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'sessions'))
    since = dt.datetime.fromisoformat(request['started_at'].replace('Z', '+00:00')).timestamp()
    deadline, cid, path, actual = time.monotonic() + timeout, request.get('conversation'), None, None
    problem = ''
    while True:
        raw = (output / 'stdout').read_text(encoding='utf-8', errors='replace')
        tool_started = False
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'thread.started':
                cid = event.get('thread_id')
            if (event.get('item') or {}).get('type') in ('command_execution', 'file_change'):
                tool_started = True
        if cid and path is None:
            try:
                if str(uuid.UUID(cid)) != cid:
                    raise ValueError('not canonical')
                path = next(sessions.rglob('*%s*.jsonl' % cid), None)
            except (ValueError, TypeError):
                problem = 'EXECUTION_POLICY_UNVERIFIED: invalid native conversation ID'
                break
        if path:
            actual = latest_context(path, since, request['cwd'], request['model'])
        if actual:
            problem = policy_error(actual, request['execution'])
            break
        if tool_started or proc.poll() is not None or time.monotonic() >= deadline:
            problem = 'EXECUTION_POLICY_UNVERIFIED: native turn_context receipt missing for this invocation'
            break
        time.sleep(0.05)
    receipt = {'verified': not problem, 'problem': problem, 'conversation_id': cid,
               'source': str(path) if path else None, 'actual': actual}
    (output / 'execution-actual.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    if problem:
        stop_tree(proc)
    proc.communicate()


def verify_receipt(output, request):
    try:
        receipt = json.loads((Path(output) / 'execution-actual.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 'EXECUTION_POLICY_UNVERIFIED: actual launch receipt missing', None
    actual = receipt.get('actual')
    if receipt.get('verified') is not True or not isinstance(actual, dict):
        return receipt.get('problem') or 'EXECUTION_POLICY_UNVERIFIED', actual
    try:
        if dt.datetime.fromisoformat(actual['timestamp'].replace('Z', '+00:00')) < dt.datetime.fromisoformat(request['started_at'].replace('Z', '+00:00')):
            return 'EXECUTION_POLICY_UNVERIFIED: receipt belongs to an earlier invocation', actual
    except (KeyError, ValueError):
        return 'EXECUTION_POLICY_UNVERIFIED: invalid receipt timestamp', actual
    return policy_error(actual, request['execution']), actual
