"""Invariant I-D: every unfinished order has exactly one way forward (design authorization-conveyance-and-env-split.md, A1).

Regression for 0-ethercat_reliability 11-11: the lead blocked an unfinished delivery, the front cleared it with
retry, and the order was left awaiting a disposition that no one would ever give.
"""
from bf import api, routing
from bf.store import BfError, transact
from test_bf_core import Base
import test_bf_delivery


class Disposition(Base):
    legacy_protocol = False
    _p = test_bf_delivery.DeliveryProtocol
    init, work, decide = _p.init, _p.work, _p.decide

    def blocked(self):
        self.work()
        out, dec = self.decide({'blocked': self.impl('D1'), 'reason': '需板测窗口', 'missing': '资源交还后重新裁决'})
        self.assertTrue(out['legal'], out)
        return self.impl('D1')

    def test_front_retry_on_lead_owned_block_returns_to_the_lead(self):
        oid = self.blocked()
        st = self.st()
        self.assertEqual(routing.ops_owner(st.orders[oid]), 'lead')
        self.assertEqual([x['owner'] for x in api.report(self.base)['ops']], ['lead'])
        api.retry(self.base, oid, '用户已协调开发板')
        st = self.st()
        self.assertEqual(routing.disposition(st, st.orders[oid]), 'lead')
        dec = [o for o in st.orders.values() if o['type'] == 'decide' and o['state'] == 'open'
               and o['input'].get('subject') == oid]
        self.assertEqual(dec[0]['input']['trigger'], 'operational_recovery')
        self.assertIn('用户已协调开发板', dec[0]['input']['note'])
        self.assertNotIn(oid, api.runnable(st))

    def test_a_commit_that_strands_an_order_is_refused(self):
        oid = self.blocked()
        before = (self.base / 'orders.json').read_bytes()

        def strand(st):
            st.orders[oid].pop('ops')       # what the old front retry did
        with self.assertRaisesRegex(BfError, '状态不一致'):
            transact(self.base, 'strand', 'test', strand)
        self.assertEqual((self.base / 'orders.json').read_bytes(), before)

    def test_historical_stranded_order_is_reported_and_escalate_repairs_it(self):
        oid = self.blocked()
        # Simulate a store written before the invariant: bypass transact.
        import json
        data = json.loads((self.base / 'orders.json').read_text(encoding='utf-8'))
        data['orders'][oid].pop('ops')
        (self.base / 'orders.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        ops = api.report(self.base)['ops']
        self.assertEqual([(x['order'], x['kind']) for x in ops], [(oid, 'no_disposition')])
        self.assertIn('受阻待处理', api.report_text(self.base))
        api.escalate(self.base, oid, '历史状态：带事实重开负责人裁决')
        st = self.st()
        self.assertEqual(routing.disposition(st, st.orders[oid]), 'lead')
        self.assertEqual(api.report(self.base)['ops'], [])

    def test_unrelated_transactions_are_not_blocked_by_a_historical_strand(self):
        oid = self.blocked()
        import json
        data = json.loads((self.base / 'orders.json').read_text(encoding='utf-8'))
        data['orders'][oid].pop('ops')
        (self.base / 'orders.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        api.stop(self.base)
        api.resume(self.base)

    def test_superseded_order_never_shows_as_blocked(self):
        oid = self.blocked()

        def supersede(st):
            st.orders[oid]['state'] = 'superseded'
        transact(self.base, 'supersede', 'test', supersede)
        self.assertEqual(api.report(self.base)['ops'], [])

    def test_mechanical_faults_stay_with_the_front(self):
        oid = self.impl('D1')

        def fault(st):
            routing.pause(st, st.orders[oid], 'CLI 账号失效', 'tool_failure')
        transact(self.base, 'fault', 'test', fault)
        self.assertEqual(api.report(self.base)['ops'][0]['owner'], 'front')
        api.retry(self.base, oid, '已切换账号')
        self.assertIn(oid, api.runnable(self.st()))




class Conveyance(Base):
    """A2: structured state reaches every card, and a block waiting on a fact reopens when the fact changes."""
    legacy_protocol = False
    _p = test_bf_delivery.DeliveryProtocol
    init, work, decide = _p.init, _p.work, _p.decide

    def block(self, waits_on=None):
        self.work()
        action = {'blocked': self.impl('D1'), 'reason': '计划写明板测暂缓', 'missing': '解除暂缓后重新裁决'}
        if waits_on is not None:
            action['waits_on'] = waits_on
        return self.decide(action)

    def open_decides(self, oid):
        return [o for o in self.st().orders.values() if o['type'] == 'decide' and o['state'] == 'open'
                and o['input'].get('subject') == oid]

    def test_card_exports_current_holds_and_adjustments(self):
        from bf import adjust, cards
        adjust.hold(self.base, ['B1'], self.approval, reason='另一任务占用开发板')
        st = self.st()
        oid = self.impl('D1')
        text = cards.render(st, oid, api.assign(st, oid))
        self.assertIn('当前生效状态（以此为准）', text)
        self.assertIn('B1（另一任务占用开发板）', text)
        adjust.unhold(self.base, ['B1'], self.approval)
        st = self.st()
        text = cards.render(st, oid, api.assign(st, oid))
        self.assertIn('暂缓（hold）：无', text)
        self.assertIn('unhold B1（批准 %s' % self.approval, text)

    def test_unhold_reopens_a_block_that_waits_on_it(self):
        from bf import adjust
        adjust.hold(self.base, ['B1'], self.approval, reason='占板')
        adjust.unhold(self.base, ['B1'], self.approval)
        adjust.hold(self.base, ['B1'], self.approval, reason='占板')
        # the lead's block names the hold it waits on
        st = self.st()
        out, _ = self.block([{'hold': 'B1'}])
        self.assertTrue(out['legal'], out)
        oid = self.impl('D1')
        self.assertEqual(self.open_decides(oid), [])
        adjust.unhold(self.base, ['B1'], self.approval)
        reopened = self.open_decides(oid)
        self.assertEqual([d['input']['trigger'] for d in reopened], ['condition_changed'])
        self.assertIn('unhold', reopened[0]['input']['changed'])
        self.assertEqual(reopened[0]['input']['prior_problem'], '计划写明板测暂缓')

    def test_grant_and_revision_reopen_matching_blocks_only(self):
        from bf import adjust
        out, _ = self.block([{'grant': 'D1'}])
        self.assertTrue(out['legal'], out)
        oid = self.impl('D1')
        api.revise_plan(self.base, self.st().plan, self.approval)
        self.assertEqual(self.open_decides(oid), [], 'revision is not what this block waits on')
        adjust.grant(self.base, 'D1', self.approval, primary=1)
        self.assertEqual(len(self.open_decides(oid)), 1)

    def test_block_without_waits_on_stays_until_someone_acts(self):
        from bf import adjust
        out, _ = self.block()
        self.assertTrue(out['legal'], out)
        api.revise_plan(self.base, self.st().plan, self.approval)
        self.assertEqual(self.open_decides(self.impl('D1')), [])
        self.assertTrue(self.st().orders[self.impl('D1')]['ops'])

    def test_waits_on_must_be_mechanical(self):
        st = self.st()
        for bad in ([{'resource': 'board'}], [{'hold': 'nowhere'}], [{'revision': 'yes'}], [], 'B1'):
            self.assertTrue(routing.waits_on_problem(st, bad), bad)
        self.assertEqual(routing.waits_on_problem(st, [{'hold': 'B1'}, {'grant': 'D1'}, {'revision': True}]), '')
        out, _ = self.block([{'resource': 'board'}])
        self.assertFalse(out['legal'])

class ReviewFindings(Base):
    legacy_protocol = False
    _p = test_bf_delivery.DeliveryProtocol
    init, work, decide = _p.init, _p.work, _p.decide

    def test_retry_never_silently_clears_a_lead_block(self):
        oid = self.impl('D1')

        def block(st):
            routing.pause(st, st.orders[oid], '负责人判定受阻', 'technical_block', missing='m')
        transact(self.base, 'block', 'test', block)
        api.retry(self.base, oid, '前端认为已恢复')
        st = self.st()
        self.assertNotIn(oid, api.runnable(st))
        self.assertEqual(routing.disposition(st, st.orders[oid]), 'waiting')

    def test_historical_strand_does_not_block_commits_that_touch_it(self):
        import json
        self.work()
        oid = self.impl('D1')
        data = json.loads((self.base / 'orders.json').read_text(encoding='utf-8'))
        for o in data['orders'].values():
            if o['type'] == 'decide':
                o['state'] = 'done'
        (self.base / 'orders.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

        def touch(st):
            st.orders[oid].setdefault('ops_log', []).append({'op': 'note'})
        transact(self.base, 'touch', 'test', touch)

    def test_drift_clears_after_revision_without_a_decide(self):
        oid = self.impl('D1')

        def drift(st):
            routing.pause(st, st.orders[oid], '环境不一致', 'environment_drift', missing='m', waits_on=[{'revision': True}])
        transact(self.base, 'drift', 'test', drift)
        api.revise_plan(self.base, self.st().plan, self.approval)
        st = self.st()
        self.assertFalse(st.orders[oid].get('ops'))
        self.assertIn(oid, api.runnable(st))

    def test_wait_on_a_change_made_by_the_same_bundle_is_rejected(self):
        self.work()
        out, _ = self.decide({'blocked': self.impl('D1'), 'reason': 'r', 'missing': 'm', 'waits_on': [{'revision': True}]})
        self.assertTrue(out['legal'], out)
        from bf import gate
        st = self.st()
        problem = gate.validate_decision(st, {'diagnosis': 'd', 'actions': [
            {'grant': {'D1': {'primary': 1}}},
            {'blocked': self.impl('D1'), 'reason': 'r', 'missing': 'm', 'waits_on': [{'grant': 'D1'}]}]})
        self.assertIn('已在本裁决中改变', problem)


if __name__ == '__main__':
    import unittest
    unittest.main()
