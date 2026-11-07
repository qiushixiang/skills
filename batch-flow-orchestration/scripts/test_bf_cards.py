#!/usr/bin/env python3
"""Cards link the restored role norms (design §25). Written before the implementation. Python 3.8 stdlib only."""
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bf import api, cards  # noqa: E402
from test_bf_core import make_plan, NEEDS_DECISION  # noqa: E402

REFS = HERE.parent / 'references'


class Cards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / '.batch-flow'
        (self.base / 'records').mkdir(parents=True)
        approval = Path(self.tmp.name) / 'ok.txt'
        approval.write_text('批准')
        plan = make_plan()
        plan['deliverables']['D1']['non_goals'] = ['不做 EMCY']
        plan['review_groups']['R12']['focus'] = '只按集成目标放行，重点核对帧隔离'
        api.init(self.base, plan, approval)

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, type_, target):
        st = api.load(self.base)
        oid = api.orders(st, type=type_, target=target)[0]['id']
        return cards.render(st, oid, api.assign(st, oid))

    def test_executor_card_links_engineering_sections_and_non_goals(self):
        card = self.card('implement', 'D1')
        self.assertIn('## 规范', card)
        self.assertIn(str(REFS / 'engineering.md'), card)
        for section in ['必要验证', '证据标准']:
            self.assertIn(section, card)
        self.assertIn(str(REFS / 'roles.md'), card)
        self.assertIn('不做 EMCY', card)

    def test_review_card_links_review_norms_and_focus(self):
        card = self.card('review', 'R12')
        self.assertIn(str(REFS / 'engineering.md'), card)
        self.assertIn('审阅边界与问题处置', card)
        self.assertIn('只按集成目标放行，重点核对帧隔离', card)

    def test_decide_card_links_lead_norms_and_documents_every_action(self):
        oid = api.orders(api.load(self.base), type='implement', target='D1')[0]['id']
        run = api.start(self.base, oid)
        api.deliver(self.base, oid, run, NEEDS_DECISION, conversation_id='c')
        card = self.card('decide', 'D1')
        self.assertIn(str(REFS / 'technical-lead.md'), card)
        for action in ['open', 'supersede', 'retry', 'grant', 'plan_revision', 'ask_user', 'refresh_design', 'allow_protected']:
            self.assertIn(action, card)

    def test_referenced_docs_and_sections_exist(self):
        for name, sections in {'engineering.md': ['必要验证', '证据标准', '审阅边界与问题处置'],
                               'technical-lead.md': ['裁决（decide）', '分诊（triage）', '兜底实施']}.items():
            text = (REFS / name).read_text(encoding='utf-8')
            for s in sections:
                self.assertIn('## ' + s, text)


if __name__ == '__main__':
    unittest.main()
