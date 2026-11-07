#!/usr/bin/env python3
"""batch-flow v2 phase C specification tests: plan-model extensions needed by real plans, and the one-time v1 importer.

Design §14, §19. Written before the implementation. Python 3.8, standard library only.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import flow  # noqa: E402  (v1, only to build a fixture ledger)
from bf import api, importer  # noqa: E402
from test_bf_core import make_plan  # noqa: E402


class ModelExtensions(unittest.TestCase):  # §19.1
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / '.batch-flow'
        (self.base / 'records').mkdir(parents=True)
        self.approval = Path(self.tmp.name) / 'ok.txt'
        self.approval.write_text('批准')

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self):
        p = make_plan()
        p['deliverables']['C12'] = {'batch': 'B1', 'title': 'commit', 'instruction': '本地提交 B1 已审阅改动',
                                     'depends_on': ['R12', 'D1', 'D2']}
        p['review_groups']['T13'] = {'deliverables': ['D1', 'D3'], 'batch': 'B2', 'reviewer': 'third_party'}
        p['roles']['third_party'] = {'tool': 'codex', 'model': 'tp-m', 'effort': 'medium'}
        return p

    def test_cross_batch_group_multi_membership_and_review_dependency(self):
        api.init(self.base, self.plan(), self.approval)
        st = api.load(self.base)
        reviews = {o['target']: o for o in api.orders(st, type='review')}
        self.assertEqual(set(reviews), {'R12', 'R3', 'T13'})
        d1 = api.orders(st, type='implement', target='D1')[0]['id']
        self.assertIn(d1, reviews['R12']['needs'])
        self.assertIn(d1, reviews['T13']['needs'])  # D1 is in two groups
        self.assertEqual(api.assign(st, reviews['T13']['id'])['role'], 'third_party')
        release_b2 = api.orders(st, type='release', target='B2')[0]
        self.assertIn(reviews['T13']['id'], release_b2['needs'])  # group batch given explicitly
        commit = api.orders(st, type='implement', target='C12')[0]['id']
        self.assertNotIn(commit, api.runnable(st, parallel=9))

    def test_group_spanning_batches_needs_explicit_batch(self):
        p = self.plan()
        del p['review_groups']['T13']['batch']
        with self.assertRaises(api.BfError):
            api.init(self.base, p, self.approval)

    def test_review_group_dependency_waits_for_review(self):
        api.init(self.base, self.plan(), self.approval)
        for target in ['D1', 'D2']:
            oid = api.orders(api.load(self.base), type='implement', target=target)[0]['id']
            rid = api.start(self.base, oid)
            ev = self.base / 'records' / ('%032x.json' % (len(target + oid) * 10 + ord(target[-1])))
            ev.write_text(target)
            api.deliver(self.base, oid, rid, '结果：VERIFIED\n停止原因：完成\n验证证据：%s\n' % ev, conversation_id='c' + target)
        commit = api.orders(api.load(self.base), type='implement', target='C12')[0]['id']
        self.assertNotIn(commit, api.runnable(api.load(self.base), parallel=9))  # R12 not yet released
        review = api.orders(api.load(self.base), type='review', target='R12')[0]['id']
        rid = api.start(self.base, review)
        ev = self.base / 'records' / ('%032x.json' % 777)
        ev.write_text('r')
        api.deliver(self.base, review, rid, '结果：RELEASE\n复跑与证据：%s\n' % ev, conversation_id='rv')
        self.assertIn(commit, api.runnable(api.load(self.base), parallel=9))


class Importer(unittest.TestCase):  # §14, §19.2
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.v1dir = self.root / 'v1' / '.batch-flow'
        self.v1dir.mkdir(parents=True)
        self.proof = self.v1dir / 'records' / 'proof.json'
        self.proof.parent.mkdir()
        self.proof.write_text('{"exit_code": 0}')
        self.approval = self.root / 'cutover.txt'
        self.approval.write_text('用户：同意切换，重新执行 B2.E')
        self.s = None
        self.n = 0
        self.build()
        self.store = self.v1dir / 'flow.json'
        self.store.write_text(json.dumps(self.s, ensure_ascii=False))
        self.base = self.root / 'v2' / '.batch-flow'

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, actor, kind, data):
        self.n += 1
        req = {'request_id': 'r%d' % self.n, 'expected_revision': self.s['revision'] if self.s else 0,
               'actor': actor, 'operations': [{'type': kind, 'data': data}]}
        self.s, _ = flow.apply_request(self.s, req, self.v1dir)

    def build(self):
        roles = {r: {'tool': 'agy', 'model': 'm-' + r, 'effort': 'medium'} for r in flow.ROLES}
        roles['architect'] = {'tool': 'claude-agent', 'model': 'claude-opus-5-5', 'effort': 'medium'}

        def task(kind, role, scope, deps=(), budgets=(), **extra):
            return dict(kind=kind, role=role, scope=list(scope), subjects=['code'], depends_on=list(deps),
                        consumes=list(budgets), instruction='do ' + kind,
                        goal_contract={'goal': 'goal of ' + kind, 'acceptance': ['accept ' + kind], 'non_goals': []}, **extra)
        handoff = {'backup_budget': 'bk', 'lead_budget': 'ld', 'backup': {'tool': 'opencode', 'model': 'bk-m', 'effort': 'default'}}
        plan = {'name': 'v1demo', 'roles': roles,
                'batches': {'B1': {'name': 'first', 'completion_task': 'B1.release'},
                            'B2': {'name': 'second', 'completion_task': 'B2.release'}},
                'subjects': {'code': 'v1'},
                'budgets': {'E1': {'limit': 3}, 'E2': {'limit': 3}, 'R': {'limit': 9}, 'T': {'limit': 3}, 'C': {'limit': 2},
                            'bk': {'limit': 1}, 'ld': {'limit': 1}, 'O': {'limit': 1}},
                'tasks': {
                    'B1.E': task('execute', 'executor', ['B1'], budgets=['E1'], handoff=handoff),
                    'B1.R': task('review', 'reviewer', ['B1'], ['B1.E'], ['R'], repair_tasks=['B1.E']),
                    'B1.C': task('commit', 'executor', ['B1'], ['B1.R'], ['C']),
                    'B1.release': task('acceptance', 'orchestrator', ['B1'], ['B1.R', 'B1.C']),
                    'B2.E': task('execute', 'executor', ['B2'], ['B1.release'], ['E2']),
                    'B2.R': task('review', 'reviewer', ['B2'], ['B2.E'], ['R'], repair_tasks=['B2.E']),
                    'P.T': task('third_party', 'third_party', ['B1', 'B2'], ['B2.R'], ['T'], repair_tasks=['B2.E']),
                    'B2.release': task('acceptance', 'orchestrator', ['B2'], ['P.T']),
                    'old.E': task('execute', 'executor', ['B2'], budgets=['O'], retired=True),
                },
                'decision_points': {},
                'cli_execution': {'codex': {'require_host': True, 'env': {'entry': ['/opt/run_board_test.sh'], 'preflight': '/opt/run_board_test.sh preflight'}}},
                'goal_contract': {'goal': 'ship it', 'acceptance': ['all green'], 'non_goals': ['no async']}}
        self.apply('architect', 'init', {'plan': plan, 'simulation': True, 'task_mode': 'local',
                                         'context': {'conversation_id': 'planner', 'mode': 'fresh', 'reason': 'fixture'}})
        self.apply('orchestrator', 'authorize', {'batches': ['B1', 'B2'], 'evidence': [str(self.proof)]})
        for tid in ['B1.E', 'B1.R', 'B1.C', 'B1.release']:
            t = self.s['plan']['tasks'][tid]
            self.apply('orchestrator', 'start', {'task': tid, 'call': dict(self.s['plan']['roles'][t['role']], conversation_id='c-' + tid)})
            self.apply(t['role'], 'finish', {'attempt': flow.latest(self.s, tid)['id'], 'result': flow.success_result(t),
                                            'evidence': [str(self.proof)]})
        self.apply('orchestrator', 'start', {'task': 'B2.E', 'call': dict(roles['executor'], conversation_id='c-b2')})
        self.apply('executor', 'finish', {'attempt': flow.latest(self.s, 'B2.E')['id'], 'result': 'FAILED', 'evidence': [str(self.proof)]})

    def test_import_maps_plan_and_progress(self):
        before = hashlib.sha256(self.store.read_bytes()).hexdigest()
        report = importer.import_v1(self.store, self.base, self.approval)
        self.assertEqual(hashlib.sha256(self.store.read_bytes()).hexdigest(), before)  # v1 untouched (I9)
        st = api.load(self.base)
        p = st.plan
        self.assertEqual([b['id'] for b in p['batches']], ['B1', 'B2'])
        self.assertEqual(sorted(p['deliverables']), ['B1.C', 'B1.E', 'B2.E'])  # acceptance dropped, retired skipped
        self.assertEqual(sorted(p['deliverables']['B1.C']['depends_on']), ['B1.R'])
        self.assertEqual(p['deliverables']['B2.E']['acceptance'], ['accept execute'])
        self.assertEqual(p['deliverables']['B2.E']['instruction'], 'do execute')
        self.assertEqual(sorted(p['deliverables']['B2.E']['depends_on']), ['B1.C', 'B1.R'])  # acceptance expanded
        self.assertEqual(p['review_groups']['B1.R']['deliverables'], ['B1.E'])
        self.assertEqual(p['review_groups']['P.T']['deliverables'], ['B2.E'])
        self.assertEqual(p['review_groups']['P.T']['batch'], 'B2')
        self.assertEqual(p['review_groups']['P.T'].get('reviewer'), 'third_party')
        self.assertEqual(p['roles']['lead']['model'], 'claude-opus-5-5')
        self.assertEqual(p['roles']['backup_executor']['model'], 'bk-m')
        self.assertEqual(p['env']['codex']['entry'], ['/opt/run_board_test.sh'])
        self.assertEqual(p['goal']['objective'], 'ship it')
        self.assertEqual(p['envelope']['batches'], ['B1', 'B2'])
        done = lambda type_, target: api.orders(st, type=type_, target=target)[0]['state']
        self.assertEqual((done('implement', 'B1.E'), done('implement', 'B1.C'), done('review', 'B1.R'), done('release', 'B1')),
                         ('done', 'done', 'done', 'done'))
        b1 = api.orders(st, type='implement', target='B1.E')[0]
        self.assertEqual(b1['runs'][0]['performer'], 'imported')
        self.assertEqual(b1['runs'][0]['evidence'], {str(self.proof): hashlib.sha256(self.proof.read_bytes()).hexdigest()})
        b2 = api.orders(st, type='implement', target='B2.E')[0]
        self.assertEqual((b2['state'], b2['runs']), ('open', []))  # re-executed as a fresh order (user decision)
        self.assertEqual(api.runnable(st), [b2['id']])
        self.assertIn('B2.E', report['open'])
        self.assertIn('B2.E', report['v1_attempts_not_carried'])
        self.assertIn('old.E', report['dropped'])
        self.assertEqual(json.loads((self.base / 'import-report.json').read_text())['open'], report['open'])

    def test_import_refuses_existing_target_and_missing_approval(self):
        importer.import_v1(self.store, self.base, self.approval)
        with self.assertRaises(api.BfError):
            importer.import_v1(self.store, self.base, self.approval)
        with self.assertRaises(api.BfError):
            importer.import_v1(self.store, self.root / 'v3' / '.batch-flow', self.root / 'missing.txt')

    def test_broken_v1_evidence_is_reported_and_blocks(self):
        self.proof.write_text('tampered')
        report = importer.import_v1(self.store, self.base, self.approval)
        self.assertIn(str(self.proof), report['broken_evidence'])
        self.assertEqual(api.runnable(api.load(self.base)), [])


if __name__ == '__main__':
    unittest.main()
