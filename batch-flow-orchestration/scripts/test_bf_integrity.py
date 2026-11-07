#!/usr/bin/env python3
"""batch-flow v2 integrity specification (design §24): automatic per-run snapshots, protected paths, plan and design
checks, review integrity, evidence recency and version binding. Written before the implementation. Python 3.8 stdlib.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bf import api  # noqa: E402
from test_bf_core import make_plan, record_json, EDIT, NEEDS_DECISION  # noqa: E402

DESIGN = '# 设计\n\n## 3.1 第一批\n方案 A。\n\n## 3.2 第二批\n方案 B。\n'


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo)] + list(args), check=True, capture_output=True, text=True).stdout


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'src'
        (self.repo / 'tests').mkdir(parents=True)
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', 'user.email', 't@t')
        git(self.repo, 'config', 'user.name', 't')
        (self.repo / 'tests' / 'run_board_test.sh').write_text('#!/bin/sh\necho entry\n')
        (self.repo / 'app.c').write_text('int x;\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        self.base = self.root / 'task' / '.batch-flow'
        (self.base / 'records').mkdir(parents=True)
        self.design = self.root / 'task' / 'design.md'
        self.design.write_text(DESIGN)
        self.approval = self.root / 'ok.txt'
        self.approval.write_text('批准')
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, **over):
        p = make_plan(workspace=str(self.repo), repos=[str(self.repo)], protected=['tests/run_board_test.sh'],
                      design={'path': str(self.design)})
        for d, ref in (('D1', '3.1 第一批'), ('D2', '3.1 第一批'), ('D3', '3.2 第二批')):
            p['deliverables'][d]['design_ref'] = ref
        p.update(over)
        return p

    def init(self, plan=None):
        return api.init(self.base, plan or self.plan(), self.approval)

    def st(self):
        return api.load(self.base)

    def one(self, type_, target, state_=None):
        found = api.orders(self.st(), type=type_, target=target, state_=state_)
        self.assertEqual(len(found), 1, found)
        return found[0]

    def ev(self, exit_code=0):
        self.n += 1
        path = self.base / 'records' / ('%032x.json' % self.n)
        path.write_text(record_json(self.n, exit_code))
        return str(path)

    def done(self, extra='', ev=None):
        """Deferred: the evidence record is produced when the delivery is made, i.e. inside the run (§24.6)."""
        return lambda: '结果：VERIFIED\n停止原因：完成\n验证证据：%s\n上升问题：无\n%s' % (ev or self.ev(), extra)

    def release_text(self, extra=''):
        return lambda: '结果：RELEASE\n复跑与证据：%s\n%s' % (self.ev(), extra)

    def run_order(self, oid, text, edit=None, **kw):
        run = api.start(self.base, oid)
        if edit:
            edit()
        text = text() if callable(text) else text
        return api.deliver(self.base, oid, run, text, events=[EDIT] if edit else [], conversation_id='c-' + oid, **kw)

    def touch(self, rel, text):
        def edit():
            (self.repo / rel).write_text(text)
        return edit


class PlanChecks(Base):  # §24.3
    def test_every_deliverable_reviewed_unless_reason(self):
        p = self.plan()
        p['review_groups'] = {'R12': {'deliverables': ['D1', 'D2']}}
        with self.assertRaises(api.BfError):
            self.init(p)
        p['deliverables']['D3']['no_review'] = '纯文档汇总，由阶段第三方审阅覆盖'
        self.init(p)

    def test_instruction_acceptance_and_design_refs(self):
        for mutate in (lambda p: p['deliverables']['D1'].update(instruction=''),
                       lambda p: p['deliverables']['D1'].update(acceptance=[]),
                       lambda p: p['deliverables']['D1'].pop('design_ref'),
                       lambda p: p['deliverables']['D1'].update(design_ref='9.9 不存在')):
            p = self.plan()
            mutate(p)
            with self.assertRaises(api.BfError):
                api.init(self.root / ('x%d' % id(p)) / '.batch-flow', p, self.approval)
        result = self.init()
        self.assertEqual(self.st().plan['design']['sha256'], __import__('hashlib').sha256(self.design.read_bytes()).hexdigest())

    def test_review_rounds_warning(self):
        p = self.plan()
        p['policy']['rounds']['review'] = 1
        result = self.init(p)
        self.assertTrue(any('review' in w for w in self.st().plan.get('warnings', [])))


class DesignDrift(Base):  # §24.4
    def test_changed_section_pauses_its_deliverables_and_asks_lead(self):
        self.init()
        self.design.write_text(DESIGN.replace('方案 A。', '方案 A，改为双缓冲。'))
        st = self.st()
        runnable = api.runnable(st, parallel=5)
        self.assertNotIn(self.one('implement', 'D1')['id'], runnable)
        self.assertNotIn(self.one('implement', 'D2')['id'], runnable)
        api.check_design(self.base)                      # the driver calls this before each dispatch
        decide = self.one('decide', None)
        self.assertEqual(decide['input']['trigger'], 'design_drift')
        self.assertEqual(sorted(decide['input']['deliverables']), ['D1', 'D2'])
        self.run_order(decide['id'], '结果：VERIFIED\n停止原因：完成\n',
                       decision={'diagnosis': '仅措辞调整，方案不变', 'actions': [{'refresh_design': True}]})
        self.assertIn(self.one('implement', 'D1')['id'], api.runnable(self.st(), parallel=5))

    def test_unrelated_section_change_does_not_pause(self):
        self.init()
        self.design.write_text(DESIGN.replace('方案 B。', '方案 B2。'))
        self.assertIn(self.one('implement', 'D1')['id'], api.runnable(self.st(), parallel=5))


class Snapshots(Base):  # §24.1
    def test_each_registered_run_gets_a_snapshot_of_its_own_changes(self):
        self.init()
        (self.repo / 'scratch.txt').write_text('dirty before the run')  # pre-existing change is not this run's
        oid = self.one('implement', 'D1')['id']
        self.run_order(oid, self.done(), edit=self.touch('app.c', 'int x = 1;\n'))
        run = self.one('implement', 'D1')['runs'][-1]
        self.assertEqual(run['changed'], {str(self.repo): ['app.c']})
        snap = json.loads(Path(run['version']).read_text())
        self.assertEqual(snap['paths'], ['app.c'])
        self.assertIn('int x = 1;', snap['diff'])

    def test_review_card_and_fixed_version_bind_to_snapshots(self):
        self.init()
        for d in ('D1', 'D2'):
            self.run_order(self.one('implement', d)['id'], self.done(), edit=self.touch('%s.c' % d, d))
        versions = [self.one('implement', d)['runs'][-1]['version'] for d in ('D1', 'D2')]
        review = self.one('review', 'R12')['id']
        run = api.start(self.base, review)
        out = api.deliver(self.base, review, run, self.release_text('固定版本：%s\n' % versions[0])(), conversation_id='rv')
        self.assertFalse(out['legal'])                    # must name every reviewed deliverable's snapshot
        out = api.deliver(self.base, review, run, self.release_text('固定版本：%s %s\n' % tuple(versions))(), conversation_id='rv')
        self.assertTrue(out['legal'])


class Protected(Base):  # §24.2
    def prepare_review(self, protected=None):
        p = self.plan()
        if protected:
            p['protected'] += protected
        self.init(p)
        for d in ('D1', 'D2'):
            self.run_order(self.one('implement', d)['id'], self.done())
        return self.one('review', 'R12')['id']

    def test_generated_logs_in_source_repo_require_disposition(self):
        oid = self.prepare_review()
        def preflight():
            logs = self.repo/'tests/logs'
            (logs/'preflight-1').mkdir(parents=True)
            (logs/'preflight-1/output.txt').write_text('PASS')
            (logs/'latest').symlink_to('preflight-1')
        text = lambda: ('结果：FIX_REQUIRED\n复跑与证据：%s\n'
                        '阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:使用 bf record\n' % self.ev())
        out = self.run_order(oid, text, edit=preflight)
        self.assertFalse(out['legal'], out)
        self.assertEqual(self.one('decide', 'R12')['input']['trigger'], 'review_modified')
        run = self.st().orders[oid]['runs'][-1]
        self.assertIn('tests/logs/latest', run['changed'][str(self.repo)])
        self.assertTrue(run['snapshot'])
        self.assertFalse(api.orders(self.st(), type='repair', target='D1'))

    def test_run_artifacts_outside_source_repo_preserve_review_findings(self):
        oid = self.prepare_review()
        def preflight():
            runtime = self.base/'records/run-artifacts'
            runtime.mkdir()
            (runtime/'output.txt').write_text('PASS')
        text = lambda: ('结果：FIX_REQUIRED\n复跑与证据：%s\n'
                        '阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:修正镜像身份\n' % self.ev())
        out = self.run_order(oid, text, edit=preflight)
        self.assertTrue(out['legal'], out)
        self.assertEqual(self.one('repair','D1')['input']['findings'][0]['closure'],'修正镜像身份')
        self.assertEqual(self.st().orders[oid]['runs'][-1]['changed'], {})

    def test_log_and_source_changes_both_block_review(self):
        oid = self.prepare_review()
        def edit():
            (self.repo/'tests/output.txt').write_text('log')
            (self.repo/'app.c').write_text('changed')
        out = self.run_order(oid, self.release_text(), edit=edit)
        self.assertFalse(out['legal'])
        self.assertIn('app.c', out['reason'])
        self.assertIn('tests/output.txt', out['reason'])

    def test_tracked_and_protected_files_are_checked_regardless_of_log_names(self):
        (self.repo/'tests/logs').mkdir()
        (self.repo/'tests/logs/tracked.txt').write_text('original')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'tracked log')
        oid = self.prepare_review(['tests/logs/protected.txt'])
        def edit():
            (self.repo/'tests/logs/tracked.txt').write_text('modified')
            (self.repo/'tests/logs/protected.txt').write_text('modified')
        out = self.run_order(oid, self.release_text(), edit=edit)
        self.assertFalse(out['legal'])
        self.assertIn('tests/logs/tracked.txt', out['reason'])
        self.assertIn('tests/logs/protected.txt', out['reason'])

    def test_committing_deletion_of_tracked_log_is_still_a_review_change(self):
        (self.repo/'tests/logs').mkdir()
        (self.repo/'tests/logs/tracked.txt').write_text('original')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'tracked log')
        oid = self.prepare_review()
        def edit():
            git(self.repo, 'rm', 'tests/logs/tracked.txt')
            git(self.repo, 'commit', '-qm', 'delete log')
        out = self.run_order(oid, self.release_text(), edit=edit)
        self.assertFalse(out['legal'])
        self.assertIn('tests/logs/tracked.txt', out['reason'])

    def test_touching_a_protected_path_is_refused_and_goes_to_lead(self):
        self.init()
        oid = self.one('implement', 'D1')['id']
        out = self.run_order(oid, self.done(), edit=self.touch('tests/run_board_test.sh', '#!/bin/sh\necho changed\n'))
        self.assertFalse(out['legal'])
        self.assertEqual(out['outcome'], 'registered')    # not continued: the change itself must be handled
        run = self.one('implement', 'D1')['runs'][-1]
        self.assertEqual(run['result'], 'FAILED')
        self.assertTrue(Path(run['version']).is_file())   # snapshot kept so the change can be reverted exactly
        decide = self.one('decide', 'D1')
        self.assertEqual(decide['input']['trigger'], 'protected_change')
        self.assertIn('tests/run_board_test.sh', json.dumps(decide['input'], ensure_ascii=False))

    def test_authorizing_a_protected_change_needs_the_user(self):
        self.init()
        oid = self.one('implement', 'D1')['id']
        self.run_order(oid, '结果：ARCH_DECISION_REQUIRED\n停止原因：待裁决\n上升问题：入口需改\n', edit=None)
        decide = self.one('decide', 'D1')['id']
        self.run_order(decide, '结果：VERIFIED\n停止原因：完成\n', decision={'diagnosis': 'd', 'actions': [
            {'open': 'implement', 'target': 'D1', 'input': {'instruction': '改入口'}, 'allow_protected': ['tests/run_board_test.sh']}]})
        ask = self.one('ask_user', 'D1')
        api.answer(self.base, ask['id'], '同意改入口', approve=True)
        new = [o for o in api.orders(self.st(), type='implement', target='D1') if o['origin']['by'] == 'order'][0]
        self.assertEqual(new['input']['allow_protected'], ['tests/run_board_test.sh'])
        out = self.run_order(new['id'], self.done(), edit=self.touch('tests/run_board_test.sh', '#!/bin/sh\necho v2\n'))
        self.assertTrue(out['legal'])

    def test_reviewer_may_only_write_records(self):
        self.init()
        for d in ('D1', 'D2'):
            self.run_order(self.one('implement', d)['id'], self.done(), edit=self.touch('%s.c' % d, d))
        review = self.one('review', 'R12')['id']
        versions = ' '.join(self.one('implement', d)['runs'][-1]['version'] for d in ('D1', 'D2'))
        out = self.run_order(review, self.release_text('固定版本：%s\n' % versions), edit=self.touch('app.c', 'oops'))
        self.assertFalse(out['legal'])
        self.assertEqual(self.one('decide', 'R12')['input']['trigger'], 'review_modified')


class ReviewIntegrity(Base):  # §24.5
    def released_batch1(self):
        self.init()
        for d in ('D1', 'D2'):
            self.run_order(self.one('implement', d)['id'], self.done(), edit=self.touch('%s.c' % d, d))
        versions = ' '.join(self.one('implement', d)['runs'][-1]['version'] for d in ('D1', 'D2'))
        self.run_order(self.one('review', 'R12')['id'], self.release_text('固定版本：%s\n' % versions))

    def test_new_work_after_release_reopens_review(self):
        self.released_batch1()
        self.run_order(self.one('implement', 'D3')['id'], NEEDS_DECISION)
        self.run_order(self.one('decide', 'D3')['id'], '结果：VERIFIED\n停止原因：完成\n', decision={'diagnosis': 'd', 'actions': [
            {'open': 'implement', 'target': 'D1', 'input': {'instruction': '补一个断言'}}]})
        reviews = api.orders(self.st(), type='review', target='R12')
        self.assertEqual([r['state'] for r in reviews], ['done', 'open'])
        release = self.one('release', 'B1')
        self.assertIn(reviews[1]['id'], release['needs'])

    def test_lead_cannot_supersede_reviews_or_depended_orders(self):
        self.init()
        self.run_order(self.one('implement', 'D1')['id'], NEEDS_DECISION)
        decide = self.one('decide', 'D1')['id']
        self.run_order(decide, '结果：VERIFIED\n停止原因：完成\n', decision={'diagnosis': 'd', 'actions': [
            {'supersede': self.one('review', 'R12')['id'], 'reason': '不需要审阅'}]})
        self.assertEqual(self.one('review', 'R12')['state'], 'open')
        self.assertEqual(self.one('ask_user', 'D1')['state'], 'open')

    def test_review_needs_its_own_rerun_or_a_reason(self):
        self.init()
        for d in ('D1', 'D2'):
            self.run_order(self.one('implement', d)['id'], self.done(), edit=self.touch('%s.c' % d, d))
        old = self.one('implement', 'D1')['runs'][-1]['evidence']
        versions = ' '.join(self.one('implement', d)['runs'][-1]['version'] for d in ('D1', 'D2'))
        review = self.one('review', 'R12')['id']
        run = api.start(self.base, review)
        reuse = '结果：RELEASE\n复跑与证据：%s\n固定版本：%s\n' % (list(old)[0], versions)
        self.assertFalse(api.deliver(self.base, review, run, reuse, conversation_id='rv')['legal'])
        reason = reuse + '未复跑原因：板子被其他任务占用，已逐条核对执行者原始记录\n'
        self.assertTrue(api.deliver(self.base, review, run, reason, conversation_id='rv')['legal'])

    def test_non_blocking_suggestions_go_to_triage(self):
        self.init()
        for d in ('D1', 'D2'):
            self.run_order(self.one('implement', d)['id'], self.done(), edit=self.touch('%s.c' % d, d))
        versions = ' '.join(self.one('implement', d)['runs'][-1]['version'] for d in ('D1', 'D2'))
        self.run_order(self.one('review', 'R12')['id'],
                       self.release_text('固定版本：%s\n非阻断建议：\n- 日志级别可以降低\n' % versions))
        items = self.one('triage', 'B1')['input']['items']
        self.assertIn('日志级别可以降低', json.dumps(items, ensure_ascii=False))


class EvidenceRecency(Base):  # §24.6
    def test_verified_needs_a_passing_record_from_this_run(self):
        self.init()
        stale = self.ev()
        time.sleep(3.0)  # beyond the 2 s clock-granularity grace
        oid = self.one('implement', 'D1')['id']
        run = api.start(self.base, oid)
        (self.repo / 'app.c').write_text('int x = 2;\n')
        self.assertFalse(api.deliver(self.base, oid, run, self.done(ev=stale)(), events=[EDIT], conversation_id='c')['legal'])
        self.assertFalse(api.deliver(self.base, oid, run, self.done(ev=self.ev(exit_code=1))(), events=[EDIT], conversation_id='c')['legal'])
        self.assertTrue(api.deliver(self.base, oid, run, self.done()(), events=[EDIT], conversation_id='c')['legal'])


if __name__ == '__main__':
    unittest.main()
