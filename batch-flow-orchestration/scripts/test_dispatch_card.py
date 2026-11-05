#!/usr/bin/env python3
"""Dispatch card lifecycle on a temporary ledger (no model calls)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flow  # noqa: E402
from test_flow import plan  # noqa: E402

CARD = HERE / 'dispatch_card.py'


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.workspace = root / 'src'; self.workspace.mkdir()
        self.task = root / 'task'; (self.task / '.batch-flow').mkdir(parents=True)
        self.store = self.task / '.batch-flow' / 'flow.json'
        proof = self.task / '.batch-flow' / 'proof.txt'; proof.write_text('ok')
        p = plan()
        p['roles']['executor'] = dict(tool='claude-agent', model='sonnet', effort='default')
        p['roles']['reviewer'] = dict(tool='claude-agent', model='opus', effort='default')
        p['roles']['third_party'] = dict(tool='agy', model='gemini-3.8-flash', effort='default')
        s = None
        for i, (actor, kind, data) in enumerate([
                ('architect', 'init', {'plan': p, 'simulation': False, 'task_mode': 'local',
                                       'context': {'conversation_id': 'planner', 'mode': 'fresh', 'reason': 'fixture'}}),
                ('orchestrator', 'authorize', {'batches': ['B'], 'evidence': [str(proof)]})]):
            s, _ = flow.apply_request(s, {'request_id': str(i), 'expected_revision': s['revision'] if s else 0, 'actor': actor,
                                          'operations': [{'type': kind, 'data': data}]}, self.store.parent)
        self.store.write_text(json.dumps(s))
        self.runtime = self.task / '.batch-flow' / '.claude-launch' / ('a' * 32); self.runtime.mkdir(parents=True)
        profile = self.runtime / 'agy-profile.json'
        profile.write_text(json.dumps(dict(format='batch-flow-cli/v1', backend='agy', binary='/bin/agy', version='1.5.0')))
        (self.runtime / 'spec.json').write_text(json.dumps(dict(store=str(self.store), workspace=str(self.workspace),
                                                                  task_base=str(self.task), profiles=[str(profile)])))

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, *args, ok=True):
        proc = subprocess.run([sys.executable, str(CARD), *args], capture_output=True, text=True)
        out = json.loads(proc.stdout)
        self.assertEqual(proc.returncode == 0, ok, proc.stdout + proc.stderr)
        return out

    def rev(self):
        return str(json.loads(self.store.read_text())['revision'])

    def test_agent_card_full_cycle(self):
        out = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime))
        self.assertEqual(out['route'], 'agent')
        cid = out['card']; d = self.store.parent / 'records' / 'dispatch' / cid
        card = json.loads((d / 'card.json').read_text())
        call = card['host_call']['input']
        self.assertEqual((card['host_call']['tool'], call['subagent_type'], call['model']), ('Agent', 'bf-executor', 'sonnet'))
        self.assertIn(str(d / 'prompt.md'), call['prompt']); self.assertIn(card['prompt_sha256'], call['prompt'])
        prompt = (d / 'prompt.md').read_text()
        self.assertIn('test action', prompt); self.assertIn('结果：VERIFIED | WAIT_EVIDENCE', prompt)
        self.assertIn('references/roles/executor.md', prompt)
        started = self.card('start', '--store', str(self.store), '--card', cid, '--revision', self.rev())
        attempt = started['attempt']
        self.card('start', '--store', str(self.store), '--card', cid, '--revision', self.rev(), ok=False)
        self.card('register', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--handle', 'agent-1',
                  '--timer-handle', 'b1', '--timer-ticket', str(d / 'timer-1.ticket'), ok=False)
        (d / 'receipt.txt').write_text('Async agent launched. agentId: agent-1')
        timer = subprocess.Popen([sys.executable, str(HERE / 'progress_timer.py'), 'wait', '--ticket', str(d / 'timer-1.ticket'),
                                  '--task', 'execute', '--goal', 'g', '--seconds', '60'], stdout=subprocess.PIPE, text=True)
        self.addCleanup(timer.stdout.close); self.addCleanup(timer.wait); self.addCleanup(timer.kill)
        self.assertIn('TIMER_STARTED', timer.stdout.readline())
        self.card('register', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--handle', 'agent-1',
                  '--timer-handle', 'b1', '--timer-ticket', str(d / 'timer-1.ticket'))
        self.card('finish', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--conversation', 'agent-1', ok=False)
        (d / 'result.md').write_text('**结果：** RESULT_TYPO\n')
        bad = self.card('finish', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--conversation', 'agent-1', ok=False)
        self.assertIn('结果', bad['error'])
        (d / 'result.md').unlink(); (d / 'result.md').write_text('结果：VERIFIED\n改动与版本：无\n')
        self.card('finish', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--conversation', 'other', ok=False)
        done = self.card('finish', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--conversation', 'agent-1')
        self.assertEqual(done['result'], 'VERIFIED')
        s = json.loads(self.store.read_text())
        a = next(x for x in s['state']['attempts'] if x['id'] == attempt)
        self.assertEqual((a['result'], a['call']['conversation_id']), ('VERIFIED', 'agent-1'))
        self.assertEqual(self.card('show', '--store', str(self.store), '--card', cid)['state']['result'], 'VERIFIED')

    def test_cli_card_and_errors(self):
        self.card('make', '--store', str(self.store), '--kind', 'THIRD_PARTY_REVIEW', '--target', 'third', '--runtime', str(self.runtime), ok=False)
        s = json.loads(self.store.read_text())
        proof = str(self.store.parent / 'proof.txt')
        for i, (actor, kind, data) in enumerate([
                ('orchestrator', 'start', {'task': 'execute', 'call': dict(tool='claude-agent', model='sonnet', effort='default', conversation_id='x')}),
                ('executor', 'finish', {'attempt': 'A0001', 'result': 'VERIFIED', 'evidence': [proof]})]):
            s, _ = flow.apply_request(s, {'request_id': 'f%d' % i, 'expected_revision': s['revision'], 'actor': actor,
                                          'operations': [{'type': kind, 'data': data}]}, self.store.parent)
        self.store.write_text(json.dumps(s))
        out = self.card('make', '--store', str(self.store), '--kind', 'THIRD_PARTY_REVIEW', '--target', 'third', '--runtime', str(self.runtime))
        card = json.loads((self.store.parent / 'records' / 'dispatch' / out['card'] / 'card.json').read_text())
        self.assertEqual(card['route'], 'cli')
        command = card['host_call']['input']['command']
        self.assertIn('agent_cli.py run', command); self.assertIn('--access read', command)
        self.assertIn('--add-dir', command)
        review = self.card('make', '--store', str(self.store), '--kind', 'REVIEW', '--target', 'regular', '--runtime', str(self.runtime))
        rcard = json.loads((self.store.parent / 'records' / 'dispatch' / review['card'] / 'card.json').read_text())
        self.assertEqual((rcard['host_call']['input']['subagent_type'], rcard['access']), ('bf-reviewer', 'read'))
        s['plan']['roles']['reviewer']['effort'] = 'high'; self.store.write_text(json.dumps(s))
        err = self.card('make', '--store', str(self.store), '--kind', 'REVIEW', '--target', 'regular', '--runtime', str(self.runtime), ok=False)
        self.assertIn('effort', err['error'])


if __name__ == '__main__':
    unittest.main()
