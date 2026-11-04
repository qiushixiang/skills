#!/usr/bin/env python3
"""Budget pairing contracts: admission, shared pools, and dispatch without review capacity."""
import copy
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import flow
from test_flow import plan


class ReviewBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.proof=self.root/'proof.md';self.proof.write_text('fixture evidence and approval')
        self.s=None;self.counter=0

    def tearDown(self):self.tmp.cleanup()

    def apply(self, actor, kind, data):
        self.counter+=1
        self.s,_=flow.apply_request(self.s,dict(request_id=str(self.counter),expected_revision=self.s['revision'] if self.s else 0,
            actor=actor,operations=[dict(type=kind,data=data)]),self.root)

    def paired(self, primary=2, review=4):
        p=plan();del p['tasks']['third'];p['tasks']['done']['depends_on']=['regular']
        p['budgets']['E']['limit']=primary;p['budgets']['R']['limit']=review
        p['budgets'].update(BK=dict(limit=1),LD=dict(limit=1))
        p['tasks']['execute']['handoff']=dict(backup_budget='BK',lead_budget='LD')
        p['tasks']['regular']['repair_tasks']=['execute']
        return p

    def init(self,p):
        self.apply('architect','init',dict(plan=p,simulation=True,task_mode='local',context=dict(conversation_id='planner',mode='fresh',reason='fixture')))
        self.apply('orchestrator','authorize',dict(batches=['B'],evidence=[str(self.proof)]))

    def finish_task(self,tid,result):
        task=self.s['plan']['tasks'][tid]
        self.apply('orchestrator','start',dict(task=tid,call=dict(self.s['plan']['roles'][task['role']],conversation_id=tid+'-context')))
        self.apply(task['role'],'finish',dict(attempt=self.s['state']['attempts'][-1]['id'],result=result,evidence=[str(self.proof)]))

    def test_full_chain_is_required_at_init(self):
        with self.assertRaisesRegex(flow.FlowError,'review budget coverage'):
            self.init(self.paired(primary=3,review=2))
        self.assertIsNone(self.s)
        self.init(self.paired(primary=3,review=5))
        self.assertEqual(flow.status(self.s)['review_budget_coverage']['required_by_budget']['R'],5)

    def test_shipped_init_example_and_indirect_third_party_pairing(self):
        doc=(Path(flow.__file__).parent.parent/'references/init-example.md').read_text()
        example=json.loads(re.search(r'```json\n(.*?)\n```',doc,re.S).group(1))
        flow.validate_plan(example['plan'])
        p=self.paired();p['tasks']['third']=dict(kind='third_party',role='third_party',scope=['B'],subjects=['code'],depends_on=['regular'],consumes=['T'],instruction='independent milestone review')
        p['budgets']['T']['limit']=1
        with self.assertRaisesRegex(flow.FlowError,'review budget coverage'):flow.validate_plan(p)
        p['budgets']['T']['limit']=4;flow.validate_plan(p)

    def test_update_rejects_shortfall_atomically_even_with_user_budget_evidence(self):
        self.init(self.paired())
        self.apply('executor','signal',dict(id='D',signal='replan',reason='fixture',blocks=[],evidence=[str(self.proof)]))
        self.apply('architect','resolve',dict(id='D',summary='fixture approval',requirements=[dict(id='applied',kind='confirmation',owner='architect',depends_on=[],instruction='confirm plan')],evidence=[str(self.proof)],user_evidence=[str(self.proof)]))
        before=copy.deepcopy(self.s);p=copy.deepcopy(self.s['plan']);p['budgets']['R']['limit']=3
        with self.assertRaisesRegex(flow.FlowError,'review budget coverage'):
            self.apply('architect','plan_update',dict(plan=p,decision_id='D',context=dict(conversation_id='planner',mode='fresh',reason='fixture')))
        self.assertEqual(self.s,before)

    def test_new_budget_or_rebinding_cannot_bypass_user_approval(self):
        self.init(self.paired())
        self.apply('executor','signal',dict(id='D',signal='replan',reason='fixture',blocks=[],evidence=[str(self.proof)]))
        self.apply('architect','resolve',dict(id='D',summary='technical recommendation only',requirements=[dict(id='applied',kind='confirmation',owner='architect',depends_on=[],instruction='confirm plan')],evidence=[str(self.proof)],user_evidence=[]))
        before=copy.deepcopy(self.s);p=copy.deepcopy(self.s['plan'])
        p['budgets']['NEW_R']=dict(limit=4);p['tasks']['regular']['consumes']=['NEW_R']
        with self.assertRaisesRegex(flow.FlowError,'referenced user decision'):
            self.apply('architect','plan_update',dict(plan=p,decision_id='D',context=dict(conversation_id='planner',mode='fresh',reason='fixture')))
        self.assertEqual(self.s,before)

    def approval_decision(self, dependency=False, header=False):
        self.init(self.paired())
        self.apply('executor','signal',dict(id='D',signal='budget_exhausted',reason='fixture',blocks=['regular'],evidence=[str(self.proof)]))
        requirements=[]
        if dependency:requirements.append(dict(id='pre',kind='confirmation',owner='architect',depends_on=[],instruction='verify prerequisite'))
        requirements.append(dict(id='approve',kind='confirmation',owner='user',depends_on=['pre'] if dependency else [],instruction='approve R=5'))
        self.apply('architect','resolve',dict(id='D',summary='recommend budget change',requirements=requirements,evidence=[str(self.proof)],user_evidence=[str(self.proof)] if header else []))

    def confirm_budget(self, **extra):
        self.apply('orchestrator','decision_progress',dict(id='D',status='confirmed',requirement='approve',evidence=[str(self.proof)],**extra))

    def apply_approved_budget(self, **limits):
        p=copy.deepcopy(self.s['plan'])
        for bid,limit in dict(R=5,**limits).items():p['budgets'][bid]['limit']=limit
        self.apply('architect','plan_update',dict(plan=p,decision_id='D',context=dict(conversation_id='planner',mode='fresh',reason='apply approved change')))

    def test_later_user_confirmation_authorizes_without_rewriting_resolution(self):
        self.approval_decision();original=copy.deepcopy(self.s['decisions']['D']['resolution'])
        self.confirm_budget(budget_approval=dict(limits=dict(R=5)))
        self.apply_approved_budget()
        self.assertEqual(self.s['plan']['budgets']['R']['limit'],5)
        self.assertEqual(self.s['decisions']['D']['resolution'],original)
        self.assertEqual(self.s['state']['plan_changes'][-1]['budget_authorization_sources'],['approve'])

    def test_untyped_or_out_of_scope_user_confirmation_does_not_authorize(self):
        self.approval_decision();self.confirm_budget()
        with self.assertRaisesRegex(flow.FlowError,'referenced user decision'):self.apply_approved_budget()
        self.apply('orchestrator','decision_progress',dict(id='D',status='bind_budget_approval',requirement='approve',evidence=[str(self.proof)],budget_approval=dict(limits=dict(R=5))))
        with self.assertRaisesRegex(flow.FlowError,'referenced user decision'):self.apply_approved_budget(E=3)
        self.apply_approved_budget()

    def test_confirmation_prerequisites_and_decision_association_are_checked(self):
        self.approval_decision(dependency=True);self.confirm_budget(budget_approval=dict(limits=dict(R=5)))
        with self.assertRaisesRegex(flow.FlowError,'referenced user decision'):self.apply_approved_budget()
        self.apply('orchestrator','decision_progress',dict(id='D',status='confirmed',requirement='pre',evidence=[str(self.proof)]))
        self.s['state']['confirmations']['approve']['decision']='OTHER'
        with self.assertRaisesRegex(flow.FlowError,'referenced user decision'):self.apply_approved_budget()
        self.s['state']['confirmations']['approve']['decision']='D'
        self.apply_approved_budget()

    def test_changed_or_missing_approval_evidence_cannot_authorize(self):
        self.approval_decision();self.confirm_budget(budget_approval=dict(limits=dict(R=5)))
        before=copy.deepcopy(self.s)
        self.proof.write_text('changed evidence')
        with self.assertRaisesRegex(flow.FlowError,'referenced user decision'):self.apply_approved_budget()
        self.assertEqual(self.s,before)
        self.proof.unlink()
        with self.assertRaisesRegex(flow.FlowError,'referenced user decision'):self.apply_approved_budget()

    def test_binding_legacy_confirmation_reuses_same_evidence_and_is_immutable(self):
        self.approval_decision();self.confirm_budget()
        old_evidence=copy.deepcopy(self.s['state']['confirmations']['approve']['evidence'])
        original=copy.deepcopy(self.s['decisions']['D']['resolution'])
        binding=dict(id='D',status='bind_budget_approval',requirement='approve',evidence=[str(self.proof)],budget_approval=dict(limits=dict(R=5)))
        self.apply('orchestrator','decision_progress',binding)
        self.assertEqual(self.s['state']['confirmations']['approve']['evidence'],old_evidence)
        self.assertEqual(self.s['decisions']['D']['resolution'],original)
        with self.assertRaisesRegex(flow.FlowError,'already bound'):self.apply('orchestrator','decision_progress',binding)
        with self.assertRaisesRegex(flow.FlowError,'immutable'):self.confirm_budget()
        self.apply_approved_budget()

    def test_role_confirmation_and_unrelated_evidence_cannot_be_bound(self):
        self.approval_decision(dependency=True)
        with self.assertRaisesRegex(flow.FlowError,'user confirmation'):
            self.apply('orchestrator','decision_progress',dict(id='D',status='confirmed',requirement='pre',evidence=[str(self.proof)],budget_approval=dict(limits=dict(R=5))))
        self.confirm_budget();other=self.root/'other.md';other.write_text('unrelated')
        with self.assertRaisesRegex(flow.FlowError,'same intact original evidence'):
            self.apply('orchestrator','decision_progress',dict(id='D',status='bind_budget_approval',requirement='approve',evidence=[str(other)],budget_approval=dict(limits=dict(R=5))))

    def test_original_resolution_approval_is_compatible_but_hash_checked(self):
        self.approval_decision(header=True)
        self.proof.write_text('tampered original user approval')
        with self.assertRaisesRegex(flow.FlowError,'referenced user decision'):self.apply_approved_budget()
        self.proof.write_text('fixture evidence and approval')
        self.apply_approved_budget()
        self.assertEqual(self.s['state']['plan_changes'][-1]['budget_authorization_sources'],['resolution.user_evidence'])

    def test_confirmation_scope_can_authorize_new_pool_and_explicit_binding(self):
        self.approval_decision()
        self.confirm_budget(budget_approval=dict(limits=dict(NEW_R=4),tasks=dict(regular=dict(consumes=['NEW_R']))))
        p=copy.deepcopy(self.s['plan']);p['budgets']['NEW_R']=dict(limit=4);p['tasks']['regular']['consumes']=['NEW_R']
        self.apply('architect','plan_update',dict(plan=p,decision_id='D',context=dict(conversation_id='planner',mode='fresh',reason='apply approved new pool')))
        self.assertEqual(self.s['plan']['tasks']['regular']['consumes'],['NEW_R'])
        self.assertEqual(self.s['decisions']['D']['resolution']['user_evidence'],{})

    def test_joint_review_counts_shared_reserves_once_and_individual_repair_sequences(self):
        p=self.paired(primary=3,review=6)
        p['budgets']['D']=dict(limit=2)
        p['tasks']['design']=dict(p['tasks']['execute'],kind='design',consumes=['D'])
        p['tasks']['execute']['depends_on']=['design']
        p['tasks']['regular'].update(depends_on=['design','execute'],repair_tasks=['design','execute'])
        flow.validate_plan(p)
        report=flow.review_budget_coverage(p)
        self.assertEqual(report['required_by_budget']['R'],6)  # 1+(2-1)+(3-1)+1+1
        self.assertEqual(report['reviews'][0]['shared_reserves'],['BK','LD'])

    def test_identical_primary_pools_are_not_counted_twice(self):
        p=self.paired(primary=3,review=4)
        p['tasks']['second']=copy.deepcopy(p['tasks']['execute'])
        p['tasks']['regular'].update(depends_on=['execute','second'],repair_tasks=['execute','second'])
        self.assertEqual(flow.review_budget_coverage(p)['required_by_budget']['R'],4)

    def test_multiple_consumes_use_the_tightest_primary_limit(self):
        p=self.paired();p['budgets']['OTHER']=dict(limit=9)
        p['tasks']['execute']['consumes'].append('OTHER')
        self.assertEqual(flow.review_budget_coverage(p)['required_by_budget']['R'],4)

    def test_shared_review_pool_reserves_each_independent_review(self):
        p=self.paired(review=7)
        p['tasks']['other_review']=copy.deepcopy(p['tasks']['regular'])
        with self.assertRaisesRegex(flow.FlowError,'review budget coverage'):flow.validate_plan(p)
        p['budgets']['R']['limit']=8;flow.validate_plan(p)

    def test_handoff_without_a_review_and_mixed_pools_are_rejected(self):
        p=self.paired();p['tasks']['regular'].update(depends_on=[],repair_tasks=[])
        with self.assertRaisesRegex(flow.FlowError,'missing_handoff_review'):flow.validate_plan(p)
        p=self.paired();p['tasks']['execute']['consumes'].append('R')
        with self.assertRaisesRegex(flow.FlowError,'mixed_implementation_review_budget'):flow.validate_plan(p)

    def test_legacy_shortfall_prevents_repair_before_spending_any_executor_budget(self):
        self.init(self.paired());self.finish_task('execute','VERIFIED');self.finish_task('regular','FIX_REQUIRED')
        self.s['plan']['budgets']['R']['limit']=1  # Legacy persisted plan, not a production mutation.
        before=copy.deepcopy(self.s);report=flow.status(self.s)
        self.assertEqual(report['tasks']['execute'],'WAIT_REVIEW_BUDGET')
        self.assertEqual(self.s,before)
        action=next(a for a in report['actions'] if a['kind']=='PLAN_REPAIR' and a['target']=='execute')
        self.assertEqual(action['subagent_payload']['review_budget_blockers'][0]['budgets'],['R'])
        with self.assertRaisesRegex(flow.FlowError,'not READY'):
            self.finish_task('execute','VERIFIED')
        self.assertEqual(self.s,before)
        self.assertEqual(flow.used(self.s,'BK'),0)
        self.assertEqual(flow.used(self.s,'LD'),0)

    def test_validate_reports_legacy_gap_without_writing(self):
        self.init(self.paired());self.s['plan']['budgets']['R']['limit']=2
        path=self.root/'flow.json';flow.write_atomic(path,self.s);before=path.read_bytes()
        result=subprocess.run([sys.executable,str(Path(flow.__file__)),'validate','--store',str(path)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertFalse(json.loads(result.stdout)['review_budget_coverage']['ok'])
        self.assertEqual(path.read_bytes(),before)

    def test_legacy_plan_is_blocked_even_before_review_budget_is_exhausted(self):
        self.init(self.paired())
        self.s['plan']['budgets']['R']['limit']=2
        report=flow.status(self.s)
        self.assertEqual(flow.used(self.s,'R'),0)
        self.assertEqual(report['tasks']['execute'],'WAIT_REVIEW_BUDGET')

    def test_completed_historical_shortfall_does_not_require_retroactive_budget(self):
        self.init(self.paired());self.finish_task('execute','VERIFIED');self.finish_task('regular','RELEASE');self.finish_task('done','VERIFIED')
        self.s['plan']['budgets']['R']['limit']=1
        report=flow.active_review_budget_coverage(self.s)
        self.assertTrue(report['ok'])
        self.assertEqual(report['required_by_budget']['R'],1)
        self.assertIn('regular',report['excluded_completed_tasks'])
        p=copy.deepcopy(self.s['plan']);p['budgets']['NE']=dict(limit=2)
        p['batches']['NEW']=dict(name='next',completion_task='new_done')
        p['tasks']['new_execute']=dict(copy.deepcopy(p['tasks']['execute']),scope=['NEW'],consumes=['NE'])
        p['tasks']['new_review']=dict(copy.deepcopy(p['tasks']['regular']),scope=['NEW'],depends_on=['new_execute'],repair_tasks=['new_execute'])
        p['tasks']['new_done']=dict(copy.deepcopy(p['tasks']['done']),scope=['NEW'],depends_on=['new_review'])
        p['budgets']['R']['limit']=4
        self.assertEqual(flow.active_review_budget_coverage(self.s,p)['required_by_budget']['R'],5)
        self.assertFalse(flow.active_review_budget_coverage(self.s,p)['ok'])
        p['budgets']['R']['limit']=5
        self.assertTrue(flow.active_review_budget_coverage(self.s,p)['ok'])

    def unlaunched_redundant_start(self):
        self.init(self.paired());self.finish_task('execute','VERIFIED')
        source=self.s['state']['attempts'][-1]['id']
        self.proof.write_text('a mutable delivery was changed after acceptance')
        self.apply('orchestrator','start',dict(task='execute',call=dict(self.s['plan']['roles']['executor'],conversation_id='pending:never-launched'),replacement_reason='stale evidence triggered redundant dispatch'))
        target=self.s['state']['attempts'][-1]['id']
        self.proof.write_text('fixture evidence and approval')
        proof=self.root/'hook_denied.json';proof.write_text('{"launched":false,"reason":"pre-tool hook rejected command"}')
        return dict(attempt=target,kind='withdrawn',work_performed=False,source_attempt=source,
                    reason='allocation never dispatched; restore previous intact delivery',evidence=[str(proof)])

    def test_withdrawn_start_preserves_history_and_prior_delivery_without_spending(self):
        data=self.unlaunched_redundant_start();raw=copy.deepcopy(self.s['state']['attempts'])
        self.assertEqual(flow.used(self.s,'E'),2)
        self.apply('architect','classify_attempt',data)
        self.assertEqual(self.s['state']['attempts'],raw)
        self.assertEqual(flow.latest(self.s,'execute')['id'],data['source_attempt'])
        self.assertEqual(flow.used(self.s,'E'),1)
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'SATISFIED')
        metrics=flow.tracking.metrics(self.s,flow.status(self.s),flow.now())
        self.assertEqual(metrics['totals']['withdrawn_starts'],1)
        self.assertEqual(metrics['totals']['calls'],1)
        self.assertFalse(next(a for a in metrics['attempts'] if a['attempt']==data['attempt'])['chargeable'])
        with self.assertRaisesRegex(flow.FlowError,'withdrawn'):
            self.apply('executor','finish',dict(attempt=data['attempt'],result='INVALID',evidence=data['evidence']))
        with self.assertRaisesRegex(flow.FlowError,'withdrawn'):
            self.apply('orchestrator','register_dispatch',dict(attempt=data['attempt'],handle='late-call',evidence=data['evidence']))

    def test_withdrawal_rejects_dispatched_actual_or_changed_deliveries(self):
        data=self.unlaunched_redundant_start();original=copy.deepcopy(self.s)
        for patch in [dict(dispatch={'handle':'real-job'}),dict(call=dict(self.s['state']['attempts'][-1]['call'],conversation_id='actual-session')),dict(result='FAILED')]:
            self.s=copy.deepcopy(original);self.s['state']['attempts'][-1].update(patch)
            with self.assertRaises(flow.FlowError):self.apply('architect','classify_attempt',data)
        self.s=copy.deepcopy(original);self.proof.write_text('changed again')
        with self.assertRaisesRegex(flow.FlowError,'prior delivery'):
            self.apply('architect','classify_attempt',data)


if __name__=='__main__':unittest.main(verbosity=2)
