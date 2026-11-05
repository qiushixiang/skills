"""Goal boundaries travel from the active plan to every role, without model calls."""
import copy
from pathlib import Path
import unittest
import flow
import dispatch_card
from test_flow import plan


def contract(goal):
    return dict(goal=goal, acceptance=[goal + ' observable result'], non_goals=[goal + ' excluded work'])


def structured_plan():
    p = plan(); p['goal_contract'] = contract('deliver scoped functionality')
    for tid, task in p['tasks'].items():
        task['goal_contract'] = contract('advanced reliability' if tid == 'third' else tid + ' basic goal')
    return p


class GoalContractTests(unittest.TestCase):
    def state(self):
        return dict(plan=structured_plan(), plan_version=7, decisions={})

    def test_missing_node_or_empty_acceptance_rejected(self):
        for field in ['goal_contract', 'acceptance']:
            p = structured_plan()
            if field == 'goal_contract': del p['tasks']['execute'][field]
            else: p['tasks']['execute']['goal_contract'][field] = []
            with self.assertRaises(flow.FlowError): flow.validate_plan(p)
        p = structured_plan(); p['tasks']['execute']['retired'] = True
        del p['tasks']['execute']['goal_contract']; flow.validate_plan(p)
        flow.validate_plan(plan())  # Read compatibility, not fabricated structured goals.

    def test_each_node_keeps_its_own_validation_target(self):
        s = self.state()
        for tid in s['plan']['tasks']:
            context = flow.goal_context(s, dict(target=tid))
            row = next(r for r in context['tasks'] if r['task'] == tid)
            self.assertEqual(row['contract'], s['plan']['tasks'][tid]['goal_contract'])
            self.assertFalse(context['legacy']); self.assertFalse(context['missing'])
        advanced = flow.goal_context(s, dict(target='third'))
        self.assertEqual(advanced['tasks'][-1]['contract']['goal'], 'advanced reliability')
        self.assertEqual({r['task'] for r in flow.goal_context(s, dict(target='regular'))['tasks']}, {'execute','regular'})

    def test_diagnosis_and_confirmation_get_affected_goals(self):
        s = self.state()
        s['decisions']['D1'] = dict(blocks=['execute'], resolution=dict(requirements=[
            dict(kind='task', target='recovery')]))
        for item in [dict(target='D1'), dict(target='D1.confirm', decision='D1')]:
            context = flow.goal_context(s, item)
            self.assertEqual({r['task'] for r in context['tasks']}, {'execute','recovery'})
        context['project']['goal'] = 'mutated'
        self.assertNotEqual(s['plan']['goal_contract']['goal'], 'mutated')

    def test_prompt_renders_boundaries_before_instruction_for_all_roles(self):
        s = self.state()
        for role, target in [('executor','execute'),('reviewer','regular'),('third_party','third'),('architect','D1')]:
            payload = dict(target=target, instruction='EXACT NODE INSTRUCTION')
            card = dict(id='card', role=role, kind='DECIDE' if role=='architect' else 'EXECUTE',
                        target=target, mode='dispatch', revision=1, tool='codex', model='configured', effort='medium', access='read')
            text = dispatch_card.render_prompt(card, payload, s, Path('/tmp/store'), Path('/tmp/task'), Path('/tmp/src'))
            self.assertLess(text.index('当前目标与验收边界'), text.index('EXACT NODE INSTRUCTION'))
            self.assertIn('deliver scoped functionality excluded work', text)
            if target in s['plan']['tasks']:
                self.assertIn(s['plan']['tasks'][target]['goal_contract']['goal'], text)
            if role != 'executor': self.assertIn('先判断发现是否违反当前目标', text)
            self.assertEqual(text.count('EXACT NODE INSTRUCTION'), 1)
            self.assertNotIn('## 原始载荷', text)
            if target in s['plan']['tasks']:
                self.assertLess(text.index(s['plan']['tasks'][target]['goal_contract']['goal']),
                                text.index(s['plan']['goal_contract']['goal']))

    def test_prerequisite_chains_do_not_expand_into_review_prompt(self):
        s = self.state(); tasks = s['plan']['tasks']
        tasks['precheck'] = dict(kind='confirmation', depends_on=['regular'], goal_contract=contract('precheck'))
        tasks['focused'] = dict(kind='review', depends_on=['precheck'], goal_contract=contract('focused'))
        context = flow.goal_context(s, dict(target='focused'))
        self.assertEqual([row['task'] for row in context['tasks']], ['focused'])
        text = '\n'.join(dispatch_card.render_goal_context(context))
        self.assertNotIn('execute basic goal', text)
        context = flow.goal_context(s, dict(target='regular'))
        self.assertEqual({r['task'] for r in context['tasks']}, {'regular','execute'})
        text = '\n'.join(dispatch_card.render_goal_context(context))
        self.assertLess(text.index('regular basic goal'), text.index('execute basic goal observable result'))

    def test_status_payload_includes_project_and_node_contracts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); proof = root / 'proof'; proof.write_text('authorization')
            state, _ = flow.apply_request(None, dict(request_id='init', expected_revision=0, actor='architect', operations=[
                dict(type='init', data=dict(plan=structured_plan(), simulation=True, task_mode='local',
                    context=dict(conversation_id='planner', mode='fresh', reason='test')))]), root)
            state, _ = flow.apply_request(state, dict(request_id='auth', expected_revision=1, actor='orchestrator', operations=[
                dict(type='authorize', data=dict(batches=['B'], evidence=[str(proof)]))]), root)
            action = next(a for a in flow.status(state)['actions'] if a['target']=='execute')
            context = action['subagent_payload']['goal_context']
            self.assertEqual(context['project'], state['plan']['goal_contract'])
            self.assertEqual(context['tasks'][0]['contract'], state['plan']['tasks']['execute']['goal_contract'])

    def test_first_goal_annotation_does_not_rebudget_completed_delivery(self):
        from test_flow import FlowTests
        case = FlowTests(methodName='test_full_release'); case.setUp()
        try:
            for tid in ['execute','regular','third','done']: case.pass_task(tid)
            proposed = copy.deepcopy(case.s['plan']); proposed['goal_contract'] = contract('project')
            for tid, task in proposed['tasks'].items(): task['goal_contract'] = contract(tid)
            coverage = flow.active_review_budget_coverage(case.s, proposed)
            self.assertTrue(coverage['ok'])
            self.assertIn('execute', coverage['excluded_completed_tasks'])
            proposed['tasks']['execute']['instruction'] += ' actual implementation change'
            self.assertNotIn('execute', flow.active_review_budget_coverage(case.s, proposed)['excluded_completed_tasks'])
        finally: case.tearDown()

    def test_legacy_instruction_not_silently_interpreted_as_contract(self):
        s = self.state(); s['plan'] = plan()
        context = flow.goal_context(s, dict(target='regular'))
        self.assertTrue(context['legacy']); self.assertEqual(context['missing'], ['execute','regular'])
        self.assertIn('未自动提取或补造', '\n'.join(dispatch_card.render_goal_context(context)))


    def test_readonly_review_uses_host_tool_evidence_without_requiring_writes(self):
        s = self.state()
        for tool in ['codex','claude-agent']:
            card = dict(id='card',dir='/tmp/review-card',role='reviewer',kind='REVIEW',target='regular',mode='dispatch',revision=1,tool=tool,model='configured',effort='medium',access='read')
            text = dispatch_card.render_prompt(card,dict(target='regular',instruction='review'),s,Path('/tmp/store'),Path('/tmp/task'),Path('/tmp/src'))
            self.assertIn('只读时由宿主保存工具原始输出',text)
            self.assertNotIn('原始证据用 flow.py record 生成：',text)
            self.assertIn('未运行',text)
            if tool=='codex':self.assertIn('/tmp/review-card/run/stdout',text)
            else:self.assertIn('工具原始回执',text)
        card.update(role='executor',kind='EXECUTE',target='execute',access='write')
        text = dispatch_card.render_prompt(card,dict(target='execute',instruction='implement'),s,Path('/tmp/store'),Path('/tmp/task'),Path('/tmp/src'))
        self.assertIn('原始证据用 flow.py record 生成：',text)


if __name__ == '__main__': unittest.main()
