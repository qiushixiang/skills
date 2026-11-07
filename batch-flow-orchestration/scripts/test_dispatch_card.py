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

    def by_id(self, attempt_id):
        return next(a for a in json.loads(self.store.read_text())['state']['attempts'] if a['id'] == attempt_id)

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

    def test_ledger_cli_execution_becomes_launch_args_and_prestart_gate(self):
        import argparse
        import dispatch_card
        import agent_cli
        def configure(p):
            p['roles']['executor'] = dict(tool='codex', model='gpt-6.1-terra', effort='medium')
            p['cli_execution'] = {'codex':dict(require_host=True, add_dirs=[str(self.workspace)],
                                             approval_policy='on-request', guide='Use the fixed absolute entrypoint.')}
        self.build(configure)
        profile = self.runtime/'codex-profile.json'
        profile.write_text(json.dumps(dict(backend='codex', binary='/bin/codex', version='fake')))
        spec_path = self.runtime/'spec.json'; spec = json.loads(spec_path.read_text())
        spec['profiles'].append(str(profile)); spec_path.write_text(json.dumps(spec))
        made = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime))
        directory = self.store.parent/'records/dispatch'/made['card']
        card = json.loads((directory/'card.json').read_text())
        command = card['host_call']['input']['command']
        self.assertIn('--require-host', command)
        self.assertIn('--approval-policy on-request', command)
        self.assertIn('--add-dir', command)
        self.assertIn('Use the fixed absolute entrypoint.', (directory/'prompt.md').read_text())
        before = self.store.read_bytes()
        with unittest.mock.patch.object(agent_cli, 'check_execution', side_effect=ValueError('HOST_ENVIRONMENT_BLOCKED')):
            with self.assertRaisesRegex(dispatch_card.CardError, 'HOST_ENVIRONMENT_BLOCKED'):
                dispatch_card.start(argparse.Namespace(store=str(self.store), card=made['card'], revision=int(self.rev())))
        self.assertEqual(self.store.read_bytes(), before)

    def test_cli_execution_schema_rejects_unsafe_or_unknown_configuration(self):
        for config in ({'codex': {'add_dirs': ['/']}}, {'codex': {'approval_policy':'never'}},
                       {'codex': {'require_host':'yes'}}, {'unknown': {}}, {'codex': {'env': {'entry': []}}},
                       {'codex': {'env': {'entry': ['relative/run']}}}, {'codex': {'env': {'entry': ['/a'], 'other': 1}}}):
            p = plan(); p['cli_execution'] = config
            with self.assertRaises(flow.FlowError):
                flow.validate_plan(p)
        p = plan(); p['cli_execution'] = {'codex': {'env': {'entry': ['/opt/run_'], 'preflight': '/opt/run_test.sh preflight'}}}
        flow.validate_plan(p)

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
        self.assertIn('停止原因：完成 | 外部阻塞 | 待裁决 | 技术不可行 | 未完成', prompt)
        self.assertIn('可自行补齐的工作须在本轮继续', prompt)  # Agent route: no in-place continuation
        self.assertFalse(card['delivery_gate'])
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


    def write_cli_run(self, run, cid, text, items=(), code=0, conversation=None):
        run.mkdir()
        (run / 'request.json').write_text(json.dumps(dict(backend='codex', conversation=conversation, task='execute',
                                                          cwd=str(self.workspace), started_at=flow.now())))
        (run / 'receipt.json').write_text(json.dumps(dict(event='CLI_STARTED', pid=12345, task='execute')))
        events = [dict(type='thread.started', thread_id=cid)] + [dict(type='item.completed', item=i) for i in items] + \
                 [dict(type='item.completed', item=dict(type='agent_message', text=text)), dict(type='turn.completed')]
        (run / 'stdout').write_text('\n'.join(json.dumps(e) for e in events)); (run / 'stderr').write_text('')
        (run / 'exit.json').write_text(json.dumps(dict(returncode=code, ended_at=flow.now())))

    def gated_card(self, env=None):
        def configure(p):
            p['roles']['executor'] = dict(tool='codex', model='gpt-6.1-terra', effort='medium')
            p['cli_execution'] = {'codex': dict(require_host=True, guide='fixed entry', **({'env': env} if env else {}))}
        self.build(configure)
        profile = self.runtime / 'codex-profile.json'
        profile.write_text(json.dumps(dict(format='batch-flow-cli/v1', backend='codex', binary='/bin/codex', version='fixture')))
        spec_path = self.runtime / 'spec.json'; spec = json.loads(spec_path.read_text())
        spec['profiles'].append(str(profile)); spec_path.write_text(json.dumps(spec))
        out = self.next()
        self.assertEqual(out['do'], 'call')
        directory = self.store.parent / 'records' / 'dispatch' / out['card']
        return out, directory, json.loads((directory / 'card.json').read_text())

    UNFINISHED = '结果：FAILED\n停止原因：未完成\n上升问题：本批尚未完成；当前没有外部阻塞\n'

    def test_unfinished_delivery_continues_in_same_attempt_then_fails_into_normal_rounds(self):
        out, directory, card = self.gated_card(env=dict(entry=['/opt/entry/run_board_'], preflight='/opt/entry/run_board_test.sh preflight'))
        self.assertTrue(card['delivery_gate'])
        prompt = (directory / 'prompt.md').read_text()
        self.assertLess(prompt.index('## 环境'), prompt.index('## 执行'))
        self.assertIn('沙箱常态', prompt); self.assertIn('/opt/entry/run_board_test.sh preflight', prompt); self.assertIn('补充：fixed entry', prompt)
        cid = '10000000-0000-4000-8000-000000000001'
        attempt = self.attempt('execute')['id']
        self.write_cli_run(directory / 'run', cid, self.UNFINISHED, items=[dict(type='file_change', changes=[])])
        out = self.next()
        self.assertEqual(out['do'], 'call')
        command = out['input']['command']
        self.assertIn('--conversation ' + cid, command); self.assertIn('--resume-from ' + str(directory / 'run'), command)
        self.assertIn(str(directory / 'run-c1'), command); self.assertIn('continue-1.md', command)
        self.assertNotIn(str(directory / 'prompt.md'), command)
        self.assertIsNone(self.attempt('execute')['result']); self.assertEqual(self.attempt('execute')['id'], attempt)
        self.assertIn('尚未完成', (directory / 'continue-1.md').read_text())
        import orchestrator_guard
        self.assertTrue(orchestrator_guard.matches_card('Bash', dict(command=command), directory.parent, ['command']))
        # Continuation without progress is not continued again: registered FAILED, ordinary rounds apply.
        self.write_cli_run(directory / 'run-c1', cid, self.UNFINISHED, conversation=cid)
        out = self.next()
        a = self.by_id(attempt)
        self.assertEqual(a['result'], 'FAILED')
        self.assertIn(str(directory / 'run-c1' / 'stdout'), a['evidence']); self.assertIn(str(directory / 'gate-1.json'), a['evidence'])
        self.assertFalse(json.loads((directory / 'gate-1.json').read_text())['progress'])
        # The next ordinary round starts from status as before (new attempt, normal budget).
        self.assertEqual(out['do'], 'call'); self.assertNotEqual(self.attempt('execute')['id'], attempt)

    def test_continuation_limit_then_failed(self):
        out, directory, card = self.gated_card()
        cid = '10000000-0000-4000-8000-000000000001'; edit = [dict(type='file_change', changes=[])]
        self.write_cli_run(directory / 'run', cid, self.UNFINISHED, items=edit)
        self.assertEqual(self.next()['do'], 'call')
        self.write_cli_run(directory / 'run-c1', cid, self.UNFINISHED, items=edit, conversation=cid)
        self.assertIn('run-c2', self.next()['input']['command'])
        self.write_cli_run(directory / 'run-c2', cid, self.UNFINISHED, items=edit, conversation=cid)
        self.next()
        self.assertEqual(self.by_id('A0001')['result'], 'FAILED')
        self.assertEqual(json.loads((directory / 'state.json').read_text())['continuations'], 2)

    def test_legal_ends_finish_without_continuation(self):
        cid = '10000000-0000-4000-8000-000000000001'
        for text, result in [('结果：VERIFIED\n', 'VERIFIED'),
                             ('结果：FAILED\n停止原因：技术不可行\n上升问题：三种方案均失败，见记录\n', 'FAILED'),
                             ('结果：ARCH_DECISION_REQUIRED\n停止原因：待裁决\n上升问题：PDO 映射与冻结用例冲突\n', 'ARCH_DECISION_REQUIRED')]:
            __import__('shutil').rmtree(self.store.parent / 'records', ignore_errors=True)
            out, directory, card = self.gated_card()
            self.write_cli_run(directory / 'run', cid, text)
            self.next()
            self.assertEqual(self.by_id('A0001')['result'], result, text)
            self.assertFalse((directory / 'continue-1.json').exists())

    def test_environment_block_requires_actual_entry_failure(self):
        cid = '10000000-0000-4000-8000-000000000001'
        wait = '结果：WAIT_EVIDENCE\n停止原因：外部阻塞\n上升问题：NoNewPrivs=1\n'
        env = dict(entry=['/opt/entry/run_board_'])
        out, directory, card = self.gated_card(env=env)
        own_shell = dict(type='command_execution', command="awk '/NoNewPrivs/' /proc/self/status", exit_code=1)
        self.write_cli_run(directory / 'run', cid, wait, items=[own_shell])
        self.assertEqual(self.next()['do'], 'call')
        self.assertIn('/opt/entry/run_board_', (directory / 'continue-1.md').read_text())
        failed = dict(type='command_execution', command='/opt/entry/run_board_test.sh s3', exit_code=1)
        passed = dict(failed, exit_code=0)
        # A later successful entry run shows the environment works: still not a legal block.
        self.write_cli_run(directory / 'run-c1', cid, wait, items=[failed, passed, dict(type='file_change', changes=[])], conversation=cid)
        self.assertIn('run-c2', self.next()['input']['command'])
        self.write_cli_run(directory / 'run-c2', cid, wait, items=[failed], conversation=cid)
        self.next()
        self.assertEqual(self.by_id('A0001')['result'], 'WAIT_EVIDENCE')

    def test_environment_block_requires_the_entry_environment_failure_output(self):
        cid = '10000000-0000-4000-8000-000000000001'
        wait = '结果：WAIT_EVIDENCE\n停止原因：外部阻塞\n上升问题：入口前检失败\n'
        out, directory, card = self.gated_card(env=dict(entry=['/opt/entry/run_board_'], blocked_output='Sandbox detected|Missing sudo rule'))
        product = dict(type='command_execution', command='/opt/entry/run_board_test.sh s3', exit_code=1, aggregated_output='TC-03 FAIL')
        self.write_cli_run(directory / 'run', cid, wait, items=[product])  # a failing product test is not an environment block
        self.assertEqual(self.next()['do'], 'call')
        # Wrapped or compound entry calls miss the host allow rule: their sandbox failure is self-inflicted.
        wrapped = dict(type='command_execution', exit_code=1, aggregated_output='ERROR: Sandbox detected: NoNewPrivs is 1 (expected 0)',
                       command="/bin/bash -lc 'date -u; /opt/entry/run_board_test.sh hostcheck'")
        self.write_cli_run(directory / 'run-c1', cid, wait, items=[wrapped, dict(type='file_change', changes=[])], conversation=cid)
        self.assertIn('run-c2', self.next()['input']['command'])
        blocked = dict(wrapped, command="/bin/bash -lc '/opt/entry/run_board_test.sh hostcheck'")
        self.write_cli_run(directory / 'run-c2', cid, wait, items=[blocked], conversation=cid)
        self.next()
        self.assertEqual(self.by_id('A0001')['result'], 'WAIT_EVIDENCE')

    def test_decision_dispatch_constraint_forces_fresh_session_once(self):
        import dispatch_card
        s = json.loads(self.store.read_text())
        s['plan']['tasks']['execute']['session_policy'] = 'resume'
        s['state']['attempts'].append(dict(id='A0001', task='execute', started_at='2026-01-01T00:00:00+00:00', result='FAILED'))
        s['decisions']['DEC-1'] = dict(id='DEC-1', status='closed', resolved_at='2026-01-02T00:00:00+00:00',
                                       resolution=dict(dispatch=[dict(task='execute', fresh_session=True, note='先经入口运行 hostcheck')]))
        got = flow.pending_dispatch_constraints(s, 'execute')
        self.assertEqual([c['decision'] for c in got], ['DEC-1'])
        s['state']['attempts'].append(dict(id='A0002', task='execute', started_at='2026-01-03T00:00:00+00:00', result=None))
        self.assertEqual(flow.pending_dispatch_constraints(s, 'execute'), [])
        with self.assertRaisesRegex(flow.FlowError, 'unknown task'):
            flow.check_dispatch_constraints(s, [dict(task='nope', fresh_session=True)])
        with self.assertRaisesRegex(flow.FlowError, '300'):
            flow.check_dispatch_constraints(s, [dict(task='execute', note='x' * 301)])
        card = dict(cli_execution={}, decision_notes=[dict(decision='DEC-1', note='先经入口运行 hostcheck')], access='write',
                    role='executor', kind='EXECUTE', target='execute', mode='dispatch', id='c', tool='codex')
        prompt = dispatch_card.render_prompt(card, dict(instruction='do it', target='execute'), json.loads(self.store.read_text()), self.store, self.task, self.workspace)
        self.assertIn('裁决要求（DEC-1，本轮）：先经入口运行 hostcheck', prompt)

    def test_fresh_session_constraint_overrides_resume_hint_in_make(self):
        import argparse
        import dispatch_card
        out, directory, card = self.gated_card()
        s = json.loads(self.store.read_text()); s['plan']['tasks']['execute']['session_policy'] = 'resume'
        self.store.write_text(json.dumps(s))
        cid = '10000000-0000-4000-8000-000000000001'
        self.write_cli_run(directory / 'run', cid, '结果：FAILED\n停止原因：技术不可行\n上升问题：已试两种方案\n')
        dispatch_card.finish_card(str(self.store), directory, int(self.rev()))
        ns = lambda: argparse.Namespace(store=str(self.store), kind='EXECUTE', target='execute', workspace=None,
                                        runtime=str(self.runtime), as_lead=False, recheck=False)
        plain = dispatch_card.make(ns())
        self.assertEqual(plain['resume'], cid)
        with unittest.mock.patch.object(flow, 'pending_dispatch_constraints',
                                        return_value=[dict(task='execute', fresh_session=True, note='入口先跑 hostcheck', decision='DEC-9')]):
            fresh = dispatch_card.make(ns())
        self.assertIsNone(fresh['resume']); self.assertIn('DEC-9', fresh['replacement_reason'])
        self.assertNotIn('--conversation', fresh['host_call']['input']['command'])
        self.assertIn('裁决要求（DEC-9，本轮）：入口先跑 hostcheck', Path(fresh['prompt_file']).read_text())

    def completed_cli_card(self, receipt=True, code=0, backend='codex'):
        model='deepseek/deepseek-flash' if backend=='opencode' else 'gpt-6.1-sol'
        effort='default' if backend=='opencode' else 'medium'
        self.build(lambda p: p['roles']['executor'].update(tool=backend, model=model, effort=effort))
        profile = self.runtime / (backend+'-profile.json')
        profile.write_text(json.dumps(dict(format='batch-flow-cli/v1', backend=backend, binary='/bin/'+backend, version='fixture')))
        spec_path = self.runtime / 'spec.json'; spec = json.loads(spec_path.read_text())
        spec['profiles'].append(str(profile)); spec_path.write_text(json.dumps(spec))
        card = self.card('make', '--store', str(self.store), '--kind', 'EXECUTE', '--target', 'execute', '--runtime', str(self.runtime))
        revision = json.loads(self.store.read_text())['revision']
        self.card('start', '--store', str(self.store), '--card', card['card'], '--revision', str(revision))
        directory = Path(card['prompt_file']).parent; run = directory / 'run'; run.mkdir()
        cid = 'ses_ActualFixture012345' if backend=='opencode' else '10000000-0000-4000-8000-000000000001'
        (run / 'request.json').write_text(json.dumps(dict(backend=backend, conversation=None, task='execute', cwd=str(self.workspace),started_at=flow.now())))
        if receipt: (run / 'receipt.json').write_text(json.dumps(dict(event='CLI_STARTED', pid=12345, task='execute')))
        events = [dict(type='thread.started', thread_id=cid), dict(type='item.completed', item=dict(type='agent_message', text='结果：VERIFIED\n验证证据：fixture')), dict(type='turn.completed')]
        if backend=='opencode':
            events=[dict(type='text',sessionID=cid,part=dict(messageID='msg_final',text='结果：VERIFIED\n验证证据：fixture')),
                    dict(type='step_finish',sessionID=cid,part=dict(messageID='msg_final',reason='stop'))]
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

    def test_opencode_card_collects_native_session_and_can_find_resume_evidence(self):
        import dispatch_card as dc
        card,directory,run,cid=self.completed_cli_card(backend='opencode')
        recorded=json.loads((directory/'card.json').read_text())
        self.assertIn('--model deepseek/deepseek-flash',recorded['host_call']['input']['command'])
        self.assertEqual(recorded['route'],'cli')
        report=self.next()
        self.assertEqual(report['collected'][0]['conversation_id'],cid)
        attempt=self.attempt('execute')
        self.assertEqual(attempt['result'],'VERIFIED')
        self.assertEqual(attempt['call']['conversation_id'],cid)
        self.assertEqual(dc.prior_run(self.store,cid),run)
        self.assertFalse((directory/'receipt.txt').exists())

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


    def handback_fixture(self):
        def configure(p):
            p['roles']['architect'].update(tool='claude', model='claude-opus-5-5', effort='default')
        self.build(configure)
        s = json.loads(self.store.read_text()); proof = str(self.store.parent/'proof.txt')
        s, _ = flow.apply_request(s, dict(request_id='handback-decision', expected_revision=s['revision'],
            actor='architect', operations=[dict(type='signal', data=dict(id='D1', signal='technical_decision',
                reason='diagnose', blocks=['execute'], evidence=[proof]))]), self.store.parent)
        self.store.write_text(json.dumps(s))
        out = self.next(); directory = self.store.parent/'records/dispatch'/out['card']
        self.assertEqual(json.loads((directory/'card.json').read_text())['kind'], 'DECIDE')
        return out['card'], directory

    def handback_operations(self, owner='architect'):
        proof = str(self.store.parent/'proof.txt')
        return [dict(type='resolve', data=dict(id='D1', summary='environment fixed', evidence=[proof],
                    user_evidence=[], requirements=[dict(id='D1.ready', kind='confirmation', owner=owner,
                    depends_on=[], instruction='verify environment')])),
                dict(type='decision_progress', data=dict(id='D1', status='confirmed', requirement='D1.ready'))]

    def test_decision_delivery_is_registered_before_next_dispatch(self):
        cid, directory = self.handback_fixture()
        (directory/'registration.json').write_text(json.dumps(dict(operations=self.handback_operations())))
        self.agent('s1', 'decision-agent', cid, report='结果：VERIFIED\n结论与依据：fixed\n')
        out = self.next(); ledger = json.loads(self.store.read_text())
        self.assertEqual(ledger['decisions']['D1']['status'], 'closed')
        self.assertEqual(ledger['decisions']['D1']['resolution']['context']['conversation_id'], 'decision-agent')
        self.assertEqual(out['do'], 'call')
        self.assertEqual(json.loads((self.store.parent/'records/dispatch'/out['card']/'card.json').read_text())['target'], 'execute')
        self.assertEqual(len(list((self.store.parent/'records/dispatch').glob('D1-DECIDE-*'))), 1)
        self.next()
        self.assertEqual(sum(op['type']=='resolve' for h in json.loads(self.store.read_text())['history'] for op in h['operations']), 1)

    def test_missing_registration_reports_and_never_redispatches(self):
        cid, directory = self.handback_fixture()
        self.agent('s1', 'decision-agent', cid, report='结果：VERIFIED\n需登记的操作：see prose\n')
        for _ in range(3):
            out = self.next(); self.assertEqual(out['do'], 'report')
        self.assertEqual(len(list((self.store.parent/'records/dispatch').glob('D1-DECIDE-*'))), 1)
        error = self.card('make', '--store', str(self.store), '--kind', 'DECIDE', '--target', 'D1',
                          '--runtime', str(self.runtime), ok=False)
        self.assertIn('已有角色交付', error['error'])
        # Fixing the structured handback is enough; no new model is needed.
        (directory/'registration.json').write_text(json.dumps(dict(operations=self.handback_operations())))
        self.assertEqual(self.next()['do'], 'call')
        self.assertEqual(json.loads(self.store.read_text())['decisions']['D1']['status'], 'closed')

    def test_legacy_exact_parameter_files_are_registered_without_prose_parsing(self):
        cid, directory = self.handback_fixture(); operations = self.handback_operations()
        (directory/'resolve_D1.json').write_text(json.dumps(operations[0]['data']))
        (directory/'confirm_D1.json').write_text(json.dumps(operations[1]['data']))
        self.agent('s1', 'decision-agent', cid, report='结果：VERIFIED\n')
        self.next()
        self.assertEqual(json.loads(self.store.read_text())['decisions']['D1']['status'], 'closed')

    def test_later_invalid_confirmation_writes_nothing_and_cannot_approve_user(self):
        cid, directory = self.handback_fixture(); before = self.store.read_bytes()
        (directory/'registration.json').write_text(json.dumps(dict(operations=self.handback_operations(owner='user'))))
        self.agent('s1', 'decision-agent', cid, report='结果：VERIFIED\n')
        out = self.next(); self.assertEqual(out['do'], 'report')
        self.assertEqual(self.store.read_bytes(), before)
        self.assertEqual(len(list((self.store.parent/'records/dispatch').glob('D1-DECIDE-*'))), 1)

    def test_self_blocking_recovery_is_rejected_before_registration(self):
        cid, directory = self.handback_fixture(); before = self.store.read_bytes()
        op = self.handback_operations()[0]
        op['data']['requirements'] = [dict(id='D1.repair', kind='task', target='execute',
            versions={'code':'v1'}, depends_on=[], instruction='repair blocked task')]
        (directory/'registration.json').write_text(json.dumps(dict(operations=[op])))
        self.agent('s1', 'decision-agent', cid, report='结果：ARCH_DECISION_REQUIRED\n')
        self.assertEqual(self.next()['do'], 'report'); self.assertEqual(self.store.read_bytes(), before)

    def test_handback_cannot_target_another_decision_or_supply_an_actor(self):
        cid, directory = self.handback_fixture(); before = self.store.read_bytes()
        operations = self.handback_operations(); operations[0]['data']['id']='another-decision'
        (directory/'registration.json').write_text(json.dumps(dict(operations=operations)))
        self.agent('s1', 'decision-agent', cid, report='结果：VERIFIED\n')
        self.assertEqual(self.next()['do'], 'report'); self.assertEqual(self.store.read_bytes(), before)
        operations = self.handback_operations(); operations[0]['actor']='orchestrator'
        (directory/'registration.json').write_text(json.dumps(dict(operations=operations)))
        self.assertEqual(self.next()['do'], 'report'); self.assertEqual(self.store.read_bytes(), before)
        operations = self.handback_operations(); operations[0]['type'] = {}
        (directory/'registration.json').write_text(json.dumps(dict(operations=operations)))
        self.assertEqual(self.next()['do'], 'report'); self.assertEqual(self.store.read_bytes(), before)

    def test_handback_manifest_symlink_escape_is_rejected(self):
        import handback_registration as hr
        _, directory = self.handback_fixture()
        outside = self.task/'outside.json'; outside.write_text('{"operations": []}')
        (directory/'registration.json').symlink_to(outside)
        with self.assertRaisesRegex(hr.RegistrationError, 'escapes'):
            hr.read_operations(directory, json.loads((directory/'card.json').read_text()), {})

    def test_verified_condition_registers_only_its_dispatched_owner(self):
        self.handback_fixture()
        s = json.loads(self.store.read_text()); op = self.handback_operations()[0]
        op['data']['context'] = dict(conversation_id='original-planner', mode='fresh', reason='fixture')
        s, _ = flow.apply_request(s, dict(request_id='condition-fixture', expected_revision=s['revision'],
            actor='architect', operations=[op]), self.store.parent); self.store.write_text(json.dumps(s))
        made = self.card('make', '--store', str(self.store), '--kind', 'CONFIRM_CONDITION', '--target', 'D1.ready',
                         '--runtime', str(self.runtime))
        self.agent('s1', 'condition-agent', made['card'], report='结果：VERIFIED\n结论与依据：ready\n')
        self.next()
        self.assertEqual(json.loads(self.store.read_text())['decisions']['D1']['status'], 'closed')

    def test_interrupted_multi_actor_registration_resumes_without_duplicate_resolve(self):
        import handback_registration as hr
        cid, directory = self.handback_fixture()
        (directory/'registration.json').write_text(json.dumps(dict(operations=self.handback_operations())))
        (directory/'result.md').write_text('结果：VERIFIED\n')
        (directory/'receipt.txt').write_text('decision-agent')
        card = json.loads((directory/'card.json').read_text())
        st = json.loads((directory/'state.json').read_text()); st.update(result='VERIFIED', conversation_id='decision-agent')
        counter = []
        def commit(path):
            req = json.loads(path.read_text())
            if len(counter)==1: raise OSError('simulated interruption before second commit')
            ledger = json.loads(self.store.read_text())
            ledger, _ = flow.apply_request(ledger, req, self.store.parent); self.store.write_text(json.dumps(ledger))
            counter.append(req['request_id'])
        with self.assertRaises(OSError): hr.register(self.store, directory, card, st, commit)
        def resume(path):
            ledger, _ = flow.apply_request(json.loads(self.store.read_text()), json.loads(path.read_text()), self.store.parent)
            self.store.write_text(json.dumps(ledger))
        hr.register(self.store, directory, card, json.loads((directory/'state.json').read_text()), resume)
        ledger = json.loads(self.store.read_text())
        self.assertEqual(sum(op['type']=='resolve' for h in ledger['history'] for op in h['operations']), 1)
        self.assertIn('D1.ready', ledger['state']['confirmations'])

    def test_interrupted_registration_does_not_silently_rebase_over_other_writes(self):
        import handback_registration as hr
        _, directory=self.handback_fixture()
        (directory/'registration.json').write_text(json.dumps(dict(operations=self.handback_operations())))
        (directory/'result.md').write_text('结果：VERIFIED\n');(directory/'receipt.txt').write_text('decision-agent')
        card=json.loads((directory/'card.json').read_text());st=json.loads((directory/'state.json').read_text())
        st.update(result='VERIFIED',conversation_id='decision-agent');committed=[]
        def commit(path):
            if committed: raise OSError('interrupted')
            ledger,_=flow.apply_request(json.loads(self.store.read_text()),json.loads(path.read_text()),self.store.parent)
            self.store.write_text(json.dumps(ledger));committed.append(True)
        with self.assertRaises(OSError):hr.register(self.store,directory,card,st,commit)
        ledger=json.loads(self.store.read_text())
        ledger,_=flow.apply_request(ledger,dict(request_id='another-writer',expected_revision=ledger['revision'],
            actor='orchestrator',operations=[dict(type='observe',data={})]),self.store.parent)
        self.store.write_text(json.dumps(ledger));before=self.store.read_bytes()
        callback=unittest.mock.Mock()
        with self.assertRaisesRegex(hr.RegistrationError,'explicitly rebase'):
            hr.register(self.store,directory,card,json.loads((directory/'state.json').read_text()),callback)
        callback.assert_not_called();self.assertEqual(self.store.read_bytes(),before)

    def test_plan_repair_delivery_without_registration_cannot_loop(self):
        def configure(p): p['roles']['architect'].update(tool='claude', model='opus', effort='default')
        self.build(configure)
        s = json.loads(self.store.read_text()); s['plan']['budgets']['R']['limit']=1
        self.store.write_text(json.dumps(s))  # Legacy plan lacking review coverage.
        out = self.next(); cid = out['card']
        card = json.loads((self.store.parent/'records/dispatch'/cid/'card.json').read_text())
        self.assertEqual(card['kind'], 'PLAN_REPAIR')
        self.agent('s1', 'plan-agent', cid, report='结果：VERIFIED\n需登记的操作：not registered\n')
        self.assertEqual(self.next()['do'], 'report'); self.assertEqual(self.next()['do'], 'report')
        self.assertEqual(len(list((self.store.parent/'records/dispatch').glob('*PLAN_REPAIR-*'))), 1)

    def test_plan_repair_registers_approved_plan_before_dispatch(self):
        def configure(p): p['roles']['architect'].update(tool='claude', model='opus', effort='default')
        self.build(configure)
        s = json.loads(self.store.read_text()); s['plan']['budgets']['R']['limit']=1
        self.store.write_text(json.dumps(s)); out = self.next(); cid = out['card']
        directory = self.store.parent/'records/dispatch'/cid; proof = str(self.store.parent/'proof.txt')
        revised = json.loads(json.dumps(s['plan'])); revised['budgets']['R']['limit']=2
        operations = [dict(type='signal', data=dict(id='D2', signal='technical_decision', reason='repair coverage',
                          blocks=['execute'], evidence=[proof])),
            dict(type='resolve', data=dict(id='D2', summary='approved repair', evidence=[proof], user_evidence=[proof],
                 requirements=[dict(id='D2.ready',kind='confirmation',owner='architect',depends_on=[],instruction='plan fixed')])),
            dict(type='plan_update', data=dict(plan=revised, decision_id='D2')),
            dict(type='decision_progress', data=dict(id='D2',status='confirmed',requirement='D2.ready'))]
        (directory/'registration.json').write_text(json.dumps(dict(operations=operations)))
        self.agent('s1', 'plan-agent', cid, report='结果：VERIFIED\n')
        out = self.next(); self.assertEqual(out['do'], 'call')
        ledger = json.loads(self.store.read_text())
        self.assertEqual(ledger['decisions']['D2']['status'], 'closed')
        self.assertEqual(ledger['plan']['budgets']['R']['limit'], 2)
        self.assertEqual(len(list((self.store.parent/'records/dispatch').glob('*PLAN_REPAIR-*'))), 1)

    def test_lead_user_condition_is_asked_without_duplicate_decision_or_auto_approval(self):
        cid, directory = self.handback_fixture()
        (directory/'registration.json').write_text(json.dumps(dict(operations=[self.handback_operations(owner='user')[0]])))
        self.agent('s1', 'decision-agent', cid, report='结果：VERIFIED\n')
        out = self.next(); self.assertEqual(out['do'], 'ask_user')
        self.assertEqual(out['items'][0]['target'], 'D1.ready')
        ledger = json.loads(self.store.read_text()); self.assertNotIn('D1.ready', ledger['state']['confirmations'])
        self.assertEqual(len(list((self.store.parent/'records/dispatch').glob('D1-DECIDE-*'))), 1)

    def test_prepare_resumption_registers_permission_before_executor_continues(self):
        def configure(p): p['roles']['architect'].update(tool='claude', model='opus', effort='default')
        self.build(configure)
        s = json.loads(self.store.read_text()); proof = str(self.store.parent/'proof.txt')
        operations = [('orchestrator', dict(type='start',data=dict(task='execute',
                      call=dict(s['plan']['roles']['executor'],conversation_id='original-executor')))),
            ('executor',dict(type='finish',data=dict(attempt='A0001',result='WAIT_EVIDENCE',evidence=[proof]))),
            ('architect',dict(type='classify_attempt',data=dict(attempt='A0001',kind='environment',work_performed=False,
                                                             reason='startup failed only',evidence=[proof])))]
        for i,(actor,op) in enumerate(operations):
            s,_=flow.apply_request(s,dict(request_id='resume-fixture-'+str(i),expected_revision=s['revision'],actor=actor,operations=[op]),self.store.parent)
        self.store.write_text(json.dumps(s)); out=self.next(); cid=out['card']
        directory=self.store.parent/'records/dispatch'/cid
        self.assertEqual(json.loads((directory/'card.json').read_text())['kind'],'PREPARE_RESUMPTION')
        ready=self.store.parent/'ready.txt';ready.write_text('actual environment readiness verified')
        registration = {'operations': [{'type':'resume_attempt', 'data':
                        {'attempt':'A0001', 'reason':'environment restored', 'evidence':[str(ready)]}}]}
        (directory/'registration.json').write_text(json.dumps(registration))
        self.agent('s1','resume-agent',cid,report='结果：VERIFIED\n')
        out=self.next();self.assertEqual(out['do'],'call')
        ledger=json.loads(self.store.read_text());self.assertEqual(len(ledger['state']['resumptions']),1)
        self.assertEqual(self.attempt('execute')['continuation_of'],'A0001')

    def test_design_sync_registers_refresh_without_repeating_planning(self):
        def configure(p): p['roles']['architect'].update(tool='claude',model='opus',effort='default')
        self.build(configure)
        s=json.loads(self.store.read_text());design=self.task/'design.md';design.write_text('# scope\nold\n')
        s['plan']['tasks']['execute']['instruction']='test action scope'
        s['design']=flow.design_link(dict(path=str(design),version='v1',plan_version=s['plan_version'],sections={'execute':'scope'}),
                                     s['plan'],self.store.parent,s['plan_version'])
        self.store.write_text(json.dumps(s));design.write_text('# scope\ncorrected text\n')
        out=self.next();cid=out['card'];directory=self.store.parent/'records/dispatch'/cid
        self.assertEqual(json.loads((directory/'card.json').read_text())['kind'],'SYNC_DESIGN')
        (directory/'registration.json').write_text(json.dumps(dict(operations=[dict(type='design_refresh',
            data=dict(version='v2',reason='verified textual correction'))])))
        self.agent('s1','sync-agent',cid,report='结果：VERIFIED\n')
        self.assertEqual(self.next()['do'],'call')
        self.assertEqual(json.loads(self.store.read_text())['design']['version'],'v2')
        self.assertEqual(len(list((self.store.parent/'records/dispatch').glob('*SYNC_DESIGN-*'))),1)

    def test_collection_error_does_not_start_any_other_action(self):
        import dispatch_card as dc
        import argparse
        error = dict(collected=[], interrupted=[], running=[], pending=[], problems=[dict(error='bad receipt')])
        with unittest.mock.patch.object(dc, 'reconcile', return_value=error):
            out = dc.next_step(argparse.Namespace(store=str(self.store), runtime=str(self.runtime)))
        self.assertEqual(out['do'], 'report')
        self.assertEqual(json.loads(self.store.read_text())['state']['attempts'], [])

    def test_started_pending_decision_is_not_emitted_after_resolution(self):
        cid, _ = self.handback_fixture()
        s=json.loads(self.store.read_text())
        s,_=flow.apply_request(s,dict(request_id='already-resolved',expected_revision=s['revision'],actor='architect',
            operations=[dict(type='resolve',data=dict(id='D1',summary='already satisfied',requirements=[],clarification=True,
                        evidence=[str(self.store.parent/'proof.txt')],user_evidence=[]))]),self.store.parent)
        self.store.write_text(json.dumps(s))
        out=self.next()
        self.assertEqual(out['do'],'attention');self.assertNotIn('tool',out);self.assertEqual(out['card'],cid)
        self.assertEqual(len(list((self.store.parent/'records/dispatch').glob('D1-DECIDE-*'))),1)

    def test_cli_decision_handback_uses_actual_session_and_registers(self):
        def configure(p): p['roles']['architect'].update(tool='codex',model='gpt-6.1-terra',effort='medium')
        self.build(configure)
        profile=self.runtime/'codex-profile.json';profile.write_text(json.dumps(dict(backend='codex',binary='/bin/codex',version='fake')))
        spec_path=self.runtime/'spec.json';spec=json.loads(spec_path.read_text());spec['profiles'].append(str(profile));spec_path.write_text(json.dumps(spec))
        s=json.loads(self.store.read_text());proof=str(self.store.parent/'proof.txt')
        s,_=flow.apply_request(s,dict(request_id='cli-decision',expected_revision=s['revision'],actor='architect',
            operations=[dict(type='signal',data=dict(id='D1',signal='technical_decision',reason='diagnose',
                        blocks=['execute'],evidence=[proof]))]),self.store.parent);self.store.write_text(json.dumps(s))
        out=self.next();directory=self.store.parent/'records/dispatch'/out['card'];run=directory/'run';run.mkdir()
        (directory/'registration.json').write_text(json.dumps(dict(operations=self.handback_operations())))
        cid='10000000-0000-4000-8000-000000000009'
        (run/'request.json').write_text(json.dumps(dict(backend='codex',conversation=None,task='D1',cwd=str(self.workspace),started_at=flow.now())))
        (run/'receipt.json').write_text(json.dumps(dict(event='CLI_STARTED',pid=12345,task='D1')))
        events=[dict(type='thread.started',thread_id=cid),dict(type='item.completed',item=dict(type='agent_message',text='结果：VERIFIED\n')),dict(type='turn.completed')]
        (run/'stdout').write_text(''.join(json.dumps(x)+'\n' for x in events));(run/'stderr').write_text('')
        (run/'exit.json').write_text(json.dumps(dict(returncode=0,ended_at=flow.now())))
        self.next();ledger=json.loads(self.store.read_text())
        self.assertEqual(ledger['decisions']['D1']['status'],'closed')
        self.assertEqual(ledger['decisions']['D1']['resolution']['context']['conversation_id'],cid)

    def host_stop_fixture(self, closed=False, valid=True, old=False):
        cid,directory=self.handback_fixture();self.agent('s1','stopped-agent',cid)
        project=self.home/'.claude/projects'/__import__('re').sub(r'[^A-Za-z0-9]','-',str(self.workspace))
        at='2000-01-01T00:00:00+00:00' if old else flow.now()
        result=dict(message='Successfully stopped task: stopped-agent ('+cid+')' if valid else 'Task was not stopped',
                    task_id='stopped-agent',task_type='local_agent',command=cid)
        entries=[dict(type='assistant',timestamp=at,message=dict(content=[dict(type='tool_use',id='stop-call',name='TaskStop',input=dict(task_id='stopped-agent'))])),
                 dict(type='user',timestamp=at,message=dict(content=[dict(type='tool_result',tool_use_id='stop-call',content=json.dumps(result))]))]
        (project/'s1.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in entries))
        if closed:
            s=json.loads(self.store.read_text())
            s,_=flow.apply_request(s,dict(request_id='obsolete-decision',expected_revision=s['revision'],actor='architect',
                operations=[dict(type='resolve',data=dict(id='D1',summary='already resolved',requirements=[],clarification=True,
                            evidence=[str(self.store.parent/'proof.txt')],user_evidence=[]))]),self.store.parent)
            self.store.write_text(json.dumps(s))
        return cid,directory

    def test_successful_host_stop_clears_obsolete_running_agent(self):
        cid,directory=self.host_stop_fixture(closed=True)
        out=self.next();st=json.loads((directory/'state.json').read_text())
        self.assertTrue(st['finished']);self.assertEqual(st['result'],'INTERRUPTED')
        self.assertEqual(st['host_stop']['agent'],'stopped-agent')
        self.assertNotIn(cid,[r['card'] for r in out.get('running',[])])
        self.assertEqual(json.loads(self.store.read_text())['decisions']['D1']['status'],'closed')
        self.assertEqual(len(list((self.store.parent/'records/dispatch').glob('D1-DECIDE-*'))),1)

    def test_explicit_host_stop_never_automatically_redispatches_pending_action(self):
        cid,directory=self.host_stop_fixture()
        self.assertEqual(self.next()['do'],'report');self.assertEqual(self.next()['do'],'report')
        error=self.card('make','--store',str(self.store),'--kind','DECIDE','--target','D1','--runtime',str(self.runtime),ok=False)
        self.assertIn('宿主明确停止',error['error'])
        self.assertEqual(len(list((self.store.parent/'records/dispatch').glob('D1-DECIDE-*'))),1)

    def test_failed_or_stale_host_stop_does_not_clear_a_running_agent(self):
        for valid,old in [(False,False),(True,True)]:
            with self.subTest(valid=valid,old=old):
                cid,directory=self.host_stop_fixture(valid=valid,old=old)
                out=self.next();self.assertEqual(out['do'],'wait')
                self.assertIn(cid,[r['card'] for r in out['running']])
                self.assertFalse(json.loads((directory/'state.json').read_text()).get('finished'))
                # Clear this fixture's artifacts before resetting its ledger.
                import shutil
                shutil.rmtree(self.store.parent/'records/dispatch')

    def test_final_handback_is_preserved_even_if_host_stop_is_also_present(self):
        cid,directory=self.host_stop_fixture()
        (directory/'registration.json').write_text(json.dumps(dict(operations=self.handback_operations())))
        self.agent('s1','stopped-agent',cid,report='结果：VERIFIED\n')
        self.next();st=json.loads((directory/'state.json').read_text())
        self.assertEqual(st['result'],'VERIFIED');self.assertNotIn('host_stop',st)
        self.assertEqual(json.loads(self.store.read_text())['decisions']['D1']['status'],'closed')

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
