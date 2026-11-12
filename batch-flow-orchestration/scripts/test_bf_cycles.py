"""Behavioral specification of configurable author rounds with exactly one repair."""
import copy
from bf import api, model, policy, cycles, routing, review_budget, adjust, cards
from bf.store import BfError, transact
from test_bf_core import Base, make_plan


def cycle_plan(primary=1, backup=1, lead=1):
    p = make_plan()
    p['policy'] = {'execution_model': 'cycles-v1',
                   'rounds': {'primary': primary, 'backup': backup, 'lead': lead}}
    p['deliverables'] = {'D1': p['deliverables']['D1']}
    p['review_groups'] = {'R1': {'deliverables': ['D1']}}
    p['batches'] = p['batches'][:1]
    p['envelope']['batches'] = ['B1']
    return p


class ExecutionCycles(Base):
    """Cycle accounting invariants using archived intent-v1 orders."""
    legacy_protocol = False

    def setUp(self):
        from unittest.mock import patch
        original = routing.new_order
        def historical(*args, **kwargs):
            order = original(*args, **kwargs)
            if order.get('control_protocol') == 'delivery-v2':
                order['control_protocol'] = 'intent-v1'
            return order
        mock = patch.object(routing, 'new_order', historical)
        mock.start()
        self.addCleanup(mock.stop)
        super().setUp()

    def init(self, plan=None):
        api.init(self.base, plan or cycle_plan(), self.approval)

    def submit(self, oid):
        rid = api.start(self.base, oid, conversation_id='author-'+str(self.counter))
        self.counter += 1
        findings = self.st().orders[oid]['input'].get('findings', [])
        closure = ' '.join(f['id'] + '=已修复' for f in findings)
        out = api.deliver(self.base, oid, rid, '下一步：交付\n交付内容：固定测试产物\n阻断关闭：' + closure)
        self.assertEqual(out['result'], 'SUBMITTED')
        return rid

    def review(self, passed=False):
        o = self.one('review', 'R1')
        rid = api.start(self.base, o['id'], conversation_id='review-independent')
        text = '结果：RELEASE\n固定版本：test-v1\n未复跑原因：纯离线规格'
        if not passed:
            text = ('结果：FIX_REQUIRED\n固定版本：test-v1\n'
                    '阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:必须修复可复现缺陷')
        out = api.deliver(self.base, o['id'], rid, text)
        self.assertTrue(out['legal'], out)
        return o['id']

    def repair(self):
        return self.one('repair', 'D1')['id']

    def test_first_pass_no_repair_or_settlement(self):
        rid = self.submit(self.impl('D1'))
        self.review(True)
        st = self.st()
        self.assertEqual(cycles.spent(st, 'D1'), {'primary': 1})
        self.assertFalse(api.orders(st, type='repair'))
        self.assertFalse(api.orders(st, type='decide'))
        release = self.one('release', 'B1')
        api.release(self.base, release['id'])
        self.assertEqual(self.st().orders[release['id']]['state'], 'done')

    def test_repair_same_author_same_round_no_extra_charge(self):
        root = self.submit(self.impl('D1')); self.review()
        rep = self.repair()
        self.assertEqual(api.assign(self.st(), rep)['stage'], 'primary')
        rid = self.submit(rep)
        run = self.st().orders[rep]['runs'][-1]
        self.assertEqual(run['execution_cycle'], {'id': root, 'phase': 'repair'})
        self.assertEqual(cycles.spent(self.st(), 'D1'), {'primary': 1})
        self.review(True)
        self.assertFalse(api.orders(self.st(), type='decide'))

    def test_rejection_advances_after_one_repair(self):
        self.submit(self.impl('D1')); self.review()
        self.submit(self.repair()); self.review()
        rep = self.repair()
        self.assertEqual(api.assign(self.st(), rep)['stage'], 'backup')
        self.submit(rep)
        self.assertEqual(self.st().orders[rep]['runs'][-1]['execution_cycle']['phase'], 'implement')
        self.review()
        self.assertEqual(api.assign(self.st(), rep)['stage'], 'backup')
        self.submit(rep); self.review()
        self.assertEqual(api.assign(self.st(), rep)['stage'], 'lead')
        self.submit(rep); self.review()
        self.assertEqual(api.assign(self.st(), rep)['stage'], 'lead')
        self.submit(rep); self.review()
        st = self.st()
        self.assertEqual(cycles.spent(st, 'D1'), {'primary': 1, 'backup': 1, 'lead': 1})
        self.assertNotIn(rep, api.runnable(st))
        self.assertEqual(len(self.one('review', 'R1')['runs']), 6)
        self.assertTrue(api.orders(st, type='decide'))
        self.assertFalse(api.orders(st, type='ask_user'))

    def test_configurable_second_primary_round(self):
        self.init_fresh(cycle_plan(primary=2, backup=1, lead=0))
        root = self.submit(self.impl('D1')); self.review()
        self.submit(self.repair()); self.review()
        rep = self.repair()
        self.assertEqual(api.assign(self.st(), rep)['stage'], 'primary')
        second = self.submit(rep)
        self.assertNotEqual(root, second)
        self.assertEqual(cycles.spent(self.st(), 'D1'), {'primary': 2})
        self.review(); self.submit(rep); self.review()
        self.assertEqual(api.assign(self.st(), rep)['stage'], 'backup')

    def test_grant_and_operational_retry_restore_exhausted_repair(self):
        self.init_fresh(cycle_plan(primary=1, backup=0, lead=0))
        self.submit(self.impl('D1')); self.review()
        self.submit(self.repair()); self.review()
        rep = self.repair()
        decision = self.one('decide', 'D1')
        rid = api.start(self.base, decision['id'], conversation_id='lead-recovery')
        out = api.deliver(self.base, decision['id'], rid, '结果：VERIFIED', decision={
            'diagnosis': '原轮返修已耗尽，等待可用实施轮次',
            'actions': [{'blocked': rep, 'reason': '执行轮次用尽', 'missing': '需可用实施轮次',
                         'waits_on': [{'grant': 'D1'}]}]})
        self.assertTrue(out['legal'], out)
        self.assertIsNone(self.st().orders[rep]['performer'])
        self.assertNotIn(rep, api.runnable(self.st()))
        with self.assertRaises(BfError):
            api.retry(self.base, rep + 'x', '未知工单')
        # A lead block is never cleared by the front; the approved grant reopens the lead's disposition.
        adjust.grant(self.base, 'D1', self.approval, primary=1, lead=1)
        reopened = [o for o in self.st().orders.values() if o['type'] == 'decide' and o['state'] == 'open'
                    and o['input'].get('subject') == rep]
        self.assertEqual([o['input']['trigger'] for o in reopened], ['condition_changed'])
        rid = api.start(self.base, reopened[0]['id'], conversation_id='lead-recovery-2')
        out = api.deliver(self.base, reopened[0]['id'], rid, '结果：VERIFIED', decision={
            'diagnosis': '已批准增额，恢复原工单', 'actions': [{'retry': rep, 'reason': '按新增轮次继续'}]})
        self.assertTrue(out['legal'], out)
        self.assertIn(rep, api.runnable(self.st()))
        self.assertEqual(api.assign(self.st(), rep)['stage'], 'primary')
        self.assertEqual(cycles.spent(self.st(), 'D1'), {'primary': 1})
        run = self.submit(rep)
        self.assertEqual(self.st().orders[rep]['runs'][-1]['execution_cycle'],
                         {'id': run, 'phase': 'implement'})
        self.assertEqual(cycles.spent(self.st(), 'D1'), {'primary': 2})

    def init_fresh(self, plan):
        self.base = self.base.parent / ('fresh-'+str(self.counter))
        api.init(self.base, plan, self.approval)

    def test_unstaffed_backup_skipped(self):
        p = cycle_plan(); del p['roles']['backup_executor']; self.init_fresh(p)
        self.submit(self.impl('D1')); self.review(); self.submit(self.repair()); self.review()
        self.assertEqual(api.assign(self.st(), self.repair())['stage'], 'lead')
        self.assertIsNone(policy.rounds(self.st().plan, 'R1', 'review'))

    def test_restart_keeps_used_repair(self):
        self.submit(self.impl('D1')); self.review(); self.submit(self.repair()); self.review()
        self.assertEqual(api.assign(api.load(self.base), self.repair())['stage'], 'backup')

    def test_new_order_cannot_replenish_rounds(self):
        self.submit(self.impl('D1'))
        def create(st):
            return routing.new_order(st, 'implement', 'D1', {'by': 'order', 'ref': 'manual', 'reason': 'retry'})['id']
        oid = transact(self.base, 'test', 'test', create)
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'backup')
        self.assertEqual(cycles.spent(self.st(), 'D1'), {'primary': 1})

    def test_revision_cannot_take_back_used_round(self):
        self.submit(self.impl('D1'))
        p = copy.deepcopy(self.st().plan); p['policy']['rounds']['primary'] = 0
        with self.assertRaises(BfError): api.revise_plan(self.base, p, self.approval)

    def test_no_implicit_migration_of_live_history(self):
        self.submit(self.impl('D1'))
        p = copy.deepcopy(self.st().plan)
        p['policy'] = {'rounds': {'implement': 3, 'repair': 2, 'review': 20}}
        with self.assertRaisesRegex(BfError, 'fresh task'): api.revise_plan(self.base, p, self.approval)

    def test_reviews_have_no_capacity_or_grants(self):
        p = self.st().plan
        self.assertEqual(review_budget.coverage(p), [])
        adjust.grant(self.base, 'D1', self.approval, primary=1)
        self.assertIsNone(policy.rounds(self.st().plan, 'R1', 'review'))
        with self.assertRaises(BfError): adjust.grant(self.base, 'R1', self.approval, review=1)
        with self.assertRaises(BfError): adjust.grant(self.base, 'D1', self.approval, repair=1)

    def test_legacy_and_cycle_configuration_cannot_mix(self):
        for key in ('implement', 'repair', 'review'):
            p=cycle_plan(); p['policy']['rounds'][key]=1
            with self.assertRaises(BfError): model.normalize(p)
            p=cycle_plan(); p['policy']['overrides']={'D1':{key:1}}
            with self.assertRaises(BfError): model.normalize(p)

    def test_never_started_initial_frees_round(self):
        oid=self.impl('D1'); rid=api.start(self.base,oid)
        api.abandon(self.base,oid,rid,'not launched',not_started=True)
        self.assertEqual(cycles.spent(self.st(),'D1'),{})
        self.assertEqual(api.assign(self.st(),oid)['stage'],'primary')

    def test_cli_failure_switch_does_not_create_settlement(self):
        oid=self.impl('D1'); rid=api.start(self.base,oid)
        api.cli_failed(self.base,oid,rid,'transport',1)
        api.cli_failed(self.base,oid,rid,'transport',2)
        self.assertEqual(api.assign(self.st(),oid)['stage'],'backup')
        self.assertFalse(api.orders(self.st(),type='decide'))

    def test_handoff_after_skipped_primary_uses_remaining_primary_round(self):
        for historical_performer in (None, 'primary', 'backup'):
            self.counter += 1
            self.init_fresh(cycle_plan(primary=1, backup=1, lead=0))
            oid = self.impl('D1'); rid = api.start(self.base, oid)
            api.cli_failed(self.base, oid, rid, 'not started', 1, not_started=True)
            api.cli_failed(self.base, oid, rid, 'not started', 2, not_started=True)
            self.assertEqual(api.assign(self.st(), oid)['stage'], 'backup')
            rid = api.start(self.base, oid)
            api.deliver(self.base, oid, rid, '下一步：移交\n移交说明：设计需要收敛')
            dec = self.one('decide', 'D1'); dr = api.start(self.base, dec['id'])
            action = {'retry': oid, 'reason': '补齐设计中的三个明确缺口'}
            if historical_performer:
                action['performer'] = historical_performer
            out = api.deliver(self.base, dec['id'], dr, '结果：VERIFIED\n停止原因：完成',
                              decision={'diagnosis': '尚有主执行者轮次', 'actions': [action]})
            self.assertTrue(out['legal'], out)
            self.assertEqual(api.assign(api.load(self.base), oid)['stage'], 'primary')
            self.assertEqual(cycles.spent(self.st(), 'D1'), {'backup': 1})
            self.assertFalse(self.st().orders[oid].get('awaiting_disposition'))

    def test_impossible_retry_returns_to_lead_without_crashing_or_mutating_subject(self):
        self.init_fresh(cycle_plan(primary=1, backup=0, lead=0))
        oid = self.impl('D1'); rid = api.start(self.base, oid)
        api.deliver(self.base, oid, rid, '下一步：移交\n移交说明：需要技术处置')
        dec = self.one('decide', 'D1'); dr = api.start(self.base, dec['id'])
        before = copy.deepcopy(self.st().orders[oid])
        out = api.deliver(self.base, dec['id'], dr, '结果：VERIFIED\n停止原因：完成',
                          decision={'diagnosis': '恢复实施', 'actions': [{'retry': oid, 'reason': '继续实现'}]})
        self.assertFalse(out['legal'])
        self.assertEqual(out['outcome'], 'continue')
        self.assertIn('execution allowance', out['reason'])
        self.assertEqual(self.st().orders[oid], before)
        self.assertIsNone(self.st().orders[dec['id']]['runs'][-1]['ended'])
        out = api.deliver(self.base, dec['id'], dr, '结果：VERIFIED\n停止原因：完成',
                          decision={'diagnosis': '无剩余执行轮次', 'actions': [
                              {'blocked': oid, 'reason': '实施轮次用尽', 'missing': '新的可执行处置'}]})
        self.assertTrue(out['legal'], out)
        self.assertEqual(self.st().orders[dec['id']]['state'], 'done')

    def test_never_started_repair_preserves_repair_entitlement(self):
        self.submit(self.impl('D1')); self.review(); oid=self.repair()
        rid=api.start(self.base,oid); api.abandon(self.base,oid,rid,'not launched',not_started=True)
        self.assertEqual(api.assign(self.st(),oid)['stage'],'primary')
        self.submit(oid); self.review()
        self.assertEqual(api.assign(self.st(),oid)['stage'],'backup')

    def test_driver_collects_impossible_retry_and_requests_corrected_disposition(self):
        from bf import runner
        from test_bf_runner import FakeAdapter
        self.init_fresh(cycle_plan(primary=1, backup=0, lead=0))
        oid = self.impl('D1'); rid = api.start(self.base, oid)
        api.deliver(self.base, oid, rid, '下一步：移交\n移交说明：需要处置')
        dec = self.one('decide', 'D1'); assignment = api.assign(self.st(), dec['id'])
        dr = api.start(self.base, dec['id'])
        fake = FakeAdapter(self.base, {('decide', 'D1'): [{'hang': True}]})
        r = runner.Runner(self.base, fake)
        r._dir(dr).mkdir(parents=True, exist_ok=True)
        (r._dir(dr) / 'card-1.md').write_text('original decision card')
        r.inflight[dr] = {'handle': 'completed', 'order': dec['id'], 'assignment': assignment, 'n': 1}
        fake.pending['completed'] = {'text': '结果：VERIFIED\n停止原因：完成',
            'decision': {'diagnosis': '继续', 'actions': [{'retry': oid, 'reason': '继续实现'}]}}
        self.assertTrue(r._collect())
        self.assertEqual(len(fake.launches), 1)
        self.assertIn('execution allowance', fake.launches[0]['card'])
        self.assertEqual(self.st().orders[dec['id']]['runs'][-1]['continuations'], 1)
        self.assertTrue(self.st().orders[oid]['awaiting_disposition'])

    def test_merged_reviews_have_no_capacity(self):
        p=cycle_plan(); p['deliverables']['D2']=copy.deepcopy(p['deliverables']['D1'])
        p['review_groups']['R1']['deliverables'].append('D2')
        p['policy']['overrides']={'D2':{'primary':2,'backup':0,'lead':0}}
        p=model.normalize(p)
        self.assertIsNone(policy.rounds(p,'R1','review'))
        self.assertEqual(review_budget.coverage(p), [])

    def test_report_and_card_show_round_repair(self):
        self.submit(self.impl('D1')); self.review(); oid=self.repair()
        report=api.report(self.base)
        self.assertEqual(report['budgets']['D1'],2)
        rendered=cards.render(self.st(),oid,api.assign(self.st(),oid))
        self.assertIn('execution_round',rendered)

    def test_plan_level_finding_keeps_original_repair(self):
        self.submit(self.impl('D1'))
        o=self.one('review','R1'); rid=api.start(self.base,o['id'])
        out=api.deliver(self.base,o['id'],rid,
            '结果：FIX_REQUIRED\n阻断：B1 | 返修:D1 | 方案级:是 | 关闭条件:设计修正')
        self.assertTrue(out['legal'],out)
        self.assertEqual(api.assign(self.st(),self.repair())['stage'],'primary')
        self.submit(self.repair())
        self.assertEqual(cycles.spent(self.st(),'D1'),{'primary':1})

    def test_model_switch_rejected_even_before_first_call(self):
        p=copy.deepcopy(self.st().plan)
        p['policy']={'execution_model':'legacy-v1','rounds':{'implement':1,'repair':1,'review':20}}
        with self.assertRaisesRegex(BfError,'fresh task'):
            api.revise_plan(self.base,p,self.approval)

    def test_default_new_and_legacy_detection(self):
        p=cycle_plan(); del p['policy']['execution_model']
        self.assertEqual(model.normalize(p)['policy']['execution_model'],'cycles-v1')
        self.assertEqual(model.normalize(make_plan())['policy']['execution_model'],'legacy-v1')

    def test_cycle_review_groups_cannot_override_author_rounds(self):
        p=cycle_plan(); p['policy']['overrides']={'R1':{'primary':5}}
        with self.assertRaises(BfError): model.normalize(p)

    def test_cycle_card_distinguishes_last_round_from_repair(self):
        self.submit(self.impl('D1')); self.review(); oid=self.repair()
        facts=cards._execution_options(self.st(),self.st().orders[oid])
        self.assertEqual(facts['budget_owner'],['execution_cycle','D1'])
        self.assertTrue(facts['candidates'][0]['repair_available'])
        self.assertEqual(facts['candidates'][0]['budget_remaining'],0)
        self.assertNotIn('"settle"',cards._schema(self.st()))
        self.assertIn('"primary": 1',cards._schema(self.st()))
        self.submit(oid)
        self.assertEqual(policy.charge(self.st().orders[oid],self.st().orders[oid]['runs'][-1]),0)

    def test_failed_initial_launch_does_not_free_attempt_that_ran(self):
        oid=self.impl('D1'); rid=api.start(self.base,oid)
        api.abandon(self.base,oid,rid,'lost process without proof')
        self.assertEqual(cycles.spent(self.st(),'D1'),{'primary':1})

    def test_mixed_cli_launch_proofs_do_not_exempt_whole_round(self):
        oid=self.impl('D1'); rid=api.start(self.base,oid)
        api.cli_failed(self.base,oid,rid,'launched and failed',1,not_started=False)
        api.cli_failed(self.base,oid,rid,'not launched',2,not_started=True)
        self.assertEqual(cycles.spent(self.st(),'D1'),{'primary':1})

    def test_runner_completes_author_repair_then_backup_without_settlements(self):
        from bf.runner import Runner
        from test_bf_runner import FakeAdapter
        fix={'text':'结果：FIX_REQUIRED\n阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:修复'}
        release={'text':'结果：RELEASE\n未复跑原因：离线状态规格'}
        submit={'text':'下一步：交付\n阻断关闭：B1=已关闭'}
        adapter=FakeAdapter(self.base,{('implement','D1'):[dict(submit)],
            ('repair','D1'):[dict(submit),dict(submit)],('review','R1'):[dict(fix),dict(fix),release]})
        driver=Runner(self.base,adapter)
        for _ in range(16):
            driver.step()
            if self.one('release','B1')['state']=='done': break
        self.assertEqual(self.one('release','B1')['state'],'done')
        authors=[x['assignment']['stage'] for x in adapter.launches if x['type']!='review']
        self.assertEqual(authors,['primary','primary','backup'])
        self.assertFalse(api.orders(self.st(),type='decide'))
        driver.close()

    def test_two_review_groups_share_one_repair_per_round(self):
        p=cycle_plan(); p['review_groups']['R2']={'deliverables':['D1']}
        self.init_fresh(p)
        self.submit(self.impl('D1')); self.review()
        self.submit(self.repair()); self.review(True)
        second=self.one('review','R2'); rid=api.start(self.base,second['id'],conversation_id='second-independent')
        result=api.deliver(self.base,second['id'],rid,
            '结果：FIX_REQUIRED\n阻断：B2 | 返修:D1 | 方案级:否 | 关闭条件:另一问题')
        self.assertTrue(result['legal'],result)
        pending=api.orders(self.st(),type='repair',target='D1',state_='open')
        self.assertEqual(len(pending),1)
        self.assertEqual(api.assign(self.st(),pending[0]['id'])['stage'],'backup')
        self.submit(pending[0]['id'])
        self.assertEqual(cycles.spent(self.st(),'D1'),{'primary':1,'backup':1})

    def test_prior_review_calls_do_not_block_authors_but_missing_review_does(self):
        oid=self.impl('D1'); st=self.st()
        review=self.one('review','R1')
        for n in range(20):
            review['runs'].append({'run_id':'fake-review-%d'%n,'ended':'now','performer':'reviewer','exempt':False})
        st.orders[review['id']]=review
        self.assertEqual(review_budget.dispatch_shortfalls(st,st.orders[oid]), [])
        self.assertEqual(policy.used(review), {})
        self.assertEqual(policy.spent(st, review), {})
        self.assertEqual(policy.stage(st.plan, review, st), 'reviewer')
        self.assertEqual(cards._execution_options(st, review)['reviewer'], 'reviewer')
        del st.orders[review['id']]
        self.assertTrue(review_budget.dispatch_shortfalls(st,st.orders[oid]))

    def test_many_prior_calls_do_not_block_actual_review_or_reset_author_rounds(self):
        self.submit(self.impl('D1'))
        review = self.one('review', 'R1')
        def history(st):
            for n in range(20):
                st.orders[review['id']]['runs'].append({
                    'run_id': 'archived-review-%d' % n, 'ended': 'now', 'result': 'FIX_REQUIRED',
                    'performer': 'reviewer', 'exempt': False, 'session': 'review-independent',
                    'tool': 'claude', 'model': 'rev-m', 'effort': 'medium', 'session_kb': None})
        transact(self.base, 'fixture', 'test', history)
        self.assertIn(review['id'], api.runnable(self.st()))
        self.review(True)
        self.assertEqual(cycles.spent(self.st(), 'D1'), {'primary': 1})
        self.assertEqual(self.st().orders[review['id']]['state'], 'done')

    def test_cli_accepts_cycle_grant(self):
        import subprocess, sys
        from pathlib import Path
        from test_bf_core import write_smoke_receipts
        write_smoke_receipts(self.base)
        cli=Path(__file__).parent/'bf/cli.py'
        out=subprocess.run([sys.executable,str(cli),'--base',str(self.base),'grant','D1',
                            '--primary','1','--approval',str(self.approval)],capture_output=True,text=True)
        self.assertEqual(out.returncode,0,out.stdout+out.stderr)
        self.assertEqual(dict(cycles.limits(self.st().plan,'D1'))['primary'],2)

    def test_untagged_mixed_configuration_rejected(self):
        p=cycle_plan(); del p['policy']['execution_model']; p['policy']['rounds']['repair']=1
        with self.assertRaisesRegex(BfError,'cannot be mixed'): model.normalize(p)

    def test_cycles_do_not_write_secondary_settlement_ledger(self):
        oid=self.impl('D1'); rid=api.start(self.base,oid)
        api.cli_failed(self.base,oid,rid,'not launched',1,not_started=True)
        api.cli_failed(self.base,oid,rid,'not launched',2,not_started=True)
        st=self.st()
        self.assertFalse(st.orders[oid].get('settlements'))
        self.assertFalse(cycles.spent(st,'D1'))
