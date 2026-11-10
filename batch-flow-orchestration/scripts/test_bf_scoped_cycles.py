"""One plan can append new-cycle targets without reinterpreting any historical target."""
import copy
from bf import api, cycles, model, policy, adjust, changes, cards, review_budget
from bf.store import BfError
from test_bf_core import Base, make_plan


def appended(plan):
    p=copy.deepcopy(plan)
    p['batches'].append({'id':'B4','title':'new work'})
    p['deliverables']['N1']={'batch':'B4','title':'new target','instruction':'bounded work',
        'acceptance':['works'],'depends_on':['D1'],
        'execution':{'model':'cycles-v1','rounds':{'primary':1,'backup':1,'lead':0}}}
    p['review_groups']['NR']={'batch':'B4','deliverables':['N1']}
    p['envelope']['batches'].append('B4')
    return p


class ScopedRounds(Base):
    legacy_protocol=False

    def add(self):
        api.revise_plan(self.base,appended(self.st().plan),self.approval)

    def send(self,oid):
        rid=api.start(self.base,oid)
        text=('交付状态：已交付' if self.st().orders[oid]['control_protocol']=='delivery-v2' else '下一步：交付')+'\n阻断关闭：B1=已修复'
        result=api.deliver(self.base,oid,rid,text)
        self.assertTrue(result['legal'],result)
        return rid

    def review(self,passed=False):
        oid=self.one('review','NR')['id'];rid=api.start(self.base,oid,conversation_id='independent')
        text=('结果：RELEASE\n未复跑原因：离线测试' if passed else
              '结果：FIX_REQUIRED\n阻断：B1 | 返修:N1 | 方案级:否 | 关闭条件:修复')
        result=api.deliver(self.base,oid,rid,text)
        self.assertTrue(result['legal'],result)

    def test_append_keeps_historical_orders_and_policy(self):
        rid=self.send(self.impl('D1'))
        before=copy.deepcopy(self.st()); self.add();after=self.st()
        for oid,order in before.orders.items():
            self.assertEqual(after.orders[oid],order)
        self.assertEqual(after.plan['policy'],before.plan['policy'])
        self.assertFalse(cycles.enabled(after.plan,'D1'))
        self.assertTrue(cycles.enabled(after.plan,'N1'))
        self.assertTrue(cycles.enabled(after.plan,'NR'))
        self.assertNotIn('execution_cycle',after.orders[self.impl('D1')]['runs'][0])
        self.assertTrue(after.orders[self.impl('D1')]['runs'][0]['budget_managed'])

    def test_new_round_repair_and_switch_under_legacy_plan(self):
        self.send(self.impl('D1')); self.add()
        initial=self.send(self.impl('N1')); self.review()
        repair=self.one('repair','N1')['id'];self.send(repair)
        self.assertEqual(cycles.spent(self.st(),'N1'),{'primary':1})
        self.review()
        self.assertEqual(api.assign(self.st(),repair)['stage'],'backup')
        self.send(repair);self.review(True)
        self.assertEqual(cycles.spent(self.st(),'N1'),{'primary':1,'backup':1})
        self.assertFalse([o for o in self.st().orders.values() if o['type']=='decide' and o['target']=='N1'])

    def test_grants_remain_in_scoped_rounds(self):
        self.add();before=copy.deepcopy(self.st().plan['policy'])
        adjust.grant(self.base,'N1',self.approval,primary=1)
        st=self.st()
        self.assertEqual(st.plan['policy'],before)
        self.assertEqual(cycles.config(st.plan,'N1')['primary'],2)
        self.assertEqual(policy.rounds(st.plan,'NR','review'),6)
        with self.assertRaises(BfError):adjust.grant(self.base,'NR',self.approval,review=1)
        with self.assertRaises(BfError):adjust.grant(self.base,'N1',self.approval,repair=1)

    def test_role_decision_grant_uses_scoped_storage(self):
        from bf import register
        from bf.store import transact
        self.add()
        def increase(st):
            register._execute(st,{'id':'test-source'}, {'grant':{'N1':{'primary':1}}},None)
        transact(self.base,'test','test',increase)
        st=self.st()
        self.assertNotIn('N1',st.plan['policy']['overrides'])
        self.assertEqual(cycles.config(st.plan,'N1')['primary'],2)

    def test_existing_target_cannot_switch_even_without_calls(self):
        p=copy.deepcopy(self.st().plan)
        p['deliverables']['D1']['execution']={'model':'cycles-v1','rounds':{'primary':1}}
        p['review_groups']={'R1':{'deliverables':['D1']},'R2':{'deliverables':['D2']},'R3':{'deliverables':['D3']}}
        with self.assertRaisesRegex(BfError,'existing target execution model'):
            api.revise_plan(self.base,p,self.approval)

    def test_mixed_model_review_group_rejected(self):
        p=appended(self.st().plan)
        p['review_groups']['NR']['deliverables'].append('D1')
        with self.assertRaisesRegex(BfError,'cannot mix'):model.normalize(p)

    def test_cannot_change_scoped_rounds_without_user_approval(self):
        self.add();p=copy.deepcopy(self.st().plan)
        p['deliverables']['N1']['execution']['rounds']['primary']=9
        self.assertFalse(changes.internal_revision(self.st().plan,p))

    def test_report_and_cards_resolve_target_model(self):
        self.send(self.impl('D1'));self.add();self.send(self.impl('N1'));self.review()
        report=api.report(self.base)
        self.assertEqual(set(report['execution_rounds']),{'N1'})
        self.assertNotIn('"settle"',cards._schema(self.st(),'N1'))
        self.assertIn('"settle"',cards._schema(self.st(),'D1'))
        facts=cards._execution_options(self.st(),self.one('repair','N1'))
        self.assertEqual(facts['budget_owner'],['execution_cycle','N1'])
        rows=review_budget.coverage(self.st().plan)
        self.assertEqual(next(x['available'] for x in rows if x['group']=='NR'),4)
        self.assertEqual(next(x['available'] for x in rows if x['group']=='R12'),32)

    def test_invalid_scoped_config_rejected(self):
        for config in ({'model':'legacy-v1'},{'model':'cycles-v1','rounds':{'repair':1}},
                       {'model':'cycles-v1','rounds':{'primary':True}}):
            p=appended(self.st().plan);p['deliverables']['N1']['execution']=config
            with self.assertRaises(BfError):model.normalize(p)

    def test_old_cli_recovery_stays_legacy_new_uses_cycle(self):
        self.add()
        oid=self.impl('D1');rid=api.start(self.base,oid)
        api.cli_failed(self.base,oid,rid,'transport',1)
        api.cli_failed(self.base,oid,rid,'transport',2)
        self.assertTrue([o for o in self.st().orders.values() if o['input'].get('accounting_only')])
        self.assertEqual(api.assign(self.st(),oid)['stage'],'backup')

    def test_removed_target_cannot_be_readded_with_different_model(self):
        old=copy.deepcopy(self.st().plan)
        without=copy.deepcopy(old)
        del without['deliverables']['D1']
        without['review_groups']['R12']['deliverables']=['D2']
        without['deliverables']['D3']['depends_on']=['D2']
        api.revise_plan(self.base,without,self.approval)
        again=copy.deepcopy(self.st().plan)
        again['deliverables']['D1']=copy.deepcopy(old['deliverables']['D1'])
        again['deliverables']['D1']['execution']={'model':'cycles-v1','rounds':{'primary':1}}
        again['review_groups']['NR']={'deliverables':['D1']}
        with self.assertRaisesRegex(BfError,'existing target execution model'):
            api.revise_plan(self.base,again,self.approval)

    def test_report_ignores_removed_historical_review_groups(self):
        self.send(self.impl('D1'));self.add()
        p=copy.deepcopy(self.st().plan)
        del p['review_groups']['R12']
        p['review_groups']['R1']={'deliverables':['D1']}
        p['review_groups']['R2']={'deliverables':['D2']}
        api.revise_plan(self.base,p,self.approval)
        self.send(self.impl('N1'))
        report=api.report(self.base)
        self.assertEqual(set(report['execution_rounds']),{'N1'})
        self.assertTrue(report['budgets']['N1']>=0)
