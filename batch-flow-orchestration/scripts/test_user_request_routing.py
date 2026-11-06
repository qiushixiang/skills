#!/usr/bin/env python3
"""Mechanical release and lead-originated questions on isolated ledgers."""
import argparse
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

import dispatch_card as dc
import flow
import test_dispatch_card as card_fixture


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=card_fixture.CardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.build(lambda p:p['roles']['architect'].update(tool='claude-agent',model='opus',effort='default'))
        self.store=self.fixture.store
        self.proof=str(self.store.parent/'proof.txt')

    def state(self):return json.loads(self.store.read_text())

    def apply(self,actor,kind,data):
        s=self.state()
        new,_=flow.apply_request(s,dict(request_id=uuid.uuid4().hex,expected_revision=s['revision'],actor=actor,
            operations=[dict(type=kind,data=data)]),self.store.parent)
        flow.write_atomic(self.store,new)

    def complete_dependencies(self):
        for tid in ['execute','regular','third']:
            s=self.state();t=s['plan']['tasks'][tid]
            self.apply('orchestrator','start',dict(task=tid,call=dict(s['plan']['roles'][t['role']],conversation_id=tid+'-context')))
            self.apply(t['role'],'finish',dict(attempt=flow.latest(self.state(),tid)['id'],
                       result=flow.success_result(t),evidence=[self.proof]))

    def lead_request(self,user_owned=False):
        if user_owned:
            s=self.state();s['plan']['decision_points']['approval']=dict(signal='authorization_required',owner='user',
                blocks=['execute'],instruction='legacy user authorization');flow.write_atomic(self.store,s)
        self.apply('orchestrator','signal',dict(id='D1',signal='authorization_required' if user_owned else 'technical_decision',
            reason='evidence requires a decision',blocks=['execute'],evidence=[self.proof],
            **({'decision_point':'approval'} if user_owned else {})))
        self.apply('architect','resolve',dict(id='D1',summary='Lead has checked the evidence; one user decision remains.',
            requirements=[dict(id='D1.approval',kind='confirmation',owner='user',depends_on=[],
                               instruction='Approve this specific scope after the stated diagnosis.')],
            evidence=[self.proof],user_evidence=[]))

    def test_ready_release_registers_mechanically_without_user_or_extra_lead_gate(self):
        self.complete_dependencies()
        out=self.fixture.next()
        self.assertEqual(out['do'],'done',out)
        self.assertIn('RELEASE done',out['registered'])
        s=self.state();a=flow.latest(s,'done')
        self.assertEqual(a['result'],'VERIFIED')
        self.assertEqual(a['call']['conversation_id'],'s1')
        self.assertFalse(s['decisions'])
        self.assertEqual(len([a for a in s['state']['attempts'] if a['task']=='done']),1)
        revision=s['revision']
        self.assertEqual(self.fixture.next()['do'],'done')
        self.assertEqual(self.state()['revision'],revision)

    def test_invalid_dependency_evidence_cannot_release(self):
        self.complete_dependencies();Path(self.proof).write_text('changed')
        out=self.fixture.next()
        self.assertNotEqual(out['do'],'ask_user')
        self.assertIsNone(flow.latest(self.state(),'done'))

    def test_explicit_lead_user_condition_blocks_release_until_confirmed(self):
        self.complete_dependencies()
        self.apply('orchestrator','signal',dict(id='D1',signal='technical_decision',reason='actual unresolved scope question',
                                              blocks=['done'],evidence=[self.proof]))
        self.apply('architect','resolve',dict(id='D1',summary='One specific user permission is required.',
            requirements=[dict(id='D1.permission',kind='confirmation',owner='user',depends_on=[],instruction='Approve the diagnosed scope.')],
            evidence=[self.proof],user_evidence=[]))
        before=self.store.read_bytes()
        self.assertEqual(self.fixture.next()['do'],'ask_user')
        self.assertEqual(self.fixture.next()['do'],'ask_user')
        self.assertEqual(before,self.store.read_bytes())
        self.assertIsNone(flow.latest(self.state(),'done'))
        self.apply('orchestrator','decision_progress',dict(id='D1',status='confirmed',requirement='D1.permission',evidence=[self.proof]))
        out=self.fixture.next()
        self.assertEqual(out['do'],'done',out)
        self.assertIn('CLOSE_DECISION D1',out['registered'])
        self.assertIn('RELEASE done',out['registered'])

    def test_model_and_authorization_gaps_dispatch_lead_instead_of_asking_user(self):
        s=self.state();del s['plan']['roles']['executor'];flow.write_atomic(self.store,s)
        out=self.fixture.next()
        self.assertEqual(out['do'],'call',out)
        self.assertEqual(out['input']['subagent_type'],'bf-architect')
        self.assertEqual(len(self.state()['decisions']),1)
        # A repeat returns the same pending call, not another diagnosis or a user question.
        repeated=self.fixture.next()
        self.assertEqual(repeated['card'],out['card'])
        self.assertEqual(len(self.state()['decisions']),1)

    def test_authorization_gap_also_goes_to_lead(self):
        s=self.state();s['state']['authorized_batches']=[];flow.write_atomic(self.store,s)
        out=self.fixture.next()
        self.assertEqual(out['do'],'call',out)
        self.assertEqual(out['input']['subagent_type'],'bf-architect')

    def test_unavailable_lead_is_reported_not_turned_into_user_choices(self):
        s=self.state();del s['plan']['roles']['executor']
        s['plan']['roles']['architect']['tool']='unavailable-role-tool';flow.write_atomic(self.store,s)
        out=self.fixture.next()
        self.assertEqual(out['do'],'report',out)
        self.assertNotIn('items',out)
        self.assertTrue(out['problems'])

    def test_only_formal_lead_request_is_forwarded_verbatim(self):
        self.lead_request()
        before=self.store.read_bytes();out=self.fixture.next()
        self.assertEqual(out['do'],'ask_user',out)
        self.assertEqual(len(out['items']),1)
        item=out['items'][0]
        self.assertEqual(item['initiated_by'],'architect')
        self.assertEqual(item['decision'],'D1')
        self.assertEqual(item['requirement'],'D1.approval')
        self.assertEqual(item['todo'],'Approve this specific scope after the stated diagnosis.')
        self.assertEqual(before,self.store.read_bytes())

    def test_untrusted_or_stale_user_request_is_not_forwarded(self):
        self.lead_request()
        s=self.state()
        for h in s['history']:
            if any(op['type']=='resolve' for op in h['operations']):h['actor']='orchestrator'
        flow.write_atomic(self.store,s)
        self.assertEqual(self.fixture.next()['do'],'report')
        Path(self.proof).write_text('changed')
        self.assertEqual(self.fixture.next()['do'],'report')

    def test_user_owned_decision_requires_lead_request_first(self):
        self.lead_request(user_owned=True)
        self.assertEqual(self.state()['decisions']['D1']['owner'],'user')
        out=self.fixture.next()
        self.assertEqual(out['do'],'ask_user',out)
        self.assertEqual(out['items'][0]['initiated_by'],'architect')

    def test_unrecognized_coordinator_action_never_becomes_a_user_question(self):
        gate=dict(actions=[dict(kind='UNKNOWN_REGISTER',target='execute',instruction='coordinator item',
                               orchestrator_action=dict(mode='register'))],user_actions=[],running=[],disposition='CONTINUE')
        with patch.object(dc.flow,'pre_yield',return_value=gate):
            out=dc.next_step(argparse.Namespace(store=str(self.store),runtime=str(self.fixture.runtime)))
        self.assertEqual(out['do'],'report')
        self.assertNotIn('items',out)

    def test_output_contract_rejects_the_original_release_as_user_question(self):
        original=dict(do='ask_user',items=[dict(kind='RELEASE',target='done',todo='执行协调或机械登记')],
                      say='用户给出原话后登记')
        out=dc.checked_next_output(self.store,original)
        self.assertEqual(out['do'],'report')
        self.assertIn('protocol error',out['problems'][0]['error'])
        self.assertNotIn('items',out)
        self.assertIsNone(flow.latest(self.state(),'done'))

    def test_output_contract_preserves_lead_wording_and_rejects_mixed_actions(self):
        self.lead_request()
        good=self.fixture.next()
        self.assertEqual(dc.checked_next_output(self.store,good),good)
        altered=json.loads(json.dumps(good));altered['items'][0]['todo']='Choose my newly invented options'
        self.assertEqual(dc.checked_next_output(self.store,altered)['do'],'report')
        wait=dict(do='wait',user_items=good['items'],say='waiting for running work')
        self.assertEqual(dc.checked_next_output(self.store,wait),wait)
        for invalid in [dict(do='ask_user',items=[]),dict(do='call',tool='Bash',input={},items=good['items']),
                        dict(do='done',user_items=good['items']),dict(do='report',tool='Bash',input={}),
                        dict(do='unknown'),dict(do='wait',user_items='not an array')]:
            with self.subTest(invalid=invalid):
                result=dc.checked_next_output(self.store,invalid)
                self.assertEqual(result['do'],'report')
                self.assertTrue(result['problems'])

    def test_cli_checks_output_before_printing_it(self):
        import contextlib,io,sys
        unsafe=dict(do='ask_user',items=[dict(kind='RELEASE',target='done',todo='approve')])
        output=io.StringIO()
        with patch.object(sys,'argv',['dispatch_card.py','next','--store',str(self.store)]), \
             patch.object(dc,'next_step',return_value=unsafe),contextlib.redirect_stdout(output):
            self.assertEqual(dc.main(),0)
        report=json.loads(output.getvalue())
        self.assertEqual(report['do'],'report')
        self.assertNotIn('items',report)


if __name__=='__main__':unittest.main()
