"""Two delivery states; unfinished work is disposed by the lead, never auto-continued."""
import copy
from bf import api, cards, cycles, gate
from bf.store import BfError
from test_bf_core import Base
from test_bf_cycles import cycle_plan


class DeliveryProtocol(Base):
    legacy_protocol = False

    def init(self, plan=None):
        api.init(self.base, plan or cycle_plan(primary=1, backup=1, lead=0), self.approval)

    def work(self, oid=None, delivered=False):
        oid = oid or self.impl('D1')
        rid = api.start(self.base, oid, conversation_id='author-session')
        text = ('交付状态：已交付\n阻断关闭：B1=已关闭' if delivered else
                '交付状态：未交付\n未完成说明：已核对资产；尚需补齐映射，需要负责人给出下一步')
        out = api.deliver(self.base, oid, rid, text)
        self.assertTrue(out['legal'], out)
        self.assertEqual(out['outcome'], 'registered')
        return rid

    def decide(self, action):
        oid = self.impl('D1')
        dec = next(o for o in self.st().orders.values() if o['type']=='decide' and
                   o['state']=='open' and o['input'].get('subject')==oid)
        rid = api.start(self.base, dec['id'], conversation_id='lead-session')
        out = api.deliver(self.base, dec['id'], rid, '结果：VERIFIED\n停止原因：完成',
                          decision={'diagnosis':'只处置未完成工作，不评交付质量','actions':[action]})
        return out, dec['id']

    def test_delivered_goes_directly_to_independent_review(self):
        rid = self.work(delivered=True)
        st = self.st(); oid = self.impl('D1')
        self.assertEqual(st.orders[oid]['runs'][-1]['delivery_protocol'], 'delivery-v2')
        self.assertEqual(st.orders[oid]['runs'][-1]['result'], 'SUBMITTED')
        self.assertFalse(api.orders(st, type='decide'))
        review = self.one('review','R1')
        self.assertIn(review['id'],api.runnable(st))
        with self.assertRaises(BfError):api.start(self.base,review['id'],conversation_id='author-session')

    def test_unfinished_does_not_auto_continue_or_open_quality_review(self):
        self.work(); st=self.st(); oid=self.impl('D1')
        self.assertEqual(st.orders[oid]['runs'][-1]['continuations'],0)
        self.assertTrue(st.orders[oid]['awaiting_disposition'])
        self.assertNotIn(self.one('review','R1')['id'],api.runnable(st))
        dec=self.one('decide','D1')
        rendered=cards.render(st,dec['id'],api.assign(st,dec['id']))
        self.assertIn('不评定交付质量',rendered)
        self.assertIn('continue_run',rendered)

    def test_lead_can_continue_same_round_beyond_old_limit_and_restart(self):
        root=self.work(); oid=self.impl('D1'); previous=root
        for n in range(8):
            out,dec=self.decide({'continue_run':previous,'reason':'继续补齐字段映射'})
            self.assertTrue(out['legal'],out)
            st=api.load(self.base)
            self.assertEqual(api.assign(st,oid)['stage'],'primary')
            if n:
                self.assertEqual(st.orders[dec]['runs'][-1]['session_mode'],'resume')
            previous=self.work(delivered=n==7)
            run=self.st().orders[oid]['runs'][-1]
            self.assertEqual(run['execution_cycle']['id'],root)
            self.assertEqual(run['execution_cycle']['phase'],'continue')
            self.assertEqual(cycles.spent(self.st(),'D1'),{'primary':1})
        self.assertEqual(self.st().orders[oid]['state'],'done')
        self.assertEqual(len(self.st().orders[oid]['runs']),9)
        self.assertIn(self.one('review','R1')['id'],api.runnable(self.st()))

    def test_retry_ends_round_and_selects_remaining_author(self):
        self.work();oid=self.impl('D1')
        out,_=self.decide({'retry':oid,'reason':'结束本轮，换有剩余轮次的执行者处理'})
        self.assertTrue(out['legal'],out)
        self.assertEqual(api.assign(self.st(),oid)['stage'],'backup')
        self.work(delivered=True)
        self.assertEqual(cycles.spent(self.st(),'D1'),{'primary':1,'backup':1})

    def test_bad_or_old_executor_protocol_is_sent_to_lead_without_format_retry(self):
        oid=self.impl('D1');rid=api.start(self.base,oid)
        out=api.deliver(self.base,oid,rid,'下一步：继续')
        self.assertFalse(out['legal']);self.assertEqual(out['outcome'],'registered')
        self.assertEqual(out['result'],'HANDOFF')
        self.assertEqual(self.st().orders[oid]['runs'][-1]['continuations'],0)
        self.assertTrue(api.orders(self.st(),type='decide'))

    def test_duplicate_status_and_missing_reason_do_not_pass_as_delivered(self):
        order=self.st().orders[self.impl('D1')]
        for text in ('交付状态：已交付\n交付状态：未交付','交付状态：未交付',
                     '交付状态：已交付\n下一步：交付'):
            self.assertFalse(gate.judge_delivery(order,text)[0])

    def test_continuation_cannot_target_another_run_or_mix_with_retry(self):
        rid=self.work(); st=self.st();dec=self.one('decide','D1');oid=self.impl('D1')
        before=copy.deepcopy(st.orders)
        for acts in ([{'continue_run':'missing','reason':'continue'}],
                     [{'continue_run':rid,'reason':'continue'},{'retry':oid,'reason':'new round'}],
                     [{'continue_run':rid,'reason':'continue'},{'continue_run':rid,'reason':'again'}]):
            self.assertTrue(gate.validate_decision(st,{'diagnosis':'test','actions':acts},dec))
        self.assertEqual(st.orders,before)

    def test_completed_work_cannot_be_continued(self):
        rid=self.work(delivered=True)
        dec={'id':'fake','input':{'subject':self.impl('D1'),'run':rid}}
        self.assertTrue(cycles.validate_continuation(self.st(),dec,{'continue_run':rid,'reason':'again'}))

    def test_original_repair_can_continue_without_an_extra_repair_entitlement(self):
        root=self.work(delivered=True)
        def reject():
            rev=self.one('review','R1');rid=api.start(self.base,rev['id'],conversation_id='review-session')
            api.deliver(self.base,rev['id'],rid,'结果：FIX_REQUIRED\n阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:修复')
        reject();rep=self.one('repair','D1')['id'];rid=self.work(rep)
        dec=next(o for o in self.st().orders.values() if o['type']=='decide' and o['input'].get('subject')==rep)
        dr=api.start(self.base,dec['id'])
        out=api.deliver(self.base,dec['id'],dr,'结果：VERIFIED',decision={'diagnosis':'同轮完成返修',
            'actions':[{'continue_run':rid,'reason':'继续修复 B1'}]})
        self.assertTrue(out['legal'],out);self.work(rep,delivered=True);reject()
        self.assertEqual(api.assign(self.st(),rep)['stage'],'backup')
        self.assertEqual(cycles.spent(self.st(),'D1'),{'primary':1})

    def test_cli_failure_still_retries_once_and_advances(self):
        oid=self.impl('D1');rid=api.start(self.base,oid)
        self.assertEqual(api.cli_failed(self.base,oid,rid,'transport',1,not_started=True)['outcome'],'continue')
        self.assertEqual(api.cli_failed(self.base,oid,rid,'transport',2,not_started=True)['next_performer'],'backup')
        self.assertEqual(cycles.spent(self.st(),'D1'),{})

    def test_executor_card_has_two_states_and_persistence_instruction(self):
        oid=self.impl('D1');text=cards.render(self.st(),oid,api.assign(self.st(),oid))
        self.assertIn('交付状态：已交付 | 未交付',text)
        self.assertNotIn('下一步：继续 | 交付 | 移交',text)
        self.assertIn('在当前调用内持续完成',text)

    def test_driver_disposition_then_continuation_has_one_independent_review(self):
        from bf.runner import Runner
        from test_bf_runner import FakeAdapter
        oid=self.impl('D1')
        fake=FakeAdapter(self.base,{
            ('implement','D1'):[{'text':'交付状态：未交付\n未完成说明：缺字段映射，需明确下一步'},
                                {'text':'交付状态：已交付'}],
            ('decide','D1'):[{'text':'结果：VERIFIED','decision':{'diagnosis':'补齐即可，不作质量审阅',
                              'actions':[{'continue_run':oid+'-r1','reason':'补齐字段映射后完成原交付'}]}}],
            ('review','R1'):[{'text':'结果：RELEASE\n未复跑原因：离线状态测试'}]})
        driver=Runner(self.base,fake)
        try:
            for _ in range(16):
                driver.step()
                if self.one('release','B1')['state']=='done':break
            self.assertEqual(self.one('release','B1')['state'],'done')
            self.assertEqual([x['type'] for x in fake.launches],['implement','decide','implement','review'])
            self.assertEqual(cycles.spent(self.st(),'D1'),{'primary':1})
            self.assertEqual(fake.launches[2]['assignment']['session']['mode'],'resume')
        finally:driver.close()

    def test_continuation_bundle_waits_for_approval_and_then_preserves_original_round(self):
        root=self.work();oid=self.impl('D1');dec=self.one('decide','D1')
        rid=api.start(self.base,dec['id'])
        out=api.deliver(self.base,dec['id'],rid,'结果：VERIFIED',decision={'diagnosis':'整包批准后继续',
            'actions':[{'continue_run':root,'reason':'原作者继续补齐映射'}, {'grant':{'D1':{'backup':1}}}]})
        self.assertTrue(out['legal'],out)
        self.assertNotIn('continuation_of',self.st().orders[oid]['input'])
        ask=self.one('ask_user','D1')
        api.answer(self.base,ask['id'],'批准整包处置',True)
        self.assertEqual(api.assign(self.st(),oid)['stage'],'primary')
        self.work(delivered=True)
        self.assertEqual(self.st().orders[oid]['runs'][-1]['execution_cycle']['id'],root)
        self.assertEqual(cycles.spent(self.st(),'D1'),{'primary':1})
