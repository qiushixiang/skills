#!/usr/bin/env python3
"""Regression checks from independent maintenance review; no live host calls."""
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import flow
from test_flow import plan


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proof = self.root / 'proof.txt'
        self.proof.write_text('immutable test fixture evidence')
        self.state = None
        self.counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, actor, kind, data):
        self.counter += 1
        request = dict(request_id=str(self.counter), expected_revision=self.state['revision'] if self.state else 0,
                       actor=actor, operations=[dict(type=kind, data=data)])
        self.state, _ = flow.apply_request(self.state, request, self.root)

    def init(self, linked=False):
        p = plan()
        del p['tasks']['third']
        del p['roles']['third_party']
        del p['roles']['orchestrator']
        p['tasks']['done'].update(role='architect', depends_on=['regular'])
        for task in p['tasks'].values():
            task['dispatch_owner'] = 'architect'
        data = dict(plan=p, simulation=True, task_mode='batched' if linked else 'local', context=dict(conversation_id='planner', mode='fresh', reason='planning'))
        if linked:
            self.document = self.root / 'design.md'
            self.document.write_text('# Implementation\nOriginal plan\n')
            p['tasks']['execute']['instruction'] = 'Implementation; implement and verify scoped behavior'
            data['design'] = dict(path='design.md', version='d1', plan_version=1, sections={'execute': 'Implementation'})
        self.apply('architect', 'init', data)
        self.apply('architect', 'authorize', dict(batches=['B'], evidence=[str(self.proof)]))

    def start(self, tid, context=None):
        task = self.state['plan']['tasks'][tid]
        self.apply(task.get('dispatch_owner', 'orchestrator'), 'start', dict(
            task=tid, call=dict(self.state['plan']['roles'][task['role']], conversation_id=context or tid+'-context')))

    def finish(self, tid):
        task = self.state['plan']['tasks'][tid]
        self.apply(task['role'], 'finish', dict(attempt=flow.latest(self.state, tid)['id'],
                   result=flow.success_result(task), evidence=[str(self.proof)]))

    def test_local_loop_with_native_receipt_and_decision_closure(self):
        self.init()
        self.start('execute')
        due = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)).isoformat()
        self.apply('architect', 'register_dispatch', dict(attempt=flow.latest(self.state, 'execute')['id'],
                   handle='fixture-job', timer_kind='native', timer_id='fixture-timer',
                   owner_conversation_id='planner', due_at=due, evidence=[str(self.proof)]))
        self.assertEqual(flow.pre_yield(self.state, 'architect')['disposition'], 'WAIT_HOST')
        self.finish('execute')
        with self.assertRaises(flow.FlowError):
            self.start('regular', 'planner')
        self.start('regular')
        self.finish('regular')
        self.apply('architect', 'signal', dict(id='D', signal='confirmation', reason='fixture', blocks=['done'], evidence=[str(self.proof)]))
        self.apply('architect', 'resolve', dict(id='D', summary='confirm recovery', requirements=[dict(
                   id='C', kind='confirmation', owner='architect', depends_on=[], instruction='confirm')],
                   evidence=[str(self.proof)], user_evidence=[], context=dict(conversation_id='planner',
                   mode='resume', source_conversation_id='planner', reason='same task')))
        for status in ['confirmed', 'applied', 'closed']:
            data = dict(id='D', status=status, evidence=[str(self.proof)])
            if status == 'confirmed':
                data['requirement'] = 'C'
            self.apply('architect', 'decision_progress', data)
        self.start('done', 'planner')
        self.finish('done')
        self.assertTrue(flow.status(self.state)['complete'])
        self.assertEqual(flow.pre_yield(self.state, 'architect')['disposition'], 'COMPLETE')
        self.assertEqual(flow.used(self.state, 'E'), 1)
        self.assertEqual(flow.used(self.state, 'R'), 1)

    def test_design_refresh_cli_preserves_plan_and_excludes_editor_from_review(self):
        self.init(linked=True)
        self.start('execute')
        self.finish('execute')
        self.document.write_text('# Implementation\nCorrected spelling\n')
        path = self.root / 'flow.json'
        path.write_text(json.dumps(self.state))
        data = dict(version='d1.1', reason='spelling only', context=dict(conversation_id='editor', mode='fresh', reason='correction'))
        result = subprocess.run([sys.executable, flow.__file__, 'design_refresh', '--store', str(path),
            '--actor', 'architect', '--revision', str(self.state['revision']), '--data', '-'],
            input=json.dumps(data), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.state = json.loads(path.read_text())
        self.assertEqual(self.state['plan_version'], 1)
        self.assertTrue(flow.design_current(self.state))
        with self.assertRaises(flow.FlowError):
            self.start('regular', 'editor')
        self.start('regular', 'independent-review')

    def test_incomplete_script_receipt_and_expired_native_receipt_are_rejected(self):
        self.init()
        self.start('execute')
        base = dict(attempt=flow.latest(self.state, 'execute')['id'], handle='work', evidence=[str(self.proof)])
        with self.assertRaisesRegex(flow.FlowError, 'timer_ticket'):
            self.apply('architect', 'register_dispatch', dict(base, timer_handle='timer'))
        expired = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(flow.FlowError, 'already due'):
            self.apply('architect', 'register_dispatch', dict(base, timer_kind='native',
                timer_id='timer', owner_conversation_id='planner', due_at=expired))
        self.assertEqual(flow.pre_yield(self.state, 'architect')['disposition'], 'CONTINUE')

    def test_design_drift_blocks_downstream_but_not_unrelated_work(self):
        self.init(linked=True)
        self.start('execute')
        self.finish('execute')
        task = copy.deepcopy(self.state['plan']['tasks']['done'])
        task.update(kind='verify', depends_on=[], subjects=[])
        self.state['plan']['tasks']['unrelated'] = task
        self.document.write_text('# Implementation\nUnregistered change\n')
        status = flow.status(self.state)
        self.assertEqual(status['tasks']['regular'], 'WAIT_DESIGN_SYNC')
        self.assertEqual(status['tasks']['unrelated'], 'READY')
        with self.assertRaises(flow.FlowError):
            self.start('regular')
        self.start('unrelated')
        self.finish('unrelated')

    def add_global_work(self):
        p = self.state['plan']
        p['roles']['orchestrator'] = dict(p['roles']['architect'])
        p['batches']['GLOBAL'] = dict(name='global', completion_task='global')
        p['tasks']['global'] = dict(kind='verify', role='orchestrator', scope=['GLOBAL'],
            subjects=[], depends_on=[], consumes=[], instruction='global work')
        self.apply('orchestrator', 'authorize', dict(batches=['B', 'GLOBAL'], evidence=[str(self.proof)]))

    def test_local_completion_ignores_inactive_recovery_and_global_work(self):
        self.init()
        self.add_global_work()
        for tid in ['execute', 'regular', 'done']:
            self.start(tid)
            self.finish(tid)
        self.assertFalse(flow.status(self.state)['complete'])
        self.assertEqual(flow.status(self.state)['tasks']['recovery'], 'INACTIVE')
        self.assertEqual(flow.pre_yield(self.state, 'architect')['disposition'], 'COMPLETE')
        self.assertEqual(flow.pre_yield(self.state)['disposition'], 'CONTINUE')

    def test_external_dependency_without_job_is_not_wait_host(self):
        self.init()
        self.add_global_work()
        self.state['plan']['tasks']['execute']['depends_on'] = ['global']
        self.assertEqual(flow.pre_yield(self.state, 'architect')['disposition'], 'STALLED')
        self.assertFalse(flow.pre_yield(self.state, 'architect')['can_yield'])

    def test_plan_registered_before_models_waits_until_user_assigns(self):
        p = plan()
        del p['tasks']['third']
        for role in ['third_party', 'orchestrator', 'executor', 'reviewer']:
            del p['roles'][role]
        p['tasks']['done'].update(role='architect', depends_on=['regular'])
        p['budgets'].update(BK={'limit': 1}, LD={'limit': 1})
        p['budgets']['R']['limit']=4
        p['tasks']['execute']['handoff'] = dict(backup_budget='BK', lead_budget='LD')
        for task in p['tasks'].values():
            task['dispatch_owner'] = 'architect'
        flow.validate_plan(p)
        self.apply('architect', 'init', dict(plan=p, simulation=True, task_mode='local',
                   context=dict(conversation_id='planner', mode='fresh', reason='plan first, models later')))
        self.apply('architect', 'authorize', dict(batches=['B'], evidence=[str(self.proof)]))
        report = flow.status(self.state)
        self.assertEqual(report['tasks']['execute'], 'WAIT_MODELS')
        action = next(a for a in report['actions'] if a['target'] == 'execute')
        self.assertEqual((action['kind'], action['missing']), ('ASSIGN_MODELS', {'role': 'executor'}))
        self.assertEqual(action['orchestrator_action']['mode'],'register')
        self.assertEqual(flow.pre_yield(self.state, 'architect')['disposition'], 'CONTINUE')
        self.assertEqual(report['unassigned_models'], {'roles': ['executor', 'reviewer'], 'backups': ['execute']})
        call = dict(tool='t', model='m', effort='e', conversation_id='exec-1')
        with self.assertRaisesRegex(flow.FlowError, 'model not assigned'):
            self.apply('architect', 'start', dict(task='execute', call=call))
        with self.assertRaisesRegex(flow.FlowError, 'first assignment'):
            self.apply('orchestrator', 'set_models', dict(roles={'executor': {'model': 'm'}},
                       backups={}, reason='partial', user_evidence=[str(self.proof)]))
        with self.assertRaisesRegex(flow.FlowError, 'not used by the plan'):
            self.apply('orchestrator', 'set_models', dict(roles={'third_party': {'tool': 't', 'model': 'm', 'effort': 'e'}},
                       backups={}, reason='unused', user_evidence=[str(self.proof)]))
        version = self.state['plan_version']
        self.apply('orchestrator', 'set_models', dict(roles={'executor': dict(tool='t', model='m', effort='e'),
                   'reviewer': dict(tool='t', model='r', effort='e')}, backups={'execute': dict(tool='t', model='b', effort='e')},
                   reason='user assigned models', user_evidence=[str(self.proof)]))
        self.assertEqual(self.state['plan_version'], version + 1)
        self.assertEqual(flow.status(self.state)['tasks']['execute'], 'READY')
        self.assertEqual(flow.status(self.state)['unassigned_models'], {'roles': [], 'backups': []})
        self.start('execute')
        self.finish('execute')
        self.start('regular')
        self.assertEqual(self.state['state']['attempts'][-1]['performer_config']['model'], 'r')

    def test_owner_cannot_change_until_running_attempt_finishes(self):
        self.init(); self.start('execute')
        self.apply('architect','signal',dict(id='D',signal='replan',reason='fixture',blocks=['done'],evidence=[str(self.proof)]))
        old=copy.deepcopy(self.state); p=copy.deepcopy(old['plan'])
        p['roles']['orchestrator']=dict(p['roles']['architect'])
        p['tasks']['execute'].pop('dispatch_owner')
        context=dict(conversation_id='planner',mode='resume',source_conversation_id='planner',reason='replan')
        with self.assertRaisesRegex(flow.FlowError,'running task owner'):
            self.apply('architect','plan_update',dict(plan=p,decision_id='D',context=context))
        self.assertEqual(self.state,old)
        due=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=10)).isoformat()
        self.apply('architect','register_dispatch',dict(attempt=flow.latest(self.state,'execute')['id'],handle='job',timer_kind='native',timer_id='timer',owner_conversation_id='planner',due_at=due,evidence=[str(self.proof)]))
        self.assertIn('execute',flow.pre_yield(self.state,'architect')['running'])
        self.finish('execute'); before=flow.used(self.state,'E')
        self.apply('architect','plan_update',dict(plan=p,decision_id='D',context=context))
        self.assertEqual(self.state['plan']['tasks']['execute'].get('dispatch_owner','orchestrator'),'orchestrator')
        self.assertEqual(flow.used(self.state,'E'),before)
        self.assertEqual(flow.latest(self.state,'execute')['dispatch_owner'],'architect')

    def test_legacy_inconsistent_running_owner_is_visible_and_correctable(self):
        self.init(); self.start('execute')
        self.state['plan']['tasks']['execute']['dispatch_owner']='orchestrator'
        self.assertIn('execute',flow.pre_yield(self.state,'architect')['running'])
        self.apply('architect','signal',dict(id='D',signal='replan',reason='fixture',blocks=['done'],evidence=[str(self.proof)]))
        p=copy.deepcopy(self.state['plan']); p['tasks']['execute']['dispatch_owner']='architect'
        self.apply('architect','plan_update',dict(plan=p,decision_id='D',context=dict(conversation_id='planner',mode='resume',source_conversation_id='planner',reason='repair owner')))
        self.assertEqual(flow.latest(self.state,'execute')['dispatch_owner'],'architect')

    def test_local_expansion_requires_design_association(self):
        self.init(); self.apply('architect','signal',dict(id='D',signal='replan',reason='fixture',blocks=['done'],evidence=[str(self.proof)]))
        p=copy.deepcopy(self.state['plan']);p['batches']['B2']=dict(name='second',completion_task='done')
        with self.assertRaisesRegex(flow.FlowError,'requires design'):
            self.apply('architect','plan_update',dict(plan=p,decision_id='D',context=dict(conversation_id='planner',mode='resume',source_conversation_id='planner',reason='expansion')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
