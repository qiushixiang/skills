"""Review recovery instructions must expire when a newer repair is submitted."""
import copy

from bf import api, cards, policy, routing
from bf.store import BfError, transact
from test_bf_core import Base


class ReviewRouting(Base):
    legacy_protocol = False

    def submit(self, oid, version):
        rid = api.start(self.base, oid)
        result = api.deliver(self.base, oid, rid, '下一步：交付\n改动与版本：%s\n验证证据：%s\n阻断关闭：B1=已关闭' % (version, version))
        self.assertEqual(result['result'], 'SUBMITTED')
        account = next(o for o in self.st().orders.values() if o['type'] == 'decide'
                       and o['state'] == 'open' and o['input'].get('run') == rid)
        self.go(account['id'], self.done(), decision={'diagnosis': '有效技术调用结算',
            'actions': [{'settle': rid, 'charge': 1, 'reason': '实际交付'}]})
        return rid

    def prepare(self):
        old = self.evidence()
        self.submit(self.impl('D1'), old)
        self.submit(self.impl('D2'), self.evidence())
        review = self.one('review', 'R12')['id']
        self.go(review, self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:修复实际缺陷') + '固定版本：%s\n' % old)
        repair = self.one('repair', 'D1')['id']
        # A prior coordinator told the reviewer to correct only its original report.
        def recovery(st):
            source = routing.new_order(st, 'decide', 'R12', {'by': 'user', 'ref': None})
            source['state'] = 'done'
            call = api.new_run(source['id'], 1, 'lead', 'codex', 'test', 'default', None, 'fresh', 'test')
            call.update(ended='2000-01-01T00:00:00Z', result='VERIFIED')
            source['runs'] = [call]
            st.orders[review]['input']['recovery'] = {'source': source['id'], 'instruction': '仅修正旧版本格式'}
        transact(self.base, 'fixture', 'test', recovery)
        return old, review, repair

    def test_new_submission_expires_old_review_instruction(self):
        old, review, repair = self.prepare()
        self.submit(repair, self.evidence())
        obj = self.st().orders[review]
        self.assertNotIn('recovery', obj['input'])
        self.assertEqual(obj['recovery_history'][0]['instruction'], '仅修正旧版本格式')
        self.assertFalse(obj.get('fresh_next', False))
        self.assertIn(review, api.runnable(self.st(), 100))

    def stale_fixture(self):
        old, review, repair = self.prepare()
        recovery = copy.deepcopy(self.st().orders[review]['input']['recovery'])
        rid = self.submit(repair, self.evidence())
        # Recreate the pre-fix bug without changing historical calls.
        transact(self.base, 'fixture', 'test', lambda st: st.orders[review]['input'].update(recovery=recovery))
        self.go(review, self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:旧缺陷仍存在') + '固定版本：%s\n' % old)
        return review, repair, rid

    def test_restore_resubmits_latest_delivery_without_budget_or_history_changes(self):
        review, repair, rid = self.stale_fixture()
        before = self.st()
        runs = copy.deepcopy(before.orders[repair]['runs'])
        used = policy.spent(before, before.orders[repair])
        result = api.restore_review(self.base, repair, rid, self.approval)
        after = self.st()
        self.assertEqual(result['review'], review)
        self.assertEqual(after.orders[repair]['state'], 'done')
        self.assertEqual(after.orders[repair]['runs'], runs)
        self.assertEqual(policy.spent(after, after.orders[repair]), used)
        self.assertEqual(after.plan, before.plan)
        self.assertIn(review, api.runnable(after, 100))
        self.assertNotIn('recovery', after.orders[review]['input'])
        self.assertEqual(after.orders[repair]['performer'], 'primary')

    def test_restore_rejects_review_of_current_delivery(self):
        review, repair, rid = self.stale_fixture()
        def current(st):
            version = st.orders[repair]['runs'][-1]['version']
            st.orders[review]['runs'][-1]['delivery'] = '固定版本：%s\n' % version
        transact(self.base, 'fixture', 'test', current)
        revision = self.st().revision
        with self.assertRaises(BfError):
            api.restore_review(self.base, repair, rid, self.approval)
        self.assertEqual(self.st().revision, revision)

    def test_restore_rejects_unknown_call(self):
        review, repair, rid = self.stale_fixture()
        with self.assertRaises(BfError):
            api.restore_review(self.base, repair, 'nonexistent', self.approval)

    def test_restore_accepts_multiline_fixed_version_and_registered_snapshot(self):
        review, repair, rid = self.stale_fixture()
        original_order = self.impl('D1')
        def multiline(st):
            original = st.orders[original_order]['runs'][0]
            original['snapshot'] = [original['version']]
            original['version'] = '原始交付摘要'
            call = st.orders[review]['runs'][-1]
            call['delivery'] = call['delivery'].replace('固定版本：', '固定版本：\n')
        transact(self.base, 'fixture', 'test', multiline)
        api.restore_review(self.base, repair, rid, self.approval)
        self.assertEqual(self.st().orders[repair]['state'], 'done')
