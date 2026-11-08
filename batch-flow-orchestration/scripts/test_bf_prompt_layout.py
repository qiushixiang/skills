"""Role cards keep approved acceptance explicit and history out of active instructions."""
import copy
import tempfile
import unittest
from pathlib import Path
from bf import cards, model
from bf.store import State, BfError
from test_bf_core import make_plan


class PromptLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        plan = model.normalize(make_plan())
        plan['deliverables']['D1'].update(
            instruction='implement-current-scope-once', acceptance=['confirm-direct-result', 'prove-real-run'],
            constraints=['preserve-ABI'], non_goals=['future-performance-belongs-elsewhere'])
        plan['review_groups']['R12']['focus'] = 'inspect-direct-regression'
        def order(oid, kind, target):
            return dict(id=oid, type=kind, target=target, origin={'by': 'plan'}, input={}, needs=[], runs=[], state='open')
        orders = {o['id']:o for o in [order('WO-0001', 'implement', 'D1'), order('WO-0002', 'implement', 'D2'),
                                    order('WO-0003', 'review', 'R12')]}
        self.state = State(self.base, plan, orders)

    def render(self, oid):
        data = cards.prepare(self.state, oid)
        return data, cards.render(self.state, oid, {'tool': 'codex'}, prepared=data)

    def test_executor_separates_outcomes_acceptance_bounds_and_work(self):
        data, text = self.render('WO-0001')
        headings = ['## 目标', '## 验收条件', '## 范围与约束', '## 本次工作', '## 环境与留证', '## 交付']
        self.assertEqual([text.index(h) for h in headings], sorted(text.index(h) for h in headings))
        self.assertEqual(text.count('implement-current-scope-once'), 1)
        self.assertEqual(text.count('preserve-ABI'), 1)
        for item in self.state.plan['deliverables']['D1']['acceptance']:
            self.assertIn(item, text)
        self.assertNotIn('验收：', text.split('## 验收条件')[0])
        cards.validate_export(data, text)

    def test_review_lists_only_group_targets_with_boundaries_before_contracts(self):
        _, text = self.render('WO-0003')
        self.assertIn('D1.A1', text); self.assertIn('D2.A1', text)
        self.assertNotIn('D3', text)
        self.assertLess(text.index('## 验收依据'), text.index('## 审阅边界'))
        self.assertLess(text.index('## 审阅边界'), text.index('## 实施约定'))
        for phrase in ['不要求其他批次的目标提前完成', '范围外发现只列非阻断建议',
                       '每项阻断必须绑定本组交付目标', '不是要求审阅者重新实施', 'preserve-ABI']:
            self.assertIn(phrase, text)
        self.assertEqual(text.count('implement-current-scope-once'), 1)

    def test_large_archival_observation_retained_but_not_replayed_as_commands(self):
        marker = 'HISTORICAL-DO-NOT-REPLAY-' * 30000
        self.state.orders['WO-0001']['runs'] = [dict(run_id='r1', ended='done', started='start',
            result='FAILED', started_ts=1, ended_ts=2, evidence={}, version='old', changed={},
            dispatch={'input':{'path':'old-dispatch.json'}}, observations=[{'detail':marker}])]
        data, text = self.render('WO-0003')
        self.assertEqual(data['versions_and_evidence'][0]['observations'][0]['detail'], marker)
        self.assertIn('old-dispatch.json', text)
        self.assertIn('dispatch.json 的 versions_and_evidence', text)
        self.assertNotIn(marker, text)
        self.assertLess(len(text), 12000)
        cards.validate_export(data, text)

    def test_export_cannot_drop_acceptance_scope_or_current_constraints(self):
        data, text = self.render('WO-0003')
        for key in ['acceptance', 'review_scope', 'constraints', 'contracts', 'non_goals']:
            with self.subTest(field=key), self.assertRaises(BfError):
                cards.validate_export(data, text.replace(data[key], '', 1))

    def test_special_review_instruction_is_preserved_without_replacing_acceptance(self):
        self.state.orders['WO-0003']['input'].update(instruction='verify-specific-evidence-only', custom='required-extra-input')
        data, text = self.render('WO-0003')
        self.assertIn('verify-specific-evidence-only', text)
        self.assertIn('required-extra-input', text)
        self.assertIn('confirm-direct-result', text)
        self.assertNotIn('首审：', text)
        cards.validate_export(data, text)

    def test_exact_duplicate_constraints_shown_once_without_losing_unique_rules(self):
        self.state.plan['goal']['constraints'] = ['preserve-ABI', 'unique-global-boundary']
        self.state.orders['WO-0003']['input']['constraints'] = ['preserve-ABI', 'specific-review-boundary']
        data, text = self.render('WO-0003')
        self.assertEqual(data['constraints'].count('preserve-ABI'), 1)
        self.assertIn('unique-global-boundary', text)
        self.assertIn('specific-review-boundary', text)


if __name__ == '__main__':
    unittest.main()
