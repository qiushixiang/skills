"""Actual permission receipts and controlled-entry routing; fake CLIs only, no model calls."""
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_cli
import codex_execution as execution
from bf import api, evidence, gate
from test_bf_core import Base, NEEDS_DECISION, ENTRY, cmd

CID = '01a1a540-b6eb-7ab0-aa1c-92f0cb428e69'


class PolicyReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.expected = {'approval_policy': 'on-request', 'expected_sandbox': 'workspace-write', 'expected_reviewer': 'auto_review'}
        self.actual = {'approval_policy': 'on-request', 'approvals_reviewer': 'auto_review',
                       'sandbox_policy': {'type': 'workspace-write'}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_auto_review_flag_on_fresh_and_resume_without_full_access(self):
        for session in (None, CID):
            args = argparse.Namespace(cwd=str(self.root), model='gpt-6.1-terra', effort='medium', access='write',
                                      conversation=session, settings=None, add_dir=[], approval_policy='on-request')
            profile = {'binary': '/bin/codex', 'backend': 'codex', 'help': '--approve-for-me'}
            argv, _ = agent_cli.build_command(profile, args, 'test')
            self.assertIn('--approve-for-me', argv)
            self.assertIn('sandbox_mode="workspace-write"', argv)
            self.assertNotIn('-s', argv)  # Native CLI rejects this flag combination; config retains the same explicit boundary.
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', argv)
            if session:
                self.assertLess(argv.index('--approve-for-me'), argv.index('resume'))
            args.access = 'read'
            argv, _ = agent_cli.build_command(profile, args, 'test')
            self.assertNotIn('--approve-for-me', argv)  # This flag selects workspace-write; never widen read access.
            self.assertIn('read-only', argv)
            args.access = 'write'; profile['help'] = 'old interface'
            with self.assertRaisesRegex(ValueError, 'probe'):
                agent_cli.build_command(profile, args, 'test')

    def test_mismatch_in_any_effective_permission_is_rejected(self):
        self.assertEqual(execution.policy_error(self.actual, self.expected), '')
        for key, value in [('approval_policy', 'never'), ('approvals_reviewer', 'user'), ('sandbox_policy', {'type': 'danger-full-access'})]:
            actual = dict(self.actual, **{key: value})
            self.assertIn('MISMATCH', execution.policy_error(actual, self.expected))

    def invoke(self, actual, fresh=True, expected_model='gpt-6.1-terra'):
        output = self.root / ('call-%s' % len(list(self.root.glob('call-*')))); output.mkdir()
        sessions = output / 'sessions'; sessions.mkdir()
        timestamp = dt.datetime.now(dt.timezone.utc)
        data = dict(actual, cwd=str(self.root), model=expected_model)
        event = {'type': 'turn_context', 'timestamp': (timestamp if fresh else timestamp - dt.timedelta(days=1)).isoformat(), 'payload': data}
        session = sessions / ('rollout-' + CID + '.jsonl')
        session.write_text(json.dumps(event))
        request = {'execution': self.expected, 'started_at': timestamp.isoformat(),
                   'cwd': str(self.root), 'model': 'gpt-6.1-terra', 'conversation': CID}
        stdout = (output / 'stdout').open('w')
        # Long-lived fake child makes it observable that a bad policy is terminated.
        process = subprocess.Popen([sys.executable, '-c',
            'import sys,time;sys.stdin.read();print(\'{"type":"thread.started","thread_id":"%s"}\',flush=True);time.sleep(0.4)' % CID],
            stdin=subprocess.PIPE, stdout=stdout, stderr=subprocess.DEVNULL, text=True)
        try:
            execution.communicate(process, 'test', output, request, timeout=0.2, sessions=sessions)
        finally:
            stdout.close()
            if process.poll() is None: process.kill(); process.wait()
        return output, request, process.returncode

    def test_current_actual_receipt_is_saved_for_collect(self):
        output, request, code = self.invoke(self.actual)
        self.assertEqual(code, 0)
        error, actual = execution.verify_receipt(output, request)
        self.assertEqual(error, '')
        self.assertEqual(actual['approvals_reviewer'], 'auto_review')

    def test_wrong_policy_stops_the_call_and_collect_cannot_accept_it(self):
        output, request, code = self.invoke(dict(self.actual, approval_policy='never'))
        self.assertNotEqual(code, 0)
        self.assertIn('MISMATCH', execution.verify_receipt(output, request)[0])

    def test_stale_resume_receipt_and_wrong_model_are_not_proof_of_this_launch(self):
        for kwargs in ({'fresh': False}, {'expected_model': 'another-model'}):
            output, request, code = self.invoke(self.actual, **kwargs)
            self.assertNotEqual(code, 0)
            self.assertIn('UNVERIFIED', execution.verify_receipt(output, request)[0])

    def test_collect_rejects_missing_receipt(self):
        self.assertIn('UNVERIFIED', execution.verify_receipt(self.root, {'execution': self.expected})[0])

    def test_cli_rejection_before_reading_prompt_is_saved_as_unusable(self):
        output = self.root / 'early'; output.mkdir()
        request = {'execution': self.expected, 'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                   'cwd': str(self.root), 'model': 'gpt-6.1-terra', 'conversation': None}
        with (output / 'stdout').open('w') as stdout:
            process = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(2)'], stdin=subprocess.PIPE,
                                       stdout=stdout, text=True)
            process.wait()
            execution.communicate(process, 'test', output, request, sessions=output)
        self.assertIn('UNVERIFIED', execution.verify_receipt(output, request)[0])


class ControlledEntry(Base):
    def test_external_record_refuses_runner_before_any_subprocess_is_started(self):
        with patch.object(evidence.subprocess, 'run') as launch:
            for argv in ([ENTRY, 'offline'], ['python3', 'bf/cli.py', 'record', '--', ENTRY, 'offline']):
                with self.assertRaisesRegex(api.BfError, '直接调用'):
                    evidence.record(self.base, argv)
            launch.assert_not_called()

    def test_wrapped_call_cannot_be_reported_as_environment_failure(self):
        oid = self.impl('D1'); rid = api.start(self.base, oid)
        result = api.deliver(self.base, oid, rid, NEEDS_DECISION,
                             events=[cmd('python3 bf/cli.py record -- ' + ENTRY + ' offline', 1)])
        self.assertEqual(result['outcome'], 'continue')
        self.assertIn('入口自行留证', result['reason'])
        corrected = api.deliver(self.base, oid, rid, NEEDS_DECISION, events=[cmd(ENTRY + ' offline', 1)])
        self.assertTrue(corrected['legal'])

    def test_reading_runner_is_allowed_but_wrappers_and_prefix_lookalikes_are_not_direct(self):
        for command in ['cat ' + ENTRY, 'rg keyword ' + ENTRY]:
            self.assertFalse(gate.controlled_wrapper(command, [ENTRY]))
        for command in ['timeout 20 ' + ENTRY + ' offline', 'bash ' + ENTRY + ' offline', ENTRY + ' offline && echo done']:
            self.assertTrue(gate.controlled_wrapper(command, [ENTRY]))
            self.assertFalse(gate.direct_entry(command, [ENTRY]))
        self.assertFalse(gate.direct_entry(ENTRY + '.other offline', [ENTRY]))
        self.assertTrue(gate.direct_entry("/bin/bash -lc '" + ENTRY + " offline'", [ENTRY]))
