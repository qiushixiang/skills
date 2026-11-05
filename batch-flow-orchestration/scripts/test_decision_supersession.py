"""Historical scope withdrawal must not impersonate passed recovery gates."""
import copy
import unittest
import flow
import test_flow


class DecisionSupersessionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_flow.FlowTests(methodName='test_full_release'); self.fixture.setUp()
        c = self.fixture; e = str(c.e)
        c.apply('architect','signal',dict(id='OLD',signal='budget_exhausted',reason='old repair',blocks=['recovery'],evidence=[e]))
        c.apply('architect','resolve',dict(id='OLD',summary='old scope',requirements=[dict(id='OLD.budget',kind='confirmation',owner='user',depends_on=[],instruction='approve additional budget')],evidence=[e],user_evidence=[]))
        c.apply('architect','signal',dict(id='NEW',signal='scope_correction',reason='user withdrew old scope',blocks=[],evidence=[e]))
        c.apply('architect','resolve',dict(id='NEW',summary='new scoped review still required',requirements=[dict(id='NEW.review',kind='task',target='regular',versions={'code':'v1'},depends_on=[],instruction='independent new review')],evidence=[e],user_evidence=[e]))
        p = copy.deepcopy(c.s['plan']);p['tasks']['recovery']['retired']=True
        c.apply('architect','plan_update',dict(plan=p,decision_id='NEW',context=dict(conversation_id='scope-author',mode='fresh',reason='withdraw old scope')))
        self.data=dict(id='OLD',replacement='NEW',reason='old path formally withdrawn',evidence=[e],context=dict(conversation_id='scope-author',mode='fresh',reason='historical cleanup'))

    def tearDown(self): self.fixture.tearDown()

    def test_supersession_preserves_history_and_does_not_pass_new_review(self):
        c=self.fixture;old=copy.deepcopy(c.s['decisions']['OLD']);state=copy.deepcopy(c.s['state']);budgets=copy.deepcopy(c.s['plan']['budgets'])
        self.assertTrue(any(a['target']=='OLD.budget' for a in flow.status(c.s)['actions']))
        c.apply('architect','supersede_decision',self.data)
        report=flow.status(c.s)
        self.assertEqual(report['decision_states']['OLD'],'superseded')
        self.assertFalse(any(a.get('decision')=='OLD' for a in report['actions']))
        self.assertEqual(c.s['decisions']['OLD']['resolution'],old['resolution'])
        self.assertEqual(c.s['state']['attempts'],state['attempts'])
        self.assertEqual(c.s['state']['confirmations'],state['confirmations'])
        self.assertEqual(c.s['plan']['budgets'],budgets)
        self.assertFalse(flow.decision_closed(c.s,c.s['decisions']['OLD']))
        self.assertEqual(flow.blockers(c.s,'recovery'),['OLD'])
        self.assertFalse(flow.success(c.s,'regular'))
        self.assertEqual(report['decision_states']['NEW'],'decided')
        with self.assertRaises(flow.FlowError):c.apply('architect','supersede_decision',self.data)

    def test_cannot_supersede_active_or_still_required_scope(self):
        c=self.fixture
        for mode in ['active','dependent']:
            saved=copy.deepcopy(c.s)
            if mode=='active':c.s['plan']['tasks']['recovery']['retired']=False
            else:c.s['plan']['tasks']['execute']['depends_on']=['recovery']
            with self.assertRaises(flow.FlowError):c.apply('architect','supersede_decision',self.data)
            c.s=saved

    def test_requires_original_user_authorization_and_formal_plan_change(self):
        c=self.fixture
        for mode in ['authorization','plan','replacement']:
            saved=copy.deepcopy(c.s)
            if mode=='authorization':c.s['decisions']['NEW']['resolution']['user_evidence']={}
            elif mode=='plan':c.s['state']['plan_changes']=[]
            else:self.data['replacement']='OLD'
            with self.assertRaises(flow.FlowError):c.apply('architect','supersede_decision',self.data)
            c.s=saved;self.data['replacement']='NEW'
        c.e.write_text('changed original evidence')
        with self.assertRaises(flow.FlowError):c.apply('architect','supersede_decision',self.data)

    def test_architect_only_and_no_reusing_retired_budget_decision(self):
        c=self.fixture
        with self.assertRaises(flow.FlowError):c.apply('orchestrator','supersede_decision',self.data)
        c.apply('architect','supersede_decision',self.data)
        with self.assertRaises(flow.FlowError):c.apply('orchestrator','decision_progress',dict(id='OLD',status='confirmed',requirement='OLD.budget',evidence=[str(c.e)]))
        with self.assertRaises(flow.FlowError):c.apply('architect','plan_update',dict(plan=copy.deepcopy(c.s['plan']),decision_id='OLD',context=self.data['context']))
        p=copy.deepcopy(c.s['plan']);p['tasks']['recovery']['retired']=False
        with self.assertRaises(flow.FlowError):c.apply('architect','plan_update',dict(plan=p,decision_id='NEW',context=self.data['context']))


if __name__ == '__main__':unittest.main()
