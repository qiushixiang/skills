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
                                                                  task_base=str(self.task), profiles=[str(profile)],
                                                                  launch_id='L1', session_id='s1', started_at='2000-01-01T00:00:00+00:00')))
        self.home = Path(self.tmp.name) / 'home'

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, *args, ok=True):
        proc = subprocess.run([sys.executable, str(CARD), *args], capture_output=True, text=True)
        out = json.loads(proc.stdout)
        self.assertEqual(proc.returncode == 0, ok, proc.stdout + proc.stderr)
        return out

    def next(self):
        proc = subprocess.run([sys.executable, '-c', 'import sys,runpy; from pathlib import Path; Path.home=lambda: Path(%r); '
                               'sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")' % str(self.home),
                               str(CARD), 'next', '--store', str(self.store), '--runtime', str(self.runtime)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def agent(self, session, agent_id, card, report=None):
        d = self.home / '.claude' / 'projects' / __import__('re').sub(r'[^A-Za-z0-9]', '-', str(self.workspace)) / session / 'subagents'
        d.mkdir(parents=True, exist_ok=True)
        meta = dict(description=card, spawnDepth=1)
        lines = [dict(type='user', message=dict(role='user', content='read card'))]
        if report:
            meta['finished'] = True
            lines.append(dict(type='assistant', message=dict(content=[dict(type='tool_use', name='SubagentHandback', input=dict(message=report))])))
        (d / ('agent-%s.meta.json' % agent_id)).write_text(json.dumps(meta))
        (d / ('agent-%s.jsonl' % agent_id)).write_text(''.join(json.dumps(x) + '\n' for x in lines))

    def restart(self, session, launch):
        spec = json.loads((self.runtime / 'spec.json').read_text())
        spec.update(launch_id=launch, started_at=flow.now())
        if session:
            spec['session_id'] = session
        (self.runtime / 'spec.json').write_text(json.dumps(spec))

    def attempt(self, task):
        return [a for a in json.loads(self.store.read_text())['state']['attempts'] if a['task'] == task][-1]

    def test_next_drives_the_whole_cycle(self):
        out = self.next()
        self.assertEqual((out['do'], out['tool']), ('call', 'Agent'))
        cid = out['card']; call = out['input']
        self.assertEqual((call['description'], call['subagent_type'], call['run_in_background']), (cid, 'bf-executor', True))
        self.assertIsNone(self.attempt('execute').get('result'))
        again = self.next()  # call not made yet: same card again, nothing new
        self.assertEqual((again['do'], again['card']), ('call', cid))
        self.assertEqual(len(list((self.store.parent / 'records' / 'dispatch').iterdir())), 1)
        self.agent('s1', 'ag1', cid)
        self.assertEqual(self.next()['do'], 'wait')
        self.assertEqual(self.attempt('execute')['dispatch']['handle'], 'ag1')
        self.agent('s1', 'ag1', cid, report='结果：VERIFIED\n改动与版本：无\n')
        out = self.next()
        self.assertEqual(out['collected'][0]['result'], 'VERIFIED')
        self.assertEqual((self.attempt('execute')['result'], self.attempt('execute')['call']['conversation_id']), ('VERIFIED', 'ag1'))
        d = self.store.parent / 'records' / 'dispatch' / cid
        self.assertTrue((d / 'result.md').read_text().startswith('结果：VERIFIED'))
        self.assertIn('ag1', (d / 'receipt.txt').read_text())
        self.assertEqual(out['do'], 'call'); self.assertNotEqual(out['card'], cid)  # moves on to the next action

    def test_restart_collects_finished_and_retries_lost_work(self):
        cid = self.next()['card']; self.agent('s1', 'ag1', cid)
        self.assertEqual(self.next()['do'], 'wait')
        self.restart('s2', 'L2')  # orchestrator restarted: the running subagent died with s1
        out = self.next()
        self.assertEqual(out['interrupted'], [dict(card=cid, result='INVALID')])
        first = [a for a in json.loads(self.store.read_text())['state']['attempts'] if a['task'] == 'execute'][0]
        self.assertEqual(first['result'], 'INVALID')
        self.assertTrue((self.store.parent / 'records' / 'dispatch' / cid / 'lost.json').is_file())
        self.assertEqual(out['do'], 'call')
        retry = self.attempt('execute'); self.assertIsNone(retry.get('result'))
        self.assertTrue(retry.get('retry_of'))
        # The retry finished in s2 but the orchestrator restarted before collecting: the report is not lost.
        self.agent('s2', 'ag2', out['card'], report='结果：VERIFIED\n')
        self.restart('s3', 'L3')
        self.assertEqual(self.next()['collected'][0]['result'], 'VERIFIED')
        self.assertEqual(self.attempt('execute')['result'], 'VERIFIED')

    def test_agy_host_uses_the_same_next(self):
        self.build(lambda p: p['roles']['executor'].update(tool='agy', model='gemini-3.8-flash', effort='default'))
        agy = self.task / '.batch-flow' / '.agy-launch' / ('b' * 32); agy.mkdir(parents=True)
        spec = json.loads((self.runtime / 'spec.json').read_text())
        for k in ['session_id']: spec.pop(k)
        profile = agy / 'agy-profile.json'; profile.write_text((self.runtime / 'agy-profile.json').read_text())
        spec.update(profiles=[str(profile)], launch_id='A1'); (agy / 'spec.json').write_text(json.dumps(spec))
        (agy / 'owner.json').write_text(json.dumps(dict(conversation_id='agy-main')))
        self.runtime = agy
        out = self.next()
        self.assertEqual((out['do'], out['tool']), ('call', 'run_command'))
        self.assertIn('agent_cli.py run', out['input']['CommandLine'])
        cid = out['card']; run = self.store.parent / 'records' / 'dispatch' / cid / 'run'
        self.assertEqual(self.next()['card'], cid)  # not started on the host yet: same call again
        run.mkdir(); (run / 'receipt.json').write_text(json.dumps(dict(pid=__import__('os').getpid())))
        self.assertEqual(self.next()['do'], 'wait')
        self.assertEqual(self.attempt('execute')['dispatch']['owner_conversation_id'], 'agy-main')
        dead = subprocess.Popen(['true']); dead.wait()
        (run / 'receipt.json').unlink(); (run / 'receipt.json').write_text(json.dumps(dict(pid=dead.pid)))
        self.restart(None, 'A2')
        out = self.next()
        self.assertEqual(out['interrupted'], [dict(card=cid, result='INVALID')])
        self.assertEqual(out['tool'], 'run_command')

    def agy_runtime(self, tool, model):
        self.build(lambda p: p['roles']['executor'].update(tool=tool, model=model, effort='default'))
        agy = self.task / '.batch-flow' / '.agy-launch' / ('d' * 32); agy.mkdir(parents=True, exist_ok=True)
        spec = json.loads((self.runtime / 'spec.json').read_text()); spec.pop('session_id')
        spec.update(launch_id='N1', model='gemini-3.8-flash-medium'); (agy / 'spec.json').write_text(json.dumps(spec))
        (agy / 'owner.json').write_text(json.dumps(dict(conversation_id='agy-main')))
        self.runtime = agy
        return agy

    def log(self, agy, tool, label):
        with (agy / 'decisions.jsonl').open('a') as f:
            f.write(json.dumps(dict(tool=tool, label=label, decision='allow')) + '\n')

    def test_agy_native_subagent_through_next(self):
        agy = self.agy_runtime('agy-native', 'inherit')
        out = self.next()
        self.assertEqual((out['tool'], out['input']['Name'], out['input']['EnableWriteTools']), ('define_subagent', 'batch_flow_executor', True))
        self.assertEqual(json.loads(self.store.read_text())['state']['attempts'], [])  # nothing started before the definition
        self.log(agy, 'define_subagent', 'batch_flow_executor')
        out = self.next(); cid = out['card']
        self.assertEqual((out['tool'], out['input']['TypeName'], out['input']['Role']), ('invoke_subagent', 'batch_flow_executor', cid))
        self.assertNotIn('Model', out['input'])  # inherit: same model as the orchestrator
        self.assertEqual(self.next()['card'], cid)  # not sent yet: same call again
        self.log(agy, 'invoke_subagent', cid)
        out = self.next()
        self.assertEqual(out['do'], 'wait'); self.assertIn('result.md', out['running'][0]['todo'])
        self.assertEqual(self.attempt('execute')['dispatch']['owner_conversation_id'], 'agy-main')
        (self.store.parent / 'records' / 'dispatch' / cid / 'result.md').write_text('结果：VERIFIED\n')
        self.assertEqual(self.next()['collected'][0]['result'], 'VERIFIED')
        self.assertEqual(self.attempt('execute')['result'], 'VERIFIED')

    def test_agy_native_passes_a_configured_model(self):
        agy = self.agy_runtime('agy-native', 'gemini-3.8-pro')
        self.log(agy, 'define_subagent', 'batch_flow_executor')
        self.assertEqual(self.next()['input']['Model'], 'gemini-3.8-pro')

    def rev(self):
        return str(json.loads(self.store.read_text())['revision'])

    def test_card_contents(self):
        out = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime))
        self.assertEqual(out['route'], 'agent')
        cid = out['card']; d = self.store.parent / 'records' / 'dispatch' / cid
        card = json.loads((d / 'card.json').read_text())
        call = card['host_call']['input']
        self.assertEqual((card['host_call']['tool'], call['subagent_type'], call['model'], card['launch']), ('Agent', 'bf-executor', 'sonnet', 'L1'))
        self.assertIn(str(d / 'prompt.md'), call['prompt']); self.assertIn(card['prompt_sha256'], call['prompt'])
        prompt = (d / 'prompt.md').read_text()
        self.assertIn('test action', prompt); self.assertIn('结果：VERIFIED | WAIT_EVIDENCE', prompt)
        self.assertIn('references/roles/executor.md', prompt)
        self.card('start', '--store', str(self.store), '--card', cid, '--revision', self.rev())
        self.card('start', '--store', str(self.store), '--card', cid, '--revision', self.rev(), ok=False)
        self.card('finish', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--conversation', 'agent-1', ok=False)
        (d / 'receipt.txt').write_text('agentId: agent-1')
        (d / 'result.md').write_text('**结果：** RESULT_TYPO\n')
        self.assertIn('结果', self.card('finish', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--conversation', 'agent-1', ok=False)['error'])
        (d / 'result.md').unlink(); (d / 'result.md').write_text('结果：VERIFIED\n')
        self.card('finish', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--conversation', 'other', ok=False)
        self.assertEqual(self.card('finish', '--store', str(self.store), '--card', cid, '--revision', self.rev(), '--conversation', 'agent-1')['result'], 'VERIFIED')

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
        outer_dir = self.store.parent / 'records' / 'dispatch' / outer['card']
        (outer_dir / 'receipt.txt').write_text('agentId: lead-agent')
        (outer_dir / 'result.md').write_text('结果：VERIFIED\n结论与依据：执行者已完成\n')
        self.assertEqual(self.card('finish', '--store', str(self.store), '--card', outer['card'], '--revision', self.rev(),
                                   '--conversation', 'lead-agent')['result'], 'VERIFIED')
        self.build(lambda p: None)
        err = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime), '--as-lead', ok=False)
        self.assertIn('delegate_to_lead', err['error'])

    def architect_gate_fixture(self):
        def mutate(p):
            p['roles']['architect'].update(tool='claude', model='opus', effort='default')
            p['tasks']['gate'] = dict(kind='confirmation', role='architect', scope=['B'], subjects=['code'],
                                    depends_on=[], consumes=[], dispatch_owner='architect', instruction='verify host evidence')
            p['tasks']['execute']['depends_on'] = ['gate']
        self.build(mutate)

    def test_architect_owned_formal_confirmation_finishes_before_board(self):
        self.architect_gate_fixture()
        out = self.next(); cid = out['card']
        directory = self.store.parent / 'records' / 'dispatch' / cid
        card = json.loads((directory / 'card.json').read_text())
        self.assertEqual((card['target'], card['role'], card['actor'], card['ledger_start']),
                         ('gate', 'architect', 'architect', 'start'))
        prompt = (directory / 'prompt.md').read_text()
        self.assertIn('你直接承担本台账正式任务', prompt)
        self.assertNotIn('这是条件确认/裁决类动作，不是台账正式任务', prompt)
        self.assertIn('不要用 decision_progress 代替', prompt)
        self.assertIsNone(self.attempt('gate').get('result'))
        self.agent('s1', 'gate-agent', cid, report='结果：VERIFIED\n结论与依据：已有宿主证据有效\n需登记的操作：无\n')
        out = self.next()
        self.assertEqual(self.attempt('gate')['result'], 'VERIFIED')
        self.assertEqual(flow.status(json.loads(self.store.read_text()))['tasks']['gate'], 'SATISFIED')
        self.assertEqual(self.attempt('execute')['task'], 'execute')
        self.assertEqual(out['collected'][0]['card'], cid)
        self.assertNotEqual(out['card'], cid)
        self.next()
        self.assertEqual(len([a for a in json.loads(self.store.read_text())['state']['attempts'] if a['task'] == 'gate']), 1)

    def test_legacy_formal_card_cannot_silently_finish_as_non_task(self):
        self.architect_gate_fixture()
        out = self.card('make', '--store', str(self.store), '--kind', 'CONFIRM', '--target', 'gate', '--runtime', str(self.runtime))
        cid = out['card']; directory = self.store.parent / 'records' / 'dispatch' / cid
        card = json.loads((directory / 'card.json').read_text())
        card.update(mode='delegate_to_lead', ledger_start=None, actor='orchestrator')
        (directory / 'card.json').write_text(json.dumps(card))
        (directory / 'receipt.txt').write_text('agentId: old-agent')
        (directory / 'result.md').write_text('结果：VERIFIED\n需登记的操作：decision_progress\n')
        out = self.card('finish', '--store', str(self.store), '--card', cid, '--revision', self.rev(),
                        '--conversation', 'old-agent', ok=False)
        self.assertIn('旧正式任务卡缺少 start/attempt', out['error'])
        self.assertEqual(json.loads(self.store.read_text())['state']['attempts'], [])
        if (directory / 'state.json').exists():
            self.assertFalse(json.loads((directory / 'state.json').read_text()).get('finished'))

    def test_architect_formal_wait_keeps_board_blocked_and_routes_diagnosis(self):
        self.architect_gate_fixture()
        cid = self.next()['card']
        self.agent('s1', 'gate-agent', cid, report='结果：WAIT_EVIDENCE\n结论与依据：缺少宿主实证\n')
        self.next()
        s = json.loads(self.store.read_text())
        self.assertEqual(self.attempt('gate')['result'], 'WAIT_EVIDENCE')
        self.assertEqual(flow.status(s)['tasks']['execute'], 'WAIT_DEPENDENCY')
        self.assertEqual(len([a for a in s['state']['attempts'] if a['task'] == 'gate']), 1)
        self.assertTrue(s['decisions'])

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
        self.assertIn('没有可派发动作', self.card(*mk, ok=False)['error'])
        s = json.loads(self.store.read_text())
        gate = flow.pre_yield(s, 'orchestrator', None, self.store.parent)
        self.assertNotIn('c1', [a['target'] for a in gate['actions'] if a['kind'] == 'CONFIRM_CONDITION'])
        self.assertEqual(gate['user_actions'], [])
        self.assertIn('REGISTER_DECISION', [a['kind'] for a in flow.pre_yield(s)['actions']])
        self.assertEqual(len(s['state']['confirmation_waits']), 1)
        self.card(*mk, '--recheck', ok=False)
        out = self.next()
        self.assertEqual(out['do'], 'call')
        diagnostic = json.loads((self.store.parent / 'records' / 'dispatch' / out['card'] / 'card.json').read_text())
        self.assertEqual(diagnostic['kind'], 'DECIDE')
        self.assertEqual(diagnostic['role'], 'architect')

    def interrupted_confirmation_collection(self):
        """Crash after the formal write and before finished card state is saved."""
        import dispatch_card as dc
        self.confirmation_fixture(delegate=True)
        out = self.card('make', '--store', str(self.store), '--kind', 'CONFIRM_CONDITION', '--target', 'c1',
                        '--runtime', str(self.runtime))
        directory = self.store.parent / 'records' / 'dispatch' / out['card']
        (directory / 'receipt.txt').write_text('agentId: lead-recovery')
        (directory / 'result.md').write_text('结果：WAIT_EVIDENCE\nwaiting for board evidence\n')
        revision_before = int(self.rev())
        with unittest.mock.patch.object(dc, 'save_state', side_effect=OSError('simulated card state crash')):
            with self.assertRaisesRegex(OSError, 'simulated card state crash'):
                dc.finish_card(self.store, directory, revision_before, 'lead-recovery')
        ledger = json.loads(self.store.read_text())
        self.assertEqual(ledger['revision'], revision_before + 1)
        self.assertEqual(len(ledger['state']['confirmation_waits']), 1)
        self.assertFalse(dc.state(directory).get('finished'))
        return out['card'], directory, revision_before

    def test_finish_reuses_wait_after_crash_before_card_state_save(self):
        card, directory, stale_revision = self.interrupted_confirmation_collection()
        original_ledger = self.store.read_bytes()
        out = self.card('finish', '--store', str(self.store), '--card', card, '--revision', str(stale_revision),
                        '--conversation', 'lead-recovery')
        self.assertEqual(out['revision'], stale_revision + 1)
        self.assertEqual(self.store.read_bytes(), original_ledger)
        self.assertTrue(json.loads((directory / 'state.json').read_text())['finished'])

    def test_next_recovers_wait_after_crash_before_card_state_save(self):
        card, directory, _ = self.interrupted_confirmation_collection()
        self.agent('s1', 'lead-recovery', card, '结果：WAIT_EVIDENCE\nwaiting for board evidence\n')
        out = self.next()
        self.assertEqual(out['do'], 'call')
        self.assertEqual(out['collected'][0]['card'], card)
        self.assertTrue(json.loads((directory / 'state.json').read_text())['finished'])
        ledger = json.loads(self.store.read_text())
        self.assertEqual(len(ledger['state']['confirmation_waits']), 1)
        operations = [op for h in ledger['history'] for op in h['operations']]
        self.assertEqual(sum(op['type'] == 'decision_progress' and op['data'].get('status') == 'waiting' for op in operations), 1)
        diagnostic = json.loads((self.store.parent / 'records' / 'dispatch' / out['card'] / 'card.json').read_text())
        self.assertEqual(diagnostic['kind'], 'DECIDE')

    def test_interrupted_wait_cannot_reuse_overwritten_evidence(self):
        card, directory, _ = self.interrupted_confirmation_collection()
        original_ledger = self.store.read_bytes()
        (directory / 'result.md').write_text('结果：WAIT_EVIDENCE\noverwritten original evidence\n')
        out = self.card('finish', '--store', str(self.store), '--card', card, '--revision', self.rev(),
                        '--conversation', 'lead-recovery', ok=False)
        self.assertIn('原始等待证据已改变', out['error'])
        self.assertEqual(self.store.read_bytes(), original_ledger)
        import dispatch_card as dc
        self.assertFalse(dc.state(directory).get('finished'))

    def test_next_migrates_legacy_wait_and_diagnoses_empty_blocks(self):
        self.confirmation_fixture(delegate=True)
        s = json.loads(self.store.read_text())
        s['decisions']['D1']['blocks'] = []
        self.store.write_text(json.dumps(s))
        mk = ['make', '--store', str(self.store), '--kind', 'CONFIRM_CONDITION', '--target', 'c1', '--runtime', str(self.runtime)]
        out = self.card(*mk); d = self.store.parent / 'records' / 'dispatch' / out['card']
        result = d / 'result.md'; result.write_text('结果：WAIT_EVIDENCE\n依赖尚未提供\n')
        original_state = dict(finished='2026-01-01T00:00:00+00:00', result='WAIT_EVIDENCE',
                              progress_marker=flow.progress_marker(s))
        (d / 'state.json').write_text(json.dumps(original_state))
        # Legacy markers cannot be made retryable by unrelated progress or --recheck.
        s['plan_version'] += 1; self.store.write_text(json.dumps(s))
        self.card(*mk, '--recheck', ok=False)
        gate = flow.pre_yield(s, root=self.store.parent)
        self.assertEqual(gate['user_actions'], [])
        self.assertIn('REGISTER_CONFIRMATION_WAIT', [a['kind'] for a in gate['actions']])
        out = self.next()
        self.assertEqual(out['do'], 'call')
        card = json.loads((self.store.parent / 'records' / 'dispatch' / out['card'] / 'card.json').read_text())
        self.assertEqual(card['kind'], 'DECIDE')
        s = json.loads(self.store.read_text())
        wait = s['state']['confirmation_waits'][0]
        self.assertEqual(wait['evidence_hashes'], {str(result): flow.hashlib.sha256(result.read_bytes()).hexdigest()})
        diagnostic = s['decisions'][card['target']]
        self.assertEqual(diagnostic['blocks'], [])
        self.assertEqual(diagnostic['owner'], 'architect')
        self.assertEqual(json.loads((d / 'state.json').read_text()), original_state)
        self.assertEqual(len([h for h in s['history'] if any(o['type'] == 'decision_progress' and o['data'].get('status') == 'waiting' for o in h['operations'])]), 1)

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


    def test_structured_goals_reach_native_card_and_stale_card_cannot_start(self):
        def goals(p):
            from test_goal_contract import contract
            p['goal_contract'] = contract('project outcome')
            for tid, task in p['tasks'].items(): task['goal_contract'] = contract(tid)
        self.build(goals)
        card = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime))
        text = Path(card['prompt_file']).read_text()
        card_data = json.loads((Path(card['prompt_file']).parent / 'card.json').read_text())
        payload = json.loads(Path(card_data['payload_file']).read_text())
        self.assertEqual(payload['target'], 'execute')
        self.assertIn('goal_context', payload)
        self.assertNotIn('## 原始载荷', text)
        self.assertEqual(text.count('test action'), 1)
        self.assertIn('project outcome excluded work', text)
        self.assertIn('execute observable result', text)
        s = json.loads(self.store.read_text()); s['plan_version'] += 1
        s['plan']['tasks']['execute']['goal_contract']['goal'] = 'updated authorized goal'
        self.store.write_text(json.dumps(s))
        err = self.card('start', '--store', str(self.store), '--card', card['card'], '--revision', str(s['revision']), ok=False)
        self.assertIn('目标或计划版本', err['error'])
        self.assertFalse(json.loads(self.store.read_text())['state']['attempts'])


    def completed_cli_card(self, receipt=True, code=0):
        self.build(lambda p: p['roles']['executor'].update(tool='codex', model='gpt-6.1-sol', effort='medium'))
        profile = self.runtime / 'codex-profile.json'
        profile.write_text(json.dumps(dict(format='batch-flow-cli/v1', backend='codex', binary='/bin/codex', version='fixture')))
        spec_path = self.runtime / 'spec.json'; spec = json.loads(spec_path.read_text())
        spec['profiles'].append(str(profile)); spec_path.write_text(json.dumps(spec))
        card = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime))
        revision = json.loads(self.store.read_text())['revision']
        self.card('start', '--store', str(self.store), '--card', card['card'], '--revision', str(revision))
        directory = Path(card['prompt_file']).parent; run = directory / 'run'; run.mkdir()
        cid = '10000000-0000-4000-8000-000000000001'
        (run / 'request.json').write_text(json.dumps(dict(backend='codex', conversation=None, task='execute', started_at=flow.now())))
        if receipt: (run / 'receipt.json').write_text(json.dumps(dict(event='CLI_STARTED', pid=12345, task='execute')))
        events = [dict(type='thread.started', thread_id=cid), dict(type='item.completed', item=dict(type='agent_message', text='结果：VERIFIED\n验证证据：fixture')), dict(type='turn.completed')]
        (run / 'stdout').write_text('\n'.join(json.dumps(e) for e in events))
        (run / 'stderr').write_text('')
        (run / 'exit.json').write_text(json.dumps(dict(returncode=code, ended_at=flow.now())))
        return card, directory, run, cid

    def test_next_collects_fast_cli_completion_using_original_json_receipt(self):
        card, directory, run, cid = self.completed_cli_card()
        self.assertFalse((directory / 'receipt.txt').exists())
        report = self.next()
        self.assertEqual(report['collected'][0]['card'], card['card'])
        self.assertFalse(report.get('problems'))
        attempt = self.attempt('execute')
        self.assertEqual((attempt['result'], attempt['call']['conversation_id']), ('VERIFIED', cid))
        self.assertIn(str(run / 'receipt.json'), attempt['evidence'])
        self.assertIn(str(run / 'request.json'), attempt['evidence'])
        self.assertNotIn(str(directory / 'receipt.txt'), attempt['evidence'])
        self.assertFalse((directory / 'receipt.txt').exists())
        self.next()
        attempts = [a for a in json.loads(self.store.read_text())['state']['attempts'] if a['task']=='execute']
        self.assertEqual(len(attempts), 1)

    def test_cli_without_original_launch_receipt_cannot_finish(self):
        card, directory, run, cid = self.completed_cli_card(receipt=False)
        revision = json.loads(self.store.read_text())['revision']
        error = self.card('finish', '--store', str(self.store), '--card', card['card'], '--revision', str(revision), ok=False)
        self.assertIn('run/receipt.json', error['error'])
        self.assertIsNone(self.attempt('execute')['result'])
        self.assertFalse((directory / 'receipt.txt').exists())

    def test_exit_without_cli_request_is_closed_not_reported_every_round(self):
        card, directory, run, cid = self.completed_cli_card()
        (run / 'request.json').unlink()
        report = self.next()
        self.assertFalse(report.get('problems'))
        self.assertEqual([x['card'] for x in report['interrupted']], [card['card']])
        st = json.loads((directory / 'state.json').read_text())
        self.assertTrue(st['finished'])
        first = next(a for a in json.loads(self.store.read_text())['state']['attempts'] if a['id'] == st['attempt'])
        self.assertEqual(first['result'], 'INVALID')
        self.assertFalse(self.next().get('problems'))

    def test_failed_cli_does_not_accept_response_as_success(self):
        card, directory, run, cid = self.completed_cli_card(code=1)
        revision = json.loads(self.store.read_text())['revision']
        error = self.card('finish', '--store', str(self.store), '--card', card['card'], '--revision', str(revision), ok=False)
        self.assertIn('不可用', error['error'])
        self.assertIsNone(self.attempt('execute')['result'])


    def test_resume_finds_verified_legacy_run_within_bound_ledger(self):
        import dispatch_card
        import shutil
        card, directory, run, cid = self.completed_cli_card()
        legacy = self.store.parent / 'records' / 'legacy_architect_run_01'
        shutil.move(str(run), str(legacy))
        before = self.store.read_bytes()
        self.assertEqual(dispatch_card.prior_run(self.store, cid), legacy)
        self.assertEqual(self.store.read_bytes(), before)
        # A native ID alone is insufficient: failed completion may not be resumed.
        (legacy / 'exit.json').write_text(json.dumps(dict(returncode=1, ended_at=flow.now())))
        with self.assertRaises(dispatch_card.CardError): dispatch_card.prior_run(self.store, cid)
        # An unrelated task base must not supply a proof for this ledger.
        outside = self.workspace / 'other-task' / 'records' / 'successful_run'
        outside.parent.mkdir(parents=True); shutil.move(str(legacy), str(outside))
        (outside / 'exit.json').write_text(json.dumps(dict(returncode=0, ended_at=flow.now())))
        with self.assertRaises(dispatch_card.CardError): dispatch_card.prior_run(self.store, cid)


    def test_decision_card_reuses_pending_registration_without_starting_twice(self):
        s = json.loads(self.store.read_text())
        proof = str(self.task / '.batch-flow' / 'proof.txt')
        request = dict(request_id='pending-decision', expected_revision=s['revision'], actor='architect', operations=[
            dict(type='signal', data=dict(id='D1',signal='scope_blocked',reason='diagnose',blocks=['done'],evidence=[proof])),
            dict(type='decision_start', data=dict(id='D1',call=dict(s['plan']['roles']['architect'],conversation_id='pending:D1')))])
        s,_ = flow.apply_request(s,request,self.store.parent); self.store.write_text(json.dumps(s))
        before = self.store.read_bytes(); old_start = s['decisions']['D1']['handling']['started_at']
        card = self.card('make','--store',str(self.store),'--kind','DECIDE','--target','D1','--runtime',str(self.runtime))
        self.card('start','--store',str(self.store),'--card',card['card'],'--revision',str(s['revision']))
        self.assertEqual(self.store.read_bytes(), before)
        state = json.loads((Path(card['prompt_file']).parent / 'state.json').read_text())
        self.assertTrue(state['reused_pending_handling']); self.assertEqual(state['started'],old_start)
        duplicate = self.card('make','--store',str(self.store),'--kind','DECIDE','--target','D1','--runtime',str(self.runtime))
        error = self.card('start','--store',str(self.store),'--card',duplicate['card'],'--revision',str(s['revision']),ok=False)
        self.assertIn('active dispatch card',error['error'])


if __name__ == '__main__':
    unittest.main()
