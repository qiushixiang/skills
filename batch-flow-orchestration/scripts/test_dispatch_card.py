#!/usr/bin/env python3
"""Dispatch card lifecycle on a temporary ledger (no model calls)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

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
        self.build(lambda p: None)

    def build(self, mutate):
        if self.store.exists():
            self.store.unlink()
        proof = self.task / '.batch-flow' / 'proof.txt'
        p = plan()
        p['roles']['executor'] = dict(tool='claude-agent', model='sonnet', effort='default')
        p['roles']['reviewer'] = dict(tool='claude-agent', model='opus', effort='default')
        p['roles']['third_party'] = dict(tool='agy', model='gemini-3.8-flash', effort='default')
        mutate(p)
        s = None
        for i, (actor, kind, data) in enumerate([
                ('architect', 'init', {'plan': p, 'simulation': False, 'task_mode': 'local',
                                       'context': {'conversation_id': 'planner', 'mode': 'fresh', 'reason': 'fixture'}}),
                ('orchestrator', 'authorize', {'batches': ['B'], 'evidence': [str(proof)]})]):
            s, _ = flow.apply_request(s, {'request_id': str(i), 'expected_revision': s['revision'] if s else 0, 'actor': actor,
                                          'operations': [{'type': kind, 'data': data}]}, self.store.parent)
        self.store.write_text(json.dumps(s))
        self.runtime = self.task / '.batch-flow' / '.claude-launch' / ('a' * 32); self.runtime.mkdir(parents=True, exist_ok=True)
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

    def test_lead_dispatches_executor_with_nested_card(self):
        def delegate(p):
            p['tasks']['execute']['dispatch_owner'] = 'architect'
            p['roles']['architect'].update(tool='claude', model='claude-opus-5-5', effort='medium')
        self.build(delegate)
        outer = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime))
        d = self.store.parent / 'records' / 'dispatch' / outer['card']
        card = json.loads((d / 'card.json').read_text())
        self.assertEqual((card['role'], card['host_call']['input']['subagent_type'], card['ledger_start']), ('architect', 'bf-architect', None))
        self.assertEqual(card['host_call']['input']['effort'], 'medium')
        self.assertIn('--as-lead', (d / 'prompt.md').read_text())
        inner = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime), '--as-lead')
        cid = inner['card']; d = self.store.parent / 'records' / 'dispatch' / cid
        card = json.loads((d / 'card.json').read_text())
        call = card['host_call']['input']
        self.assertTrue(cid.endswith('-lead') and card['lead'])
        self.assertEqual((card['role'], card['actor'], call['subagent_type'], call['run_in_background']), ('executor', 'architect', 'bf-executor', False))
        self.assertNotIn('register', ' '.join(inner['steps']).replace('register_dispatch', ''))
        attempt = self.card('start', '--store', str(self.store), '--card', cid, '--revision', self.rev())['attempt']
        s = json.loads(self.store.read_text())
        self.assertEqual(next(a for a in s['state']['attempts'] if a['id'] == attempt)['dispatch_owner'], 'architect')
        self.assertEqual(flow.pre_yield(s)['dispatch_problems'], [])  # lead-held attempt, no orchestrator timer
        (d / 'receipt.txt').write_text('done. agentId: agent-9'); (d / 'result.md').write_text('结果：VERIFIED\n')
        self.assertEqual(self.card('finish', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--conversation', 'agent-9')['result'], 'VERIFIED')
        self.build(lambda p: None)
        err = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime), '--as-lead', ok=False)
        self.assertIn('delegate_to_lead', err['error'])

    def confirmation_fixture(self, delegate):
        def mutate(p):
            p['roles']['architect'].update(tool='claude', model='claude-opus-5-5', effort='default')
            p['decision_points']['contract']['blocks'] = ['execute']
            if delegate:
                p['tasks']['execute']['dispatch_owner'] = 'architect'
        self.build(mutate)
        s = json.loads(self.store.read_text()); proof = str(self.store.parent / 'proof.txt')
        for i, (actor, kind, data) in enumerate([
                ('executor', 'signal', dict(id='D1', signal='contract_change', reason='r', blocks=['execute'], decision_point='contract', evidence=[proof])),
                ('architect', 'resolve', dict(id='D1', summary='s', evidence=[proof], user_evidence=[], requirements=[
                    dict(id='c1', kind='confirmation', owner='executor', depends_on=[], instruction='entry fixed')]))]):
            s, _ = flow.apply_request(s, {'request_id': 'c%d' % i, 'expected_revision': s['revision'], 'actor': actor,
                                          'operations': [{'type': kind, 'data': data}]}, self.store.parent)
        self.store.write_text(json.dumps(s))

    def test_waiting_confirmation_is_not_redispatched(self):
        self.confirmation_fixture(delegate=True)
        mk = ['make', '--store', str(self.store), '--kind', 'CONFIRM_CONDITION', '--target', 'c1', '--runtime', str(self.runtime)]
        self.assertIn('不是台账正式任务', self.card(*mk, '--as-lead', ok=False)['error'])
        out = self.card(*mk); d = self.store.parent / 'records' / 'dispatch' / out['card']
        card = json.loads((d / 'card.json').read_text())
        self.assertTrue(card['host_call']['input']['run_in_background'])  # orchestrator cards stay background
        prompt = (d / 'prompt.md').read_text()
        self.assertIn('不派发执行者', prompt); self.assertNotIn('--as-lead', prompt)
        (d / 'receipt.txt').write_text('agentId: lead-1'); (d / 'result.md').write_text('结果：WAIT_EVIDENCE\n')
        self.card('finish', '--store', str(self.store), '--card', out['card'], '--revision', self.rev(), '--conversation', 'lead-1')
        self.assertIn('--recheck', self.card(*mk, ok=False)['error'])
        s = json.loads(self.store.read_text())
        gate = flow.pre_yield(s, 'orchestrator', None, self.store.parent)
        self.assertNotIn('c1', [a['target'] for a in gate['actions']])
        self.assertIn('c1', [a['target'] for a in gate['user_actions']])
        self.assertIn('c1', [a['target'] for a in flow.pre_yield(s)['actions']])
        self.card(*mk, '--recheck')

    def test_confirmation_waits_while_owned_work_runs(self):
        self.build(lambda p: p['roles']['architect'].update(tool='claude', model='opus', effort='default'))
        s = json.loads(self.store.read_text()); proof = str(self.store.parent / 'proof.txt')
        for i, (actor, kind, data) in enumerate([
                ('executor', 'signal', dict(id='D1', signal='contract_change', reason='r', blocks=['done'], decision_point='contract', evidence=[proof])),
                ('architect', 'resolve', dict(id='D1', summary='s', evidence=[proof], user_evidence=[], requirements=[
                    dict(id='c1', kind='confirmation', owner='architect', depends_on=[], instruction='check')]))]):
            s, _ = flow.apply_request(s, {'request_id': 'w%d' % i, 'expected_revision': s['revision'], 'actor': actor,
                                          'operations': [{'type': kind, 'data': data}]}, self.store.parent)
        self.assertIn('c1', [a['target'] for a in flow.pre_yield(s)['actions']])
        s, _ = flow.apply_request(s, {'request_id': 'w9', 'expected_revision': s['revision'], 'actor': 'orchestrator', 'operations': [
            {'type': 'start', 'data': dict(task='execute', call=dict(tool='claude-agent', model='sonnet', effort='default', conversation_id='x'))}]},
            self.store.parent)
        self.assertNotIn('c1', [a['target'] for a in flow.pre_yield(s)['actions']])

    def test_resume_only_same_backend_and_same_session(self):
        same = flow.same_backend
        self.assertTrue(same(dict(tool='agy'), 'agy-cli')); self.assertFalse(same(dict(tool='claude'), 'agy'))
        self.assertTrue(same(dict(tool='claude'), 'claude-agent')); self.assertFalse(same(dict(tool='claude-agent'), None))
        self.assertTrue(same(dict(tool='codex'), None))
        import dispatch_card as dc
        home = Path(self.tmp.name) / 'home'
        spec = dict(session_id='s1', workspace='/w/x')
        with unittest.mock.patch.object(Path, 'home', return_value=home):
            self.assertFalse(dc.agent_in_session(spec, 'a1'))
            agent = home / '.claude' / 'projects' / '-w-x' / 's1' / 'subagents' / 'agent-a1.jsonl'
            agent.parent.mkdir(parents=True); agent.write_text('{}')
            self.assertTrue(dc.agent_in_session(spec, 'a1'))
        self.assertTrue(dc.agent_in_session(None, 'a1'))

    def test_unreachable_context_falls_back_fresh_with_reason(self):
        self.build(lambda p: p['tasks']['execute'].update(session_policy='resume'))
        s = json.loads(self.store.read_text()); proof = str(self.store.parent / 'proof.txt')
        call = dict(tool='claude-agent', model='sonnet', effort='default', conversation_id='agent-old')
        for i, (actor, kind, data) in enumerate([('orchestrator', 'start', dict(task='execute', call=call)),
                                                 ('executor', 'finish', dict(attempt='A0001', result='FAILED', evidence=[proof]))]):
            s, _ = flow.apply_request(s, {'request_id': 'u%d' % i, 'expected_revision': s['revision'], 'actor': actor,
                                          'operations': [{'type': kind, 'data': data}]}, self.store.parent)
        self.store.write_text(json.dumps(s))
        spec = json.loads((self.runtime / 'spec.json').read_text()); spec.update(session_id='nosuch', workspace=str(self.workspace))
        (self.runtime / 'spec.json').write_text(json.dumps(spec))
        out = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime))
        card = json.loads((self.store.parent / 'records' / 'dispatch' / out['card'] / 'card.json').read_text())
        self.assertEqual((card['route'], card['resume']), ('agent', None)); self.assertIn('agent-old', card['replacement_reason'])
        home = Path(self.tmp.name) / 'home'
        agent = home / '.claude' / 'projects' / __import__('re').sub(r'[^A-Za-z0-9]', '-', str(self.workspace)) / 'nosuch' / 'subagents' / 'agent-agent-old.jsonl'
        agent.parent.mkdir(parents=True)
        for size, route in [(1000, 'send_message'), (400 * 1024, 'agent')]:
            agent.write_text('x' * size)
            proc = subprocess.run([sys.executable, '-c', 'import sys,runpy; from pathlib import Path; Path.home=lambda: Path(%r); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")' % str(home),
                                   str(CARD), 'make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime)],
                                  capture_output=True, text=True)
            self.assertEqual(json.loads(proc.stdout).get('route'), route, proc.stdout)
        self.card('start', '--store', str(self.store), '--card', out['card'], '--revision', self.rev())

    def test_abandon_lost_agent_records_invalid_and_offers_retry(self):
        out = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime))
        cid = out['card']; d = self.store.parent / 'records' / 'dispatch' / cid
        self.card('start', '--store', str(self.store), '--card', cid, '--revision', self.rev())
        (d / 'receipt.txt').write_text('agentId: agent-gone')
        st = json.loads((d / 'state.json').read_text()); st['handle'] = 'agent-gone'; (d / 'state.json').write_text(json.dumps(st))
        spec = json.loads((self.runtime / 'spec.json').read_text()); spec.update(session_id='new', workspace=str(self.workspace))
        (self.runtime / 'spec.json').write_text(json.dumps(spec))
        ab = ['abandon', '--store', str(self.store), '--card', cid, '--reason', 'host session ended', '--runtime', str(self.runtime)]
        res = self.card(*ab, '--revision', self.rev())
        self.assertEqual(res['result'], 'INVALID')
        s = json.loads(self.store.read_text())
        self.assertEqual(s['state']['attempts'][-1]['result'], 'INVALID')
        action = next(a for a in flow.status(s)['actions'] if a['target'] == 'execute')
        self.assertEqual(action.get('retry_of'), s['state']['attempts'][-1]['id'])
        self.card(*ab, '--revision', self.rev(), ok=False)

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
        s['plan']['roles']['reviewer'].update(tool='claude', model='claude-opus-5-5', effort='medium'); self.store.write_text(json.dumps(s))
        review = self.card('make', '--store', str(self.store), '--kind', 'REVIEW', '--target', 'regular', '--runtime', str(self.runtime))
        call = json.loads((self.store.parent / 'records' / 'dispatch' / review['card'] / 'card.json').read_text())['host_call']
        self.assertEqual((call['tool'], call['input']['model'], call['input']['effort']), ('Agent', 'opus', 'medium'))
        s['plan']['roles']['reviewer']['effort'] = 'turbo'; self.store.write_text(json.dumps(s))
        err = self.card('make', '--store', str(self.store), '--kind', 'REVIEW', '--target', 'regular', '--runtime', str(self.runtime), ok=False)
        self.assertIn('effort', err['error'])
        s['plan']['roles']['reviewer'].update(effort='default', model='gpt-5'); self.store.write_text(json.dumps(s))
        err = self.card('make', '--store', str(self.store), '--kind', 'REVIEW', '--target', 'regular', '--runtime', str(self.runtime), ok=False)
        self.assertIn('模型', err['error'])


if __name__ == '__main__':
    unittest.main()
