#!/usr/bin/env python3
"""batch-flow v2 phase A specification tests (written before the implementation).

Each test names the design section it pins (design/workorder-redesign.md). The implementation in
scripts/bf/ must satisfy these tests unchanged; changing a test needs the technical lead's approval.
Python 3.8, standard library only.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bf import api  # noqa: E402
from bf.api import BfError  # noqa: E402

ENTRY = '/opt/task/tests/run_board_test.sh'


def make_plan(**over):
    p = {
        'name': 'demo',
        'goal': {'objective': 'ship X', 'constraints': ['keep Y']},
        'roles': {
            'executor': {'tool': 'codex', 'model': 'exec-m', 'effort': 'medium'},
            'backup_executor': {'tool': 'opencode', 'model': 'backup-m', 'effort': 'default'},
            'lead': {'tool': 'claude', 'model': 'lead-m', 'effort': 'medium'},
            'reviewer': {'tool': 'claude', 'model': 'rev-m', 'effort': 'medium'},
        },
        'policy': {
            'rounds': {'implement': 3, 'backup': 1, 'lead': 1, 'repair': 2, 'review': 3, 'decide_per_source': 2},
            'context': {'max_session_kb': 300},
            'continue_limit': 2,
        },
        'batches': [{'id': 'B1', 'title': 'one'}, {'id': 'B2', 'title': 'two'}],
        'deliverables': {
            'D1': {'batch': 'B1', 'title': 'd1', 'instruction': 'do d1', 'acceptance': ['a1'], 'depends_on': []},
            'D2': {'batch': 'B1', 'title': 'd2', 'instruction': 'do d2', 'acceptance': ['a2'], 'depends_on': []},
            'D3': {'batch': 'B2', 'title': 'd3', 'instruction': 'do d3', 'acceptance': ['a3'], 'depends_on': ['D1', 'D2']},
        },
        'review_groups': {'R12': {'deliverables': ['D1', 'D2']}, 'R3': {'deliverables': ['D3']}},
        'envelope': {'batches': ['B1', 'B2']},
        'env': {'codex': {'entry': [ENTRY], 'blocked_output': 'Sandbox detected|Missing sudo rule'}},
    }
    p.update(over)
    return p


UNFINISHED = '结果：FAILED\n停止原因：未完成\n上升问题：无\n'
INFEASIBLE = '结果：FAILED\n停止原因：技术不可行\n上升问题：试过方案 A、B 均失败\n'
NEEDS_DECISION = '结果：ARCH_DECISION_REQUIRED\n停止原因：待裁决\n上升问题：验收与冻结资产冲突\n'
ENV_BLOCK = '结果：WAIT_EVIDENCE\n停止原因：外部阻塞\n上升问题：入口前检失败\n'
EDIT = {'type': 'file_change'}


def cmd(command, code, output=''):
    return {'type': 'command_execution', 'command': command, 'exit_code': code, 'aggregated_output': output}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / '.batch-flow'
        self.base.mkdir()
        (self.base / 'records').mkdir()
        self.approval = Path(self.tmp.name) / 'approval.txt'
        self.approval.write_text('用户：计划批准')
        self.counter = 0
        self.init()

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, plan=None):
        api.init(self.base, plan or make_plan(), self.approval)

    def reinit(self, **plan_over):
        """Switch this test to a fresh task directory initialised with a modified plan."""
        self.counter += 1000
        self.base = Path(self.tmp.name) / ('bf-%d' % self.counter) / '.batch-flow'
        (self.base / 'records').mkdir(parents=True)
        api.init(self.base, make_plan(**plan_over), self.approval)

    def st(self):
        return api.load(self.base)

    def one(self, type_, target=None, state_=None):
        found = api.orders(self.st(), type=type_, target=target, state_=state_)
        self.assertEqual(len(found), 1, found)
        return found[0]

    def evidence(self):
        self.counter += 1
        path = self.base / 'records' / ('%032x.json' % self.counter)
        path.write_text(json.dumps({'exit_code': 0, 'n': self.counter}))
        return str(path)

    def done(self, extra=''):
        return '结果：VERIFIED\n停止原因：完成\n验证证据：%s\n上升问题：无\n%s' % (self.evidence(), extra)

    def release_text(self):
        return '结果：RELEASE\n复跑与证据：%s\n' % self.evidence()

    def fix(self, *findings):
        return '结果：FIX_REQUIRED\n复跑与证据：%s\n%s\n' % (self.evidence(), '\n'.join(findings))

    def run(self, order_id, text, events=(), conversation_id=None, start_conversation=None, **kw):
        run_id = api.start(self.base, order_id, conversation_id=start_conversation)
        return api.deliver(self.base, order_id, run_id, text, events=events,
                           conversation_id=conversation_id or 'conv-%s-%s' % (order_id, run_id), **kw)

    def impl(self, target):
        return self.one('implement', target)['id']

    def verify_batch1(self):
        self.run(self.impl('D1'), self.done())
        self.run(self.impl('D2'), self.done())


class StoreInvariants(Base):
    def test_init_opens_implement_review_release(self):  # §4, §5.2 row 1
        st = self.st()
        self.assertEqual([o['target'] for o in api.orders(st, type='implement')], ['D1', 'D2', 'D3'])
        reviews = api.orders(st, type='review')
        self.assertEqual([o['target'] for o in reviews], ['R12', 'R3'])
        self.assertEqual(sorted(reviews[0]['needs']), sorted([self.impl('D1'), self.impl('D2')]))
        self.assertEqual([o['target'] for o in api.orders(st, type='release')], ['B1', 'B2'])
        self.assertEqual(api.orders(st, type='triage'), [])
        for name in ['plan.json', 'orders.json', 'events.jsonl']:
            self.assertTrue((self.base / name).is_file(), name)

    def test_init_requires_user_approval_file(self):  # I6
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        with self.assertRaises(BfError):
            api.init(other, make_plan(), Path(self.tmp.name) / 'missing.txt')

    def test_plan_validation_rejects_unknown_dependency_and_mixed_review_group(self):  # §4.1
        other = Path(self.tmp.name) / 'o2'
        other.mkdir()
        bad = make_plan()
        bad['deliverables']['D3']['depends_on'] = ['DX']
        with self.assertRaises(BfError):
            api.init(other, bad, self.approval)
        bad = make_plan(review_groups={'RX': {'deliverables': ['D1', 'D3']}})
        with self.assertRaises(BfError):
            api.init(other, bad, self.approval)

    def test_cas_idempotency_and_event_log(self):  # I1
        rev = self.st().revision
        with self.assertRaises(BfError):
            api.start(self.base, self.impl('D1'), expected_revision=rev + 5)
        first = api.start(self.base, self.impl('D1'), expected_revision=rev, request_id='req-1')
        again = api.start(self.base, self.impl('D1'), request_id='req-1')
        self.assertEqual(first, again)
        self.assertEqual(self.st().revision, rev + 1)
        lines = (self.base / 'events.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), self.st().revision)
        self.assertEqual(json.loads(lines[-1])['request_id'], 'req-1')

    def test_state_is_derived_from_files_only(self):  # I7
        self.run(self.impl('D1'), self.done())
        before = api.runnable(self.st(), parallel=3)
        self.assertEqual(api.runnable(api.load(self.base), parallel=3), before)

    def test_runs_are_append_only(self):  # I9
        oid = self.impl('D1')
        self.run(oid, INFEASIBLE)
        first = copy.deepcopy(self.one('implement', 'D1')['runs'][0])
        self.run(oid, INFEASIBLE)
        runs = self.one('implement', 'D1')['runs']
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0], first)

    def test_stop_and_resume(self):  # I6
        api.stop(self.base)
        self.assertEqual(api.runnable(self.st(), parallel=3), [])
        with self.assertRaises(BfError):
            api.start(self.base, self.impl('D1'))
        api.resume(self.base)
        self.assertIn(self.impl('D1'), api.runnable(self.st(), parallel=3))


class Selection(Base):
    def test_dependencies_parallel_and_priority(self):  # §5.4
        st = self.st()
        self.assertEqual(api.runnable(st), [self.impl('D1')])
        self.assertEqual(api.runnable(st, parallel=3), [self.impl('D1'), self.impl('D2')])  # D3 waits for D1, D2
        self.run(self.impl('D1'), NEEDS_DECISION)
        decide = self.one('decide', 'D1')['id']
        self.assertEqual(api.runnable(self.st(), parallel=3), [decide, self.impl('D2')])  # decide before implement

    def test_one_running_order_per_target_and_exclusive_resources(self):  # §5.4, §6.4
        deliverables = make_plan()['deliverables']
        deliverables['D1']['resources'] = ['board']
        deliverables['D2']['resources'] = ['board']
        self.reinit(deliverables=deliverables)
        api.start(self.base, self.impl('D1'))
        self.assertEqual(api.runnable(self.st(), parallel=3), [])  # D1 running, D2 shares the board

    def test_envelope_limits_batches(self):  # I6, §4.1
        deliverables = make_plan()['deliverables']
        deliverables['D3']['depends_on'] = []
        self.reinit(deliverables=deliverables, envelope={'batches': ['B1']})
        self.assertEqual(api.runnable(self.st(), parallel=5), [self.impl('D1'), self.impl('D2')])

    def test_open_decide_blocks_its_target_only(self):  # §5.4
        self.run(self.impl('D1'), NEEDS_DECISION)
        runnable = api.runnable(self.st(), parallel=5)
        self.assertNotIn(self.impl('D1'), runnable)
        self.assertIn(self.impl('D2'), runnable)


class Gate(Base):
    def test_unfinished_continues_in_same_run_then_registers_failed(self):  # §10.3
        oid = self.impl('D1')
        rid = api.start(self.base, oid)
        out = api.deliver(self.base, oid, rid, UNFINISHED, events=[EDIT], conversation_id='c1')
        self.assertEqual((out['legal'], out['outcome']), (False, 'continue'))
        out = api.deliver(self.base, oid, rid, UNFINISHED, events=[EDIT], conversation_id='c1')
        self.assertEqual(out['outcome'], 'continue')
        out = api.deliver(self.base, oid, rid, UNFINISHED, events=[EDIT], conversation_id='c1')
        self.assertEqual(out['outcome'], 'registered')
        run = self.one('implement', 'D1')['runs'][0]
        self.assertEqual((run['result'], run['segments']), ('FAILED', 3))
        self.assertEqual(len(self.one('implement', 'D1')['runs']), 1)
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'primary')  # next ordinary round, same ladder

    def test_continuation_requires_progress(self):  # §10.3
        oid = self.impl('D1')
        rid = api.start(self.base, oid)
        self.assertEqual(api.deliver(self.base, oid, rid, UNFINISHED, events=[EDIT])['outcome'], 'continue')
        self.assertEqual(api.deliver(self.base, oid, rid, UNFINISHED, events=[])['outcome'], 'registered')

    def test_verified_needs_existing_evidence(self):  # I5
        oid = self.impl('D1')
        rid = api.start(self.base, oid)
        out = api.deliver(self.base, oid, rid, '结果：VERIFIED\n停止原因：完成\n验证证据：%s\n' % (self.base / 'records' / 'nope.json'))
        self.assertFalse(out['legal'])
        out = api.deliver(self.base, oid, rid, self.done(), events=[EDIT])
        self.assertTrue(out['legal'])
        self.assertEqual(self.one('implement', 'D1')['state'], 'done')

    def test_reason_and_result_must_match(self):  # §10.2
        oid = self.impl('D1')
        rid = api.start(self.base, oid)
        self.assertFalse(api.deliver(self.base, oid, rid, '结果：FAILED\n停止原因：待裁决\n上升问题：x\n', events=[EDIT])['legal'])
        self.assertFalse(api.deliver(self.base, oid, rid, '结果：FAILED\n停止原因：技术不可行\n上升问题：无\n', events=[EDIT])['legal'])

    def test_environment_block_needs_direct_entry_environment_failure(self):  # §10.3, DEC-41
        oid = self.impl('D1')
        rid = api.start(self.base, oid)
        own_shell = cmd("awk '/NoNewPrivs/' /proc/self/status", 0, 'NoNewPrivs: 1')
        self.assertFalse(api.deliver(self.base, oid, rid, ENV_BLOCK, events=[own_shell, EDIT])['legal'])
        wrapped = cmd("/bin/bash -lc 'date; %s s3'" % ENTRY, 1, 'ERROR: Sandbox detected')
        self.assertFalse(api.deliver(self.base, oid, rid, ENV_BLOCK, events=[wrapped, EDIT])['legal'])
        product = cmd("/bin/bash -lc '%s s3'" % ENTRY, 1, 'TC-03 FAIL')
        out = api.deliver(self.base, oid, rid, ENV_BLOCK, events=[product])
        self.assertEqual(out['outcome'], 'registered')  # limit reached: FAILED, not an environment block
        self.assertEqual(self.one('implement', 'D1')['runs'][0]['result'], 'FAILED')

    def test_legal_environment_block_without_work_is_exempt_and_opens_decide(self):  # §7
        oid = self.impl('D1')
        blocked = cmd("/bin/bash -lc '%s preflight'" % ENTRY, 1, 'ERROR: Sandbox detected: NoNewPrivs is 1')
        out = self.run(oid, ENV_BLOCK, events=[blocked])
        self.assertTrue(out['legal'])
        run = self.one('implement', 'D1')['runs'][0]
        self.assertEqual((run['result'], run['exempt']), ('WAIT_EVIDENCE', True))
        self.assertEqual(self.one('decide', 'D1')['state'], 'open')
        self.assertIn(self.one('decide', 'D1')['id'], self.one('implement', 'D1')['needs'])

    def test_environment_block_after_edits_is_charged(self):  # §7
        blocked = cmd("/bin/bash -lc '%s preflight'" % ENTRY, 1, 'ERROR: Sandbox detected')
        self.run(self.impl('D1'), ENV_BLOCK, events=[EDIT, blocked])
        self.assertFalse(self.one('implement', 'D1')['runs'][0]['exempt'])

    def test_review_findings_must_be_structured_and_in_group(self):  # §10.2
        self.verify_batch1()
        rid_order = self.one('review', 'R12')['id']
        rid = api.start(self.base, rid_order)
        self.assertFalse(api.deliver(self.base, rid_order, rid, '结果：FIX_REQUIRED\n问题：请修正文档\n')['legal'])
        self.assertFalse(api.deliver(self.base, rid_order, rid, self.fix('阻断：B1 | 返修:D3 | 方案级:否 | 关闭条件:x'))['legal'])


class Routing(Base):
    def test_verified_review_release_and_next_batch(self):  # §5.2
        self.verify_batch1()
        review = self.one('review', 'R12')['id']
        self.assertEqual(api.runnable(self.st()), [review])
        self.assertEqual(api.assign(self.st(), review)['role'], 'reviewer')
        self.run(review, self.release_text())
        release = self.one('release', 'B1')['id']
        runnable = api.runnable(self.st(), parallel=5)
        self.assertIn(release, runnable)
        self.assertIn(self.impl('D3'), runnable)
        with self.assertRaises(BfError):
            api.assign(self.st(), release)  # mechanical, no role
        api.release(self.base, release)
        self.assertEqual(self.one('release', 'B1')['state'], 'done')

    def test_fix_required_repairs_only_named_target_with_findings(self):  # §5.2, problem 5/6
        self.verify_batch1()
        review = self.one('review', 'R12')['id']
        self.run(review, self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:改正报告文字'))
        repair = self.one('repair', 'D1')
        self.assertEqual(api.orders(self.st(), type='repair', target='D2'), [])
        self.assertEqual([f['id'] for f in repair['input']['findings']], ['B1'])
        self.assertEqual(repair['input']['findings'][0]['closure'], '改正报告文字')
        self.assertEqual(repair['input']['review_order'], review)
        self.assertIn(repair['id'], self.one('review', 'R12')['needs'])
        self.assertEqual(api.runnable(self.st()), [repair['id']])
        rid = api.start(self.base, repair['id'])
        self.assertFalse(api.deliver(self.base, repair['id'], rid, self.done(), events=[EDIT])['legal'])  # closure line required
        out = api.deliver(self.base, repair['id'], rid, self.done('阻断关闭：B1=已关闭 已改第 2.3 节\n'), events=[EDIT])
        self.assertTrue(out['legal'])
        self.assertEqual(api.runnable(self.st()), [review])
        self.run(review, self.release_text())
        self.assertEqual(self.one('review', 'R12')['state'], 'done')

    def test_repair_chain_does_not_inherit_implement_stage(self):  # §8, problem 7
        oid = self.impl('D1')
        for _ in range(3):
            self.run(oid, INFEASIBLE)
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'backup')
        self.run(oid, self.done())
        self.run(self.impl('D2'), self.done())
        self.run(self.one('review', 'R12')['id'], self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:x'))
        a = api.assign(self.st(), self.one('repair', 'D1')['id'])
        self.assertEqual((a['stage'], a['model']), ('primary', 'exec-m'))

    def test_plan_level_finding_goes_to_lead(self):  # §8
        self.verify_batch1()
        self.run(self.one('review', 'R12')['id'], self.fix('阻断：B1 | 返修:D2 | 方案级:是 | 关闭条件:改方案'))
        a = api.assign(self.st(), self.one('repair', 'D2')['id'])
        self.assertEqual((a['stage'], a['role']), ('lead', 'lead'))

    def test_repair_escalates_when_finding_stays_open(self):  # §8
        policy = make_plan()['policy']
        policy['rounds']['review'] = 10
        self.reinit(policy=policy)
        self.verify_batch1()
        review = self.one('review', 'R12')['id']
        finding = '阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:x'
        self.run(review, self.fix(finding))
        stages = []
        for _ in range(4):
            repair = self.one('repair', 'D1')
            stages.append(api.assign(self.st(), repair['id'])['stage'])
            self.run(repair['id'], self.done('阻断关闭：B1=已关闭 y\n'), events=[EDIT])
            self.run(review, self.fix(finding))
        self.assertEqual(stages, ['primary', 'primary', 'backup', 'lead'])
        self.assertEqual(len(api.orders(self.st(), type='repair', target='D1')), 1)  # same order reopened
        with self.assertRaises(BfError):
            api.assign(self.st(), self.one('repair', 'D1')['id'])
        self.assertEqual(self.one('decide', 'D1')['state'], 'open')  # exhausted chain → decide

    def test_implement_ladder_then_decide(self):  # I3, §8
        oid = self.impl('D1')
        stages = []
        for _ in range(5):
            a = api.assign(self.st(), oid)
            stages.append((a['stage'], a['model']))
            self.run(oid, INFEASIBLE)
        self.assertEqual(stages, [('primary', 'exec-m')] * 3 + [('backup', 'backup-m'), ('lead', 'lead-m')])
        with self.assertRaises(BfError):
            api.assign(self.st(), oid)
        decide = self.one('decide', 'D1')
        self.assertIn(decide['id'], self.one('implement', 'D1')['needs'])
        self.assertEqual(api.assign(self.st(), decide['id'])['role'], 'lead')

    def test_decide_within_envelope_executes_directly(self):  # §7
        self.run(self.impl('D1'), NEEDS_DECISION)
        decide = self.one('decide', 'D1')['id']
        decision = {'diagnosis': '按方案 B 继续', 'actions': [
            {'open': 'implement', 'target': 'D2', 'input': {'instruction': '补充 D2 的接口说明'}},
            {'retry': self.impl('D1'), 'reason': '按方案 B', 'fresh_session': True}]}
        out = self.run(decide, '结果：VERIFIED\n停止原因：完成\n', decision=decision)
        self.assertTrue(out['legal'])
        self.assertEqual(self.one('decide', 'D1')['state'], 'done')
        self.assertEqual(len(api.orders(self.st(), type='implement', target='D2')), 2)
        self.assertIn(self.impl('D1'), api.runnable(self.st(), parallel=5))
        self.assertEqual(api.assign(self.st(), self.impl('D1'))['session']['mode'], 'fresh')

    def test_decide_supersede(self):  # §7
        self.run(self.impl('D1'), NEEDS_DECISION)
        decide = self.one('decide', 'D1')['id']
        self.run(decide, '结果：VERIFIED\n停止原因：完成\n',
                 decision={'diagnosis': 'd', 'actions': [{'supersede': self.impl('D1'), 'reason': '目标已由 D2 覆盖'}]})
        self.assertEqual(api.orders(self.st(), type='implement', target='D1')[0]['state'], 'superseded')

    def test_grant_or_plan_change_asks_user(self):  # §7, I2, I6
        oid = self.impl('D1')
        for _ in range(5):
            self.run(oid, INFEASIBLE)
        decide = self.one('decide', 'D1')['id']
        self.run(decide, '结果：VERIFIED\n停止原因：完成\n',
                 decision={'diagnosis': 'd', 'actions': [{'grant': {'D1': {'implement': 2}}}]})
        ask = self.one('ask_user', 'D1')
        self.assertIn(ask['id'], self.one('implement', 'D1')['needs'])
        self.assertEqual(api.runnable(self.st(), parallel=5).count(ask['id']), 0)  # never dispatched to a role
        api.answer(self.base, ask['id'], '同意追加两轮', approve=True)
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'primary')
        self.assertEqual(self.st().plan['policy']['overrides']['D1']['implement'], 5)

    def test_rejected_answer_returns_to_lead(self):  # §7
        oid = self.impl('D1')
        for _ in range(5):
            self.run(oid, INFEASIBLE)
        self.run(self.one('decide', 'D1')['id'], '结果：VERIFIED\n停止原因：完成\n',
                 decision={'diagnosis': 'd', 'actions': [{'grant': {'D1': {'implement': 1}}}]})
        api.answer(self.base, self.one('ask_user', 'D1')['id'], '不加预算，缩小范围', approve=False)
        decides = api.orders(self.st(), type='decide', target='D1', state_='open')
        self.assertEqual(len(decides), 1)
        self.assertIn('不加预算，缩小范围', json.dumps(decides[0]['input'], ensure_ascii=False))

    def test_open_outside_envelope_asks_user(self):  # I6
        other = Path(self.tmp.name) / 'env2'
        (other / 'records').mkdir(parents=True)
        api.init(other, make_plan(envelope={'batches': ['B1']}), self.approval)
        oid = api.orders(api.load(other), type='implement', target='D1')[0]['id']
        rid = api.start(other, oid)
        api.deliver(other, oid, rid, NEEDS_DECISION, conversation_id='c')
        decide = api.orders(api.load(other), type='decide', target='D1')[0]['id']
        rid = api.start(other, decide)
        api.deliver(other, decide, rid, '结果：VERIFIED\n停止原因：完成\n', conversation_id='l',
                    decision={'diagnosis': 'd', 'actions': [{'open': 'implement', 'target': 'D3', 'input': {'instruction': 'x'}}]})
        self.assertEqual(len(api.orders(api.load(other), type='ask_user')), 1)
        self.assertEqual(len(api.orders(api.load(other), type='implement', target='D3')), 1)

    def test_decide_limit_per_source_then_ask_user(self):  # §8
        oid = self.impl('D1')
        self.run(oid, NEEDS_DECISION)
        for _ in range(2):
            decide = self.one('decide', 'D1', state_='open')['id']
            self.run(decide, '结果：VERIFIED\n停止原因：完成\n', decision={'diagnosis': 'still unclear', 'actions': []})
            self.run(oid, NEEDS_DECISION)
        self.assertEqual(len(api.orders(self.st(), type='decide', target='D1')), 2)
        self.assertEqual(api.orders(self.st(), type='decide', target='D1', state_='open'), [])
        self.assertEqual(self.one('ask_user', 'D1')['state'], 'open')

    def test_candidates_go_to_batch_triage_and_gate_release(self):  # §5.2, decision 3
        self.run(self.impl('D1'), self.done('候选新项：\n- CAN 自测偶发超时（records/x）\n- SII 偶发读失败\n'))
        self.run(self.impl('D2'), self.done())
        self.run(self.one('review', 'R12')['id'], self.release_text())
        triage = self.one('triage', 'B1')
        self.assertEqual(len(triage['input']['items']), 2)
        release = self.one('release', 'B1')['id']
        self.assertNotIn(release, api.runnable(self.st(), parallel=5))
        self.assertEqual(api.assign(self.st(), triage['id'])['role'], 'lead')
        ids = [item['id'] for item in triage['input']['items']]
        partial = {'diagnosis': 't', 'items': {ids[0]: {'disposition': 'merge', 'target': 'D3', 'reason': '同属 D3'}}}
        rid = api.start(self.base, triage['id'])
        self.assertFalse(api.deliver(self.base, triage['id'], rid, '结果：VERIFIED\n停止原因：完成\n', decision=partial, events=[EDIT])['legal'])
        full = {'diagnosis': 't', 'items': {ids[0]: {'disposition': 'merge', 'target': 'D3', 'reason': '同属 D3'},
                                            ids[1]: {'disposition': 'defer', 'reason': '待实物从站'}}}
        self.assertTrue(api.deliver(self.base, triage['id'], rid, '结果：VERIFIED\n停止原因：完成\n', decision=full)['legal'])
        self.assertIn(release, api.runnable(self.st(), parallel=5))
        merged = [o for o in api.orders(self.st(), type='implement', target='D3') if o['origin']['by'] == 'order']
        self.assertEqual(len(merged), 1)

    def test_budget_never_reset_and_revision_needs_approval(self):  # I2
        oid = self.impl('D1')
        self.run(oid, INFEASIBLE)
        plan = copy.deepcopy(self.st().plan)
        with self.assertRaises(BfError):
            api.revise_plan(self.base, plan, Path(self.tmp.name) / 'missing.txt')
        plan['policy']['rounds']['implement'] = 0
        with self.assertRaises(BfError):
            api.revise_plan(self.base, plan, self.approval)  # below what was already used
        plan['policy']['rounds']['implement'] = 4
        api.revise_plan(self.base, plan, self.approval)
        self.assertEqual(self.st().plan['policy']['rounds']['implement'], 4)
        self.assertEqual(len(self.one('implement', 'D1')['runs']), 1)

    def test_broken_evidence_blocks_dependents(self):  # I5
        self.run(self.impl('D1'), self.done())
        self.run(self.impl('D2'), self.done())
        evidence = list(self.one('implement', 'D1')['runs'][0]['evidence'])[0]
        Path(evidence).write_text('changed')
        st = self.st()
        self.assertEqual(api.broken_evidence(st), [evidence])
        self.assertNotIn(self.one('review', 'R12')['id'], api.runnable(st, parallel=5))


class Context(Base):  # §9
    def test_implement_rounds(self):
        oid = self.impl('D1')
        a = api.assign(self.st(), oid)
        self.assertEqual(a['session']['mode'], 'fresh')
        self.assertTrue(a['session']['rule'])
        self.run(oid, INFEASIBLE, conversation_id='s1')
        a = api.assign(self.st(), oid)
        self.assertEqual((a['session']['mode'], a['session']['conversation_id']), ('resume', 's1'))
        rid = api.start(self.base, oid, conversation_id='s1')
        api.deliver(self.base, oid, rid, INFEASIBLE, conversation_id='s1', session_kb=400)
        self.assertEqual(api.assign(self.st(), oid)['session']['mode'], 'fresh')  # over the size limit
        self.run(oid, INFEASIBLE, conversation_id='s2')
        a = api.assign(self.st(), oid)
        self.assertEqual(a['stage'], 'backup')
        self.assertEqual(a['session']['mode'], 'fresh')  # stage change

    def test_rejected_environment_claim_forces_fresh_session(self):
        oid = self.impl('D1')
        rid = api.start(self.base, oid)
        own = cmd("awk '/NoNewPrivs/' /proc/self/status", 0, 'NoNewPrivs: 1')
        api.deliver(self.base, oid, rid, ENV_BLOCK, events=[own, EDIT], conversation_id='s1')
        api.deliver(self.base, oid, rid, ENV_BLOCK, events=[own, EDIT], conversation_id='s1')
        api.deliver(self.base, oid, rid, ENV_BLOCK, events=[own, EDIT], conversation_id='s1')
        self.assertEqual(api.assign(self.st(), oid)['session']['mode'], 'fresh')

    def test_repair_review_decide_triage(self):
        self.run(self.impl('D1'), self.done(), conversation_id='impl-1')
        self.run(self.impl('D2'), self.done(), conversation_id='impl-2')
        review = self.one('review', 'R12')['id']
        a = api.assign(self.st(), review)
        self.assertEqual(a['session']['mode'], 'fresh')
        self.run(review, self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:x'), conversation_id='rev-1')
        repair = self.one('repair', 'D1')['id']
        self.assertEqual(api.assign(self.st(), repair)['session']['mode'], 'fresh')
        self.run(repair, self.done('阻断关闭：B1=已关闭 y\n'), events=[EDIT], conversation_id='rep-1')
        a = api.assign(self.st(), review)
        self.assertEqual((a['session']['mode'], a['session']['conversation_id']), ('resume', 'rev-1'))
        self.run(review, self.release_text(), conversation_id='rev-1')
        self.run(self.impl('D3'), NEEDS_DECISION, conversation_id='impl-3')
        decide = self.one('decide', 'D3')['id']
        self.run(decide, '结果：VERIFIED\n停止原因：完成\n', decision={'diagnosis': 'd', 'actions': []}, conversation_id='lead-1')
        self.run(self.impl('D3'), NEEDS_DECISION, conversation_id='impl-3')
        second = api.orders(self.st(), type='decide', target='D3', state_='open')[0]['id']
        a = api.assign(self.st(), second)
        self.assertEqual((a['session']['mode'], a['session']['conversation_id']), ('resume', 'lead-1'))

    def test_review_never_resumes_an_author_session(self):  # I4
        self.run(self.impl('D1'), self.done(), conversation_id='shared')
        self.run(self.impl('D2'), self.done(), conversation_id='impl-2')
        review = self.one('review', 'R12')['id']
        self.run(review, self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:x'), conversation_id='rev-1')
        repair = self.one('repair', 'D1')['id']
        self.run(repair, self.done('阻断关闭：B1=已关闭 y\n'), events=[EDIT], conversation_id='rev-1')  # same id as reviewer
        a = api.assign(self.st(), review)
        self.assertEqual(a['session']['mode'], 'fresh')
        with self.assertRaises(BfError):
            api.start(self.base, review, conversation_id='shared')  # an implementation session cannot review

    def test_decide_does_not_resume_an_author_lead_session(self):
        oid = self.impl('D1')
        self.run(oid, NEEDS_DECISION, conversation_id='e0')
        self.run(self.one('decide', 'D1', state_='open')['id'], '结果：VERIFIED\n停止原因：完成\n',
                 decision={'diagnosis': 'd', 'actions': []}, conversation_id='L')
        self.run(oid, INFEASIBLE, conversation_id='e1')
        self.run(oid, INFEASIBLE, conversation_id='e2')
        self.run(oid, INFEASIBLE, conversation_id='e3')  # backup
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'lead')
        self.run(oid, NEEDS_DECISION, conversation_id='L', start_conversation='L')  # lead implemented D1 in session L
        second = self.one('decide', 'D1', state_='open')['id']
        self.assertEqual(api.assign(self.st(), second)['session']['mode'], 'fresh')

if __name__ == '__main__':
    unittest.main()
