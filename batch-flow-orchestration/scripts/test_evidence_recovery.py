"""Host-neutral wait recovery; no model or external CLI execution."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import flow
from test_flow import plan


class EvidenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.proof = self.root/'proof.txt'; self.proof.write_text('original failed check')
        self.ready = self.root/'ready.txt'; self.ready.write_text('dependency now ready')
        self.s = None; self.seq = 0
        p = plan()
        p['tasks']['gate'] = dict(kind='confirmation', role='architect', scope=['B'], subjects=['code'],
                                 depends_on=[], consumes=[], instruction='verify board evidence')
        self.apply('architect', 'init', dict(plan=p, simulation=True, task_mode='local',
                   context=dict(conversation_id='planner', mode='fresh', reason='fixture')))
        self.apply('orchestrator', 'authorize', dict(batches=['B'], evidence=[str(self.proof)]))

    def tearDown(self): self.tmp.cleanup()

    def apply(self, actor, kind, data):
        self.seq += 1
        self.s, _ = flow.apply_request(self.s, dict(request_id=str(self.seq), expected_revision=self.s['revision'] if self.s else 0,
                       actor=actor, operations=[dict(type=kind, data=data)]), self.root)

    def start(self, tid):
        task = self.s['plan']['tasks'][tid]
        prev = flow.latest(self.s, tid)
        cid = prev['call']['conversation_id'] if prev and task.get('session_policy') == 'resume' else 'worker-'+str(self.seq)
        self.apply('orchestrator', 'start', dict(task=tid, call=dict(self.s['plan']['roles'][task['role']], conversation_id=cid)))

    def finish(self, tid, result):
        self.apply(self.s['plan']['tasks'][tid]['role'], 'finish', dict(attempt=flow.latest(self.s, tid)['id'], result=result, evidence=[str(self.proof)]))

    def diagnose(self):
        issue = flow.issues(self.s)[0]
        data = flow.issue_signal(self.s, issue['key'], self.root)
        self.apply('orchestrator', 'signal', data)
        return data['id']

    def resolve(self, did, rid='ready'):
        self.apply('architect', 'resolve', dict(id=did, summary='verify recovery before retry',
                  requirements=[dict(id=rid, kind='confirmation', owner='architect', depends_on=[], instruction='check repaired dependency')],
                  evidence=[str(self.proof)], user_evidence=[]))

    def confirm(self, did, rid):
        self.apply('orchestrator', 'decision_progress', dict(id=did, status='confirmed', requirement=rid, evidence=[str(self.ready)]))

    def close(self, did):
        self.apply('orchestrator', 'decision_progress', dict(id=did, status='closed', evidence=[str(self.ready)]))

    def wait(self, owner='architect', blocks=None):
        self.apply('executor', 'signal', dict(id='original', signal='technical_decision', reason='check external condition',
                   blocks=['execute'] if blocks is None else blocks, evidence=[str(self.proof)]))
        self.apply('architect', 'resolve', dict(id='original', summary='condition needed',
                   requirements=[dict(id='original.check', kind='confirmation', owner=owner, depends_on=[], instruction='check external proof')],
                   evidence=[str(self.proof)], user_evidence=[]))
        self.apply('orchestrator', 'decision_progress', dict(id='original', status='waiting', requirement='original.check', evidence=[str(self.proof)]))

    def test_zero_budget_formal_wait_requires_diagnosis_and_structured_recovery(self):
        self.start('gate'); self.finish('gate', 'WAIT_EVIDENCE')
        self.assertEqual(flow.status(self.s)['tasks']['gate'], 'DECISION_BLOCKED')
        self.assertEqual([i['signal'] for i in flow.issues(self.s)], ['technical_decision'])
        before = copy.deepcopy(self.s)
        self.assertEqual(flow.pre_yield(self.s)['user_actions'], [])
        with self.assertRaises(flow.FlowError): self.start('gate')
        self.assertEqual(self.s, before)
        did = self.diagnose()
        self.assertEqual(next(a for a in flow.status(self.s)['actions'] if a['kind'] == 'DECIDE')['role'], 'architect')
        with self.assertRaises(flow.FlowError):
            self.apply('architect', 'resolve', dict(id=did, summary='try unchanged', clarification=True, requirements=[], evidence=[str(self.proof)], user_evidence=[]))
        self.resolve(did)
        with self.assertRaises(flow.FlowError): self.close(did)
        self.confirm(did, 'ready')
        with self.assertRaises(flow.FlowError): self.start('gate')
        self.close(did); self.start('gate')
        self.assertEqual(len(flow.attempts(self.s, 'gate')), 2)
        self.assertEqual(flow.latest(self.s, 'gate')['consumes'], [])
        self.assertEqual(self.s['state']['attempts'][0], before['state']['attempts'][0])

    def test_wait_and_exhausted_budget_have_one_diagnostic(self):
        self.s['plan']['budgets']['E']['limit'] = 1
        self.start('execute'); self.finish('execute', 'WAIT_EVIDENCE')
        self.assertEqual(len(flow.issues(self.s)), 1)
        did = self.diagnose(); self.resolve(did); self.confirm(did, 'ready'); self.close(did)
        # Diagnosis does not manufacture an extra technical round.
        self.assertEqual(flow.used(self.s, 'E'), 1)
        self.assertEqual(flow.issues(self.s)[0]['signal'], 'budget_exhausted')
        with self.assertRaises(flow.FlowError): self.start('execute')

    def test_non_task_wait_is_host_neutral_and_unrelated_progress_never_retries(self):
        self.wait(owner='executor', blocks=[])
        before = copy.deepcopy(self.s['state']['confirmation_waits'])
        self.start('gate'); self.finish('gate', 'VERIFIED')
        for root in [None, self.root]:
            gate = flow.pre_yield(self.s, root=root)
            self.assertNotEqual(gate['disposition'], 'WAIT_USER')
            self.assertEqual(gate['user_actions'], [])
            self.assertNotIn('original.check', [a['target'] for a in gate['actions'] if a['kind'] == 'CONFIRM_CONDITION'])
            self.assertIn('REGISTER_DECISION', [a['kind'] for a in gate['actions']])
        self.assertEqual(self.s['state']['confirmation_waits'], before)
        did = self.diagnose()
        self.assertEqual(self.s['decisions'][did]['blocks'], [])
        self.resolve(did); self.confirm(did, 'ready'); self.close(did)
        self.assertIn('original.check', [a['target'] for a in flow.status(self.s)['actions'] if a['kind'] == 'CONFIRM_CONDITION'])
        self.confirm('original', 'original.check')
        self.assertEqual(flow.waiting_conditions(self.s), {})

    def test_explicit_user_condition_retains_user_wait(self):
        self.wait(owner='user', blocks=[])
        # Remove unrelated ready tasks so disposition reflects just this explicit user condition.
        for task in self.s['plan']['tasks'].values(): task['decision_only'] = True
        gate = flow.pre_yield(self.s)
        self.assertEqual(flow.issues(self.s), [])
        self.assertEqual(gate['disposition'], 'WAIT_USER')
        self.assertEqual(gate['user_actions'][0]['role'], 'user')

    def test_empty_blocks_visible_to_architect_without_task_authority(self):
        self.wait(blocks=[])
        self.assertIn('REGISTER_DECISION', [a['kind'] for a in flow.pre_yield(self.s, 'architect')['actions']])
        did = self.diagnose()
        self.assertIn(did, [a['target'] for a in flow.pre_yield(self.s, 'architect')['actions']])
        with self.assertRaises(flow.FlowError):
            self.apply('architect', 'decision_progress', dict(id='original', status='confirmed', requirement='original.check', evidence=[str(self.ready)]))

    def test_wait_validator_and_changed_raw_evidence_fail_closed(self):
        self.wait(blocks=[])
        for field, value in [('requirement', 'missing'), ('source_key', 'invented'), ('evidence_hashes', {})]:
            broken = copy.deepcopy(self.s); broken['state']['confirmation_waits'][0][field] = value
            with self.assertRaises(flow.FlowError): flow.validate_store(broken)
        self.proof.write_text('overwritten result')
        with self.assertRaises(flow.FlowError): flow.issue_signal(self.s, flow.issues(self.s)[0]['key'], self.root)


if __name__ == '__main__': unittest.main()
