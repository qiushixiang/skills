"""Regression coverage for work-order budgets, contract revisions and causal hand-offs. No models."""
import copy
import json
import subprocess
import time
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bf import api, cards, policy, runner, routing
from test_bf_core import Base, INFEASIBLE, NEEDS_DECISION, make_plan, user_question
from test_bf_integrity import Base as RepoBase
from test_bf_runner import FakeAdapter


class Workflow(Base):
    def test_attention_updates_on_answer_without_a_running_driver(self):
        self.go(self.impl('D1'),NEEDS_DECISION)
        self.decide([user_question('选择后续测试设备')])
        ask=self.one('ask_user','D1')['id']
        path=self.base/'attention.md'
        self.assertIn(ask,path.read_text())
        api.stop(self.base)
        api.answer(self.base,ask,'批准',True)
        self.assertEqual(path.read_text(),'')
        self.assertTrue(self.st().stopped)

    def test_design_review_runs_before_implementation_and_product_review_waits(self):
        p = make_plan()
        p['deliverables']['D0'] = {'batch':'B1','instruction':'交付设计','acceptance':['设计固定版本'], 'depends_on':[]}
        p['review_groups']['D0.R'] = {'deliverables':['D0'],'reviewer':'reviewer'}
        p['deliverables']['D1']['depends_on'] = ['D0.R']
        self.reinit(deliverables=p['deliverables'],review_groups=p['review_groups'])
        design, impl = self.impl('D0'), self.impl('D1')
        design_review = self.one('review','D0.R')['id']
        product_review = self.one('review','R12')['id']
        self.assertNotIn(impl,api.runnable(self.st(),10))
        self.go(design,self.done())
        self.assertIn(design_review,api.runnable(self.st(),10))
        self.assertNotIn(impl,api.runnable(self.st(),10))
        self.assertNotIn(product_review,api.runnable(self.st(),10))
        self.go(design_review,self.release_text())
        self.assertIn(impl,api.runnable(self.st(),10))
        self.assertNotIn(product_review,api.runnable(self.st(),10))

    def test_decision_opened_review_records_all_current_work_dependencies(self):
        self.verify_batch1()
        self.go(self.impl('D3'),NEEDS_DECISION)
        d = self.one('decide','D3')['id']
        self.go(d,'结果：VERIFIED\n停止原因：完成',decision={'diagnosis':'独立复核','actions':[
            {'open':'review','target':'R12','input':{'instruction':'复核固定交付'}}]})
        reviews = api.orders(self.st(),type='review',target='R12')
        new = reviews[-1]
        self.assertTrue({self.impl('D1'),self.impl('D2')}.issubset(set(new['needs'])))

    def test_all_reviews_wait_for_new_work_even_if_old_needs_are_incomplete(self):
        self.verify_batch1()
        self.go(self.impl('D3'),NEEDS_DECISION)
        d = self.one('decide','D3')['id']
        self.go(d,'结果：VERIFIED\n停止原因：完成',decision={'diagnosis':'补充实施并复核','actions':[
            {'open':'review','target':'R12','input':{}},
            {'open':'implement','target':'D1','input':{'instruction':'补充实现'}}]})
        s = self.st()
        self.assertFalse(any(o['id'] in api.runnable(s,10) for o in api.orders(s,type='review',target='R12')))
        new_impl = api.orders(s,type='implement',target='D1')[-1]
        self.go(new_impl['id'],self.done())
        self.assertTrue(any(o['id'] in api.runnable(self.st(),10)
                            for o in api.orders(self.st(),type='review',target='R12')))

    def test_owner_compiles_effective_constraints_without_inheriting_retired_history(self):
        s = self.st()
        impl = s.orders[self.impl('D1')]
        impl['state'] = 'superseded'
        ask = routing.new_order(s, 'ask_user', 'D1', {'by':'order','ref':impl['id'],'reason':'needs user'})
        ask['state'] = 'done'
        ask['output'] = {'answer':'入口改用 bf record，保留 S15'}
        impl['needs'].append(ask['id'])
        other = routing.new_order(s, 'review', 'R12', {'by':'plan','ref':'r1','reason':'review'})
        other['state'] = 'done'
        other['output'] = {'result':'RELEASE', 'private_verdict':'PRIVATE REVIEW VERDICT'}
        ask['needs'].append(other['id'])
        review = api.orders(s,type='review',target='R12')[0]
        c = cards.render(s,review['id'],api.assign(s,review['id']))
        self.assertNotIn('入口改用 bf record，保留 S15',c)
        review['input']['constraints'] = ['使用 bf record 留证']
        c = cards.render(s,review['id'],api.assign(s,review['id']))
        self.assertIn('使用 bf record 留证', c)
        self.assertNotIn('PRIVATE REVIEW VERDICT',c)
        child = routing.new_order(s, 'implement', 'D1', {'by':'order','ref':impl['id'],'reason':'decision'},
                                  {'instruction':'apply the authorized direction'})
        review['needs'] = [child['id']]
        c = cards.render(s,review['id'],api.assign(s,review['id']))
        self.assertNotIn('入口改用 bf record，保留 S15',c)
        self.assertIn('使用 bf record 留证', c)
        self.assertNotIn('PRIVATE REVIEW VERDICT',c)
        s.plan['deliverables']['D1']['contract_version'] = 2
        c = cards.render(s,review['id'],api.assign(s,review['id']))
        self.assertNotIn('入口改用 bf record，保留 S15',c)

    def test_rejected_answer_on_superseded_subject_reaches_lead_and_blocks_review(self):
        self.verify_batch1()
        oid = self.impl('D1')
        # Reproduce a deferred proposal whose original work has already been retired.
        from bf.store import transact
        def setup(s):
            subject = s.orders[oid]
            subject['state'] = 'superseded'
            source = routing.new_order(s, 'decide', 'D1', {'by':'order','ref':oid,'reason':'wait_evidence'},
                                       {'subject':oid})
            source['state'] = 'done'
            return routing.open_ask(s, source, oid, '批准旧方案?', pending=[user_question('old proposal')],
                                    request=user_question('legacy')['user_request'])['id']
        ask = transact(self.base, 'test_setup', 'test', setup)
        old_runs = copy.deepcopy(self.st().orders[oid]['runs'])
        api.answer(self.base, ask, '拒绝旧方案，按新方向开实施单', False)
        s = self.st()
        ds = [o for o in s.orders.values() if o['type']=='decide' and o['state']=='open']
        self.assertEqual(len(ds), 1)
        d = ds[0]
        self.assertEqual(d['input']['trigger'], 'user_rejected')
        self.assertIn('新方向', d['input']['answer'])
        self.assertEqual(s.orders[oid]['state'], 'superseded')
        self.assertEqual(s.orders[oid]['runs'], old_runs)
        review = self.one('review','R12')['id']
        self.assertIn(d['id'],s.orders[review]['needs'])
        self.assertNotIn(review,api.runnable(s,10))
        self.assertIn(d['id'],api.runnable(s,10))
        self.assertFalse(any(o['type']=='implement' and o['origin']['ref']==ask for o in s.orders.values()))

    def test_plan_card_uses_current_instruction_but_decision_card_keeps_specific_scope(self):
        oid = self.impl('D1')
        s = self.st()
        s.orders[oid]['input']['instruction'] = 'obsolete instruction'
        c = cards.render(s, oid, api.assign(s, oid))
        self.assertIn('do d1', c)
        self.assertNotIn('obsolete instruction', c)
        s.orders[oid]['origin'] = {'by':'order', 'ref':'WO-missing', 'reason':'decision'}
        s.orders[oid]['input']['instruction'] = 'specific approved scope'
        c = cards.render(s, oid, api.assign(s, oid))
        self.assertIn('specific approved scope', c)
        self.assertNotIn('当前完整指令 D1：do d1', c)
        self.assertIn('验收：a1', c)

    def decide(self, actions):
        return self.go(self.one('decide', 'D1', 'open')['id'],
                       '结果：VERIFIED\n停止原因：完成', decision={'diagnosis': '先处置再恢复', 'actions': actions})

    def test_new_implementation_shares_target_budget(self):
        oid = self.impl('D1')
        for _ in range(3):
            self.go(oid, INFEASIBLE)
        self.go(oid, NEEDS_DECISION)
        self.decide([{'open': 'implement', 'target': 'D1', 'input': {'instruction': '继续'}}])
        new = api.orders(self.st(), type='implement', target='D1')[-1]
        self.assertEqual(api.assign(self.st(), new['id'])['stage'], 'lead')
        self.assertEqual(api.report(self.base)['budgets']['D1'], 1)

    def test_superseding_a_review_dependency_requires_user(self):
        oid = self.impl('D1')
        self.go(oid, NEEDS_DECISION)
        self.decide([{'supersede': oid, 'reason': '换单'}])
        self.assertEqual(self.st().orders[oid]['state'], 'open')
        ask = self.one('ask_user', 'D1')['id']
        api.answer(self.base, ask, '批准作废', True)
        self.assertEqual(self.st().orders[oid]['state'], 'superseded')

    def test_decision_child_finishes_before_original_resumes(self):
        oid = self.impl('D1')
        self.go(oid, NEEDS_DECISION)
        self.decide([{'open': 'implement', 'target': 'D2', 'input': {'instruction': '前置修复'}}])
        child = api.orders(self.st(), type='implement', target='D2')[-1]
        self.assertIn(child['id'], self.st().orders[oid]['needs'])
        self.assertNotIn(oid, api.runnable(self.st(), 10))
        self.go(child['id'], self.done())
        self.assertIn(oid, api.runnable(self.st(), 10))

    def test_contract_revision_invalidates_implementation_review_and_release(self):
        self.verify_batch1()
        review = self.one('review', 'R12')['id']
        self.go(review, self.release_text())
        rel = self.one('release', 'B1')['id']
        api.release(self.base, rel)
        old = copy.deepcopy(self.st().orders[self.impl('D1')])
        p = copy.deepcopy(self.st().plan)
        p['deliverables']['D1'].update(instruction='new instruction', acceptance=['new acceptance'])
        api.revise_plan(self.base, p, self.approval, revalidate=['D1'])
        s = self.st()
        current = api.orders(s, type='implement', target='D1', state_='open')
        self.assertEqual(len(current), 1)
        self.assertEqual(cards.prepare(self.st(), current[0]['id'])['input']['instruction'], 'new instruction')
        self.assertEqual(s.orders[old['id']], old)  # immutable historical delivery
        self.assertEqual(s.orders[review]['state'], 'done')
        self.assertTrue(api.orders(s, type='review', target='R12', state_='open'))
        self.assertEqual(s.orders[rel]['state'], 'open')
        self.assertNotIn(rel, api.runnable(s, 10))
        self.assertFalse(any(x['id'] == 'B1' and x['state'] == 'done' for x in api.report(self.base)['batches']))
        self.go(current[0]['id'], self.done())
        self.go(self.one('review', 'R12', state_='open')['id'], self.release_text())
        self.assertIn(rel, api.runnable(self.st(), 10))
        api.release(self.base, rel)
        self.assertEqual(self.st().orders[rel]['state'], 'done')

    def test_changed_review_membership_waits_for_new_member(self):
        self.verify_batch1()
        self.go(self.one('review', 'R12')['id'], self.release_text())
        p = copy.deepcopy(self.st().plan)
        p['deliverables']['D4'] = dict(p['deliverables']['D1'], title='new', instruction='do new')
        p['review_groups']['R12']['deliverables'].append('D4')
        api.revise_plan(self.base, p, self.approval)
        reviews = api.orders(self.st(), type='review', target='R12', state_='open')
        self.assertEqual(len(reviews), 1)
        member = self.one('implement', 'D4')['id']
        self.assertIn(member, reviews[0]['needs'])
        self.assertNotIn(reviews[0]['id'], api.runnable(self.st(), 10))

    def test_budget_only_revision_does_not_create_work(self):
        before = set(self.st().orders)
        p = copy.deepcopy(self.st().plan)
        p['policy']['rounds']['implement'] += 1
        api.revise_plan(self.base, p, self.approval)
        self.assertEqual(set(self.st().orders), before)

    def test_revision_retires_old_contract_decisions_and_questions(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        self.decide([user_question('旧方案使用哪台设备?')])
        ask = self.one('ask_user', 'D1')['id']
        self.assertIn(ask,(self.base/'attention.md').read_text())
        p = copy.deepcopy(self.st().plan)
        p['deliverables']['D1']['instruction'] = '用户已批准的新方案'
        api.revise_plan(self.base, p, self.approval, revalidate=['D1'])
        self.assertEqual(self.st().orders[ask]['state'], 'superseded')
        self.assertEqual((self.base/'attention.md').read_text(),'')
        current = self.one('implement', 'D1', state_='open')['id']
        self.assertIn(current, api.runnable(self.st(), 10))

    def test_repair_invalidates_previously_passed_third_party(self):
        p = make_plan()
        p['roles']['third_party'] = p['roles']['reviewer']
        p['review_groups']['T12'] = {'deliverables': ['D1', 'D2'], 'reviewer': 'third_party'}
        self.reinit(roles=p['roles'], review_groups=p['review_groups'])
        self.verify_batch1()
        self.go(self.one('review', 'T12')['id'], self.release_text())
        r = self.one('review', 'R12')['id']
        self.go(r, self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:修复'))
        rep = self.one('repair', 'D1')['id']
        self.go(rep, self.done('阻断关闭：B1=已关闭'))
        self.go(r, self.release_text())
        third = api.orders(self.st(), type='review', target='T12', state_='open')
        self.assertEqual(len(third), 1)
        rel = self.one('release', 'B1')['id']
        self.assertNotIn(rel, api.runnable(self.st(), 10))
        self.go(third[0]['id'], self.release_text())
        self.assertIn(rel, api.runnable(self.st(), 10))

    def test_decision_card_carries_failure_and_source_instruction(self):
        oid = self.impl('D1')
        for _ in range(2):
            rid = api.start(self.base, oid)
            api.abandon(self.base, oid, rid, 'specific authentication failure')
        api.escalate(self.base, oid, 'specific operational note')
        d = self.one('decide', 'D1')['id']
        c = cards.render(self.st(), d, api.assign(self.st(), d))
        for text in ('specific authentication failure', 'specific operational note', 'do d1'):
            self.assertIn(text, c)

    def test_session_limit_counts_all_calls_in_same_session(self):
        oid = self.impl('D1')
        for _ in range(2):
            self.go(oid, INFEASIBLE, conversation_id='same', session_kb=160)
        self.assertEqual(api.assign(self.st(), oid)['session']['rule'], 'size-limit')

    def test_cyclic_disposition_is_returned_to_lead(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        out = self.decide([{'open': 'implement', 'target': 'D3', 'input': {'instruction': 'depends on D1'}}])
        self.assertFalse(out['legal'])
        self.assertIn('等待环', out['reason'])

    def test_lead_explicitly_selects_dependent_work_to_repeat(self):
        self.verify_batch1()
        self.go(self.impl('D3'), self.done())
        p = copy.deepcopy(self.st().plan)
        p['deliverables']['D1']['acceptance'] = ['changed interface']
        api.revise_plan(self.base, p, self.approval, revalidate=['D1'])
        self.assertEqual(self.one('implement', 'D3')['state'], 'done')
        api.revise_plan(self.base, p, self.approval, revalidate=['D3'])
        self.assertEqual(len(api.orders(self.st(), type='implement', target='D3', state_='open')), 1)

    def test_exhausted_waiter_returns_to_lead_after_child_completes(self):
        p = make_plan()['policy']
        p['rounds'].update(implement=1, backup=0, lead=1)
        self.reinit(policy=p)
        oid = self.impl('D1')
        self.go(oid, NEEDS_DECISION)
        self.decide([{'open': 'implement', 'target': 'D1', 'input': {'instruction': '前置修复'}}])
        child = api.orders(self.st(), type='implement', target='D1')[-1]
        self.go(child['id'], self.done())
        self.assertEqual(self.one('decide', 'D1', state_='open')['input']['trigger'], 'exhausted')

    def test_new_contract_with_spent_budget_goes_to_lead(self):
        p = make_plan()['policy']
        p['rounds'].update(implement=1, backup=0, lead=1)
        self.reinit(policy=p)
        oid = self.impl('D1')
        self.go(oid, INFEASIBLE)
        self.go(oid, self.done())
        p = copy.deepcopy(self.st().plan)
        p['deliverables']['D1']['instruction'] = 'revised work'
        api.revise_plan(self.base, p, self.approval, revalidate=['D1'])
        self.assertEqual(self.one('decide', 'D1', state_='open')['input']['trigger'], 'exhausted')

    def test_cross_batch_review_reopens_its_own_batch_release(self):
        p = make_plan()
        p['roles']['third_party'] = p['roles']['reviewer']
        p['review_groups']['T13'] = {'deliverables': ['D1', 'D3'], 'batch': 'B2', 'reviewer': 'third_party'}
        self.reinit(roles=p['roles'], review_groups=p['review_groups'])
        self.verify_batch1()
        self.go(self.impl('D3'), self.done())
        self.go(self.one('review', 'T13')['id'], self.release_text())
        self.go(self.one('review', 'R12')['id'], self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:修复'))
        fresh = self.one('review', 'T13', state_='open')
        self.assertIn(fresh['id'], self.one('release', 'B2')['needs'])

    def test_reopened_review_shares_budget_and_accepts_explicit_group_grant(self):
        self.verify_batch1()
        review = self.one('review', 'R12')['id']
        self.go(review, self.release_text())
        self.go(self.impl('D3'), NEEDS_DECISION)
        self.go(self.one('decide', 'D3')['id'], '结果：VERIFIED\n停止原因：完成',
                decision={'diagnosis':'补充当前批次', 'actions':[{'open':'implement','target':'D1','input':{'instruction':'补充'}}]})
        fresh = self.one('review', 'R12', state_='open')
        self.assertEqual(policy.spent(self.st(), fresh), {'reviewer': 1})
        from bf import adjust
        adjust.grant(self.base, 'R12', self.approval, review=1)
        self.assertEqual(policy.rounds(self.st().plan, 'R12', 'review'), make_plan()['policy']['rounds']['review'] + 1)

    def test_driver_singleton_and_stop_boundary(self):
        fake = FakeAdapter(self.base, {('implement', 'D1'): [{'hang': True}]})
        first = runner.Runner(self.base, fake)
        second = runner.Runner(self.base, fake)
        try:
            first.step()
            with self.assertRaises(api.BfError):
                second.step()
            self.assertEqual(runner.ensure_running(self.base)['reason'], 'already running')
        finally:
            first.close()
            second.close()
        api.stop(self.base)
        self.assertFalse(runner.ensure_running(self.base)['started'])


# Fake only the CLI backend; the real bf CLI, adapter, driver, records and answer path run as processes.
FAKE_CLI = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, SCRIPTS)
from bf import api, evidence
from test_bf_core import user_question
a = sys.argv[1:]
def opt(n):
    return a[a.index(n)+1] if n in a else None
if a[0] == 'probe':
    Path(opt('--profile')).write_text('{}')
elif a[0] == 'run':
    rd = Path(opt('--run-dir')); rd.mkdir()
    base = rd.parents[3]
    order = api.load(base).orders[opt('--task')]
    kind = order['type']
    if kind == 'implement' and order['target'] == 'D1' and len(order['runs']) == 1:
        text = '结果：ARCH_DECISION_REQUIRED\n停止原因：待裁决\n上升问题：需批准继续'
    elif kind == 'decide':
        actions = ([{'retry':order['input']['subject']}] if order['input']['trigger'] == 'user_answer'
                   else [user_question('选择测试设备?')])
        (rd.parent/'decision.json').write_text(json.dumps({'diagnosis':'环境已核实，按答复处置', 'actions':actions}))
        text = '结果：VERIFIED\n停止原因：完成'
    else:
        record, code = evidence.record(base, [sys.executable, '-c', 'print("actual validation")'])
        text = ('结果：RELEASE\n复跑与证据：' if kind == 'review' else '结果：VERIFIED\n停止原因：完成\n验证证据：') + record['evidence']
    (rd/'response').write_text(text)
    (rd/'stdout').write_text('')
    (rd/'exit.json').write_text('{"returncode":0}')
else:
    rd = Path(opt('--run-dir'))
    print(json.dumps({'usable':True, 'conversation_id': str(rd.parent.name), 'response':(rd/'response').read_text()}))
'''


class Lifecycle(Base):
    def cli(self, *args):
        command = [sys.executable, str(Path(__file__).parent / 'bf' / 'cli.py'), '--base', str(self.base)] + list(args)
        return json.loads(subprocess.run(command, check=True, capture_output=True, text=True, timeout=20).stdout)

    def test_cli_answer_ensure_run_completes_waiting_task(self):
        p = copy.deepcopy(self.st().plan)
        p['workspace'] = str(Path(self.tmp.name))
        api.revise_plan(self.base, p, self.approval)
        backend = Path(self.tmp.name) / 'fake_cli.py'
        backend.write_text('SCRIPTS = %r\n' % str(Path(__file__).resolve().parent) + FAKE_CLI)
        self.cli('run', '--agent-cli', str(backend))
        ask = api.orders(self.st(), type='ask_user', state_='open')[0]
        self.assertFalse(api.driver_status(self.base)['alive'])
        answer = Path(self.tmp.name) / 'answer.txt'
        answer.write_text('批准继续')
        self.cli('answer', ask['id'], '--file', str(answer), '--approve')
        self.assertTrue(self.cli('ensure-run')['started'])
        deadline = time.monotonic() + 20
        final_report = None
        while time.monotonic() < deadline:
            try:
                final_report = json.loads((self.base / 'driver.log').read_text().splitlines()[-1])
            except (OSError, ValueError, IndexError):
                final_report = None
            if final_report and final_report.get('driver', {}).get('exited'):
                break
            time.sleep(0.05)
        self.assertTrue(final_report and final_report.get('driver', {}).get('exited'),
                        (self.base / 'driver.log').read_text())
        self.assertTrue(all(o['state'] == 'done' for o in api.orders(self.st(), type='release')),
                        (self.base / 'driver.log').read_text())


class Versions(RepoBase):
    def test_auto_snapshot_tampering_blocks_dependents(self):
        self.init()
        oid = self.one('implement', 'D1')['id']
        self.run_order(oid, self.done(), edit=self.touch('app.c', 'int x=1;\n'))
        snap = self.st().orders[oid]['runs'][-1]['snapshot'][0]
        content = json.loads(Path(snap).read_text())
        content['diff'] = 'changed after registration'
        Path(snap).write_text(json.dumps(content))
        self.assertIn(snap, api.broken_evidence(self.st()))
        self.run_order(self.one('implement', 'D2')['id'], self.done())
        self.assertIn(self.one('review', 'R12')['id'], api.runnable(self.st(), 10))


if __name__ == '__main__':
    unittest.main()
