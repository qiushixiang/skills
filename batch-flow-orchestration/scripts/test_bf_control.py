"""New default protocol behavior, accounting atomicity and recovery invariants."""
import copy
import json
from bf import api, cards, policy, routing
from bf.store import BfError
from test_bf_core import Base, make_plan, EDIT


class IntentControl(Base):
    legacy_protocol = False

    def start_work(self):
        oid = self.impl('D1')
        return oid, api.start(self.base, oid)

    def send(self, oid, rid, intent, **kwargs):
        return api.deliver(self.base, oid, rid, '下一步：%s\n未完成与限制：测试未通过\n移交说明：无法解决接口问题' % intent, **kwargs)

    def coordinator(self, subject, accounting=False):
        return next(o for o in self.st().orders.values() if o['type'] == 'decide' and o['state'] == 'open'
                    and o['input'].get('subject') == subject and bool(o['input'].get('accounting_only')) == accounting)

    def decide(self, coordinator, actions):
        return self.go(coordinator['id'], self.done(), decision={'diagnosis': '基于调用事实决定下一步', 'actions': actions})

    def settle(self, rid, charge=0):
        return {'settle': rid, 'charge': charge, 'reason': '工具阻塞无有效技术尝试' if not charge else '已形成有效技术尝试'}

    def restore(self, oid, performer='primary'):
        return {'retry': oid, 'performer': performer, 'reason': '已消除工具阻塞，有明确可执行修复方法'}

    def test_default_card_and_bounded_continuation(self):
        oid = self.impl('D1'); st = self.st()
        card = cards.render(st, oid, api.assign(st, oid))
        self.assertIn('下一步：继续 | 交付 | 移交', card)
        self.assertNotIn('结果：VERIFIED | FAILED', card)
        rid = api.start(self.base, oid)
        for i in range(2):
            out = self.send(oid, rid, '继续')
            self.assertEqual(out['outcome'], 'continue')
        out = self.send(oid, rid, '继续')
        self.assertEqual(out['result'], 'HANDOFF')
        o = self.st().orders[oid]
        self.assertEqual(len(o['runs']), 1)
        self.assertTrue(o['awaiting_disposition'])
        self.assertEqual(policy.used(o), {})
        self.assertEqual(policy.committed(self.st(), o), {'primary': 1})

    def test_submission_goes_directly_to_review_with_separate_accounting(self):
        for d in ('D1', 'D2'):
            oid = self.impl(d); self.send(oid, api.start(self.base, oid), '交付')
            o = self.st().orders[oid]
            self.assertEqual(o['state'], 'done')
            self.assertNotIn(self.coordinator(oid, True)['id'], o['needs'])
        review = self.one('review', 'R12')
        self.assertIn(review['id'], api.runnable(self.st(), 10))
        account = self.coordinator(self.impl('D1'), True)
        api.start(self.base, account['id'])
        self.assertIn(review['id'], api.runnable(self.st(), 10))
        st = self.st(); card = cards.render(st, account['id'], {'tool':'claude'})
        self.assertNotIn('do d1', card)
        self.assertNotIn('验收：a1', card)

    def test_handoff_not_retried_until_explicit_disposition(self):
        oid, rid = self.start_work(); self.send(oid, rid, '移交')
        self.assertNotIn(oid, api.runnable(self.st(), 10))
        with self.assertRaises(BfError): api.start(self.base, oid)
        decision = self.coordinator(oid)
        out = self.decide(decision, [self.settle(rid)])
        self.assertFalse(out['legal'])
        self.assertTrue(self.st().orders[oid]['awaiting_disposition'])

    def test_lead_can_resume_original_without_spending_or_choose_backup(self):
        oid, rid = self.start_work(); self.send(oid, rid, '移交')
        self.decide(self.coordinator(oid), [self.settle(rid), self.restore(oid)])
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'primary')
        rid2 = api.start(self.base, oid); self.send(oid, rid2, '移交')
        self.decide(self.coordinator(oid), [self.settle(rid2, 1), self.restore(oid, 'backup')])
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'backup')
        self.assertEqual(policy.spent(self.st(), self.st().orders[oid]), {'primary': 1})
        self.assertEqual(len(self.st().orders[oid]['runs']), 2)

    def test_invalid_protocols_handoff_without_guessing(self):
        for text in ('', '结果：FAILED\n停止原因：未完成', '下一步：other', '下一步：交付\n下一步：移交',
                     '下一步：交付\n结果：VERIFIED'):
            self.reinit(); oid, rid = self.start_work()
            out = api.deliver(self.base, oid, rid, text)
            self.assertEqual(out['result'], 'HANDOFF', text)
            self.assertEqual(out['outcome'], 'registered')

    def test_missing_handoff_reason_still_hands_off(self):
        oid, rid = self.start_work()
        out = api.deliver(self.base, oid, rid, '下一步：移交')
        self.assertTrue(out['legal'])
        self.assertEqual(out['result'], 'HANDOFF')

    def test_unable_is_not_parsed_as_no_issue(self):
        from bf import gate
        self.assertTrue(gate.parse('上升问题：无法解决', self.base)['has_issue'])

    def test_conflicting_settlements_atomic_in_either_order(self):
        for charges in ((0,1),(1,0)):
            self.reinit(); oid, rid = self.start_work(); self.send(oid, rid, '移交')
            out = self.decide(self.coordinator(oid), [self.settle(rid, n) for n in charges] + [self.restore(oid)])
            self.assertFalse(out['legal'])
            self.assertNotIn(rid, self.st().orders[oid].get('settlements', {}))

    def test_same_charge_bundle_is_idempotent(self):
        oid, rid = self.start_work(); self.send(oid, rid, '移交')
        out = self.decide(self.coordinator(oid), [self.settle(rid), self.settle(rid), self.restore(oid)])
        self.assertTrue(out['legal'])
        self.assertEqual(len(self.st().orders[oid]['settlements']), 1)

    def test_only_confirmed_whole_call_unstarted_is_free(self):
        oid, rid = self.start_work(); api.abandon(self.base, oid, rid, 'spawn error', not_started=True)
        self.assertEqual(policy.charge(self.st().orders[oid], self.st().orders[oid]['runs'][0]), 0)
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'primary')
        rid2 = api.start(self.base, oid); api.abandon(self.base, oid, rid2, 'no reply')
        self.assertIsNone(policy.charge(self.st().orders[oid], self.st().orders[oid]['runs'][-1]))
        self.assertTrue(self.st().orders[oid]['awaiting_disposition'])

    def test_failed_continuation_spawn_is_not_whole_call_unstarted(self):
        oid, rid = self.start_work(); self.send(oid, rid, '继续')
        api.abandon(self.base, oid, rid, 'continuation spawn failed', not_started=True)
        self.assertIsNone(policy.charge(self.st().orders[oid], self.st().orders[oid]['runs'][0]))

    def test_recover_requires_settlement_and_explicit_person(self):
        for missing in ('settlement', 'performer'):
            self.reinit(); oid, rid = self.start_work(); self.send(oid, rid, '移交')
            acts = [self.restore(oid)] if missing == 'settlement' else [self.settle(rid), {'retry':oid, 'reason':'try'}]
            out = self.decide(self.coordinator(oid), acts)
            self.assertFalse(out['legal'])

    def test_accounting_is_strictly_one_run_no_quality_actions(self):
        oid, rid = self.start_work(); self.send(oid, rid, '交付')
        acc = self.coordinator(oid, True)
        out = self.decide(acc, [self.settle(rid), {'blocked':oid, 'reason':'x', 'missing':'x'}])
        self.assertFalse(out['legal'])
        self.assertEqual(self.st().orders[oid]['state'], 'done')

    def test_reservations_share_budget_across_orders_and_reloads(self):
        self.reinit(policy={'rounds':{'implement':1,'backup':1,'lead':1,'repair':1,'review':2},'continue_limit':2})
        oid, rid = self.start_work(); self.send(oid, rid, '交付')
        from bf.store import transact
        extra = transact(self.base, 'test_open', 'test', lambda st: routing.new_order(st, 'implement', 'D1', {'by':'test','ref':oid})['id'])
        self.assertIsNone(policy.stage(self.st().plan, self.st().orders[extra], self.st()))
        self.decide(self.coordinator(oid, True), [self.settle(rid)])
        self.assertEqual(api.assign(self.st(), extra)['stage'], 'primary')

    def test_review_verdict_keeps_independent_accounting(self):
        for d in ('D1','D2'):
            oid=self.impl(d); self.send(oid, api.start(self.base,oid), '交付')
        rev=self.one('review','R12'); out=self.go(rev['id'], self.release_text())
        self.assertTrue(out['legal'])
        self.assertEqual(self.st().orders[rev['id']]['state'], 'done')
        self.assertTrue(self.coordinator(rev['id'], True)['input']['accounting_only'])

    def test_invalid_review_requires_lead_not_implicit_retry(self):
        for d in ('D1','D2'):
            oid=self.impl(d); self.send(oid, api.start(self.base,oid), '交付')
        rev=self.one('review','R12'); out=self.go(rev['id'], '结果：FAILED')
        self.assertEqual(out['result'],'HANDOFF')
        self.assertTrue(self.st().orders[rev['id']]['awaiting_disposition'])

    def test_real_edit_and_failed_test_are_observations_not_charges(self):
        oid,rid=self.start_work(); self.send(oid,rid,'移交',events=[EDIT,{'type':'command_execution','command':'false','exit_code':1}])
        o=self.st().orders[oid]; r=o['runs'][-1]
        self.assertTrue(r['edited']); self.assertTrue(r['failed']); self.assertIsNone(policy.charge(o,r))
        self.decide(self.coordinator(oid),[self.settle(rid,1),self.restore(oid)])
        r=self.st().orders[oid]['runs'][-1]
        self.assertTrue(r['edited']); self.assertTrue(r['failed'])

    def test_blocked_disposition_never_implicitly_resumes(self):
        oid,rid=self.start_work(); self.send(oid,rid,'移交')
        out=self.decide(self.coordinator(oid),[self.settle(rid),{'blocked':oid,'reason':'缺少设备','missing':'测试板'}])
        self.assertTrue(out['legal']); self.assertNotIn(oid,api.runnable(self.st(),10))

    def test_retiring_contract_keeps_pending_accounting(self):
        oid,rid=self.start_work(); self.send(oid,rid,'交付')
        p=make_plan(); p['deliverables']['D1']['instruction']='changed'
        api.revise_plan(self.base,p,self.approval,revalidate=['D1'])
        self.assertEqual(self.coordinator(oid,True)['state'],'open')

    def test_settlement_rejects_wrong_run_and_bool(self):
        for invalid in ('bool', 'missing'):
            self.reinit(); oid,rid=self.start_work(); self.send(oid,rid,'交付')
            act={'settle':rid if invalid == 'bool' else 'missing','charge':True if invalid == 'bool' else 0,'reason':'x'}
            out=self.decide(self.coordinator(oid,True),[act]); self.assertFalse(out['legal'])

    def test_real_runner_continuation_submit_account_review_release(self):
        from bf.runner import Runner
        from test_bf_runner import FakeAdapter, release
        self.reinit(envelope={'batches':['B1']})
        script={('implement','D1'):[{'text':'下一步：继续'},{'text':'下一步：交付'}],
                ('implement','D2'):[{'text':'下一步：交付'}],('review','R12'):[release()]}
        for kind,target in (('implement','D1'),('implement','D2'),('review','R12')):
            oid=self.one(kind,target)['id']
            script[('decide',target)]=[{'text':'结果：VERIFIED','decision':{'diagnosis':'有效技术调用','actions':[self.settle(oid+'-r1',1)]}}]
        fake=FakeAdapter(self.base,script); Runner(self.base,fake).run_until_idle()
        self.assertEqual(self.one('release','B1')['state'],'done')
        self.assertEqual(len(self.st().orders[self.impl('D1')]['runs']),1)
        self.assertEqual(fake.launches[1]['resume'],fake.launches[0]['assignment']['session'].get('conversation_id') or 'conv-1')
        self.assertEqual(sum(len(o['settlements']) for o in self.st().orders.values() if 'settlements' in o),3)

    def test_mechanical_not_started_retries_remain_bounded(self):
        oid=self.impl('D1')
        for _ in range(2):
            rid=api.start(self.base,oid); api.abandon(self.base,oid,rid,'spawn failed',not_started=True)
        o=self.st().orders[oid]
        self.assertTrue(o.get('ops'))
        self.assertEqual(policy.used(o),{})
        self.assertNotIn(oid,api.runnable(self.st(),10))

    def test_accounting_replay_preserves_original_history(self):
        from bf.store import transact
        from bf import register
        oid,rid=self.start_work(); self.send(oid,rid,'交付')
        acc=self.coordinator(oid,True); before=copy.deepcopy(self.st().orders[oid]['runs'][0])
        self.decide(acc,[self.settle(rid,1)])
        transact(self.base,'test_replay','test',lambda st:register.apply_actions(st,st.orders[acc['id']],[self.settle(rid,1)],oid))
        self.assertEqual(self.st().orders[oid]['runs'][0],before)
        self.assertEqual(len(self.st().orders[oid]['settlements']),1)
        with self.assertRaises(BfError):
            transact(self.base,'test_conflict','test',lambda st:register.apply_actions(st,st.orders[acc['id']],[self.settle(rid,0)],oid))

    def test_call_context_excludes_repository_baseline(self):
        from bf.store import transact
        oid,rid=self.start_work()
        transact(self.base,'test_observation','test',lambda st:st.orders[oid]['runs'][0].update(
            baseline={'/tmp/missing-bf-control-repository':{'files':{'unrelated-component-marker':'sha'}}}))
        self.send(oid,rid,'交付')
        acc=self.coordinator(oid,True); st=self.st()
        card=cards.render(st,acc['id'],api.assign(st,acc['id']))
        self.assertNotIn('unrelated-component-marker',card)
        self.assertIn('call_facts',card)
        self.assertIn('未完成与限制',card)

    def test_retry_instruction_reaches_fresh_executor_without_full_diagnosis(self):
        oid,rid=self.start_work(); self.send(oid,rid,'移交')
        action=self.restore(oid); action.update(reason='先修接口 X，再运行验证 Y',fresh_session=True)
        coordinator=self.coordinator(oid)
        self.decide(coordinator,[self.settle(rid),action])
        st=self.st(); a=api.assign(st,oid); self.assertEqual(a['session']['mode'],'fresh')
        card=cards.render(st,oid,a)
        self.assertIn('先修接口 X，再运行验证 Y',card)
        self.assertNotIn('基于调用事实决定下一步',card)
        rid2=api.start(self.base,oid); self.send(oid,rid2,'移交')
        action=self.restore(oid); action['reason']='新的当前步骤 Z'
        self.decide(self.coordinator(oid),[self.settle(rid2),action])
        st=self.st(); card=cards.render(st,oid,api.assign(st,oid))
        self.assertIn('新的当前步骤 Z',card)
        self.assertNotIn('先修接口 X，再运行验证 Y',card)

    def test_accounting_candidate_cannot_add_engineering_dependency(self):
        oid,rid=self.start_work(); self.send(oid,rid,'交付')
        account=self.coordinator(oid,True)
        before=list(self.one('release','B1')['needs'])
        out=self.go(account['id'],self.done('候选新项：添加无关功能'),decision={'diagnosis':'预算结算','actions':[self.settle(rid)]})
        self.assertTrue(out['legal'])
        self.assertEqual(api.orders(self.st(),type='triage'),[])
        self.assertEqual(self.one('release','B1')['needs'],before)

    def test_settle_cannot_override_legacy_charge(self):
        from bf.store import transact
        from bf import gate
        oid,rid=self.start_work(); self.send(oid,rid,'移交')
        transact(self.base,'legacy_fixture','test',lambda st:st.orders[oid]['runs'][0].pop('budget_managed'))
        st=self.st(); account=self.coordinator(oid)
        reason=gate.validate_decision(st,{'diagnosis':'不能冲销旧扣账','actions':[self.settle(rid),self.restore(oid)]},account)
        self.assertTrue(reason)
        self.assertEqual(policy.spent(st,st.orders[oid]),{'primary':1})

    def test_wrapped_controlled_entry_hands_off_instead_of_submitting(self):
        from test_bf_core import ENTRY,cmd
        oid,rid=self.start_work()
        out=self.send(oid,rid,'交付',events=[cmd('python3 '+ENTRY,1,'failed')])
        self.assertFalse(out['legal'])
        self.assertEqual(out['result'],'HANDOFF')
        self.assertEqual(self.st().orders[oid]['state'],'open')
        self.assertIn('直接调用',self.coordinator(oid)['input']['issue'])

    def test_explicit_lead_recovery_clears_recorded_block(self):
        from bf.store import transact
        oid,rid=self.start_work(); self.send(oid,rid,'移交')
        self.decide(self.coordinator(oid),[self.settle(rid),{'blocked':oid,'reason':'缺少设备','missing':'测试板'}])
        decision=transact(self.base,'test_resolved','test',lambda st:routing.open_decide(st,st.orders[oid],'resolved',{'issue':'设备已补齐'},force=True)['id'])
        self.decide(self.st().orders[decision],[self.restore(oid)])
        self.assertNotIn('ops',self.st().orders[oid])
        self.assertIn(oid,api.runnable(self.st(),10))
        self.assertEqual(self.st().orders[oid]['ops_log'][-1]['op'],'decide_retry')

    def test_open_cannot_claim_internal_accounting_exemption(self):
        from bf import gate
        oid,rid=self.start_work();self.send(oid,rid,'移交')
        reason=gate.validate_decision(self.st(),{'diagnosis':'开立工作','actions':[
            {'open':'implement','target':'D2','input':{'instruction':'实现','accounting_only':True}},
            self.settle(rid),self.restore(oid)]},self.coordinator(oid))
        self.assertTrue(reason)

    def test_new_review_old_escalation_labels_all_handoff(self):
        for result,why in (('ARCH_DECISION_REQUIRED','待裁决'),('FAILED','技术不可行'),('WAIT_EVIDENCE','外部阻塞')):
            self.reinit()
            for d in ('D1','D2'):
                oid=self.impl(d);self.send(oid,api.start(self.base,oid),'交付')
            rev=self.one('review','R12');out=self.go(rev['id'],'结果：%s\n停止原因：%s\n上升问题：需要负责人处置'%(result,why))
            self.assertEqual(out['result'],'HANDOFF')
            self.assertTrue(self.st().orders[rev['id']]['awaiting_disposition'])
            self.coordinator(rev['id'])

    def test_segment_receipt_is_atomic_and_duplicate_delivery_is_free(self):
        oid,rid=self.start_work()
        first=api.deliver(self.base,oid,rid,'下一步：继续',segment=1)
        again=api.deliver(self.base,oid,rid,'下一步：继续',segment=1)
        self.assertEqual(first,again)
        self.assertEqual(self.st().orders[oid]['runs'][0]['segments'],1)
        final=api.deliver(self.base,oid,rid,'下一步：交付',segment=2)
        self.assertEqual(final,api.deliver(self.base,oid,rid,'下一步：交付',segment=2))
        self.assertEqual(self.st().orders[oid]['runs'][0]['segments'],2)

    def test_restart_after_collect_before_next_launch_does_not_recount(self):
        from bf.runner import Runner
        from test_bf_runner import FakeAdapter
        oid,rid=self.start_work();a=api.assign(self.st(),self.impl('D2'))
        first=Runner(self.base,FakeAdapter(self.base,{}))
        first.inflight[rid]={'handle':{'old':'finished'},'order':oid,'assignment':a,'n':1};first._persist(rid)
        api.deliver(self.base,oid,rid,'下一步：继续',conversation_id='original-session',segment=1)
        restarted=Runner(self.base,FakeAdapter(self.base,{('implement','D1'):[{'text':'下一步：交付'}]}))
        restarted._recover(self.st())
        self.assertIsNone(restarted.inflight[rid]['handle'])
        self.assertEqual(restarted.inflight[rid]['n'],2)
        self.assertEqual(self.st().orders[oid]['runs'][0]['segments'],1)
        restarted._launch_pending();restarted._collect()
        self.assertEqual(self.st().orders[oid]['runs'][0]['segments'],2)
        self.assertEqual(self.st().orders[oid]['state'],'done')

    def test_uncertain_launch_is_handed_off_not_launched_again(self):
        from bf.runner import Runner
        from test_bf_runner import FakeAdapter
        oid,rid=self.start_work();first=Runner(self.base,FakeAdapter(self.base,{}))
        first.inflight[rid]={'handle':None,'order':oid,'assignment':{},'n':1,'launching':True};first._persist(rid)
        fake=FakeAdapter(self.base,{})
        restarted=Runner(self.base,fake);restarted._recover(self.st())
        self.assertEqual(fake.launches,[])
        self.assertTrue(self.st().orders[oid]['awaiting_disposition'])
        self.assertIsNone(policy.charge(self.st().orders[oid],self.st().orders[oid]['runs'][0]))


if __name__ == '__main__':
    import unittest
    unittest.main()
