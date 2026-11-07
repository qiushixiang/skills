#!/usr/bin/env python3
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock
import flow


def plan():
    roles={r:{'tool':'agy','model':'gemini-3.8-flash-medium','effort':'medium'} for r in flow.ROLES}
    def task(kind,role,deps=(),budgets=(),**extra):
        return dict(kind=kind,role=role,scope=['B'],subjects=['code'],depends_on=list(deps),consumes=list(budgets),instruction='test action',**extra)
    return {'name':'test','roles':roles,'batches':{'B':{'name':'batch','completion_task':'done'}},'subjects':{'code':'v1'},'budgets':{'E':{'limit':2},'R':{'limit':2},'T':{'limit':2}},'tasks':{
        'execute':task('execute','executor',budgets=['E'],session_policy='resume'),
        'regular':task('review','reviewer',['execute'],['R'],session_policy='resume'),
        'third':task('third_party','third_party',['execute'],['T'],session_policy='independent'),
        'done':task('acceptance','orchestrator',['regular','third']),
        'recovery':task('review','reviewer',decision_only=True),
    },'decision_points':{'contract':{'signal':'contract_change','owner':'architect','blocks':['done'],'instruction':'decide contract'}},'feedback_minutes':{'execute':30}}

class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.e=self.root/'proof.txt';self.e.write_text('proof')
        self.s=None;self.counter=0
        self.apply('architect','init',{'plan':plan(),'simulation':True,'task_mode':'local','context':{'conversation_id':'initial-planner','mode':'fresh','reason':'fixture planning'}})
        self.apply('orchestrator','authorize',{'batches':['B'],'evidence':[str(self.e)]})
    def tearDown(self):self.tmp.cleanup()
    def apply(self,actor,kind,data,**kw):
        self.counter+=1
        req={'request_id':str(self.counter),'expected_revision':self.s['revision'] if self.s else 0,'actor':actor,'operations':[{'type':kind,'data':data}],**kw}
        self.s,_=flow.apply_request(self.s,req,self.root);return self.s
    def start(self,tid,**extra):
        t=self.s['plan']['tasks'][tid];prev=flow.latest(self.s,tid)
        cid=prev['call']['conversation_id'] if prev and t.get('session_policy')=='resume' else f'context-{self.counter}'
        self.apply('orchestrator','start',{'task':tid,'call':dict(self.s['plan']['roles'][t['role']],conversation_id=cid),**extra})
        return self.s['state']['attempts'][-1]['id']
    def finish(self,tid,result=None,**extra):
        t=self.s['plan']['tasks'][tid]
        self.apply(t['role'],'finish',{'attempt':flow.latest(self.s,tid)['id'],'result':result or flow.success_result(t),'evidence':[str(self.e)],**extra})
    def pass_task(self,tid):self.start(tid);self.finish(tid)
    def signal(self):
        self.apply('executor','signal',{'id':'D1','signal':'contract_change','reason':'contract changed','blocks':['done'],'decision_point':'contract','evidence':[str(self.e)]})
    def resolve(self):
        self.apply('architect','resolve',{'id':'D1','summary':'review before resume','requirements':[{'id':'req','kind':'task','target':'recovery','versions':{'code':'v1'},'depends_on':[],'instruction':'review revised design'}],'evidence':[str(self.e)],'user_evidence':[]})
    def test_missing_third_never_done(self):
        self.pass_task('execute');self.pass_task('regular')
        st=flow.status(self.s);self.assertEqual(st['tasks']['done'],'WAIT_DEPENDENCY');self.assertNotEqual(st['batches']['B'],'DONE')
        self.assertIn('third',[a['target'] for a in st['actions']])
    def test_full_release(self):
        for t in ['execute','regular','third','done']:self.pass_task(t)
        self.assertTrue(flow.status(self.s)['complete'])
    def test_status_cache_preserves_report_and_reduces_evidence_reads(self):
        for t in ['execute','regular','third','done']:self.pass_task(t)
        at=flow.now()
        with mock.patch.object(flow.hashlib,'sha256',wraps=flow.hashlib.sha256) as hashes:
            original=flow.status.__wrapped__(self.s,at)
            uncached_reads=hashes.call_count
        with mock.patch.object(flow.hashlib,'sha256',wraps=flow.hashlib.sha256) as hashes:
            cached=flow.status(self.s,at)
            self.assertLess(hashes.call_count,uncached_reads)
        self.assertEqual(cached,original)
        self.assertIsNone(flow._status_cache.get())

    def test_status_cache_does_not_survive_evidence_or_state_changes(self):
        for t in ['execute','regular','third','done']:self.pass_task(t)
        self.assertTrue(flow.status(self.s)['complete'])
        # Evidence can change without a ledger revision bump.
        self.e.write_text('changed evidence')
        self.assertFalse(flow.status(self.s)['complete'])
        self.e.write_text('proof')
        self.assertTrue(flow.status(self.s)['complete'])
        flow.latest(self.s,'execute')['result']='FAILED'
        self.assertFalse(flow.status(self.s)['complete'])

    def test_status_cache_keeps_recursion_paths_separate_and_clears_on_error(self):
        self.pass_task('execute')
        @flow.status_query
        def check(s):
            self.assertTrue(flow.success(s,'execute'))
            self.assertFalse(flow.success(s,'execute',{'execute'}))
            self.assertTrue(flow.success(s,'execute',seen=set()))
            raise RuntimeError('query failed')
        with self.assertRaisesRegex(RuntimeError,'query failed'):check(self.s)
        self.assertIsNone(flow._status_cache.get())

    def test_shared_dependency_dag_checks_each_delivery_once_per_query(self):
        # 14 diamonds have only 29 nodes, but enumerating ancestor paths visits
        # tens of thousands of deliveries and repeatedly hashes their evidence.
        tasks={};attempts=[];previous=[]
        for layer in range(15):
            names=['root'] if layer==0 else [f'left{layer}',f'right{layer}']
            for name in names:
                tasks[name]=dict(kind='acceptance',role='orchestrator',depends_on=list(previous),subjects=['code'])
                attempts.append(dict(id='A'+name,task=name,result='VERIFIED',subjects={'code':'v1'},
                                     evidence={str(self.e):flow.hashlib.sha256(self.e.read_bytes()).hexdigest()}))
            previous=names
        self.s['plan']['tasks']=tasks;self.s['state']['attempts']=attempts
        @flow.status_query
        def check(s):return all(flow.success(s,name) for name in previous)
        with mock.patch.object(flow.hashlib,'sha256',wraps=flow.hashlib.sha256) as hashes:
            self.assertTrue(check(self.s));self.assertLessEqual(hashes.call_count,len(tasks))
        self.e.write_text('changed after query')
        self.assertFalse(check(self.s))

    def test_shared_dependency_dag_is_memoized_outside_status_too(self):
        # plan_update/validate call success() through active_review_budget_coverage without a status scope;
        # uncached that recursion is exponential and hung plan_update and the stop hook on a 60-task ledger.
        tasks={};attempts=[];previous=[]
        for layer in range(15):
            names=['root'] if layer==0 else [f'left{layer}',f'right{layer}']
            for name in names:
                tasks[name]=dict(kind='acceptance',role='orchestrator',depends_on=list(previous),subjects=['code'])
                attempts.append(dict(id='A'+name,task=name,result='VERIFIED',subjects={'code':'v1'},
                                     evidence={str(self.e):flow.hashlib.sha256(self.e.read_bytes()).hexdigest()}))
            previous=names
        self.s['plan']['tasks']=tasks;self.s['state']['attempts']=attempts
        with mock.patch.object(flow.hashlib,'sha256',wraps=flow.hashlib.sha256) as hashes:
            self.assertTrue(flow.success(self.s,previous[0]));self.assertLessEqual(hashes.call_count,len(tasks))
        self.assertIsNone(flow._status_cache.get())
        self.e.write_text('changed after query')
        self.assertFalse(flow.success(self.s,previous[0]))

    def test_shared_dependency_cache_preserves_real_cycles(self):
        evidence={str(self.e):flow.hashlib.sha256(self.e.read_bytes()).hexdigest()}
        self.s['plan']['tasks']={name:dict(kind='acceptance',role='orchestrator',depends_on=[other],subjects=['code'])
                                for name,other in [('a','b'),('b','a')]}
        self.s['state']['attempts']=[dict(id='A'+name,task=name,result='VERIFIED',subjects={'code':'v1'},evidence=evidence)
                                   for name in ['a','b']]
        @flow.status_query
        def check(s):return flow.success(s,'a')
        self.assertFalse(check(self.s));self.assertIsNone(flow._status_cache.get())

    def test_shared_dependency_cache_keeps_can_close_paths_separate(self):
        evidence={str(self.e):flow.hashlib.sha256(self.e.read_bytes()).hexdigest()}
        self.s['plan']['tasks']={name:dict(kind='acceptance',role='orchestrator',depends_on=[],subjects=['code'])
                                for name in ['a','b']}
        self.s['plan']['tasks']['b']['can_close']=['a']
        self.s['state']['attempts']=[
            dict(id='Aa',task='a',result='FAILED',subjects={'code':'v1'},evidence=evidence),
            dict(id='Ab',task='b',result='VERIFIED',subjects={'code':'v1'},evidence=evidence,closes=['Aa'])]
        @flow.status_query
        def check(s):
            self.assertTrue(flow.success(s,'a'))
            self.assertFalse(flow.success(s,'a',{'b'}))
            self.assertTrue(flow.success(s,'a'))
        check(self.s)

    def test_start_before_dependencies_rejected(self):
        with self.assertRaises(flow.FlowError):self.start('done')
    def test_exhausted_budget_prompts_decision(self):
        for _ in range(2):self.start('execute');self.finish('execute','FIX_REQUIRED')
        self.assertIn('REGISTER_DECISION',[a['kind'] for a in flow.status(self.s)['actions']])
        with self.assertRaises(flow.FlowError):self.start('execute')
    def test_next_actions_distinguish_dispatch_from_registration(self):
        action=next(a for a in flow.status(self.s)['actions'] if a['target']=='execute')
        self.assertEqual(action['orchestrator_action']['mode'],'dispatch')
        self.assertNotIn('instruction',action)
        self.assertEqual(action['subagent_payload']['instruction'],'test action')
        self.assertIn('禁止直接执行 git',action['orchestrator_todo'])
        self.assertEqual(action['cli_adapter']['backend'],'agy')
        self.assertTrue(Path(action['cli_adapter']['entrypoint']).is_file())
        self.assertTrue(Path(action['cli_adapter']['reference']).is_file())
        self.assertIn('不得仅切换 --actor',action['orchestrator_action']['instruction'])
        self.pass_task('execute')
        action=next(a for a in flow.status(self.s)['actions'] if a['target']=='regular')
        self.assertEqual(action['orchestrator_action']['mode'],'dispatch')
        self.signal()
        action=next(a for a in flow.status(self.s)['actions'] if a['kind']=='DECIDE')
        self.assertEqual(action['role'],'architect')
        self.assertEqual(action['orchestrator_action']['mode'],'dispatch')
    def test_local_next_action_hands_off_to_lead(self):
        self.s['plan']['tasks']['execute']['dispatch_owner']='architect'
        action=next(a for a in flow.status(self.s)['actions'] if a['target']=='execute')
        self.assertEqual(action['orchestrator_action']['mode'],'delegate_to_lead')
        self.s['plan']['tasks']['done'].update(dispatch_owner='architect',role='architect')
        self.signal()
        action=next(a for a in flow.status(self.s)['actions'] if a['kind']=='DECIDE')
        self.assertEqual(action['orchestrator_action']['mode'],'delegate_to_lead')
    def test_native_agent_does_not_receive_cli_adapter(self):
        for tool,backend in [('codex-cli','codex'),('claude-cli','claude'),('opencode','opencode'),('opencode-cli','opencode'),('claude-agent',None),('collaboration',None)]:
            self.s['plan']['roles']['executor']['tool']=tool
            action=next(a for a in flow.status(self.s)['actions'] if a['target']=='execute')
            if backend:
                self.assertEqual(action['cli_adapter']['backend'],backend)
            else:
                self.assertNotIn('cli_adapter',action)

    def test_codex_legacy_logical_decision_context_is_not_offered_for_resume(self):
        self.signal()
        self.s['plan']['roles']['architect']['tool']='codex'
        decision=self.s['decisions']['D1']
        decision['handling']={'started_at':'legacy-time','call':dict(self.s['plan']['roles']['architect'],conversation_id='dec05_arch_ctx')}
        before=copy.deepcopy(self.s)
        action=next(a for a in flow.status(self.s)['actions'] if a['kind']=='DECIDE')
        self.assertEqual(action['context_hint']['mode'],'fresh')
        self.assertIsNone(action['context_hint']['conversation_id'])
        self.assertEqual(self.s,before)
        call=dict(self.s['plan']['roles']['architect'],conversation_id='pending:replacement')
        with self.assertRaises(flow.FlowError):
            self.apply('architect','decision_start',dict(id='D1',call=call))
        self.apply('architect','decision_start',dict(id='D1',call=call,replacement_reason='legacy logical name is not a native Codex session',evidence=[str(self.e)]))
        decision=self.s['decisions']['D1']
        self.assertEqual(decision['handling_history'][0]['previous'],before['decisions']['D1']['handling'])
        self.assertEqual(self.s['plan']['budgets'],before['plan']['budgets'])
        with self.assertRaises(flow.FlowError):
            self.apply('architect','decision_start',dict(id='D1',call=call,replacement_reason='repeat',evidence=[str(self.e)]))

    def test_new_codex_decision_rejects_logical_names(self):
        self.signal()
        self.s['plan']['roles']['architect']['tool']='codex'
        call=dict(self.s['plan']['roles']['architect'],conversation_id='dec05_arch_ctx')
        with self.assertRaisesRegex(flow.FlowError,'native session ID'):
            self.apply('architect','decision_start',dict(id='D1',call=call))
    def test_technical_instructions_are_payload_not_coordinator_todo(self):
        instruction='git commit -m reviewed; run ./build.sh'
        for kind,role in [('execute','executor'),('design','architect'),('commit','executor'),('review','reviewer'),('verify','executor')]:
            self.s['plan']['tasks']['execute'].update(kind=kind,role=role,instruction=instruction)
            action=next(a for a in flow.status(self.s)['actions'] if a['target']=='execute')
            self.assertNotIn('instruction',action)
            self.assertEqual(action['subagent_payload']['instruction'],instruction)
            self.assertEqual(action['subagent_payload']['call_config'],self.s['plan']['roles'][role])
            self.assertNotIn(instruction,action['orchestrator_todo'])
            self.assertIn('只将 subagent_payload',action['orchestrator_todo'])
    def test_bad_legacy_technical_owner_is_not_a_direct_action(self):
        self.s['plan']['tasks']['execute']['role']='orchestrator'
        report=flow.status(self.s)
        self.assertEqual(report['tasks']['execute'],'WAIT_ROLE_ASSIGNMENT')
        action=next(a for a in report['actions'] if a['target']=='execute')
        self.assertEqual(action['kind'],'PLAN_REPAIR')
        self.assertEqual(action['orchestrator_action']['mode'],'dispatch')
        self.assertNotIn('instruction',action)
    def test_pre_yield_role_audit_blocks_even_completed_flow(self):
        for tid in ['execute','regular','third','done']:self.pass_task(tid)
        for status in ['violation','unavailable']:
            report=flow.pre_yield(self.s,boundary_audit=dict(status=status,findings=[]))
            self.assertEqual(report['disposition'],'ROLE_BOUNDARY_BLOCKED')
            self.assertFalse(report['can_yield'])
        self.assertEqual(flow.pre_yield(self.s,boundary_audit=dict(status='clean'))['disposition'],'COMPLETE')
    def test_budget_registration_is_not_technical_diagnosis(self):
        self.budget_signal()
        action=next(a for a in flow.pre_yield(self.s)['actions'] if a['kind']=='DECIDE')
        self.assertEqual(action['orchestrator_action']['mode'],'dispatch')
        self.apply('architect','resolve',dict(id='D-BUDGET',summary='user must decide additional budget',requirements=[dict(id='u',kind='confirmation',owner='user',depends_on=[],instruction='approve budget')],evidence=[str(self.e)],user_evidence=[]))
        action=next(a for a in flow.status(self.s)['actions'] if a['kind']=='CONFIRM_CONDITION')
        self.assertEqual(action['orchestrator_action']['mode'],'request_user')
    def test_decision_recovery_before_unblocking(self):
        self.signal();self.resolve()
        st=flow.status(self.s);self.assertEqual(st['tasks']['done'],'DECISION_BLOCKED');self.assertEqual(st['tasks']['recovery'],'READY')
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','decision_progress',{'id':'D1','status':'applied','evidence':[str(self.e)]})
        self.pass_task('recovery')
        self.assertIn('CLOSE_DECISION',[a['kind'] for a in flow.status(self.s)['actions']])
        for value in ['applied','closed']:self.apply('orchestrator','decision_progress',{'id':'D1','status':value,'evidence':[str(self.e)]})
        self.assertTrue(flow.decision_closed(self.s,self.s['decisions']['D1']))
    def test_unknown_signal_supported(self):
        self.apply('reviewer','signal',{'id':'unknown','signal':'new_problem','reason':'new premise','blocks':['done'],'evidence':[str(self.e)]})
        self.assertIn('DECIDE',[a['kind'] for a in flow.status(self.s)['actions']])
    def test_signal_requires_evidence(self):
        with self.assertRaises(flow.FlowError):self.apply('executor','signal',{'id':'d','signal':'new','reason':'x','blocks':['done'],'evidence':[]})
    def test_transitive_block(self):
        self.pass_task('execute');self.pass_task('regular');self.pass_task('third')
        self.apply('reviewer','signal',{'id':'d','signal':'x','reason':'x','blocks':['regular'],'evidence':[str(self.e)]})
        self.assertEqual(flow.status(self.s)['tasks']['done'],'WAIT_DEPENDENCY')
    def test_version_change_invalidates_review(self):
        self.pass_task('execute');self.pass_task('regular')
        self.apply('executor','change_subject',{'subject':'code','version':'v2','evidence':[str(self.e)]})
        self.assertFalse(flow.success(self.s,'regular'))
    def test_cannot_recycle_version(self):
        self.pass_task('execute');self.apply('executor','change_subject',{'subject':'code','version':'v2','evidence':[str(self.e)]})
        with self.assertRaises(flow.FlowError):self.apply('executor','change_subject',{'subject':'code','version':'v1','evidence':[str(self.e)]})
    def test_evidence_drift_invalidates(self):
        self.pass_task('execute');self.e.write_text('changed');self.assertFalse(flow.success(self.s,'execute'))
    def test_cycle_rejected(self):
        p=plan();p['tasks']['execute']['depends_on']=['done']
        with self.assertRaises(flow.FlowError):flow.validate_plan(p)
    def test_unknown_field_rejected(self):
        p=plan();p['tasks']['execute']['typo']=True
        with self.assertRaises(flow.FlowError):flow.validate_plan(p)
    def test_wrong_role_rejected(self):
        with self.assertRaises(flow.FlowError):self.apply('executor','plan_update',{'plan':plan(),'decision_id':'D1'})
    def test_wrong_model_rejected(self):
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','start',{'task':'execute','call':{'tool':'agy','model':'wrong','effort':'medium','conversation_id':'id'}})
    def test_result_wrong_role_rejected(self):
        aid=self.start('execute')
        with self.assertRaises(flow.FlowError):self.apply('reviewer','finish',{'attempt':aid,'result':'VERIFIED','evidence':[str(self.e)]})
    def test_invalid_retry_not_extra_round(self):
        aid=self.start('execute');self.finish('execute','INVALID');self.start('execute',retry_of=aid);self.finish('execute')
        self.assertEqual(flow.used(self.s,'E'),1)
    def test_no_invalid_retry_infinite_loop(self):
        aid=self.start('execute');self.finish('execute','INVALID');self.start('execute',retry_of=aid);self.finish('execute','INVALID')
        st=flow.status(self.s);self.assertEqual(st['tasks']['execute'],'BUDGET_BLOCKED')
    def test_resume_requires_reason(self):
        self.start('execute');self.finish('execute','FIX_REQUIRED')
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','start',{'task':'execute','call':dict(self.s['plan']['roles']['executor'],conversation_id='new')})
    def test_revision_conflict(self):
        req={'request_id':'conflict','expected_revision':0,'actor':'orchestrator','operations':[{'type':'authorize','data':{'batches':[],'evidence':[str(self.e)]}}]}
        with self.assertRaises(flow.FlowError):flow.apply_request(self.s,req,self.root)
    def test_idempotency(self):
        req={'request_id':'idempotent','expected_revision':self.s['revision'],'actor':'orchestrator','operations':[{'type':'authorize','data':{'batches':['B'],'evidence':[str(self.e)]}}]}
        s,_=flow.apply_request(self.s,req,self.root);ss,changed=flow.apply_request(s,req,self.root)
        self.assertFalse(changed);self.assertEqual(s,ss)
    def test_transaction_rollback(self):
        before=copy.deepcopy(self.s)
        req={'request_id':'rollback','expected_revision':self.s['revision'],'actor':'orchestrator','operations':[{'type':'authorize','data':{'batches':[],'evidence':[str(self.e)]}},{'type':'bad','data':{}}]}
        with self.assertRaises(flow.FlowError):flow.apply_request(self.s,req,self.root)
        self.assertEqual(self.s,before)
    def test_budget_changes_need_user_source(self):
        self.signal();self.resolve();p=copy.deepcopy(self.s['plan']);p['budgets']['E']['limit']=3
        with self.assertRaises(flow.FlowError):self.apply('architect','plan_update',{'plan':p,'decision_id':'D1'})
    def test_recovery_cycle_rejected(self):
        self.signal()
        with self.assertRaises(flow.FlowError):self.apply('architect','resolve',{'id':'D1','summary':'x','requirements':[{'id':'r','kind':'task','target':'recovery','versions':{'code':'v1'},'instruction':'x','depends_on':['r']}],'evidence':[str(self.e)],'user_evidence':[]})
    def test_unknown_requirement_kind_rejected(self):
        self.signal()
        with self.assertRaises(flow.FlowError):self.apply('architect','resolve',{'id':'D1','summary':'x','requirements':[{'id':'r','kind':'magic','instruction':'x','depends_on':[]}],'evidence':[str(self.e)],'user_evidence':[]})
    def test_inactive_recovery_not_suggested(self):self.assertEqual(flow.status(self.s)['tasks']['recovery'],'INACTIVE')
    def test_progress_threshold(self):
        self.apply('orchestrator','start',{'task':'execute','call':dict(self.s['plan']['roles']['executor'],conversation_id='timed'),'started_at':'2026-10-24T01:00:00+08:00'})
        st=flow.status(self.s,'2026-10-24T01:31:00+08:00');self.assertIn('REPORT_PROGRESS',[a['kind'] for a in st['actions']])
    def test_production_rejects_historical_replay(self):
        self.s['simulation']=False
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','history',{'task':'execute','call':dict(self.s['plan']['roles']['executor'],conversation_id='history'),'result':'VERIFIED','evidence':[str(self.e)],'source':str(self.e)})
    def test_closed_decision_reopens_on_version_drift(self):
        self.signal();self.resolve();self.pass_task('recovery')
        for st in ['applied','closed']:self.apply('orchestrator','decision_progress',{'id':'D1','status':st,'evidence':[str(self.e)]})
        self.apply('architect','change_subject',{'subject':'code','version':'v2','evidence':[str(self.e)]})
        self.assertFalse(flow.decision_closed(self.s,self.s['decisions']['D1']))
    def test_pending_invocation_cannot_pass(self):
        self.apply('orchestrator','start',{'task':'execute','call':dict(self.s['plan']['roles']['executor'],conversation_id='pending:xyz')})
        with self.assertRaises(flow.FlowError):self.finish('execute')
    def test_review_failure_sends_to_executor_before_r2(self):
        self.s['plan']['tasks']['regular']['repair_tasks']=['execute']
        self.pass_task('execute');self.start('regular');self.finish('regular','FIX_REQUIRED')
        st=flow.status(self.s)
        self.assertEqual(st['tasks']['regular'],'WAIT_REPAIR')
        self.assertTrue(any(a['target']=='execute' and a['kind']=='REPAIR' for a in st['actions']))
        self.start('execute');self.finish('execute')
        self.assertEqual(flow.status(self.s)['tasks']['regular'],'READY')
    def test_review_failure_without_assignment_requests_plan(self):
        self.pass_task('execute');self.start('regular');self.finish('regular','FIX_REQUIRED')
        self.assertTrue(any(a['kind']=='PLAN_REPAIR' for a in flow.status(self.s)['actions']))
    def test_repair_cannot_avoid_shared_budget(self):
        self.s['plan']['tasks']['regular']['repair_tasks']=['execute']
        self.start('execute');self.finish('execute','FIX_REQUIRED');self.pass_task('execute')
        self.start('regular');self.finish('regular','FIX_REQUIRED')
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'BUDGET_BLOCKED')
    def test_delivery_can_pin_new_code_version(self):
        self.start('execute')
        self.apply('executor','change_subject',{'subject':'code','version':'v2','evidence':[str(self.e)]})
        self.finish('execute',subject_versions={'code':'v2'})
        self.assertTrue(flow.success(self.s,'execute'))
        self.assertEqual(flow.latest(self.s,'execute')['started_subjects'],{'code':'v1'})
    def test_reviewer_cannot_rebind_reviewed_version(self):
        self.pass_task('execute');self.start('regular')
        with self.assertRaises(flow.FlowError):self.finish('regular',subject_versions={'code':'v1'})
    def test_closed_confirmation_invalidates_on_evidence_change(self):
        self.signal()
        self.apply('architect','resolve',{'id':'D1','summary':'user confirm','requirements':[{'id':'r','kind':'confirmation','depends_on':[],'instruction':'confirm','owner':'user'}],'evidence':[str(self.e)],'user_evidence':[]})
        self.apply('orchestrator','decision_progress',{'id':'D1','status':'confirmed','requirement':'r','evidence':[str(self.e)]})
        for st in ['applied','closed']:self.apply('orchestrator','decision_progress',{'id':'D1','status':st,'evidence':[str(self.e)]})
        self.e.write_text('changed')
        self.assertFalse(flow.decision_closed(self.s,self.s['decisions']['D1']))
    def test_cli_no_arguments_reads_without_writing(self):
        path=self.root/'.batch-flow/flow.json';flow.write_atomic(path,self.s)
        before=path.read_bytes();mtime=path.stat().st_mtime_ns
        proc=subprocess.run([sys.executable,flow.__file__],cwd=self.root,capture_output=True,text=True)
        self.assertEqual(proc.returncode,0);self.assertIn('actions',json.loads(proc.stdout))
        self.assertEqual(path.read_bytes(),before);self.assertEqual(path.stat().st_mtime_ns,mtime)
        self.assertFalse(Path(str(path)+'.lock').exists())
    def test_cli_invalid_request_preserves_store(self):
        path=self.root/'.batch-flow/flow.json';flow.write_atomic(path,self.s);before=path.read_bytes()
        req=self.root/'request.json';req.write_text(json.dumps({'request_id':'bad','expected_revision':self.s['revision'],'actor':'architect','operations':[{'type':'unknown','data':{}}]}))
        proc=subprocess.run([sys.executable,flow.__file__,'apply','--request',str(req)],cwd=self.root,capture_output=True,text=True)
        self.assertEqual(proc.returncode,2);self.assertEqual(path.read_bytes(),before)
    def test_concurrent_writers_get_revision_conflict(self):
        path=self.root/'.batch-flow/flow.json';flow.write_atomic(path,self.s)
        procs=[]
        for n in range(2):
            req=self.root/f'request-{n}.json';req.write_text(json.dumps({'request_id':f'parallel-{n}','expected_revision':self.s['revision'],'actor':'orchestrator','operations':[{'type':'authorize','data':{'batches':['B'],'evidence':[str(self.e)]}}]}))
            procs.append(subprocess.Popen([sys.executable,flow.__file__,'apply','--request',str(req)],cwd=self.root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        for proc in procs:proc.communicate(timeout=10)
        self.assertEqual(sorted(proc.returncode for proc in procs),[0,2])
        self.assertEqual(json.loads(path.read_text())['revision'],self.s['revision']+1)
    def budget_signal(self, decision_point=None):
        for _ in range(2):self.start('execute');self.finish('execute','FIX_REQUIRED')
        issue=next(a['issue'] for a in flow.status(self.s)['actions'] if a['kind']=='REGISTER_DECISION')
        data={'id':'D-BUDGET','signal':'budget_exhausted','reason':'diagnose before escalation','blocks':['execute'],'source_key':issue['key'],'evidence':[str(self.e)]}
        if decision_point:data['decision_point']=decision_point
        self.apply('orchestrator','signal',data)
    def test_exhaustion_routes_to_architect_not_user(self):
        self.budget_signal();actions=flow.status(self.s)['actions']
        self.assertTrue(any(a['kind']=='DECIDE' and a['role']=='architect' and a['target']=='D-BUDGET' for a in actions))
        self.assertFalse(any(a['role']=='user' for a in actions))
    def test_budget_point_cannot_bypass_architect(self):
        self.s['plan']['decision_points']['budget-user']={'signal':'budget_exhausted','owner':'user','blocks':['execute'],'instruction':'legacy user routing'}
        self.budget_signal('budget-user')
        self.assertEqual(self.s['decisions']['D-BUDGET']['owner'],'architect')
    def test_review_budget_exhaustion_requires_diagnosis_before_user_wait(self):
        self.s['plan']['tasks']['regular']['repair_tasks']=['execute']
        self.pass_task('execute')
        self.start('regular');self.finish('regular','FIX_REQUIRED')
        self.pass_task('execute')
        self.pass_task('third')
        self.start('regular');self.finish('regular','FIX_REQUIRED')
        with self.assertRaises(flow.FlowError):self.start('regular')
        report=flow.pre_yield(self.s)
        self.assertEqual(report['disposition'],'CONTINUE')
        self.assertFalse(report['can_yield'])
        issue=next(a['issue'] for a in report['actions'] if a['kind']=='REGISTER_DECISION' and a['target']=='regular')
        self.apply('orchestrator','signal',dict(id='review-budget',signal=issue['signal'],reason='R2 still requires fixes; hold repair and review for recovery diagnosis',blocks=['regular','execute'],source_key=issue['key'],evidence=[str(self.e)]))
        # Even an old ledger that routed exhaustion to the user must diagnose first.
        self.s['decisions']['review-budget']['owner']='user'
        report=flow.pre_yield(self.s)
        self.assertEqual(flow.status(self.s)['tasks']['regular'],'DECISION_BLOCKED')
        diagnosis=next(a for a in report['actions'] if a['kind']=='DECIDE' and a['target']=='review-budget')
        self.assertEqual(diagnosis['role'],'architect')
        self.assertIn('当前不是等待用户确认',diagnosis['subagent_payload']['instruction'])
        self.assertEqual(report['disposition'],'CONTINUE')
        self.assertFalse(report['can_yield'])
        self.assertEqual(report['user_actions'],[])
        data=dict(id='review-budget',summary='Diagnosis: another review round needs user approval',requirements=[dict(id='review-budget.user',kind='confirmation',owner='user',depends_on=[],instruction='approve one additional review round')],evidence=[str(self.e)],user_evidence=[])
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','resolve',data)
        self.apply('architect','resolve',data)
        report=flow.pre_yield(self.s)
        self.assertEqual(report['disposition'],'WAIT_USER')
        self.assertTrue(report['can_yield'])
        self.assertTrue(any(a['kind']=='CONFIRM_CONDITION' and a['role']=='user' for a in report['user_actions']))
        self.assertEqual(self.s['plan']['budgets']['R']['limit'],2)
    def test_architect_can_arrange_recovery_without_user(self):
        self.budget_signal()
        self.apply('architect','resolve',{'id':'D-BUDGET','summary':'review design before selecting a repair method','requirements':[{'id':'budget.design','kind':'task','target':'recovery','versions':{'code':'v1'},'depends_on':[],'instruction':'review existing design under authorized plan'}],'evidence':[str(self.e)],'user_evidence':[]})
        actions=flow.status(self.s)['actions']
        self.assertTrue(any(a['target']=='recovery' and a['kind']=='REVIEW' for a in actions))
        self.assertFalse(any(a['role']=='user' for a in actions))
        self.assertEqual(self.s['plan']['budgets']['E']['limit'],2)
    def test_only_architect_can_introduce_user_prerequisite(self):
        self.budget_signal()
        data={'id':'D-BUDGET','summary':'architecture diagnosis requires extra authorized budget','requirements':[{'id':'budget.user','kind':'confirmation','owner':'user','depends_on':[],'instruction':'decide whether to add budget after architecture diagnosis'}],'evidence':[str(self.e)],'user_evidence':[]}
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','resolve',data)
        self.apply('architect','resolve',data)
        self.assertTrue(any(a['kind']=='CONFIRM_CONDITION' and a['role']=='user' for a in flow.status(self.s)['actions']))
    def test_review_cannot_inherit_executor_context(self):
        self.pass_task('execute');cid=flow.latest(self.s,'execute')['call']['conversation_id']
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','start',{'task':'regular','call':dict(self.s['plan']['roles']['reviewer'],conversation_id=cid)})
    def test_review_cannot_inherit_plan_author_context(self):
        self.signal();p=copy.deepcopy(self.s['plan']);p['tasks']['execute']['instruction']='revised'
        self.apply('architect','plan_update',{'plan':p,'decision_id':'D1','context':{'conversation_id':'author','mode':'fresh','reason':'replan'}})
        self.pass_task('execute')
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','start',{'task':'regular','call':dict(self.s['plan']['roles']['reviewer'],conversation_id='author')})
    def test_pending_review_cannot_hide_author_context_at_finish(self):
        self.signal();self.apply('architect','plan_update',{'plan':copy.deepcopy(self.s['plan']),'decision_id':'D1','context':{'conversation_id':'author','mode':'fresh','reason':'plan revision'}})
        self.pass_task('execute');self.apply('orchestrator','start',{'task':'regular','call':dict(self.s['plan']['roles']['reviewer'],conversation_id='pending:review')})
        with self.assertRaises(flow.FlowError):self.finish('regular',call=dict(self.s['plan']['roles']['reviewer'],conversation_id='author'))
    def test_same_node_r2_reuses_r1_when_not_author(self):
        self.s['plan']['tasks']['regular']['repair_tasks']=['execute']
        self.pass_task('execute');self.start('regular');cid=flow.latest(self.s,'regular')['call']['conversation_id'];self.finish('regular','FIX_REQUIRED')
        self.pass_task('execute')
        hint=next(a['context_hint'] for a in flow.status(self.s)['actions'] if a['target']=='regular')
        self.assertEqual(hint['mode'],'resume');self.assertEqual(hint['conversation_id'],cid)
        self.start('regular');self.assertEqual(flow.latest(self.s,'regular')['call']['conversation_id'],cid)
    def test_review_context_cannot_become_decision_author(self):
        self.pass_task('execute');self.start('regular');cid=flow.latest(self.s,'regular')['call']['conversation_id'];self.finish('regular','FIX_REQUIRED')
        self.signal()
        with self.assertRaises(flow.FlowError):self.apply('architect','plan_update',{'plan':copy.deepcopy(self.s['plan']),'decision_id':'D1','context':{'conversation_id':cid,'mode':'resume','source_conversation_id':cid,'reason':'review revealed plan contradiction'}})
    def test_invalid_resume_context_reference_rejected(self):
        self.signal()
        with self.assertRaises(flow.FlowError):self.apply('architect','plan_update',{'plan':copy.deepcopy(self.s['plan']),'decision_id':'D1','context':{'conversation_id':'one','mode':'resume','source_conversation_id':'two','reason':'wrong lineage'}})
    def test_combined_lead_requires_decision_context(self):
        for r in ['architect','reviewer']:self.s['plan']['roles'][r]['principal']='technical_lead'
        self.signal()
        with self.assertRaises(flow.FlowError):self.resolve()
    def test_closed_architecture_signal_allows_same_batch_resume(self):
        self.start('execute');a=flow.latest(self.s,'execute');self.finish('execute','ARCH_DECISION_REQUIRED')
        self.apply('executor','signal',{'id':'D-ARCH','signal':'technical_decision','reason':'design premise changed','blocks':['execute'],'source_key':'arch:'+a['id'],'evidence':[str(self.e)]})
        self.apply('architect','resolve',{'id':'D-ARCH','summary':'recover under existing budget','requirements':[{'id':'arch.review','kind':'task','target':'recovery','versions':{'code':'v1'},'depends_on':[],'instruction':'review changed plan'}],'evidence':[str(self.e)],'user_evidence':[]})
        self.pass_task('recovery')
        for st in ['applied','closed']:self.apply('orchestrator','decision_progress',{'id':'D-ARCH','status':st,'evidence':[str(self.e)]})
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'READY')
        self.start('execute');self.assertEqual(flow.used(self.s,'E'),2)
    def test_architect_cannot_inherit_executor_conversation(self):
        self.pass_task('execute');cid=flow.latest(self.s,'execute')['call']['conversation_id'];self.signal()
        with self.assertRaises(flow.FlowError):self.apply('architect','plan_update',{'plan':copy.deepcopy(self.s['plan']),'decision_id':'D1','context':{'conversation_id':cid,'mode':'resume','source_conversation_id':cid,'reason':'wrong executor history'}})
    def test_executor_cross_batch_context_reuse_rejected(self):
        self.pass_task('execute');cid=flow.latest(self.s,'execute')['call']['conversation_id']
        self.s['plan']['batches']['B2']={'name':'second','completion_task':'execute2'}
        self.s['plan']['tasks']['execute2']=dict(copy.deepcopy(self.s['plan']['tasks']['execute']),scope=['B2'])
        self.s['state']['authorized_batches'].append('B2')
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','start',{'task':'execute2','call':dict(self.s['plan']['roles']['executor'],conversation_id=cid)})
    def test_same_batch_design_and_execute_may_share_context(self):
        self.pass_task('execute');cid=flow.latest(self.s,'execute')['call']['conversation_id']
        self.s['plan']['tasks']['design']=dict(copy.deepcopy(self.s['plan']['tasks']['execute']),kind='design',consumes=[])
        self.apply('orchestrator','start',{'task':'design','call':dict(self.s['plan']['roles']['executor'],conversation_id=cid)})
        self.assertEqual(flow.latest(self.s,'design')['call']['conversation_id'],cid)
    def test_snapshot_write_and_read(self):
        path=self.root/'flow.json';flow.write_atomic(path,self.s);self.assertEqual(json.loads(path.read_text()),self.s)


    def init_grouped_review_plan(self):
        p = plan()
        p['subjects'] = {'B1.code':'v1','B2.code':'v1','B3.code':'v1'}
        p['budgets'] = {'E':{'limit':3},'R':{'limit':2},'T':{'limit':3}}
        p['batches'] = {bid:{'name':bid,'completion_task':'release'} for bid in ['B1','B2','B3']}
        def node(kind, role, scope, subjects, deps, consumes=(), **extra):
            return dict(kind=kind, role=role, scope=scope, subjects=subjects, depends_on=deps,
                        consumes=list(consumes), instruction='grouped review test', **extra)
        p['tasks'] = {
            'E1':node('execute','executor',['B1'],['B1.code'],[],['E']),
            'E2':node('execute','executor',['B2'],['B2.code'],['E1'],['E']),
            'R12':node('review','reviewer',['B1','B2'],['B1.code','B2.code'],['E1','E2'],['R'],repair_tasks=['E1','E2']),
            'E3':node('execute','executor',['B3'],['B3.code'],['R12'],['E']),
            'T123':node('third_party','third_party',['B1','B2','B3'],list(p['subjects']),['E3'],['T'],session_policy='independent'),
            'release':node('acceptance','orchestrator',['B1','B2','B3'],list(p['subjects']),['R12','E3','T123']),
        }
        p['decision_points'] = {}
        self.s = None
        self.apply('architect','init',{'plan':p,'simulation':True,'task_mode':'local','context':{'conversation_id':'initial-planner','mode':'fresh','reason':'fixture planning'}})
        self.apply('orchestrator','authorize',{'batches':list(p['batches']),'evidence':[str(self.e)]})

    def test_grouped_review_and_milestone_third_party_gate_release(self):
        self.init_grouped_review_plan()
        self.pass_task('E1')
        self.assertEqual(flow.status(self.s)['tasks']['E2'], 'READY')
        with self.assertRaises(flow.FlowError): self.start('R12')
        self.pass_task('E2')
        with self.assertRaises(flow.FlowError): self.start('E3')
        with self.assertRaises(flow.FlowError): self.start('T123')
        self.pass_task('R12')
        self.assertTrue(flow.success(self.s,'R12'))
        self.assertFalse(flow.status(self.s)['complete'])
        self.pass_task('E3')
        self.assertEqual(flow.status(self.s)['tasks']['T123'], 'READY')
        with self.assertRaises(flow.FlowError): self.start('release')
        self.pass_task('T123'); self.pass_task('release')
        self.assertTrue(flow.status(self.s)['complete'])
        self.assertEqual(len([a for a in self.s['state']['attempts'] if a['task']=='R12']),1)

    def test_grouped_review_cannot_release_changed_member_version(self):
        self.init_grouped_review_plan()
        for task in ['E1','E2','R12']: self.pass_task(task)
        self.apply('executor','change_subject',{'subject':'B1.code','version':'v2','evidence':[str(self.e)]})
        self.assertFalse(flow.success(self.s,'R12'))
        with self.assertRaises(flow.FlowError): self.start('E3')
        with self.assertRaises(flow.FlowError): self.start('T123')
        with self.assertRaises(flow.FlowError): self.start('release')

    def init_handoff_plan(self):
        p = plan()
        p['budgets'].update(backup={'limit':1}, lead={'limit':1})
        p['budgets']['R']['limit'] = 4
        p['budgets']['T']['limit'] = 4
        p['tasks']['execute']['handoff'] = {'backup':{'tool':'agy','model':'backup-model','effort':'high'}, 'backup_budget':'backup','lead_budget':'lead'}
        self.s = None
        self.apply('architect','init',{'plan':p,'simulation':True,'task_mode':'local','context':{'conversation_id':'initial-planner','mode':'fresh','reason':'fixture planning'}})
        self.apply('orchestrator','authorize',{'batches':['B'],'evidence':[str(self.e)]})

    def handoff_round(self, result, cid=None):
        action = next(a for a in flow.status(self.s)['actions'] if a['target']=='execute' and a['kind'] in {'EXECUTE','REPAIR'})
        call = {k:action['call_config'][k] for k in ['tool','model','effort']}
        call['conversation_id'] = cid or 'implementation-'+str(self.counter)
        self.apply('orchestrator','start',{'task':'execute','call':call,'replacement_reason':'planned owner handoff or context refresh'})
        aid = flow.latest(self.s,'execute')['id']
        self.apply(action['role'],'finish',{'attempt':aid,'result':result,'evidence':[str(self.e)]})
        return action

    def test_primary_backup_once_then_lead_and_independent_release(self):
        self.init_handoff_plan()
        self.assertEqual(self.handoff_round('FAILED')['executor_stage'],'primary')
        self.assertEqual(self.handoff_round('FAILED')['executor_stage'],'primary')
        backup = self.handoff_round('FAILED')
        self.assertEqual((backup['executor_stage'],backup['call_config']['model']),('backup','backup-model'))
        lead = self.handoff_round('VERIFIED')
        self.assertEqual((lead['executor_stage'],lead['role']),('lead','architect'))
        self.assertEqual([flow.used(self.s,b) for b in ['E','backup','lead']],[2,1,1])
        self.assertFalse(flow.status(self.s)['complete'])
        with self.assertRaises(flow.FlowError): self.start('done')
        for tid in ['regular','third','done']: self.pass_task(tid)
        self.assertTrue(flow.status(self.s)['complete'])
        report = flow.tracking.metrics(self.s,flow.status(self.s),flow.now())
        self.assertIn('lead',json.dumps(report))

    def test_successful_primary_never_dispatches_backup(self):
        self.init_handoff_plan(); self.handoff_round('VERIFIED')
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'SATISFIED')
        self.assertEqual(flow.used(self.s,'backup'),0)

    def test_review_rejection_after_backup_delivery_goes_to_lead(self):
        self.init_handoff_plan()
        self.s['plan']['tasks']['regular']['repair_tasks']=['execute']
        self.handoff_round('FAILED'); self.handoff_round('FAILED')
        self.handoff_round('VERIFIED')
        self.start('regular'); self.finish('regular','FIX_REQUIRED')
        action = next(a for a in flow.status(self.s)['actions'] if a['target']=='execute' and a['kind']=='REPAIR')
        self.assertEqual((action['executor_stage'],action['role']),('lead','architect'))
        self.assertEqual(flow.used(self.s,'backup'),1)

    def test_handoff_rejects_wrong_model_and_previous_owner_context(self):
        self.init_handoff_plan()
        primary = dict(self.s['plan']['roles']['executor'],conversation_id='primary-context')
        wrong = dict(primary,model='backup-model')
        with self.assertRaises(flow.FlowError): self.apply('orchestrator','start',{'task':'execute','call':wrong})
        self.handoff_round('FAILED',cid='primary-context'); self.handoff_round('FAILED',cid='primary-context')
        with self.assertRaises(flow.FlowError): self.handoff_round('FAILED',cid='primary-context')
        self.assertEqual(flow.used(self.s,'backup'),0)

    def test_lead_failure_stops_at_finite_budget(self):
        self.init_handoff_plan()
        for _ in range(4): self.handoff_round('FAILED')
        report = flow.status(self.s)
        self.assertEqual(report['tasks']['execute'],'DECISION_BLOCKED')
        self.assertTrue(any(a['kind']=='REGISTER_DECISION' and a['target']=='execute' for a in report['actions']))
        with self.assertRaises(flow.FlowError): self.start('execute')
        self.assertEqual(flow.used(self.s,'backup'),1)
        self.assertEqual(flow.used(self.s,'lead'),1)

    def test_backup_tool_retry_stays_in_same_technical_opportunity(self):
        self.init_handoff_plan();self.handoff_round('FAILED');self.handoff_round('FAILED')
        self.handoff_round('INVALID')
        action = next(a for a in flow.status(self.s)['actions'] if a['target']=='execute' and a['kind']=='EXECUTE')
        self.assertEqual(action['executor_stage'],'backup')
        call = dict(action['call_config'],conversation_id='backup-retry')
        self.apply('orchestrator','start',{'task':'execute','call':call,'retry_of':action['retry_of'],'replacement_reason':'tool retry'})
        self.apply('executor','finish',{'attempt':flow.latest(self.s,'execute')['id'],'result':'VERIFIED','evidence':[str(self.e)]})
        self.assertEqual(flow.used(self.s,'backup'),1)

    def test_lead_implementation_context_cannot_review_its_own_work(self):
        self.init_handoff_plan()
        for _ in range(3): self.handoff_round('FAILED')
        self.handoff_round('VERIFIED',cid='lead-author')
        with self.assertRaises(flow.FlowError):
            self.apply('orchestrator','start',{'task':'regular','call':dict(self.s['plan']['roles']['reviewer'],conversation_id='lead-author')})

    def test_backup_budget_is_one_and_activated_policy_cannot_reset(self):
        self.init_handoff_plan()
        bad = copy.deepcopy(self.s['plan']); bad['budgets']['backup']['limit']=2
        with self.assertRaises(flow.FlowError): flow.validate_plan(bad)
        for _ in range(3): self.handoff_round('FAILED')
        self.signal()
        bad = copy.deepcopy(self.s['plan'])
        bad['budgets']['backup-new']={'limit':1}
        bad['tasks']['execute']['handoff']['backup_budget']='backup-new'
        with self.assertRaises(flow.FlowError): self.apply('architect','plan_update',{'plan':bad,'decision_id':'D1'})

    def test_lead_delivery_requires_architect_actor(self):
        self.init_handoff_plan()
        for _ in range(3): self.handoff_round('FAILED')
        action = next(a for a in flow.status(self.s)['actions'] if a['target']=='execute' and a['kind']=='EXECUTE')
        self.apply('orchestrator','start',{'task':'execute','call':dict(action['call_config'],conversation_id='lead-delivery'),'replacement_reason':'lead takeover'})
        data = {'attempt':flow.latest(self.s,'execute')['id'],'result':'VERIFIED','evidence':[str(self.e)]}
        with self.assertRaises(flow.FlowError): self.apply('executor','finish',data)
        self.apply('architect','finish',data)

    def test_pending_backup_id_cannot_hide_previous_owner_context(self):
        self.init_handoff_plan();self.handoff_round('FAILED',cid='primary');self.handoff_round('FAILED',cid='primary')
        action = next(a for a in flow.status(self.s)['actions'] if a['target']=='execute' and a['kind']=='EXECUTE')
        self.apply('orchestrator','start',{'task':'execute','call':dict(action['call_config'],conversation_id='pending:backup'),'replacement_reason':'backup takeover'})
        with self.assertRaises(flow.FlowError):
            self.apply('executor','finish',{'attempt':flow.latest(self.s,'execute')['id'],'result':'VERIFIED','evidence':[str(self.e)],'call':dict(action['call_config'],conversation_id='primary')})

    def test_handoff_reserves_cannot_be_spent_by_regular_review(self):
        self.init_handoff_plan()
        bad = copy.deepcopy(self.s['plan']); bad['tasks']['regular']['consumes']=['backup']
        with self.assertRaises(flow.FlowError): flow.validate_plan(bad)

    def test_each_execution_round_and_retry_reports_its_recorded_model(self):
        self.init_handoff_plan()
        self.s['plan']['roles']['executor']['model'] = 'primary-model'
        self.s['plan']['roles']['architect']['model'] = 'lead-model'
        self.handoff_round('FAILED'); self.handoff_round('FAILED')
        self.handoff_round('INVALID')
        action = next(a for a in flow.status(self.s)['actions'] if a['target']=='execute' and a['kind']=='EXECUTE')
        self.apply('orchestrator','start',{'task':'execute','call':dict(action['call_config'],conversation_id='backup-retry'),'retry_of':action['retry_of'],'replacement_reason':'retry tool call'})
        self.apply('executor','finish',{'attempt':flow.latest(self.s,'execute')['id'],'result':'FAILED','evidence':[str(self.e)]})
        self.handoff_round('VERIFIED')
        m = flow.tracking.metrics(self.s,flow.status(self.s),flow.now())
        rows = m['attempts']
        self.assertEqual([(a['round'],a['executor_stage'],a['model']) for a in rows],[(1,'primary','primary-model'),(2,'primary','primary-model'),(3,'backup','backup-model'),(3,'backup','backup-model'),(4,'lead','lead-model')])
        self.assertEqual(rows[-1]['performer_role'],'architect')
        self.assertTrue(all(a['tool']=='agy' and a['effort'] and a['model_source']=='recorded_call' for a in rows))
        groups = {g['executor_stage']:g for g in m['model_usage']}
        self.assertEqual((groups['primary']['calls'],groups['primary']['technical_rounds']),(2,2))
        self.assertEqual((groups['backup']['calls'],groups['backup']['technical_rounds'],groups['backup']['invalid_calls']),(2,1,1))
        self.assertEqual(groups['lead']['model'],'lead-model')
        self.assertEqual(sum(g['calls'] for g in m['model_usage']),len(rows))

    def test_inflight_and_historical_model_use_start_snapshot_after_plan_change(self):
        aid = self.start('execute')
        original_call = copy.deepcopy(flow.latest(self.s,'execute')['call'])
        self.signal()
        p = copy.deepcopy(self.s['plan']); p['roles']['executor']['model']='future-model'
        self.apply('architect','plan_update',{'plan':p,'decision_id':'D1'})
        self.apply('executor','finish',{'attempt':aid,'result':'VERIFIED','call':original_call,'evidence':[str(self.e)]})
        m = flow.tracking.metrics(self.s,flow.status(self.s),flow.now())
        self.assertEqual(m['attempts'][0]['model'],original_call['model'])
        self.assertEqual(m['model_usage'][0]['model'],original_call['model'])
        self.assertEqual(flow.latest(self.s,'execute')['performer_config']['model'],original_call['model'])

    def test_missing_historical_model_is_unknown_without_plan_inference(self):
        self.pass_task('execute')
        attempt = flow.latest(self.s,'execute')
        del attempt['call']['model']
        attempt.pop('performer_config',None)
        m = flow.tracking.metrics(self.s,flow.status(self.s),flow.now())
        self.assertIsNone(m['attempts'][0]['model'])
        self.assertEqual(m['attempts'][0]['model_source'],'unknown')
        self.assertIsNone(m['model_usage'][0]['model'])

    def test_init_requires_author_and_explicit_mode(self):
        p=plan(); base={'plan':p,'simulation':True}
        def init(data):
            req={'request_id':'case','expected_revision':0,'actor':'architect','operations':[{'type':'init','data':data}]}
            return flow.apply_request(None,req,self.root)[0]
        with self.assertRaises(flow.FlowError): init(base)
        ctx=dict(conversation_id='planner',mode='fresh',reason='planning')
        with self.assertRaises(flow.FlowError): init(dict(base,task_mode='local'))
        with self.assertRaises(flow.FlowError): init(dict(base,task_mode='local',context=dict(conversation_id='pending:planner',mode='fresh',reason='planning')))
        with self.assertRaises(flow.FlowError): init(dict(base,task_mode='batched',context=ctx))
        state=init(dict(base,task_mode='local',context=ctx))
        self.assertNotIn('design',state)
        self.assertEqual(state['state']['plan_changes'][0]['context']['conversation_id'],'planner')
        with self.assertRaises(flow.FlowError): flow.check_task_context(state,'regular','planner')
        state.pop('task_mode'); state['state']['plan_changes']=[]
        flow.validate_store(state)
        self.assertEqual(state['state']['plan_changes'],[])

    def test_local_dispatch_owner_and_duplicate_running_attempt(self):
        for t in self.s['plan']['tasks'].values():t['dispatch_owner']='architect'
        self.s['plan']['tasks']['done']['role']='architect'
        call=dict(self.s['plan']['roles']['executor'],conversation_id='local-executor')
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','start',{'task':'execute','call':call})
        self.apply('architect','start',{'task':'execute','call':call})
        with self.assertRaises(flow.FlowError):self.apply('architect','start',{'task':'execute','call':call})
        self.assertEqual(flow.pre_yield(self.s,'architect')['disposition'],'CONTINUE')

    def test_native_timer_receipt_and_due_gate(self):
        aid=self.start('execute')
        due=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=10)).isoformat()
        data=dict(attempt=aid,handle='work-1',timer_kind='native',timer_id='schedule-1',owner_conversation_id='owner-1',due_at=due,evidence=[str(self.e)])
        for missing in ['timer_id','owner_conversation_id','due_at']:
            bad={k:v for k,v in data.items() if k!=missing}
            with self.assertRaises(flow.FlowError):self.apply('orchestrator','register_dispatch',bad)
        self.apply('orchestrator','register_dispatch',data)
        self.assertEqual(flow.pre_yield(self.s)['disposition'],'WAIT_HOST')
        self.s['state']['attempts'][-1]['dispatch']['due_at']=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=1)).isoformat()
        self.assertEqual(flow.pre_yield(self.s)['disposition'],'CONTINUE')

    def test_design_hash_covers_only_cited_sections(self):
        document=self.root/'design.md'
        document.write_text('# 记录\n决议一\n\n# 实施章节\n初始方案\n\n**第二批说明。** 第二批方案\n\n# 附录\n旧\n')
        p=plan();p['tasks']['execute']['instruction']='目标；实施章节；计划版本 1'
        p['batches']['B2']={'name':'second','completion_task':'execute2'}
        p['tasks']['execute2']=dict(kind='execute',role='executor',scope=['B2'],subjects=['code'],depends_on=[],consumes=['E'],instruction='第二批说明；计划版本 1')
        link=dict(path='design.md',version='d1',plan_version=1,sections={'execute':'实施章节','execute2':'第二批说明'})
        ctx=dict(conversation_id='author-plan',mode='fresh',reason='initial plan')
        s,_=flow.apply_request(None,{'request_id':'i','expected_revision':0,'actor':'architect','operations':[{'type':'init','data':{'plan':p,'simulation':True,'task_mode':'batched','design':link,'context':ctx}}]},self.root)
        s,_=flow.apply_request(s,{'request_id':'a','expected_revision':s['revision'],'actor':'orchestrator','operations':[{'type':'authorize','data':{'batches':['B','B2'],'evidence':[str(self.e)]}}]},self.root)
        document.write_text('# 记录\n决议一\n决议二\n\n# 实施章节\n初始方案\n\n**第二批说明。** 第二批方案\n\n# 附录\n新\n')
        st=flow.status(s);self.assertEqual((st['tasks']['execute'],st['tasks']['execute2']),('READY','READY'))
        document.write_text('# 记录\n决议一\n决议二\n\n# 实施章节\n修改方案\n\n**第二批说明。** 第二批方案\n\n# 附录\n新\n')
        st=flow.status(s);self.assertEqual((st['tasks']['execute'],st['tasks']['execute2']),('WAIT_DESIGN_SYNC','READY'))
        self.assertIn('SYNC_DESIGN',[a['kind'] for a in st['actions'] if a['target']=='execute'])
        legacy=dict(s);legacy['design']={k:v for k,v in s['design'].items() if k!='section_hashes'}
        self.assertEqual(flow.status(legacy)['tasks']['execute2'],'WAIT_DESIGN_SYNC')
        document.write_text('# 记录\n决议一\n决议二\n\n# 实施章节\n初始方案\n\n第二批方案改写，不再提及\n')
        self.assertEqual(flow.status(s)['tasks']['execute2'],'WAIT_DESIGN_SYNC')

    def test_design_link_drift_refresh_and_failed_update(self):
        document=self.root/'design.md';document.write_text('# 实施章节\n初始方案')
        p=plan();p['tasks']['execute']['instruction']='目标与验收；实施章节；计划版本 1'
        link=dict(path='design.md',version='d1',plan_version=1,sections={'execute':'实施章节'})
        ctx=dict(conversation_id='author-plan',mode='fresh',reason='initial plan')
        request={'request_id':'linked-init','expected_revision':0,'actor':'architect','operations':[{'type':'init','data':{'plan':p,'simulation':False,'task_mode':'batched','design':link,'context':ctx}}]}
        linked,_=flow.apply_request(None,request,self.root)
        self.assertEqual(linked['design']['plan_version'],1)
        authorize={'request_id':'linked-authorize','expected_revision':linked['revision'],'actor':'orchestrator','operations':[{'type':'authorize','data':{'batches':['B'],'evidence':[str(self.e)]}}]}
        linked,_=flow.apply_request(linked,authorize,self.root)
        document.write_text('# 实施章节\n修正错字')
        self.assertEqual(flow.status(linked)['tasks']['execute'],'WAIT_DESIGN_SYNC')
        update={'request_id':'bad-update','expected_revision':linked['revision'],'actor':'architect','operations':[{'type':'plan_update','data':{'plan':p,'decision_id':'missing'}}]}
        with self.assertRaises(flow.FlowError):flow.apply_request(linked,update,self.root)
        self.assertEqual(linked['plan_version'],1)
        refresh={'request_id':'refresh','expected_revision':linked['revision'],'actor':'architect','operations':[{'type':'design_refresh','data':{'version':'d1.1','reason':'文字纠错，不改方案','context':dict(conversation_id='author-plan',mode='resume',source_conversation_id='author-plan',reason='correction')}}]}
        linked,_=flow.apply_request(linked,refresh,self.root)
        self.assertEqual(linked['plan_version'],1)
        self.assertEqual(flow.status(linked)['tasks']['execute'],'READY')

    def test_initial_author_and_decision_author_cannot_review(self):
        self.s['state']['plan_changes'].append(dict(plan_version=1,context=dict(conversation_id='initial-author',mode='fresh',reason='initial')))
        self.pass_task('execute')
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','start',{'task':'regular','call':dict(self.s['plan']['roles']['reviewer'],conversation_id='initial-author')})
        self.signal()
        self.apply('architect','resolve',{'id':'D1','summary':'decision','requirements':[{'id':'req','kind':'confirmation','owner':'architect','depends_on':[],'instruction':'check'}],'evidence':[str(self.e)],'user_evidence':[],'context':dict(conversation_id='decision-author',mode='fresh',reason='decision')})
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','start',{'task':'regular','call':dict(self.s['plan']['roles']['reviewer'],conversation_id='decision-author')})

    def test_local_authorize_preserves_global_scope_and_pre_yield_is_scoped(self):
        p=self.s['plan'];p['batches']['LOCAL']={'name':'local','completion_task':'local_done'}
        p['tasks']['local_work']=dict(kind='execute',role='executor',scope=['LOCAL'],subjects=['code'],depends_on=[],consumes=['E'],instruction='local work',dispatch_owner='architect')
        p['tasks']['local_done']=dict(kind='acceptance',role='architect',scope=['LOCAL'],subjects=[],depends_on=['local_work'],consumes=[],instruction='local close',dispatch_owner='architect')
        self.apply('architect','authorize',{'batches':['LOCAL'],'evidence':[str(self.e)]})
        self.assertEqual(set(self.s['state']['authorized_batches']),{'B','LOCAL'})
        self.assertEqual(flow.pre_yield(self.s,'architect')['actions'][0]['target'],'local_work')
        self.assertIn('execute',[a['target'] for a in flow.pre_yield(self.s)['actions']])
        with self.assertRaises(flow.FlowError):self.apply('architect','authorize',{'batches':['B'],'evidence':[str(self.e)]})

    def test_context_groups_reject_cross_group_decision_reuse(self):
        p=self.s['plan'];p['batches']['OTHER']={'name':'other','completion_task':'other_done'}
        p['tasks']['other_done']=dict(kind='acceptance',role='orchestrator',scope=['OTHER'],subjects=[],depends_on=[],consumes=[],instruction='other close')
        p['context_groups']={'main':['B'],'other':['OTHER']}
        self.apply('executor','signal',{'id':'main-decision','signal':'main','reason':'main','blocks':['execute'],'evidence':[str(self.e)]})
        self.apply('executor','signal',{'id':'other-decision','signal':'other','reason':'other','blocks':['other_done'],'evidence':[str(self.e)]})
        call=dict(p['roles']['architect'],conversation_id='decision-session')
        self.apply('architect','decision_start',{'id':'main-decision','call':call})
        with self.assertRaisesRegex(flow.FlowError,'another technical group'):
            self.apply('architect','decision_start',{'id':'other-decision','call':call})

    def test_linked_plan_update_checks_design_section_and_relative_path(self):
        document=self.root/'design.md';document.write_text('# 章节一\n方案一')
        p=plan();p['tasks']['execute']['instruction']='章节一；计划版本 1；执行'
        ctx=dict(conversation_id='plan-author',mode='fresh',reason='plan')
        first={'request_id':'linked','expected_revision':0,'actor':'architect','operations':[{'type':'init','data':{'plan':p,'simulation':True,'task_mode':'batched','context':ctx,'design':dict(path='design.md',version='d1',plan_version=1,sections={'execute':'章节一'})}}]}
        linked,_=flow.apply_request(None,first,self.root)
        proof=self.e
        signal={'request_id':'signal','expected_revision':linked['revision'],'actor':'executor','operations':[{'type':'signal','data':{'id':'D1','signal':'change','reason':'change','blocks':['execute'],'evidence':[str(proof)]}}]}
        linked,_=flow.apply_request(linked,signal,self.root)
        document.write_text('# 章节二\n方案二')
        changed=copy.deepcopy(p);changed['tasks']['execute']['instruction']='章节二；计划版本 2；执行'
        update=dict(plan=changed,decision_id='D1',context=dict(conversation_id='plan-author',mode='resume',source_conversation_id='plan-author',reason='revision'),design=dict(path='design.md',version='d2',plan_version=2,sections={'execute':'章节二'}))
        bad=copy.deepcopy(update);bad['design']['sections']['execute']='不存在'
        request={'request_id':'bad','expected_revision':linked['revision'],'actor':'architect','operations':[{'type':'plan_update','data':bad}]}
        with self.assertRaises(flow.FlowError):flow.apply_request(linked,request,self.root)
        self.assertEqual(linked['plan_version'],1)
        request['request_id']='good';request['operations'][0]['data']=update
        linked,_=flow.apply_request(linked,request,self.root)
        self.assertEqual(linked['plan_version'],2)
        self.assertEqual(linked['design']['sections']['execute'],'章节二')

    def test_orchestrator_cannot_take_over_technical_operations(self):
        self.signal()
        cases = [
            ('init',{'plan':plan(),'simulation':True,'task_mode':'local','context':{'conversation_id':'initial-planner','mode':'fresh','reason':'fixture planning'}}),
            ('plan_update',{'plan':copy.deepcopy(self.s['plan']),'decision_id':'D1'}),
            ('resolve',{'id':'D1','summary':'unauthorized decision','requirements':[],'evidence':[str(self.e)],'user_evidence':[]}),
            ('change_subject',{'subject':'code','version':'v2','evidence':[str(self.e)]}),
        ]
        for operation, data in cases:
            with self.subTest(operation=operation):
                before = copy.deepcopy(self.s)
                with self.assertRaisesRegex(flow.FlowError,'cannot perform'):
                    self.apply('orchestrator',operation,data)
                self.assertEqual(self.s,before)
        aid = self.start('execute')
        with self.assertRaisesRegex(flow.FlowError,'responsible role'):
            self.apply('orchestrator','finish',{'attempt':aid,'result':'VERIFIED','evidence':[str(self.e)]})

    def test_later_decision_hint_resumes_task_dedicated_session(self):
        self.signal()
        self.apply('architect','decision_start',{'id':'D1','call':dict(self.s['plan']['roles']['architect'],conversation_id='dedicated-decider')})
        self.apply('executor','signal',{'id':'suggestions','signal':'nonblocking','reason':'consider follow-up','blocks':[],'evidence':[str(self.e)]})
        hint = next(a['context_hint'] for a in flow.status(self.s)['actions'] if a['kind']=='DECIDE' and a['target']=='suggestions')
        self.assertEqual((hint['mode'],hint['conversation_id']),('resume','dedicated-decider'))
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'READY')

    def repin_fixture(self):
        self.signal()
        p = copy.deepcopy(self.s['plan'])
        p['tasks']['execute']['kind'] = 'design'
        p['tasks']['third'].update(kind='confirmation', role='orchestrator', depends_on=['regular'])
        self.apply('architect', 'plan_update', {'plan':p, 'decision_id':'D1'})
        requirements = [
            {'id':'design', 'kind':'task', 'target':'execute', 'versions':{'code':'v1'}, 'depends_on':[], 'instruction':'deliver design'},
            {'id':'review', 'kind':'task', 'target':'regular', 'versions':{'code':'v1'}, 'depends_on':['design'], 'instruction':'independent design review'},
            {'id':'user', 'kind':'confirmation', 'owner':'user', 'depends_on':['review'], 'instruction':'confirm scope'},
            {'id':'baseline', 'kind':'task', 'target':'third', 'versions':{'code':'v1'}, 'depends_on':['review','user'], 'instruction':'check authorization'},
        ]
        self.apply('architect', 'resolve', {'id':'D1', 'summary':'recover before implementation', 'requirements':requirements, 'evidence':[str(self.e)], 'user_evidence':[]})
        self.apply('orchestrator', 'decision_progress', {'id':'D1', 'status':'confirmed', 'requirement':'user', 'evidence':[str(self.e)]})
        self.pass_task('execute')
        self.apply('executor', 'change_subject', {'subject':'code', 'version':'v2', 'evidence':[str(self.e)]})
        self.pass_task('execute')
        self.basis = self.root / 'repin-basis.txt'
        self.basis.write_text('version advancement mechanism and authorized repair')

    def repin_data(self, requirement='design', **extra):
        return dict(id='D1', requirement=requirement, versions={'code':'v2'}, evidence=[str(self.basis)], reason='bind recovery to delivered version', **extra)

    def test_repin_unblocks_design_review_and_preserves_all_gates(self):
        self.repin_fixture()
        self.assertTrue(flow.success(self.s, 'execute'))
        self.assertEqual(flow.status(self.s)['tasks']['regular'], 'WAIT_RECOVERY_DEPENDENCY')
        self.assertEqual(flow.status(self.s)['actions'], [])
        before = copy.deepcopy(self.s)
        req = {'request_id':'repin-three', 'expected_revision':self.s['revision'], 'actor':'architect',
               'operations':[{'type':'repin_requirement', 'data':self.repin_data(r)} for r in ['design','review','baseline']]}
        self.s, changed = flow.apply_request(self.s, req, self.root)
        self.assertTrue(changed)
        self.assertEqual(self.s['revision'], before['revision'] + 1)
        self.assertEqual(self.s['plan'], before['plan'])
        self.assertEqual(self.s['plan_version'], before['plan_version'])
        for key in ['attempts','confirmations','authorized_batches','authorization_evidence','feedback']:
            self.assertEqual(self.s['state'][key], before['state'][key])
        self.assertEqual(flow.used(self.s, 'E'), 2)
        dec = self.s['decisions']['D1']; original = before['decisions']['D1']
        self.assertEqual(dec['blocks'], original['blocks'])
        self.assertEqual(dec['status'], original['status'])
        self.assertEqual(len(dec['resolution']['requirements']), 4)
        self.assertEqual(len(dec['requirement_repins']), 3)
        for audit in dec['requirement_repins']:
            prior = next(r for r in original['resolution']['requirements'] if r['id'] == audit['requirement'])
            self.assertEqual(audit['before'], prior)
            self.assertEqual(audit['after'], dict(prior, versions={'code':'v2'}))
            self.assertEqual(audit['evidence_hashes'], flow.evidence([str(self.basis)], self.root))
            self.assertEqual(audit['actor'], 'architect')
            self.assertEqual(audit['revision'], self.s['revision'])
        action = next(a for a in flow.status(self.s)['actions'] if a['target'] == 'regular')
        self.assertEqual((action['kind'], action['role']), ('REVIEW','reviewer'))
        self.assertEqual(action['subjects'], {'code':'v2'})
        self.assertEqual(action['context_hint']['mode'], 'fresh')
        self.assertEqual(flow.status(self.s)['tasks']['done'], 'DECISION_BLOCKED')
        self.assertFalse(flow.requirement_ok(self.s, dec['resolution']['requirements'][1]))

    def test_repin_rejects_noncurrent_or_incomplete_versions(self):
        self.repin_fixture()
        for versions in [{}, {'code':'v1'}, {'code':'v3'}, {'code':'v2','extra':'v2'}, ['v2']]:
            with self.subTest(versions=versions):
                before = copy.deepcopy(self.s)
                data = self.repin_data(); data['versions'] = versions
                with self.assertRaises(flow.FlowError): self.apply('architect', 'repin_requirement', data)
                self.assertEqual(self.s, before)

    def test_repin_rejects_condition_or_decision_edits(self):
        self.repin_fixture()
        for key, value in [('target','third'), ('depends_on',[]), ('instruction','waive'), ('kind','confirmation'),
                           ('owner','user'), ('requirements',[]), ('blocks',[]), ('status','closed')]:
            with self.subTest(field=key):
                before = copy.deepcopy(self.s)
                data = self.repin_data(); data[key] = value
                with self.assertRaises(flow.FlowError): self.apply('architect', 'repin_requirement', data)
                self.assertEqual(self.s, before)

    def test_repin_requires_resolved_decision_and_task_condition(self):
        self.repin_fixture()
        cases = [{'id':'missing'}, {'requirement':'missing'}, {'requirement':'user'}]
        self.apply('executor', 'signal', {'id':'pending', 'signal':'new', 'reason':'pending', 'blocks':[], 'evidence':[str(self.e)]})
        cases.append({'id':'pending'})
        for extra in cases:
            with self.subTest(extra=extra):
                data = self.repin_data(); data.update(extra)
                with self.assertRaises(flow.FlowError): self.apply('architect', 'repin_requirement', data)

    def test_repin_only_architect_and_same_value_rejected(self):
        self.repin_fixture()
        for actor in flow.ROLES - {'architect'}:
            with self.subTest(actor=actor):
                with self.assertRaises(flow.FlowError): self.apply(actor, 'repin_requirement', self.repin_data())
        self.apply('architect', 'repin_requirement', self.repin_data())
        before = copy.deepcopy(self.s)
        with self.assertRaises(flow.FlowError): self.apply('architect', 'repin_requirement', self.repin_data())
        self.assertEqual(self.s, before)

    def test_repin_requires_evidence_and_context_or_reason(self):
        self.repin_fixture()
        for extra in [{'evidence':[]}, {'evidence':[str(self.root/'missing')]}, {'reason':''}]:
            with self.subTest(extra=extra):
                data = self.repin_data(); data.update(extra)
                with self.assertRaises(flow.FlowError): self.apply('architect', 'repin_requirement', data)
        data = self.repin_data(); del data['reason']
        with self.assertRaises(flow.FlowError): self.apply('architect', 'repin_requirement', data)
        data['context'] = {'conversation_id':'decider', 'mode':'resume', 'source_conversation_id':'decider', 'reason':'same dedicated architecture context'}
        self.apply('architect', 'repin_requirement', data)
        self.assertEqual(self.s['decisions']['D1']['requirement_repins'][-1]['context'], data['context'])

    def test_repin_does_not_make_drifted_evidence_valid(self):
        self.repin_fixture()
        self.apply('architect', 'repin_requirement', self.repin_data())
        condition = self.s['decisions']['D1']['resolution']['requirements'][0]
        self.assertTrue(flow.requirement_ok(self.s, condition))
        self.e.write_text('task and confirmation evidence changed')
        self.assertFalse(flow.success(self.s, 'execute'))
        self.assertFalse(flow.requirement_ok(self.s, condition))
        self.assertFalse(flow.requirement_ok(self.s, self.s['decisions']['D1']['resolution']['requirements'][2]))

    def test_repin_does_not_revalidate_old_review(self):
        self.repin_fixture()
        for r in ['design','review','baseline']: self.apply('architect', 'repin_requirement', self.repin_data(r))
        self.pass_task('regular')
        old_review = copy.deepcopy(flow.latest(self.s, 'regular'))
        self.apply('architect', 'change_subject', {'subject':'code','version':'v3','evidence':[str(self.e)]})
        data = self.repin_data('review'); data['versions'] = {'code':'v3'}
        self.apply('architect', 'repin_requirement', data)
        self.assertEqual(flow.latest(self.s, 'regular'), old_review)
        self.assertFalse(flow.success(self.s, 'regular'))
        self.assertFalse(flow.requirement_ok(self.s, self.s['decisions']['D1']['resolution']['requirements'][1]))

    def test_repin_uses_cas_idempotency_and_transaction_rollback(self):
        self.repin_fixture()
        req = {'request_id':'repin-idempotent','expected_revision':self.s['revision'],'actor':'architect',
               'operations':[{'type':'repin_requirement','data':self.repin_data()}]}
        bad = copy.deepcopy(req); bad['expected_revision'] -= 1
        with self.assertRaises(flow.FlowError): flow.apply_request(self.s, bad, self.root)
        before = copy.deepcopy(self.s)
        bad = copy.deepcopy(req)
        bad['operations'].append({'type':'repin_requirement','data':dict(self.repin_data('review'), versions={'code':'v3'})})
        with self.assertRaises(flow.FlowError): flow.apply_request(self.s, bad, self.root)
        self.assertEqual(self.s, before)
        self.s, _ = flow.apply_request(self.s, req, self.root)
        replay, changed = flow.apply_request(self.s, req, self.root)
        self.assertFalse(changed); self.assertEqual(replay, self.s)
        self.assertEqual(len(self.s['decisions']['D1']['requirement_repins']), 1)

    def test_repin_repeated_version_advancement_can_close_recovery(self):
        self.signal(); self.resolve(); self.pass_task('recovery')
        original_attempts = copy.deepcopy(self.s['state']['attempts'])
        for version in ['v2', 'v3']:
            with self.subTest(version=version):
                self.apply('architect', 'change_subject', {'subject':'code', 'version':version, 'evidence':[str(self.e)]})
                self.apply('architect', 'repin_requirement', {
                    'id':'D1', 'requirement':'req', 'versions':{'code':version},
                    'evidence':[str(self.e)], 'reason':'review the revised recovery deliverable'})
                self.assertFalse(flow.requirement_ok(self.s, self.s['decisions']['D1']['resolution']['requirements'][0]))
                with self.assertRaises(flow.FlowError):
                    self.apply('orchestrator', 'decision_progress', {'id':'D1', 'status':'closed', 'evidence':[str(self.e)]})
                self.pass_task('recovery')
                self.assertTrue(flow.requirement_ok(self.s, self.s['decisions']['D1']['resolution']['requirements'][0]))
        self.apply('orchestrator', 'decision_progress', {'id':'D1', 'status':'closed', 'evidence':[str(self.e)]})
        self.assertTrue(flow.decision_closed(self.s, self.s['decisions']['D1']))
        self.assertEqual(self.s['state']['attempts'][:len(original_attempts)], original_attempts)
        self.assertEqual(len(self.s['decisions']['D1']['requirement_repins']), 2)

    def test_repin_requires_complete_multi_subject_mapping(self):
        self.signal()
        p = copy.deepcopy(self.s['plan'])
        p['subjects']['design'] = 'd1'
        p['tasks']['recovery']['subjects'].append('design')
        self.apply('architect', 'plan_update', {'plan':p, 'decision_id':'D1'})
        self.apply('architect', 'resolve', {'id':'D1', 'summary':'review both objects',
            'requirements':[{'id':'req', 'kind':'task', 'target':'recovery',
                'versions':{'code':'v1','design':'d1'}, 'depends_on':[], 'instruction':'review'}],
            'evidence':[str(self.e)], 'user_evidence':[]})
        self.apply('architect', 'change_subject', {'subject':'design', 'version':'d2', 'evidence':[str(self.e)]})
        data = {'id':'D1', 'requirement':'req', 'versions':{'design':'d2'},
                'evidence':[str(self.e)], 'reason':'design revised'}
        with self.assertRaises(flow.FlowError): self.apply('architect', 'repin_requirement', data)
        data['versions']['code'] = 'v1'
        self.apply('architect', 'repin_requirement', data)
        self.pass_task('recovery')
        self.assertTrue(flow.requirement_ok(self.s, self.s['decisions']['D1']['resolution']['requirements'][0]))

    def test_repin_shortcut_uses_same_validation_and_audit(self):
        self.repin_fixture()
        proc, result = self.cli('repin_requirement','--revision',str(self.s['revision']),'--actor','architect','--data','-',input=json.dumps(self.repin_data()))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(result['ok'])
        before = copy.deepcopy(self.s)
        proc, result = self.cli('repin_requirement','--revision',str(self.s['revision']),'--actor','architect','--data','-',input=json.dumps(self.repin_data()))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.s, before)

    def classify(self, kind, work=False, decision=None, tid='execute'):
        data = dict(attempt=flow.latest(self.s,tid)['id'], kind=kind, work_performed=work,
                    reason='architect verified the original work and stop boundary', evidence=[str(self.e)])
        if decision: data['decision_id'] = decision
        if kind == 'registration': data['source_attempt'] = flow.attempts(self.s,tid)[-2]['id']
        self.apply('architect','classify_attempt',data)

    def permit(self, tid='execute'):
        proof = self.root / ('readiness-'+str(self.counter)+'.txt')
        proof.write_text('actual execution environment verified or decision recovery complete')
        self.apply('architect','resume_attempt',dict(attempt=flow.latest(self.s,tid)['id'],
                   reason='verified prerequisites; continue only remaining authorized work',evidence=[str(proof)]))
        return proof

    def start_continuation(self, tid='execute'):
        action = next(a for a in flow.status(self.s)['actions'] if a['target']==tid and a.get('continuation_of'))
        call = {k:action['call_config'][k] for k in ['tool','model','effort']}
        call['conversation_id'] = 'resumed-'+str(self.counter)
        self.apply('orchestrator','start',dict(task=tid,call=call,continuation_of=action['continuation_of'],replacement_reason='verified environment or resolved decision'))
        return action

    def decision_pause(self, decision='pause'):
        self.apply('executor','signal',dict(id=decision,signal='scope_boundary',reason='need a bounded decision',blocks=['execute'],evidence=[str(self.e)]))
        self.classify('decision_pause',work=True,decision=decision)
        self.apply('architect','resolve',dict(id=decision,summary='existing scope clarified',clarification=True,requirements=[],evidence=[str(self.e)],user_evidence=[]))

    def test_environment_exemption_preserves_owner_and_counts_actual_recovery_work(self):
        self.init_handoff_plan();self.handoff_round('FAILED');self.handoff_round('WAIT_EVIDENCE')
        original = copy.deepcopy(self.s['state']['attempts'])
        self.classify('environment')
        self.assertEqual(flow.used(self.s,'E'),1)
        self.assertEqual(self.s['state']['attempts'],original)
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'WAIT_ENVIRONMENT')
        with self.assertRaises(flow.FlowError):self.start('execute')
        self.permit(); action=self.start_continuation()
        self.assertEqual(action['executor_stage'],'primary')
        self.assertEqual(flow.latest(self.s,'execute')['technical_round'],2)
        self.finish('execute')
        self.assertEqual([flow.used(self.s,b) for b in ['E','backup','lead']],[2,0,0])
        report=flow.tracking.metrics(self.s,flow.status(self.s),flow.now())
        self.assertEqual(report['totals']['calls'],3)
        self.assertEqual(report['totals']['technical_rounds'],2)
        self.assertEqual(report['totals']['environment_calls'],1)
        self.assertEqual(report['totals']['continuation_calls'],1)
        self.assertEqual(report['budgets']['E']['used'],2)

    def test_environment_retry_is_bounded_and_requires_new_readiness_evidence(self):
        self.start('execute');self.finish('execute','INVALID');self.classify('environment')
        with self.assertRaises(flow.FlowError):
            self.apply('architect','resume_attempt',dict(attempt=flow.latest(self.s,'execute')['id'],reason='same failed check',evidence=[str(self.e)]))
        self.permit();self.start_continuation();self.finish('execute','WAIT_EVIDENCE');self.classify('environment')
        self.assertEqual(flow.used(self.s,'E'),0)
        with self.assertRaises(flow.FlowError):self.permit()
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'WAIT_ENVIRONMENT')

    def test_environment_cannot_exempt_substantive_work_or_subject_changes(self):
        self.start('execute')
        self.apply('executor','change_subject',dict(subject='code',version='v2',evidence=[str(self.e)]))
        self.finish('execute','WAIT_EVIDENCE',subject_versions={'code':'v2'})
        before=copy.deepcopy(self.s)
        with self.assertRaises(flow.FlowError):self.classify('environment',work=True)
        with self.assertRaises(flow.FlowError):self.classify('environment')
        self.assertEqual(self.s,before)

    def test_registration_correction_exempts_only_its_call_without_changing_delivery(self):
        self.pass_task('execute')
        self.apply('executor','change_subject',dict(subject='code',version='v2',evidence=[str(self.e)]))
        self.pass_task('execute')
        original=copy.deepcopy(self.s)
        self.classify('registration')
        self.assertEqual(flow.used(self.s,'E'),1)
        self.assertEqual(self.s['state']['attempts'],original['state']['attempts'])
        self.assertEqual(self.s['plan'],original['plan'])
        self.assertTrue(flow.success(self.s,'execute'))
        self.assertEqual(self.s['state']['attempt_accounting'][0]['revision'],self.s['revision'])
        with self.assertRaises(flow.FlowError):self.classify('registration')

    def test_decision_continuation_at_budget_limit_does_not_handoff_or_clear_gate(self):
        self.init_handoff_plan();self.handoff_round('FAILED');self.handoff_round('WAIT_EVIDENCE')
        self.apply('executor','signal',dict(id='pause',signal='scope_boundary',reason='decision required',blocks=['execute'],evidence=[str(self.e)]))
        self.classify('decision_pause',work=True,decision='pause')
        with self.assertRaises(flow.FlowError):self.permit()
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'DECISION_BLOCKED')
        self.apply('architect','resolve',dict(id='pause',summary='clarified existing scope',clarification=True,requirements=[],evidence=[str(self.e)],user_evidence=[]))
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'WAIT_RESUMPTION')
        self.permit();action=self.start_continuation()
        self.assertEqual(action['executor_stage'],'primary')
        self.assertEqual(action['remaining']['E'],0)
        self.finish('execute')
        self.assertEqual(flow.used(self.s,'E'),2)
        self.assertFalse(flow.success(self.s,'done'))

    def test_continuation_failure_uses_next_technical_round_and_preserves_backup_stage(self):
        self.init_handoff_plan();self.handoff_round('FAILED');self.handoff_round('FAILED');self.handoff_round('WAIT_EVIDENCE')
        self.decision_pause();self.permit();self.start_continuation()
        aid=flow.latest(self.s,'execute')['id']
        self.apply('executor','finish',dict(attempt=aid,result='FAILED',evidence=[str(self.e)]))
        action=next(a for a in flow.status(self.s)['actions'] if a['target']=='execute' and a['kind']=='EXECUTE')
        self.assertEqual(action['executor_stage'],'lead')
        self.assertEqual([flow.used(self.s,b) for b in ['E','backup','lead']],[2,1,0])

    def test_decision_continuations_have_finite_allowance(self):
        self.start('execute');self.finish('execute','WAIT_EVIDENCE')
        for index in range(2):
            self.decision_pause('pause'+str(index));self.permit();self.start_continuation();self.finish('execute','WAIT_EVIDENCE')
        self.decision_pause('pause-final')
        with self.assertRaises(flow.FlowError):self.permit()
        self.assertEqual(flow.used(self.s,'E'),1)
        self.assertEqual(len(flow.attempts(self.s,'execute')),3)

    def test_recovery_continuation_does_not_wait_for_its_own_decision_closure(self):
        self.start('execute');self.finish('execute','WAIT_EVIDENCE');self.signal()
        self.apply('architect','resolve',dict(id='D1',summary='complete recovery before release',
            requirements=[dict(id='remaining',kind='task',target='execute',versions={'code':'v1'},depends_on=[],instruction='finish remaining work')],
            evidence=[str(self.e)],user_evidence=[]))
        self.classify('decision_pause',work=True,decision='D1')
        self.permit();self.start_continuation();self.finish('execute')
        self.assertEqual(flow.used(self.s,'E'),1)
        self.apply('orchestrator','decision_progress',dict(id='D1',status='closed',evidence=[str(self.e)]))
        self.assertTrue(flow.decision_closed(self.s,self.s['decisions']['D1']))

    def test_accounting_does_not_refund_substantive_retry_descendant(self):
        root=self.start('execute');self.finish('execute','INVALID')
        self.start('execute',retry_of=root);self.finish('execute')
        self.apply('architect','classify_attempt',dict(attempt=root,kind='environment',work_performed=False,reason='startup failed',evidence=[str(self.e)]))
        self.assertEqual(flow.used(self.s,'E'),1)

    def test_accounting_role_evidence_cas_and_transaction_guards(self):
        self.start('execute');self.finish('execute','WAIT_EVIDENCE')
        data=dict(attempt=flow.latest(self.s,'execute')['id'],kind='environment',work_performed=False,reason='no work started',evidence=[str(self.e)])
        before=copy.deepcopy(self.s)
        for actor in flow.ROLES-{'architect'}:
            with self.assertRaises(flow.FlowError):self.apply(actor,'classify_attempt',data)
        with self.assertRaises(flow.FlowError):self.apply('architect','classify_attempt',dict(data,evidence=[]))
        req=dict(request_id='classify-once',expected_revision=self.s['revision'],actor='architect',operations=[dict(type='classify_attempt',data=data)])
        with self.assertRaises(flow.FlowError):flow.apply_request(self.s,dict(req,expected_revision=0),self.root)
        bad=copy.deepcopy(req);bad['operations'].append(dict(type='resume_attempt',data=dict(attempt=data['attempt'],reason='missing proof',evidence=[])))
        with self.assertRaises(flow.FlowError):flow.apply_request(self.s,bad,self.root)
        self.assertEqual(self.s,before)
        self.s,_=flow.apply_request(self.s,req,self.root)
        replay,changed=flow.apply_request(self.s,req,self.root)
        self.assertFalse(changed);self.assertEqual(replay,self.s)

    def test_readiness_drift_prevents_start_and_cannot_reissue_allowance(self):
        self.start('execute');self.finish('execute','WAIT_EVIDENCE');self.classify('environment')
        proof=self.permit();proof.write_text('changed readiness evidence')
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'WAIT_ENVIRONMENT')
        with self.assertRaises(flow.FlowError):self.start('execute',continuation_of=flow.latest(self.s,'execute')['id'])
        with self.assertRaises(flow.FlowError):self.permit()

    def test_retry_allowance_increase_needs_user_decision(self):
        self.signal();p=copy.deepcopy(self.s['plan']);p['tasks']['execute']['environment_retry_limit']=2
        with self.assertRaises(flow.FlowError):self.apply('architect','plan_update',dict(plan=p,decision_id='D1'))

    def test_accounting_cli_uses_formal_management_path(self):
        self.start('execute');self.finish('execute','WAIT_EVIDENCE')
        data=dict(attempt=flow.latest(self.s,'execute')['id'],kind='environment',work_performed=False,reason='startup permission failure',evidence=[str(self.e)])
        proc,result=self.cli('classify_attempt','--actor','architect','--revision',str(self.s['revision']),'--data','-',input=json.dumps(data))
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        self.assertEqual(flow.used(self.s,'E'),0)

    def test_resumption_replacement_keeps_audit_and_consumes_allowance(self):
        p=plan();p['tasks']['execute']['environment_retry_limit']=2
        self.s=None;self.apply('architect','init',dict(plan=p,simulation=True,task_mode='local',context=dict(conversation_id='initial-planner',mode='fresh',reason='fixture planning')))
        self.apply('orchestrator','authorize',dict(batches=['B'],evidence=[str(self.e)]))
        self.start('execute');self.finish('execute','WAIT_EVIDENCE');self.classify('environment')
        old=self.permit()
        with self.assertRaises(flow.FlowError):self.permit()
        old.unlink();self.permit()
        self.assertEqual(len(self.s['state']['resumptions']),2)
        self.start_continuation();self.finish('execute')
        self.assertEqual(flow.used(self.s,'E'),1)

    def test_registration_requires_original_success_and_real_unchanged_artifact(self):
        self.pass_task('execute')
        data=dict(attempt=flow.latest(self.s,'execute')['id'],kind='registration',work_performed=False,
                  reason='metadata correction',evidence=[str(self.e)])
        with self.assertRaises(flow.FlowError):self.apply('architect','classify_attempt',data)
        with self.assertRaises(flow.FlowError):self.apply('architect','classify_attempt',dict(data,source_attempt=data['attempt']))

    def test_registration_does_not_hide_changed_delivery_behind_shared_note(self):
        stable=self.root/'stable-note.txt';stable.write_text('shared note')
        self.start('execute');self.apply('executor','finish',dict(attempt=flow.latest(self.s,'execute')['id'],result='VERIFIED',evidence=[str(self.e),str(stable)]))
        self.apply('executor','change_subject',dict(subject='code',version='v2',evidence=[str(self.e)]))
        self.e.write_text('changed actual delivery')
        self.start('execute');self.apply('executor','finish',dict(attempt=flow.latest(self.s,'execute')['id'],result='VERIFIED',evidence=[str(self.e),str(stable)]))
        with self.assertRaises(flow.FlowError):self.classify('registration')
        self.assertEqual(flow.used(self.s,'E'),2)

    def test_technical_failure_cannot_be_waived_or_paused(self):
        self.start('execute');self.finish('execute','FAILED');self.signal()
        before=copy.deepcopy(self.s)
        for kind,work in [('environment',False),('registration',False),('decision_pause',True)]:
            with self.subTest(kind=kind):
                data=dict(attempt=flow.latest(self.s,'execute')['id'],kind=kind,work_performed=work,
                          reason='cannot hide a real failure',evidence=[str(self.e)],decision_id='D1')
                with self.assertRaises(flow.FlowError):self.apply('architect','classify_attempt',data)
                self.assertEqual(self.s,before)

    def test_continuation_cannot_bypass_authorization_or_change_budget_identity(self):
        self.start('execute');self.finish('execute','WAIT_EVIDENCE');self.decision_pause();self.permit()
        self.apply('orchestrator','authorize',dict(batches=[],evidence=[str(self.e)]))
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'WAIT_AUTHORIZATION')
        with self.assertRaises(flow.FlowError):self.start('execute',continuation_of=flow.latest(self.s,'execute')['id'])

    def test_recovery_continuation_waits_for_recovery_prerequisites(self):
        self.start('execute');self.finish('execute','WAIT_EVIDENCE');self.signal()
        self.apply('architect','resolve',dict(id='D1',summary='confirmation before recovery',
            requirements=[dict(id='approval',kind='confirmation',owner='user',depends_on=[],instruction='confirm'),
                dict(id='remaining',kind='task',target='execute',versions={'code':'v1'},depends_on=['approval'],instruction='finish work')],
            evidence=[str(self.e)],user_evidence=[]))
        self.classify('decision_pause',work=True,decision='D1')
        with self.assertRaises(flow.FlowError):self.permit()
        self.apply('orchestrator','decision_progress',dict(id='D1',status='confirmed',requirement='approval',evidence=[str(self.e)]))
        self.permit()
        self.e.write_text('confirmation proof changed')
        self.assertIsNone(flow.resumption(self.s,flow.latest(self.s,'execute')))
        with self.assertRaises(flow.FlowError):self.start('execute',continuation_of=flow.latest(self.s,'execute')['id'])

    def model_update(self, roles=None, backups=None, **extra):
        return self.apply('orchestrator','set_models',dict(roles=roles or {},backups=backups or {},
            reason='user explicitly selected these call configurations',user_evidence=[str(self.e)],**extra))

    def test_user_model_update_requires_no_architecture_decision(self):
        before=copy.deepcopy(self.s)
        self.model_update(roles={'executor':{'model':'user-selected'}})
        self.assertEqual(self.s['decisions'],before['decisions'])
        self.assertEqual(self.s['plan_version'],before['plan_version']+1)
        self.assertEqual(self.s['plan']['roles']['executor']['model'],'user-selected')
        expected=copy.deepcopy(before['plan']);expected['roles']['executor']['model']='user-selected'
        self.assertEqual(self.s['plan'],expected)
        self.assertEqual(self.s['state']['attempts'],before['state']['attempts'])
        change=self.s['state']['plan_changes'][-1]
        self.assertEqual(change['user_evidence'],flow.evidence([str(self.e)],self.root))
        self.assertIsNone(change['context'])
        self.assertEqual(change['kind'],'user_model_update')

    def test_user_model_update_preserves_running_attempt_and_actual_model_history(self):
        self.start('execute')
        original=copy.deepcopy(flow.latest(self.s,'execute'))
        self.model_update(roles={'executor':{'model':'next-model'}})
        self.assertEqual(flow.latest(self.s,'execute'),original)
        self.finish('execute','FAILED',call=original['call'])
        self.assertEqual(flow.used(self.s,'E'),1)
        self.assertEqual(flow.latest(self.s,'execute')['call']['model'],original['call']['model'])
        action=next(a for a in flow.status(self.s)['actions'] if a['target']=='execute' and a['kind']=='EXECUTE')
        self.assertEqual(action['call_config']['model'],'next-model')

    def test_user_can_change_activated_backup_model_without_resetting_handoff(self):
        self.init_handoff_plan();self.handoff_round('FAILED');self.handoff_round('FAILED');self.handoff_round('FAILED')
        before=copy.deepcopy(self.s)
        self.model_update(backups={'execute':{'model':'new-backup','effort':'medium'}})
        self.assertEqual(self.s['plan']['tasks']['execute']['handoff']['backup']['model'],'new-backup')
        self.assertEqual(self.s['state']['attempts'],before['state']['attempts'])
        self.assertEqual([flow.used(self.s,b) for b in ['E','backup','lead']],[2,1,0])
        self.assertEqual(flow.performer(self.s,'execute')[0],'lead')

    def test_signal_from_issue_is_mechanical(self):
        self.start('execute');self.finish('execute','ARCH_DECISION_REQUIRED')
        action=next(a for a in flow.status(self.s)['actions'] if a['kind']=='REGISTER_DECISION')
        self.assertIn('--from-issue', action['instruction']); self.assertIn('不直接请用户批预算', action['instruction'])
        args=type('A',(),dict(revision=self.s['revision'],data=None,from_issue=action['issue']['key'],command='signal',actor='orchestrator',request_id=None,
            attempt=None,task=None,result=None,id=None,signal=None,reason=None,summary=None,blocks=None,evidence=[],note=None,git_ref=None,repo=None,clarification=False,conversation=None))()
        req=flow.shortcut(args,self.root,self.s)
        data=req['operations'][0]['data']
        self.assertEqual((data['source_key'],data['signal'],data['blocks']),(action['issue']['key'],'technical_decision',action['issue']['blocks']))
        self.s,_=flow.apply_request(self.s,req,self.root)
        kinds=[a['kind'] for a in flow.status(self.s)['actions']]
        self.assertNotIn('REGISTER_DECISION',kinds); self.assertIn('DECIDE',kinds)

    def test_backup_wildcard_covers_unfinished_handoff_tasks(self):
        self.init_handoff_plan()
        handoff=[tid for tid,t in self.s['plan']['tasks'].items() if t.get('handoff')]
        self.model_update(backups={'*':{'tool':'agy','model':'wild','effort':'medium'}})
        self.assertTrue(all(self.s['plan']['tasks'][t]['handoff']['backup']['model']=='wild' for t in handoff))
        self.model_update(backups={'*':{'model':'wild2'},'execute':{'model':'explicit'}})
        self.assertEqual(self.s['plan']['tasks']['execute']['handoff']['backup']['model'],'explicit')

    def test_model_update_cannot_change_principal_budget_or_scope(self):
        for patch in [{'principal':'different-owner'},{'budget':10},{'model':''},{'consumes':[]},{}]:
            with self.subTest(patch=patch):
                before=copy.deepcopy(self.s)
                with self.assertRaises(flow.FlowError):self.model_update(roles={'executor':patch})
                self.assertEqual(self.s,before)
        with self.assertRaises(flow.FlowError):self.model_update(backups={'execute':{'model':'x'}})
        with self.assertRaises(flow.FlowError):self.model_update(roles={'unknown':{'model':'x'}})

    def test_model_update_requires_user_evidence_and_orchestrator_role(self):
        data=dict(roles={'executor':{'model':'x'}},backups={},reason='explicit user selection',user_evidence=[])
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','set_models',data)
        data['user_evidence']=[str(self.e)]
        for actor in flow.ROLES-{'orchestrator'}:
            with self.assertRaises(flow.FlowError):self.apply(actor,'set_models',data)

    def test_model_only_change_does_not_make_review_context_a_plan_author(self):
        self.pass_task('execute');self.pass_task('regular')
        cid=flow.latest(self.s,'regular')['call']['conversation_id']
        self.model_update(roles={'reviewer':{'model':'user-review-model'}})
        self.assertNotIn(cid,flow.author_contexts(self.s))
        self.assertTrue(flow.success(self.s,'regular'))

    def test_model_update_transaction_rolls_back_and_cli_uses_same_rules(self):
        before=copy.deepcopy(self.s)
        with self.assertRaises(flow.FlowError):self.model_update(roles={'executor':{'model':'new'},'reviewer':{'principal':'bad'}})
        self.assertEqual(self.s,before)
        data=dict(roles={'executor':{'model':'user-model'}},backups={},reason='user specified',user_evidence=[str(self.e)])
        proc,result=self.cli('set_models','--actor','orchestrator','--revision',str(self.s['revision']),'--data','-',input=json.dumps(data))
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        self.assertEqual(self.s['plan']['roles']['executor']['model'],'user-model')
        before=copy.deepcopy(self.s)
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','set_models',data)
        self.assertEqual(self.s,before)

    def severe_signal(self, signal='scope_blocked', point='SEVERE_BLOCK'):
        self.s['plan']['decision_points'][point]=dict(signal=signal,owner='user',blocks=['execute'],instruction='legacy technical escalation')
        self.apply('executor','signal',dict(id='severe',signal=signal,reason='required build blocked',blocks=['execute'],decision_point=point,evidence=[str(self.e)]))

    def test_severe_block_always_routes_to_architect_before_user(self):
        for signal,point in [('scope_blocked','SEVERE_BLOCK'),('severe_block','other'),('custom_failure','SEVERE_BLOCK')]:
            with self.subTest(signal=signal):
                original=copy.deepcopy(self.s)
                self.severe_signal(signal,point)
                self.assertEqual(self.s['decisions']['severe']['owner'],'architect')
                action=next(a for a in flow.status(self.s)['actions'] if a['target']=='severe')
                self.assertEqual((action['kind'],action['role']),('DECIDE','architect'))
                self.s=original

    def test_legacy_severe_owner_routes_readonly_and_can_request_user_after_diagnosis(self):
        self.severe_signal();self.s['decisions']['severe']['owner']='user'
        before=copy.deepcopy(self.s)
        self.assertEqual(next(a for a in flow.status(self.s)['actions'] if a['target']=='severe')['role'],'architect')
        self.assertEqual(self.s,before)
        self.apply('architect','resolve',dict(id='severe',summary='no authorized local workaround; user must decide scope',
            requirements=[dict(id='severe.user',kind='confirmation',owner='user',depends_on=[],instruction='decide scope after diagnosis')],
            evidence=[str(self.e)],user_evidence=[]))
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'DECISION_BLOCKED')
        self.assertTrue(any(a['kind']=='CONFIRM_CONDITION' and a['role']=='user' for a in flow.status(self.s)['actions']))
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','decision_progress',dict(id='severe',status='closed',evidence=[str(self.e)]))

    def test_severe_authorized_workaround_can_recover_without_new_user_confirmation(self):
        self.apply('executor','signal',dict(id='severe',signal='scope_blocked',reason='local failure',blocks=['done'],evidence=[str(self.e)]))
        self.apply('architect','resolve',dict(id='severe',summary='bounded reversible workaround covered by existing user authorization; validate before release',
            requirements=[dict(id='severe.recovery',kind='task',target='execute',versions={'code':'v1'},depends_on=[],instruction='implement and validate authorized workaround')],
            evidence=[str(self.e)],user_evidence=[str(self.e)]))
        self.assertFalse(any(a['kind']=='CONFIRM_CONDITION' and a['role']=='user' for a in flow.status(self.s)['actions']))
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'READY')
        self.pass_task('execute')
        self.apply('orchestrator','decision_progress',dict(id='severe',status='closed',evidence=[str(self.e)]))
        self.assertEqual(flow.used(self.s,'E'),1)
        self.assertFalse(flow.success(self.s,'done'))
        self.assertEqual(flow.status(self.s)['tasks']['regular'],'READY')

    def test_pure_user_authorization_point_keeps_owner_but_routes_first_to_lead(self):
        self.s['plan']['decision_points']['upload']=dict(signal='authorization_required',owner='user',blocks=['done'],instruction='approve upload')
        self.apply('orchestrator','signal',dict(id='upload',signal='authorization_required',reason='upload needs consent',blocks=['done'],decision_point='upload',evidence=[str(self.e)]))
        self.assertEqual(self.s['decisions']['upload']['owner'],'user')
        self.assertEqual(next(a for a in flow.status(self.s)['actions'] if a['target']=='upload')['role'],'architect')
        with self.assertRaises(flow.FlowError):
            self.apply('architect','resolve',dict(id='upload',summary='cannot bypass user authorization',requirements=[dict(id='u',kind='confirmation',owner='architect',depends_on=[],instruction='confirm')],evidence=[str(self.e)],user_evidence=[]))

    def away_mode(self):
        return dict(mode='away',authorization='本任务先诊断并以授权内小代价workaround恢复被阻塞批次；确实不可行再转其他可派批次；不增预算或放宽验收',user_evidence=[str(self.e)])

    def test_away_mode_explicitly_registers_scope_without_architecture_loop(self):
        before=copy.deepcopy(self.s)
        self.assertEqual(flow.status(self.s)['user_mode']['mode'],'present')
        self.apply('orchestrator','set_user_mode',self.away_mode())
        self.assertEqual(flow.status(self.s)['user_mode']['mode'],'away')
        self.assertEqual(self.s['plan'],before['plan'])
        self.assertEqual(self.s['plan_version'],before['plan_version'])
        self.assertEqual(self.s['decisions'],before['decisions'])
        self.assertEqual(self.s['state']['attempts'],before['state']['attempts'])
        self.assertEqual(self.s['state']['user_mode']['user_evidence'],flow.evidence([str(self.e)],self.root))

    def test_user_return_clears_delegation_but_keeps_original_execution_authorization(self):
        self.apply('orchestrator','set_user_mode',self.away_mode())
        away=copy.deepcopy(self.s['state']['user_mode'])
        original_auth=copy.deepcopy(self.s['state']['authorization_evidence'])
        self.apply('orchestrator','set_user_mode',dict(mode='present',user_evidence=[str(self.e)]))
        self.assertEqual(self.s['state']['user_mode']['mode'],'present')
        self.assertIsNone(self.s['state']['user_mode']['authorization'])
        self.assertEqual(self.s['state']['authorized_batches'],['B'])
        self.assertEqual(self.s['state']['authorization_evidence'],original_auth)
        previous=self.s['history'][-2]['operations'][0]['data']
        self.assertEqual(previous['authorization'],away['authorization'])

    def test_away_mode_does_not_clear_user_confirmation_or_bypass_blocked_work(self):
        self.apply('executor','signal',dict(id='severe',signal='scope_blocked',reason='needs larger scope',blocks=['execute'],evidence=[str(self.e)]))
        self.apply('architect','resolve',dict(id='severe',summary='outside authorized workaround scope',
            requirements=[dict(id='user-approval',kind='confirmation',owner='user',depends_on=[],instruction='approve larger scope')],evidence=[str(self.e)],user_evidence=[]))
        before=copy.deepcopy(self.s['decisions'])
        self.apply('orchestrator','set_user_mode',self.away_mode())
        self.assertEqual(self.s['decisions'],before)
        self.assertEqual(flow.status(self.s)['tasks']['execute'],'DECISION_BLOCKED')
        with self.assertRaises(flow.FlowError):self.start('execute')
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','decision_progress',dict(id='severe',status='closed',evidence=[str(self.e)]))

    def test_user_mode_requires_scope_source_and_orchestrator(self):
        invalid=[dict(mode='away',user_evidence=[str(self.e)]),dict(mode='away',authorization='',user_evidence=[str(self.e)]),
                 dict(mode='unknown',user_evidence=[str(self.e)]),dict(mode='present',authorization='unlimited',user_evidence=[str(self.e)]),
                 dict(mode='away',authorization='bounded',user_evidence=[])]
        before=copy.deepcopy(self.s)
        for data in invalid:
            with self.assertRaises(flow.FlowError):self.apply('orchestrator','set_user_mode',data)
            self.assertEqual(self.s,before)
        for actor in flow.ROLES-{'orchestrator'}:
            with self.assertRaises(flow.FlowError):self.apply(actor,'set_user_mode',self.away_mode())

    def test_user_mode_cli_and_readonly_status_preserve_history(self):
        proc,_=self.cli('set_user_mode','--actor','orchestrator','--revision',str(self.s['revision']),'--data','-',input=json.dumps(self.away_mode()))
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        before=copy.deepcopy(self.s)
        flow.status(self.s);flow.tracking.metrics(self.s,flow.status(self.s),flow.now())
        self.assertEqual(self.s,before)

    def dispatch_receipt(self):
        ticket=self.root/'dispatch-timer.json'
        ticket.write_text(json.dumps(dict(format='batch-flow-progress-timer/v1',task='execute',status='running',started_at=flow.now(),seconds=600)))
        return dict(attempt=flow.latest(self.s,'execute')['id'],handle='actual-agent-id',timer_handle='actual-timer-session',timer_ticket=str(ticket),evidence=[str(self.e)])

    def test_pre_yield_blocks_ready_actions_and_pending_only_starts(self):
        self.assertEqual(flow.pre_yield(self.s)['disposition'],'CONTINUE')
        self.start('execute')
        before=copy.deepcopy(self.s)
        report=flow.pre_yield(self.s)
        self.assertEqual(report['disposition'],'CONTINUE')
        self.assertFalse(report['can_yield'])
        self.assertEqual(report['dispatch_problems'][0]['attempt'],flow.latest(self.s,'execute')['id'])
        self.assertEqual(self.s,before)

    def test_dispatch_receipt_allows_host_wait_but_does_not_finish_work(self):
        self.start('execute');data=self.dispatch_receipt()
        before=copy.deepcopy(self.s)
        self.apply('orchestrator','register_dispatch',data)
        report=flow.pre_yield(self.s)
        self.assertEqual(report['disposition'],'WAIT_HOST')
        self.assertTrue(report['can_yield'])
        self.assertIsNone(flow.latest(self.s,'execute')['result'])
        self.assertEqual(self.s['plan'],before['plan'])
        self.assertEqual(flow.used(self.s,'E'),1)
        self.finish('execute')
        self.assertEqual(flow.pre_yield(self.s)['disposition'],'CONTINUE')

    def test_pre_yield_expired_or_cancelled_timer_requires_followup(self):
        self.start('execute');data=self.dispatch_receipt();self.apply('orchestrator','register_dispatch',data)
        p=Path(data['timer_ticket']);t=json.loads(p.read_text());t['status']='due';p.write_text(json.dumps(t))
        self.assertEqual(flow.pre_yield(self.s)['disposition'],'CONTINUE')
        t.update(status='running',started_at='2000-01-01T00:00:00+00:00');p.write_text(json.dumps(t))
        self.assertEqual(flow.pre_yield(self.s)['disposition'],'CONTINUE')
        p.unlink();self.assertFalse(flow.pre_yield(self.s)['can_yield'])

    def test_dispatch_receipt_cannot_use_pending_handle_or_mutable_ticket_as_proof(self):
        self.start('execute');data=self.dispatch_receipt()
        before=copy.deepcopy(self.s)
        for patch in [dict(handle='pending:agent'),dict(timer_handle='pending:timer'),dict(evidence=[data['timer_ticket']])]:
            with self.assertRaises(flow.FlowError):self.apply('orchestrator','register_dispatch',dict(data,**patch))
            self.assertEqual(self.s,before)
        self.apply('orchestrator','register_dispatch',data)
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','register_dispatch',dict(data,handle='duplicate-new-agent'))

    def test_pre_yield_authorization_gap_needs_coordination_not_a_user_question(self):
        self.apply('orchestrator','authorize',dict(batches=[],evidence=[str(self.e)]))
        self.assertEqual(flow.pre_yield(self.s)['disposition'],'CONTINUE')
        self.assertEqual(flow.pre_yield(self.s)['user_actions'],[])
        self.apply('orchestrator','authorize',dict(batches=['B'],evidence=[str(self.e)]))
        for task in ['execute','regular','third','done']:self.pass_task(task)
        self.assertEqual(flow.pre_yield(self.s)['disposition'],'COMPLETE')

    def test_pre_yield_cli_is_readonly_and_nonzero_when_more_work_is_needed(self):
        before=copy.deepcopy(self.s)
        proc,result=self.cli('pre_yield')
        self.assertEqual(proc.returncode,1,proc.stdout+proc.stderr)
        self.assertEqual(result['disposition'],'CONTINUE')
        self.assertEqual(self.s,before)

    def cli(self, *args, input=None):
        store = self.root / '.batch-flow' / 'flow.json'
        flow.write_atomic(store, self.s)
        proc = subprocess.run([sys.executable, flow.__file__, '--store', str(store), *args], input=input, cwd=self.root, capture_output=True, text=True)
        self.s = json.loads(store.read_text())
        return proc, json.loads(proc.stdout)

    def test_standard_channel_runs_without_design_or_third_party(self):
        p = plan()
        del p['tasks']['third']
        p['tasks']['done']['depends_on'] = ['regular']
        p['tasks']['regular']['repair_tasks'] = ['execute']
        flow.validate_plan(p)
        self.s = None
        self.apply('architect', 'init', {'plan':p, 'simulation':True,'task_mode':'local','context':{'conversation_id':'initial-planner','mode':'fresh','reason':'fixture planning'}})
        self.apply('orchestrator', 'authorize', {'batches':['B'], 'evidence':[str(self.e)]})
        for tid in ['execute', 'regular', 'done']:
            self.pass_task(tid)
        self.assertTrue(flow.status(self.s)['complete'])

    def test_cli_start_finish_and_old_review_binding(self):
        proc, result = self.cli('start', '--revision', str(self.s['revision']), '--task', 'execute', '--tool', 'agy', '--model', 'gemini-3.8-flash-medium', '--effort', 'medium', '--conversation', 'exec-shortcut')
        self.assertEqual(proc.returncode, 0, result)
        aid = flow.latest(self.s, 'execute')['id']
        proc, result = self.cli('finish', '--revision', str(self.s['revision']), '--actor', 'executor', '--attempt', aid, '--result', 'VERIFIED', '--evidence', str(self.e), '--note', 'AC verified; no unresolved issues')
        self.assertEqual(proc.returncode, 0, result)
        self.pass_task('regular')
        self.apply('executor', 'change_subject', {'subject':'code','version':'v2','evidence':[str(self.e)]})
        self.assertFalse(flow.success(self.s, 'regular'))

    def test_cli_conflict_does_not_write_or_create_records(self):
        before = copy.deepcopy(self.s)
        proc, result = self.cli('signal', '--revision', str(self.s['revision'] - 1), '--actor', 'executor', '--id', 'd', '--signal', 'x', '--reason', 'conflict', '--blocks', 'done', '--note', 'do not save stale evidence')
        self.assertEqual(proc.returncode, 2)
        self.assertIn('revision conflict', result['error'])
        self.assertEqual(before, self.s)
        self.assertFalse((self.root / '.batch-flow' / 'records').exists())

    def test_finish_json_note_and_cli_note_use_the_same_evidence_format(self):
        aid=self.start('execute')
        data=dict(attempt=aid,result='VERIFIED',evidence=[str(self.e)],note='AC result and limitations')
        original=copy.deepcopy(self.s)
        proc,result=self.cli('finish','--revision',str(self.s['revision']),'--actor','executor',
                             '--data','-',input=json.dumps(data))
        self.assertEqual(proc.returncode,0,result)
        def notes():
            return [json.loads(Path(p).read_text()) for p in flow.latest(self.s,'execute')['evidence'] if p!=str(self.e)]
        json_notes=notes()
        self.s=original
        note=data.pop('note')
        proc,result=self.cli('finish','--revision',str(self.s['revision']),'--actor','executor',
                             '--data','-','--note',note,input=json.dumps(data))
        self.assertEqual(proc.returncode,0,result)
        self.assertEqual(notes(),json_notes)
        self.assertEqual(json_notes,[{'note':note}])

    def test_note_apply_replay_preserves_raw_request_and_creates_one_record(self):
        aid=self.start('execute')
        data=dict(attempt=aid,result='VERIFIED',evidence=[str(self.e)],note='delivery details')
        request=dict(request_id='stable-note',expected_revision=self.s['revision'],actor='executor',
                     operations=[dict(type='finish',data=data)])
        original=copy.deepcopy(request)
        self.s,changed=flow.apply_request(self.s,request,self.root)
        self.assertTrue(changed)
        history=self.s['history'][-1]
        self.assertEqual(history['submitted_operations'],request['operations'])
        self.assertNotIn('note',history['operations'][0]['data'])
        self.assertEqual(history['request_hash'],flow.digest(request))
        self.assertEqual(request,original)
        records=set((self.root/'records').glob('*.json'))
        self.s,changed=flow.apply_request(self.s,request,self.root)
        self.assertFalse(changed)
        self.assertEqual(set((self.root/'records').glob('*.json')),records)

    def test_shared_note_normalization_cleans_failed_multi_operation_transaction(self):
        aid=self.start('execute');before=copy.deepcopy(self.s)
        request=dict(request_id='bad-batch',expected_revision=self.s['revision'],actor='executor',operations=[
            dict(type='finish',data=dict(attempt=aid,result='VERIFIED',evidence=[str(self.e)],note='first note')),
            dict(type='finish',data=dict(attempt=aid,result='VERIFIED',evidence=[str(self.e)],note='second note'))])
        with self.assertRaisesRegex(flow.FlowError,'already finished'):
            flow.apply_request(self.s,request,self.root)
        self.assertEqual(self.s,before)
        self.assertEqual(list((self.root/'records').glob('*.json')),[])

    def test_note_does_not_bypass_field_role_result_or_user_evidence_checks(self):
        aid=self.start('execute');before=copy.deepcopy(self.s)
        data=dict(attempt=aid,result='VERIFIED',evidence=[str(self.e)],note='explanation')
        for actor,extra in [('reviewer',{}),('executor',{'unexpected':True}),
                            ('executor',{'result':'RELEASE'}),('executor',{'note':42}),
                            ('executor',{'note':' '}),('executor',{'evidence':'invalid'})]:
            with self.subTest(actor=actor,extra=extra),self.assertRaises(flow.FlowError):
                self.apply(actor,'finish',dict(data,**extra))
            self.assertEqual(self.s,before)
        with self.assertRaises(flow.FlowError):
            self.apply('orchestrator','set_models',dict(roles={},backups={},reason='test',note='not authorization'))
        self.assertEqual(list((self.root/'records').glob('*.json')),[])

    def test_cli_rejects_duplicate_note_and_accepts_json_start_call(self):
        data=dict(task='execute',call=dict(self.s['plan']['roles']['executor'],conversation_id='json-call'))
        proc,result=self.cli('start','--revision',str(self.s['revision']),'--actor','orchestrator',
                             '--data','-',input=json.dumps(data))
        self.assertEqual(proc.returncode,0,result)
        self.assertEqual(flow.latest(self.s,'execute')['call'],data['call'])
        before=copy.deepcopy(self.s)
        finish=dict(attempt=flow.latest(self.s,'execute')['id'],result='VERIFIED',evidence=[str(self.e)],note='in JSON')
        proc,result=self.cli('finish','--revision',str(self.s['revision']),'--actor','executor',
                             '--data','-','--note','in CLI',input=json.dumps(finish))
        self.assertEqual(proc.returncode,2)
        self.assertIn('duplicate parameter: note',result['error'])
        self.assertEqual(self.s,before)

    def test_schema_matches_every_operation_and_does_not_require_store(self):
        self.assertEqual(set(flow.OP_FIELDS),set(flow.OP_ROLES))
        store=self.root/'absent'/'flow.json'
        proc=subprocess.run([sys.executable,flow.__file__,'--store',str(store),'schema'],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        schemas=json.loads(proc.stdout)['operations']
        self.assertEqual(schemas,{kind:flow.operation_schema(kind) for kind in flow.OP_ROLES})
        self.assertIn('note',schemas['finish']['optional'])
        self.assertNotIn('note',schemas['set_models']['optional'])
        self.assertFalse(store.parent.exists())

    def test_call_inputs_reject_mixing_or_unused_flags_and_work_for_finish(self):
        call=dict(self.s['plan']['roles']['executor'],conversation_id='actual-executor')
        before=copy.deepcopy(self.s)
        data=dict(task='execute',call=call)
        proc,result=self.cli('start','--revision',str(self.s['revision']),'--actor','orchestrator',
                             '--data','-','--tool',call['tool'],input=json.dumps(data))
        self.assertEqual(proc.returncode,2)
        self.assertIn('duplicate parameter: call',result['error'])
        self.assertEqual(self.s,before)
        proc,result=self.cli('signal','--revision',str(self.s['revision']),'--actor','executor',
                             '--tool',call['tool'],'--note','must not be silently ignored')
        self.assertEqual(proc.returncode,2)
        self.assertIn('does not accept call flags',result['error'])
        self.assertEqual(self.s,before)
        self.apply('orchestrator','start',dict(task='execute',call=call))
        proc,result=self.cli('finish','--revision',str(self.s['revision']),'--actor','executor',
                             '--attempt',flow.latest(self.s,'execute')['id'],'--result','VERIFIED',
                             '--evidence',str(self.e),'--tool',call['tool'],'--model',call['model'],
                             '--effort',call['effort'],'--conversation',call['conversation_id'])
        self.assertEqual(proc.returncode,0,result)
        self.assertEqual(flow.latest(self.s,'execute')['call'],call)

    def test_cli_shortcut_still_rejects_wrong_actor(self):
        aid = self.start('execute')
        before = copy.deepcopy(self.s)
        proc, result = self.cli('finish', '--revision', str(self.s['revision']), '--actor', 'reviewer', '--attempt', aid, '--result', 'VERIFIED', '--evidence', str(self.e))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(before, self.s)

    def test_cli_record_preserves_failure_and_does_not_invoke_shell(self):
        before = copy.deepcopy(self.s)
        proc, result = self.cli('record', '--', sys.executable, '-c', 'import sys; print(sys.argv[1]); print("failure", file=sys.stderr); sys.exit(7)', '$(touch injected)')
        self.assertEqual(proc.returncode, 7)
        record = json.loads(Path(result['evidence']).read_text())
        self.assertEqual(record['exit_code'], 7)
        self.assertEqual(record['stdout'].strip(), '$(touch injected)')
        self.assertIn('failure', record['stderr'])
        self.assertFalse((self.root / 'injected').exists())
        self.assertEqual(self.s, before)

    def test_close_checks_recovery_then_closes_in_one_operation(self):
        self.signal(); self.resolve()
        before = copy.deepcopy(self.s)
        proc, result = self.cli('close', '--revision', str(self.s['revision']), '--id', 'D1', '--evidence', str(self.e))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(self.s, before)
        self.pass_task('recovery')
        proc, result = self.cli('close', '--revision', str(self.s['revision']), '--id', 'D1', '--evidence', str(self.e))
        self.assertEqual(proc.returncode, 0, result)
        self.assertTrue(flow.decision_closed(self.s, self.s['decisions']['D1']))
        self.apply('executor','change_subject',{'subject':'code','version':'v2','evidence':[str(self.e)]})
        self.assertFalse(flow.decision_closed(self.s, self.s['decisions']['D1']))
        proc, result = self.cli('close', '--revision', str(self.s['revision']), '--id', 'D1', '--evidence', str(self.e))
        self.assertEqual(proc.returncode, 2)

    def test_clarification_shortcut_closes_without_recovery_tasks(self):
        proc, result = self.cli('signal', '--revision', str(self.s['revision']), '--actor', 'executor', '--id', 'clarify', '--signal', 'ambiguity', '--reason', 'clarify existing contract', '--blocks', 'done', '--note', 'source line and recommendation')
        self.assertEqual(proc.returncode, 0, result)
        proc, result = self.cli('resolve', '--revision', str(self.s['revision']), '--id', 'clarify', '--summary', 'existing contract confirmed; no change required', '--clarification', '--conversation', 'architecture', '--note', 'original contract is sufficient')
        self.assertEqual(proc.returncode, 0, result)
        self.assertTrue(flow.decision_closed(self.s, self.s['decisions']['clarify']))
        self.assertFalse(flow.status(self.s)['complete'])

    def test_clarification_cannot_hide_budget_user_or_version_change(self):
        self.signal()
        data = dict(id='D1', summary='clarify', requirements=[], evidence=[str(self.e)], user_evidence=[], clarification=True)
        for field, value in [('signal', 'budget_exhausted'), ('owner', 'user')]:
            original = self.s['decisions']['D1'][field]
            self.s['decisions']['D1'][field] = value
            with self.assertRaises(flow.FlowError):
                self.apply('architect','resolve',data)
            self.s['decisions']['D1'][field] = original
        self.apply('executor','change_subject',{'subject':'code','version':'v2','evidence':[str(self.e)]})
        with self.assertRaises(flow.FlowError):
            self.apply('architect','resolve',data)

    def test_cli_data_from_stdin_uses_same_recovery_protocol(self):
        self.signal()
        data = dict(id='D1', summary='review before resume', requirements=[{'id':'req','kind':'task','target':'recovery','versions':{'code':'v1'},'depends_on':[],'instruction':'review'}], evidence=[str(self.e)])
        proc, result = self.cli('resolve', '--revision', str(self.s['revision']), '--data', '-', input=json.dumps(data))
        self.assertEqual(proc.returncode, 0, result)
        self.assertEqual(self.s['decisions']['D1']['status'], 'decided')
        self.assertFalse(flow.decision_closed(self.s, self.s['decisions']['D1']))

    def test_git_reference_and_scoped_workspace_snapshot(self):
        repo = self.root / 'repo'; repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
        git('init')
        (repo / 'chosen.txt').write_text('before')
        (repo / 'outside.txt').write_text('do not capture')
        git('add', '.')
        git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','base')
        commit = git('rev-parse','HEAD')
        (repo / 'chosen.txt').write_text('after')
        (repo / 'new.bin').write_bytes(b'\x00\xff')
        (repo / 'outside.txt').write_text('unrelated change')
        proc, result = self.cli('snapshot', '--repo', str(repo), '--paths', 'chosen.txt', 'new.bin')
        self.assertEqual(proc.returncode, 0, result)
        record = json.loads(Path(result['evidence']).read_text())
        self.assertEqual(record['base_commit'], commit)
        self.assertIn('+after', record['diff'])
        self.assertNotIn('unrelated change', record['diff'])
        self.assertEqual(record['untracked_base64'], {'new.bin':'AP8='})
        self.assertEqual(git('rev-parse','HEAD'), commit)
        self.start('execute')
        proc, result = self.cli('finish','--revision',str(self.s['revision']),'--actor','executor','--attempt',flow.latest(self.s,'execute')['id'],'--result','VERIFIED','--repo',str(repo),'--git-ref','HEAD','--evidence',str(self.e))
        self.assertEqual(proc.returncode, 0, result)
        references = [json.loads(Path(path).read_text()) for path in flow.latest(self.s,'execute')['evidence'] if path.endswith('.json')]
        self.assertTrue(any(record.get('commit') == commit for record in references))

class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.e=self.root/'proof.txt';self.e.write_text('fixture, not product verification')
        self.s=None;self.counter=0;self.clock=0
        p=plan();p['tasks']['regular']['repair_tasks']=['execute']
        self.apply('architect','init',{'plan':p,'simulation':True,'task_mode':'local','context':{'conversation_id':'initial-planner','mode':'fresh','reason':'fixture planning'}})
        self.apply('orchestrator','authorize',{'batches':['B'],'evidence':[str(self.e)]})
    def tearDown(self):self.tmp.cleanup()
    def at(self,second):
        return (flow.dt.datetime(2026,10,24,tzinfo=flow.dt.timezone.utc)+flow.dt.timedelta(seconds=second)).isoformat()
    def apply(self,actor,kind,data,second=None):
        if second is not None:self.clock=second
        self.counter+=1
        req={'request_id':str(self.counter),'expected_revision':self.s['revision'] if self.s else 0,'actor':actor,'at':self.at(self.clock),'operations':[{'type':kind,'data':data}]}
        self.s,_=flow.apply_request(self.s,req,self.root)
    def start(self,tid,second=0,**extra):
        role=self.s['plan']['tasks'][tid]['role'];call=dict(self.s['plan']['roles'][role],conversation_id=tid+'-session')
        self.apply('orchestrator','start',{'task':tid,'call':call,**extra},second)
    def finish(self,tid,result='VERIFIED',second=10,**extra):
        role=self.s['plan']['tasks'][tid]['role']
        self.apply(role,'finish',{'attempt':flow.latest(self.s,tid)['id'],'result':result,'evidence':[str(self.e)],**extra},second)
    def metrics(self,second=100):return flow.tracking.metrics(self.s,flow.status(self.s,self.at(second)),self.at(second))
    def timing(self,start,end):return {'started_at':self.at(start),'ended_at':self.at(end),'evidence':[str(self.e)]}
    def test_actual_timing_separates_dispatch_and_registration(self):
        self.start('execute',10)
        self.finish('execute',second=30,timing=self.timing(12,25),call_id='tool-17')
        row=self.metrics()['attempts'][0]
        self.assertEqual((row['work_seconds'],row['dispatch_seconds'],row['result_registration_seconds']),(13,2,5))
        self.assertEqual(self.metrics()['batches']['B']['observed_ready_wait_seconds'],80) # execute 0-10, reviewers 30-100
        self.assertEqual(row['call_id'],'tool-17')
    def test_missing_actual_timing_is_unknown(self):
        self.start('execute');self.finish('execute')
        self.assertIsNone(self.metrics()['totals']['work_seconds'])
        self.assertEqual(self.metrics()['totals']['unknown_work_calls'],1)
    def test_bad_actual_timing_rolls_back(self):
        self.start('execute',10);before=copy.deepcopy(self.s)
        with self.assertRaises(flow.FlowError):self.finish('execute',second=20,timing=self.timing(9,19))
        self.assertEqual(self.s,before)
        with self.assertRaises(flow.FlowError):self.finish('execute',second=20,timing=self.timing(12,21))
    def test_retry_inherits_round_without_becoming_rework(self):
        self.start('execute');self.finish('execute','INVALID',10)
        aid=flow.latest(self.s,'execute')['id'];self.start('execute',20,retry_of=aid);self.finish('execute',second=30)
        m=self.metrics()
        self.assertEqual([a['round'] for a in m['attempts']],[1,1])
        self.assertEqual((m['totals']['technical_rounds'],m['totals']['retry_calls'],m['totals']['invalid_calls']),(1,1,1))
        self.assertFalse(m['attempts'][1]['is_rework'])
    def test_review_repair_links_and_rounds(self):
        self.start('execute');self.finish('execute',second=10)
        self.start('regular',20);self.finish('regular','FIX_REQUIRED',30,cause={'category':'implementation_defect','summary':'failure fixture'})
        review=flow.latest(self.s,'regular')['id']
        self.start('execute',40);self.finish('execute',second=50,timing=self.timing(41,49))
        self.assertIn(review,flow.latest(self.s,'execute')['triggers']['attempts'])
        self.start('regular',60);self.finish('regular','RELEASE',70)
        rows=self.metrics()['attempts']
        self.assertEqual([r['round'] for r in rows],[1,1,2,2])
        self.assertEqual(self.metrics()['totals']['rework_calls'],2)
        self.assertEqual(self.metrics()['totals']['rework_seconds'],8)
    def test_unknown_trigger_and_cause_rejected(self):
        with self.assertRaises(flow.FlowError):self.start('execute',triggers={'attempts':['A9999'],'decisions':[]})
        self.start('execute')
        with self.assertRaises(flow.FlowError):self.finish('execute',cause={'category':'guess','summary':'x'})
    def test_shared_call_counted_once(self):
        p=self.s['plan'];p['batches']['B2']={'name':'second','completion_task':'done'}
        for t in p['tasks'].values():t['scope']=['B','B2']
        self.apply('orchestrator','authorize',{'batches':['B','B2'],'evidence':[str(self.e)]})
        self.start('execute');self.finish('execute',timing=self.timing(1,9))
        m=self.metrics();self.assertEqual(m['totals']['work_seconds'],8)
        for b in ['B','B2']:
            self.assertEqual(m['batches'][b]['shared_attempts'],['A0001'])
            self.assertEqual(m['batches'][b]['exclusive']['calls'],0)
    def test_snapshot_survives_later_plan_changes(self):
        self.start('execute');self.finish('execute')
        self.s['plan']['tasks']['execute']['phase']='renamed-later'
        self.assertEqual(self.metrics()['attempts'][0]['phase'],'implementation')
    def test_unchanged_observation_does_not_add_events(self):
        before=len(self.s['state']['tracking']['events'])
        self.assertFalse(flow.tracking.observe(self.s,flow.status(self.s,self.at(10)),self.at(10)))
        self.assertEqual(len(self.s['state']['tracking']['events']),before)
    def test_readonly_metrics_and_status(self):
        before=copy.deepcopy(self.s);self.metrics();flow.status(self.s)
        self.assertEqual(self.s,before)
    def test_legacy_store_does_not_invent_actual_timing(self):
        self.start('execute');self.finish('execute')
        self.s['state'].pop('tracking')
        for key in ['technical_round','task_snapshot','triggers']:self.s['state']['attempts'][0].pop(key)
        flow.validate_store(self.s);m=self.metrics()
        self.assertIsNone(m['tracking_since']);self.assertIsNone(m['totals']['work_seconds'])
        self.assertEqual(m['attempts'][0]['snapshot_source'],'current_plan_fallback')
    def test_observe_cli_is_idempotent_and_metrics_is_readonly(self):
        path=self.root/'flow.json';flow.write_atomic(path,self.s)
        command=[sys.executable,str(Path(flow.__file__)), 'observe','--store',str(path)]
        before=path.read_bytes()
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout);self.assertFalse(json.loads(result.stdout)['changed'])
        self.assertEqual(path.read_bytes(),before)
        command[2]='metrics';result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout);self.assertEqual(path.read_bytes(),before)
    def test_observe_cli_records_external_evidence_change_once(self):
        self.start('execute');self.finish('execute');path=self.root/'flow.json';flow.write_atomic(path,self.s)
        self.e.write_text('evidence drift')
        command=[sys.executable,str(Path(flow.__file__)), 'observe','--store',str(path)]
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout);self.assertTrue(json.loads(result.stdout)['changed'])
        before=path.read_bytes();result=subprocess.run(command,capture_output=True,text=True)
        self.assertFalse(json.loads(result.stdout)['changed']);self.assertEqual(path.read_bytes(),before)
    def test_decision_chain_and_reopen(self):
        self.apply('executor','signal',{'id':'D','signal':'contract_change','reason':'fixture','blocks':['execute'],'evidence':[str(self.e)]},10)
        self.apply('architect','decision_start',{'id':'D','call':dict(self.s['plan']['roles']['architect'],conversation_id='architect-context')},20)
        self.apply('architect','resolve',{'id':'D','summary':'confirmation before resume','requirements':[{'id':'c','kind':'confirmation','owner':'architect','depends_on':[],'instruction':'confirm'}],'evidence':[str(self.e)],'user_evidence':[]},30)
        self.apply('orchestrator','decision_progress',{'id':'D','status':'confirmed','requirement':'c','evidence':[str(self.e)]},40)
        self.apply('orchestrator','decision_progress',{'id':'D','status':'applied','evidence':[str(self.e)]},50)
        self.apply('orchestrator','decision_progress',{'id':'D','status':'closed','evidence':[str(self.e)]},60)
        d=self.metrics()['decisions'][0]
        self.assertEqual(d['registration_to_handling_seconds'],10)
        self.assertEqual(d['handling_to_resolution_seconds'],10)
        self.assertEqual(d['observed_blocked_seconds'],50)
        self.assertEqual(d['status'],'closed')
        self.e.write_text('drift');self.apply('orchestrator','observe',{},70)
        self.assertEqual(self.metrics()['decisions'][0]['status'],'reopened')
    def test_detected_signal_to_registration(self):
        self.start('execute');self.finish('execute','ARCH_DECISION_REQUIRED',10)
        key='arch:A0001'
        self.apply('orchestrator','signal',{'id':'D','signal':'technical_decision','reason':'fixture','source_key':key,'blocks':['execute'],'evidence':[str(self.e)]},25)
        self.assertEqual(self.metrics()['decisions'][0]['detection_to_registration_seconds'],15)
    def test_production_actual_timing_can_be_reported_without_backdating(self):
        self.s['simulation']=False
        start=flow.now()
        def live(actor,op,data):
            self.counter+=1
            self.s,_=flow.apply_request(self.s,{'request_id':str(self.counter),'expected_revision':self.s['revision'],'actor':actor,'operations':[{'type':op,'data':data}]},self.root)
        live('orchestrator','start',{'task':'execute','call':dict(self.s['plan']['roles']['executor'],conversation_id='live')})
        registered=flow.latest(self.s,'execute')['started_at']
        live('executor','finish',{'attempt':'A0001','result':'VERIFIED','evidence':[str(self.e)],'timing':{'started_at':registered,'ended_at':flow.now(),'evidence':[str(self.e)]}})
        self.assertIsNotNone(flow.latest(self.s,'execute')['timing'])

    def test_path_import_compatible_with_simulation_harness(self):
        code="import importlib.util,sys; s=importlib.util.spec_from_file_location('flow',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert callable(m.tracking.metrics)"
        result=subprocess.run([sys.executable,'-c',code,str(Path(flow.__file__))],cwd=self.root,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_completed_elapsed_and_reopen(self):
        for tid,begin,end in [('execute',10,20),('regular',30,40),('third',40,50),('done',60,70)]:
            self.start(tid,begin);self.finish(tid,'RELEASE' if tid in {'regular','third'} else 'VERIFIED',end)
        m=self.metrics(100)['batches']['B']
        self.assertEqual(m['elapsed_seconds'],60);self.assertFalse(m['elapsed_open'])
        self.apply('executor','change_subject',{'subject':'code','version':'v2','evidence':[str(self.e)]},80)
        m=self.metrics(100)['batches']['B']
        self.assertTrue(m['elapsed_open']);self.assertEqual(m['elapsed_seconds'],90)
    def test_decision_actual_timing_is_separate(self):
        self.apply('executor','signal',{'id':'D','signal':'test','reason':'fixture','blocks':['execute'],'evidence':[str(self.e)]},10)
        self.apply('architect','decision_start',{'id':'D','call':dict(self.s['plan']['roles']['architect'],conversation_id='pending:decision')},20)
        self.apply('architect','resolve',{'id':'D','summary':'confirm','requirements':[{'id':'c','kind':'confirmation','owner':'architect','depends_on':[],'instruction':'confirm'}],'evidence':[str(self.e)],'user_evidence':[],
            'context':{'conversation_id':'actual-architect','mode':'fresh','reason':'first decision'},'call_id':'decision-call','timing':self.timing(22,28)},30)
        m=self.metrics();self.assertEqual(m['decisions'][0]['work_seconds'],6)
        self.assertEqual(m['decisions'][0]['call']['conversation_id'],'actual-architect')
        self.assertEqual(m['totals']['calls'],0)
    def test_observation_rejects_other_actor(self):
        with self.assertRaises(flow.FlowError):self.apply('executor','observe',{})
        self.apply('executor','signal',{'id':'D','signal':'test','reason':'fixture','blocks':['execute'],'evidence':[str(self.e)]})
        with self.assertRaises(flow.FlowError):self.apply('orchestrator','decision_start',{'id':'D','call':dict(self.s['plan']['roles']['architect'],conversation_id='arch')})

if __name__=='__main__':unittest.main(verbosity=2)
