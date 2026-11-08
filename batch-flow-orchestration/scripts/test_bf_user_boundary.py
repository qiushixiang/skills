"""Only new user decisions produce ask_user; technical trouble remains with the responsible roles."""
import copy
import json
from pathlib import Path

from bf import api, gate, questions, routing, register
from test_bf_core import Base, NEEDS_DECISION, user_question


class UserBoundary(Base):
    def decision(self, actions):
        d = self.one('decide', 'D1', 'open')['id']
        return self.go(d, '结果：VERIFIED\n停止原因：完成', decision={'diagnosis': '已核查原因与恢复边界', 'actions': actions})

    def test_all_six_categories_require_a_concrete_decision_recommendation_and_impact(self):
        s = self.st()
        for category in questions.CATEGORIES:
            action = user_question('选择用户掌握的设备信息')
            action['user_request']['category'] = category
            self.assertEqual(gate.validate_decision(s, {'diagnosis': 'd', 'actions': [action]}), '')
            for key in ('decision', 'recommendation', 'impact'):
                invalid = copy.deepcopy(action)
                invalid['user_request'][key] = ' '
                self.assertTrue(gate.validate_decision(s, {'diagnosis': 'd', 'actions': [invalid]}))
        for action in ({'ask_user': '继续吗'}, {'ask_user': '次数用完怎么办', 'user_request': {'category': 'technical'}}):
            self.assertTrue(gate.validate_decision(s, {'diagnosis': 'd', 'actions': [action]}))

    def test_explicit_user_question_surfaces_its_decision_and_only_blocks_its_path(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        self.decision([user_question('使用设备A还是B')])
        r = api.report(self.base)
        self.assertEqual(len(r['ask_user']), 1)
        ask = r['ask_user'][0]
        self.assertEqual(ask['user_request']['category'], 'information')
        for value in ('需决定', '推荐', '影响', '设备A'):
            self.assertIn(value, ask['question'])
        self.assertIn(self.impl('D2'), api.runnable(self.st(), 10))

    def test_coordination_exhaustion_pauses_without_user_question_and_retry_can_recover(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        d = self.one('decide', 'D1')['id']
        for _ in range(2):
            rid = api.start(self.base, d)
            # Exhaust format continuations too; no valid decision was delivered.
            for _ in range(3):
                result = api.deliver(self.base, d, rid, '结果：FAILED\n停止原因：未完成')
                if result['outcome'] == 'registered':
                    break
        s = self.st()
        self.assertEqual(s.orders[d]['ops']['kind'], 'coordination_exhausted')
        self.assertEqual(api.report(self.base)['ask_user'], [])
        self.assertNotIn(self.impl('D1'), api.runnable(s, 10))
        self.assertIn(self.impl('D2'), api.runnable(s, 10))
        self.assertIn('受阻待处理：', api.report_text(self.base))
        self.assertIn('最小缺项：', api.report_text(self.base))
        self.assertIn('待你决定：无', api.report_text(self.base))
        api.retry(self.base, d, '已修复处置结构，保留原技术预算')
        self.assertIn(d, api.runnable(self.st(), 10))

    def test_coordinator_tool_failure_is_operational_not_a_user_decision(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        d = self.one('decide', 'D1')['id']
        for _ in range(2):
            rid = api.start(self.base, d)
            api.abandon(self.base, d, rid, 'CLI authentication failure')
        self.assertEqual(api.report(self.base)['ask_user'], [])
        self.assertEqual(self.st().orders[d]['ops']['kind'], 'tool_failure')
        api.escalate(self.base, d, '账号恢复，重新核查原技术问题')
        self.assertIn(d, api.runnable(self.st(), 10))

    def test_lead_can_report_unresolved_technical_block_without_asking_the_user(self):
        oid = self.impl('D1')
        self.go(oid, NEEDS_DECISION)
        result = self.decision([{'blocked': oid, 'reason': '缺少可重复的设备响应', 'missing': '恢复原设备连接并获得原始响应'}])
        self.assertIn(oid, result['paused'])
        self.assertTrue(any('受阻待处理' in line for line in api.watch_lines(self.base)))
        self.assertEqual(api.report(self.base)['ask_user'], [])
        self.assertEqual(self.st().orders[oid]['ops']['kind'], 'technical_block')
        self.assertNotIn(oid, api.runnable(self.st(), 10))
        self.assertIn(self.impl('D2'), api.runnable(self.st(), 10))

    def test_real_budget_request_is_a_concrete_user_decision(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        self.decision([{'grant': {'D1': {'implement': 1}}}])
        ask = api.report(self.base)['ask_user'][0]
        self.assertEqual(ask['user_request']['category'], 'budget')
        self.assertIn('implement', ask['question'])
        self.assertEqual(self.one('ask_user', 'D1')['input']['pending'], [{'grant': {'D1': {'implement': 1}}}])

    def test_internal_instruction_revision_does_not_ask_but_preserves_review(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        plan = copy.deepcopy(self.st().plan)
        plan['deliverables']['D1']['instruction'] = '改用另一种局部实现，仍完成 a1'
        path = Path(self.tmp.name) / 'internal-plan.json'; path.write_text(json.dumps(plan))
        self.decision([{'plan_revision': str(path), 'within_authorization': True}])
        self.assertEqual(api.report(self.base)['ask_user'], [])
        self.assertEqual(self.st().plan['review_groups'], plan['review_groups'])
        self.assertTrue(api.orders(self.st(), type='review', target='R12'))

    def test_declared_internal_revision_cannot_change_acceptance_budget_or_review(self):
        for mutate in (
            lambda p: p['policy']['rounds'].update(implement=99),
            lambda p: p['review_groups']['R12'].update(focus='different approved coverage'),
            lambda p: p['envelope'].update(protected={'D1': ['tests/runner.sh']}),
        ):
            plan = copy.deepcopy(self.st().plan); mutate(plan)
            path = Path(self.tmp.name) / 'changed-plan.json'; path.write_text(json.dumps(plan))
            action = {'plan_revision': str(path), 'within_authorization': True}
            self.assertTrue(gate.validate_decision(self.st(), {'diagnosis': 'd', 'actions': [action]}))

    def test_material_plan_revision_still_waits_for_user(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        plan = copy.deepcopy(self.st().plan); plan['deliverables']['D1']['acceptance'] = ['new acceptance']
        path = Path(self.tmp.name) / 'changed-plan.json'; path.write_text(json.dumps(plan))
        self.decision([{'plan_revision': str(path)}])
        self.assertEqual(api.report(self.base)['ask_user'][0]['user_request']['category'], 'commitment')

    def test_only_obsolete_review_retirement_is_mechanical(self):
        s = self.st(); review = self.one('review', 'R12')['id']
        self.assertTrue(register._needs_user(s, {'supersede': review, 'reason': '跳过有效审阅'}))
        s.orders[review]['contract'] = {'obsolete': True}
        self.assertFalse(register._needs_user(s, {'supersede': review, 'reason': '清理旧契约工单'}))

    def test_internal_plan_is_rechecked_at_execution(self):
        s = self.st(); plan = copy.deepcopy(s.plan)
        path = Path(self.tmp.name) / 'race-plan.json'; path.write_text(json.dumps(plan))
        action = {'plan_revision': str(path), 'within_authorization': True}
        self.assertFalse(register._needs_user(s, action))
        plan['policy']['rounds']['implement'] = 99
        path.write_text(json.dumps(plan))
        with self.assertRaises(api.BfError):
            register._execute(s, s.orders[self.impl('D1')], action, self.impl('D1'))

    def test_repetition_limit_is_a_reported_pause_not_a_user_question(self):
        s = self.st(); obj = s.orders[self.impl('D1')]
        for _ in range(2):
            routing.open_decide(s, obj, 'technical_conflict')['state'] = 'done'
        routing.open_decide(s, obj, 'technical_conflict')
        self.assertEqual(obj['ops']['kind'], 'coordination_limit')
        self.assertFalse(any(o['type'] == 'ask_user' for o in s.orders.values()))
