"""Regression tests for full-path review coverage, including the 3+1+2 S17B case."""
import copy
import tempfile
import unittest
from pathlib import Path

from bf import api, adjust, model, policy, register, review_budget
from bf.store import transact
from test_bf_core import make_plan


def plan():
    p = make_plan()
    p['deliverables'] = {'D1': p['deliverables']['D1']}
    p['review_groups'] = {'R1': {'deliverables': ['D1']}}
    p['policy']['rounds'].update(implement=3, backup=1, lead=0, repair=2, review=6)
    return p


class ReviewBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / '.batch-flow'
        self.approval = Path(self.tmp.name) / 'approval.txt'
        self.approval.write_text('批准测试计划和配套预算')

    def init(self, p=None):
        api.init(self.base, p or plan(), self.approval)
        return api.load(self.base)

    def test_five_rejected_six_accepted_and_invalid_init_is_atomic(self):
        p = plan(); p['policy']['rounds']['review'] = 5
        with self.assertRaisesRegex(api.BfError, 'R1 requires 6'):
            self.init(p)
        self.assertFalse((self.base / 'orders.json').exists())
        self.assertEqual(review_budget.coverage(self.init().plan)[0]['required'], 6)

    def test_effective_target_and_group_overrides_not_global_defaults(self):
        p = plan(); p['policy']['rounds']['review'] = 99
        p['policy']['overrides'] = {'D1': {'implement': 4}, 'R1': {'review': 6}}
        with self.assertRaisesRegex(api.BfError, 'R1 requires 7'):
            model.normalize(p)
        p['policy']['overrides']['R1']['review'] = 7
        model.normalize(p)

    def test_combined_review_reserves_each_sources_rework_but_one_initial_review(self):
        p = plan(); p['deliverables']['D2'] = copy.deepcopy(p['deliverables']['D1'])
        p['review_groups']['R1']['deliverables'].append('D2')
        p['policy']['rounds']['review'] = 10
        with self.assertRaisesRegex(api.BfError, 'R1 requires 11'):
            model.normalize(p)
        p['policy']['rounds']['review'] = 11
        model.normalize(p)

    def test_third_party_has_independent_reservation_and_no_review_is_exempt(self):
        p = plan(); p['roles']['third_party'] = p['roles']['reviewer'].copy()
        p['review_groups']['T1'] = {'deliverables': ['D1'], 'reviewer': 'third_party'}
        p['policy'].setdefault('overrides', {})['T1'] = {'review': 5}
        with self.assertRaisesRegex(api.BfError, 'T1 requires 6'):
            model.normalize(p)
        p['review_groups'] = {}; p['deliverables']['D1']['no_review'] = '纯已审提交，不新增实现'
        model.normalize(p)

    def test_unavailable_backup_is_not_counted(self):
        p = plan(); p['roles'].pop('backup_executor'); p['policy']['rounds']['review'] = 5
        self.assertEqual(review_budget.coverage(model.normalize(p))[0]['required'], 5)

    def test_revise_cannot_expand_author_budget_without_matching_review(self):
        before = self.init(); p = copy.deepcopy(before.plan)
        p['policy']['overrides']['D1'] = {'implement': 4}
        with self.assertRaisesRegex(api.BfError, 'review budget coverage'):
            api.revise_plan(self.base, p, self.approval)
        self.assertEqual(api.load(self.base).revision, before.revision)
        p['policy']['overrides']['R1'] = {'review': 7}
        api.revise_plan(self.base, p, self.approval)

    def test_grant_is_atomic_and_matching_review_must_be_reserved_first(self):
        before = self.init()
        with self.assertRaisesRegex(api.BfError, 'review budget coverage'):
            adjust.grant(self.base, 'D1', self.approval, implement=1)
        self.assertEqual(api.load(self.base).revision, before.revision)
        adjust.grant(self.base, 'R1', self.approval, review=1)
        adjust.grant(self.base, 'D1', self.approval, implement=1)
        self.assertEqual(review_budget.coverage(api.load(self.base).plan)[0]['shortfall'], 0)

    def test_decision_bundle_can_grant_author_and_review_together(self):
        self.init()
        def bundle(st):
            from bf import routing
            original = api.orders(st, type='implement')[0]
            source = routing.open_decide(st, original, 'test')
            register.apply_actions(st, source, [{'grant': {'D1': {'implement': 1}, 'R1': {'review': 1}}}],
                                   original['id'], forced=True, diagnosis='已批准关联配额')
        transact(self.base, 'test', 'test', bundle)
        self.assertEqual(review_budget.coverage(api.load(self.base).plan)[0]['shortfall'], 0)

    def consume_reviews(self, exempt=False, pending=False):
        def consume(st):
            rv = api.orders(st, type='review')[0]
            for i in range(5):
                run = api.new_run(rv['id'], i + 1, 'reviewer', 'claude', 'rev-m', 'medium', None, 'fresh', 'test')
                run.update(ended='now', exempt=exempt, budget_managed=pending)
                rv['runs'].append(run)
        transact(self.base, 'fixture', 'test', consume)

    def test_dispatch_and_preflight_reject_spent_or_pending_review_reservation(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                self.base = Path(self.tmp.name) / ('pending-%s' % pending)
                st = self.init(); oid = api.orders(st, type='implement')[0]['id']
                self.consume_reviews(pending=pending)
                before = api.load(self.base)
                with self.assertRaisesRegex(api.BfError, 'before dispatch'):
                    api.start(self.base, oid)
                self.assertEqual(api.load(self.base).revision, before.revision)
                self.assertTrue(api.preflight(self.base))
                after = api.load(self.base)
                self.assertEqual(after.orders[oid]['ops']['kind'], 'review_budget_shortfall')
                self.assertEqual(after.orders[oid]['runs'], [])
                self.assertFalse(api.orders(after, type='ask_user'))

    def test_exempt_reviews_do_not_reduce_reservation(self):
        st = self.init(); oid = api.orders(st, type='implement')[0]['id']
        self.consume_reviews(exempt=True)
        api.start(self.base, oid)

    def test_credited_reviews_restore_reservation_using_the_existing_ledger(self):
        st = self.init(); oid = api.orders(st, type='implement')[0]['id']
        self.consume_reviews()
        def credit(st):
            rv = api.orders(st, type='review')[0]
            rv['budget_credits'] = {run['run_id']: {'reason': '已批准历史冲正'} for run in rv['runs']}
        transact(self.base, 'fixture-credit', 'test', credit)
        api.start(self.base, oid)

    def test_legacy_deficit_remains_readable_but_cannot_spend_author_rounds(self):
        self.init()
        def legacy(st):
            st.plan['policy']['rounds']['review'] = 5
            st.plan_dirty = True
        transact(self.base, 'legacy-fixture', 'test', legacy)
        st = api.load(self.base)
        api.revise_plan(self.base, st.plan, self.approval)  # no quota increase for old deficit
        oid = api.orders(api.load(self.base), type='implement')[0]['id']
        with self.assertRaisesRegex(api.BfError, 'before dispatch'):
            api.start(self.base, oid)
        with self.assertRaisesRegex(api.BfError, 'review budget coverage'):
            api.revise_plan(self.base, st.plan, self.approval, revalidate=['D1'])

    def test_revalidation_never_resets_historical_review_spend(self):
        self.init(); self.consume_reviews()
        st = api.load(self.base)
        api.revise_plan(self.base, st.plan, self.approval, revalidate=['D1'])
        st = api.load(self.base)
        oid = next(o['id'] for o in api.orders(st, type='implement') if o['contract_version'] == st.plan['revision'])
        with self.assertRaisesRegex(api.BfError, 'before dispatch'):
            api.start(self.base, oid)


if __name__ == '__main__':
    unittest.main()
