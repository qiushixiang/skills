"""Autonomous recovery must not turn bookkeeping into billable implementation or repeated approvals."""
import copy
import json
from pathlib import Path

from bf import api, cards, gate, permissions, policy, reconcile, register, routing
from test_bf_core import Base, NEEDS_DECISION, EDIT, ENTRY, cmd, make_plan, user_question


class Autonomy(Base):
    def decide(self, actions):
        oid = self.one('decide', 'D1', 'open')['id']
        return self.go(oid, '结果：VERIFIED\n停止原因：完成\n',
                       decision={'diagnosis': '修复调度条件', 'actions': actions})

    def test_coordination_is_independent_of_zero_lead_fallback(self):
        p = make_plan()
        p['policy']['rounds']['lead'] = 0
        self.reinit(policy=p['policy'])
        self.go(self.impl('D1'), NEEDS_DECISION)
        d = self.one('decide', 'D1')
        self.assertEqual(api.assign(self.st(), d['id'])['role'], 'lead')
        self.decide([{'retry': self.impl('D1')}])
        s = self.st()
        self.assertEqual(policy.spent(s, s.orders[d['id']]), {})
        self.assertEqual(policy.spent(s, s.orders[self.impl('D1')]), {'primary': 1})

    def test_readonly_escalation_is_free_but_technical_work_is_charged(self):
        self.go(self.impl('D1'), NEEDS_DECISION, events=[cmd('cat README.md', 0)])
        self.assertTrue(self.st().orders[self.impl('D1')]['runs'][0]['exempt'])
        for events in ([cmd(ENTRY + ' test', 0)], [cmd('python3 opaque.py', 0)], [EDIT], []):
            self.reinit()
            self.go(self.impl('D1'), NEEDS_DECISION, events=events)
            self.assertFalse(self.st().orders[self.impl('D1')]['runs'][0]['exempt'])

    def test_opaque_or_mutating_shell_is_not_classified_as_diagnostic(self):
        for text in ['cat x > result', 'cat x; make', 'python3 test.py', 'git diff --output=x', 'rg --pre ./build x']:
            self.assertFalse(gate.readonly_command(text), text)

    def test_bf_record_preserves_diagnostic_classification_but_not_test_exemption(self):
        import shlex
        cli = Path(api.__file__).with_name('cli.py').resolve()
        prefix = ['python3', str(cli), '--base', str(self.base), 'record', '--']
        diagnostic = shlex.join(prefix + ['cat', 'README.md'])
        self.assertTrue(gate.readonly_command(diagnostic))
        self.assertFalse(gate.readonly_command(shlex.join(prefix + ['python3', 'test.py'])))
        oid = self.impl('D1')
        self.go(oid, NEEDS_DECISION, events=[cmd(diagnostic, 0)])
        self.assertTrue(self.st().orders[oid]['runs'][0]['exempt'])

    def test_bundle_approval_installs_permissions_and_grant_before_original_retry(self):
        oid = self.impl('D1')
        self.go(oid, NEEDS_DECISION)
        actions = [{'authorize': oid, 'paths': ['tests/runner.sh']},
                   {'grant': {'D1': {'implement': 1}}}, {'retry': oid}]
        self.decide(actions)
        ask = self.one('ask_user', 'D1')
        self.assertEqual(ask['input']['pending'], actions)
        self.assertNotIn('allow_protected', self.st().orders[oid]['input'])
        api.answer(self.base, ask['id'], '批准整套修复', True)
        s = self.st()
        self.assertEqual(permissions.allowed(s.plan, s.orders[oid]), ['tests/runner.sh'])
        self.assertIn(oid, api.runnable(s, 10))
        self.assertEqual(s.plan['policy']['overrides']['D1']['implement'], 4)
        self.assertEqual(s.orders[oid]['input']['authorization_sources'][-1]['source'], ask['id'])
        c = cards.render(s, oid, api.assign(s, oid))
        self.assertIn('当前生效', c)
        self.assertIn('tests/runner.sh', c)
        self.assertIn(ask['id'], c)

    def test_whole_bundle_waits_instead_of_partially_opening_work(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        count = len(api.orders(self.st(), type='implement', target='D2'))
        self.decide([{'open': 'implement', 'target': 'D2', 'input': {'instruction': '修复前置'}},
                     {'grant': {'D1': {'implement': 1}}}])
        self.assertEqual(len(api.orders(self.st(), type='implement', target='D2')), count)

    def test_plan_permission_is_effective_without_another_approval(self):
        self.reinit(envelope={'batches': ['B1', 'B2'], 'protected': {'D1': ['tests/runner.sh']}})
        oid = self.impl('D1')
        self.go(oid, NEEDS_DECISION)
        self.decide([{'authorize': oid, 'paths': ['tests/runner.sh']}, {'retry': oid}])
        self.assertEqual(api.orders(self.st(), type='ask_user'), [])
        self.assertIn(oid, api.runnable(self.st(), 10))

    def test_authorize_rejects_globs_and_wrong_or_completed_orders(self):
        s = self.st()
        oid = self.impl('D1')
        for path in ['tests/*', '../runner', '/tmp/x', 'tests/../runner', 'tests//runner']:
            self.assertTrue(gate.validate_decision(s, {'diagnosis': 'd', 'actions': [
                {'authorize': oid, 'paths': [path]}]}))
        for bad in [self.one('review', 'R12')['id'], 'WO-9999']:
            self.assertTrue(gate.validate_decision(s, {'diagnosis': 'd', 'actions': [
                {'authorize': bad, 'paths': ['tests/runner.sh']}]}))
        s.orders[oid]['state'] = 'done'
        self.assertTrue(gate.validate_decision(s, {'diagnosis': 'd', 'actions': [
            {'authorize': oid, 'paths': ['tests/runner.sh']}]}))

    def test_pending_bundle_revalidated_and_transaction_rolls_back_on_stale_target(self):
        oid = self.impl('D1')
        self.go(oid, NEEDS_DECISION)
        self.decide([{'authorize': oid, 'paths': ['tests/runner.sh']}])
        ask = self.one('ask_user', 'D1')['id']
        from bf.store import transact
        transact(self.base, 'test', 'test', lambda s: s.orders[oid].update(state='superseded'))
        before = (self.base / 'orders.json').read_bytes()
        with self.assertRaises(api.BfError):
            api.answer(self.base, ask, '批准', True)
        self.assertEqual((self.base / 'orders.json').read_bytes(), before)

    def test_directional_answer_routes_to_coordination_not_implementation(self):
        oid = self.impl('D1')
        self.go(oid, NEEDS_DECISION)
        self.decide([user_question('请选设备')])
        api.answer(self.base, self.one('ask_user', 'D1')['id'], '选最小修复', True)
        s = self.st()
        self.assertNotIn(oid, api.runnable(s, 10))
        self.assertEqual(self.one('decide', 'D1', 'open')['input']['trigger'], 'user_answer')

    def test_different_triggers_do_not_share_repetition_limit(self):
        s = self.st()
        obj = s.orders[self.impl('D1')]
        for trigger in ['arch_decision_required', 'exhausted', 'authorization_required']:
            d = routing.open_decide(s, obj, trigger)
            self.assertEqual(d['type'], 'decide')
            d['state'] = 'done'

    def test_distinct_blockers_are_separate_but_cannot_evade_total_stall_limit(self):
        s = self.st()
        obj = s.orders[self.impl('D1')]
        for i in range(6):
            d = routing.open_decide(s, obj, 'arch_decision_required', {'blocker': 'issue-%d' % i})
            self.assertEqual(d['type'], 'decide')
            d['state'] = 'done'
        routing.open_decide(s, obj, 'arch_decision_required', {'blocker': 'issue-7'})
        self.assertEqual(obj['ops']['kind'], 'no_progress')
        self.assertFalse(any(o['type'] == 'ask_user' for o in s.orders.values()))

    def test_actual_permission_progress_resets_issue_but_prose_and_new_order_do_not(self):
        s = self.st()
        obj = s.orders[self.impl('D1')]
        for _ in range(2):
            routing.open_decide(s, obj, 'blocked')['state'] = 'done'
        twin = routing.new_order(s, 'implement', 'D1', {'by': 'order', 'ref': obj['id']}, {'instruction': 'new prose'})
        routing.open_decide(s, twin, 'blocked')
        self.assertEqual(twin['ops']['kind'], 'no_progress')
        permissions.install(s, twin, ['tests/runner.sh'], 'approved')
        self.assertEqual(routing.open_decide(s, twin, 'blocked')['type'], 'decide')

    def test_grant_is_progress_but_retry_and_unrelated_prose_are_not(self):
        s = self.st()
        obj = s.orders[self.impl('D1')]
        before = routing.decision_progress(s, obj)
        obj['input']['answers'] = ['new words']
        register._execute(s, obj, {'retry': obj['id']}, obj['id'])
        self.assertEqual(routing.decision_progress(s, obj), before)
        register._execute(s, obj, {'grant': {'D1': {'implement': 1}}}, obj['id'])
        self.assertNotEqual(routing.decision_progress(s, obj), before)

    def test_known_missing_permissions_are_detected_before_any_model_run(self):
        from bf.store import transact
        oid = self.impl('D1')
        transact(self.base, 'test', 'test', lambda s: s.orders[oid]['input'].update(required_protected=['tests/runner.sh']))
        with self.assertRaises(api.BfError):
            api.start(self.base, oid)
        self.assertTrue(api.preflight(self.base))
        self.assertEqual(self.st().orders[oid]['runs'], [])
        self.assertFalse(api.preflight(self.base))
        self.assertEqual(self.one('decide', 'D1')['input']['paths'], ['tests/runner.sh'])

    def test_coordination_cannot_bypass_decision_validation_with_arch_result(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        d = self.one('decide', 'D1')['id']
        rid = api.start(self.base, d)
        out = api.deliver(self.base, d, rid, NEEDS_DECISION, decision=None)
        self.assertFalse(out['legal'])
        self.assertEqual(out['outcome'], 'continue')

    def test_empty_disposition_cannot_release_a_blocked_subject(self):
        self.go(self.impl('D1'), NEEDS_DECISION)
        d = self.one('decide', 'D1')['id']
        out = self.go(d, '结果：VERIFIED\n停止原因：完成', decision={'diagnosis': 'still blocked', 'actions': []})
        self.assertFalse(out['legal'])
        self.assertNotIn(self.impl('D1'), api.runnable(self.st(), 10))


class Reconciliation(Base):
    decide = Autonomy.decide
    def prepare(self):
        oid = self.impl('D1')
        self.go(oid, NEEDS_DECISION)
        self.decide([user_question('旧权限批准')])
        ask = self.one('ask_user', 'D1')['id']
        api.answer(self.base, ask, '批准精确接线', True)
        # Legacy empty escalation reproduced through the state API, never by editing orders.json.
        from bf.store import transact
        def legacy(s):
            for o in s.orders.values():
                if o['type'] == 'decide' and o['state'] == 'open':
                    o['state'] = 'done'
            return routing.open_ask(s, s.orders[oid], oid, '旧驱动器反复裁决',
                                    request=user_question('legacy')['user_request'])['id']
        legacy_ask = transact(self.base, 'test', 'test', legacy)
        spec = {'revision': self.st().revision, 'order': oid, 'paths': ['tests/runner.sh'],
                'prior_approvals': [ask], 'credits': [{'run': oid + '-r1', 'reason': '旧驱动器仅核查权限'}],
                'resolve_asks': [legacy_ask]}
        path = Path(self.tmp.name) / 'repair.json'
        path.write_text(json.dumps(spec))
        return oid, path, legacy_ask

    def test_reconciliation_is_dry_runnable_audited_idempotent_and_preserves_runs(self):
        oid, path, ask = self.prepare()
        before = copy.deepcopy(self.st().orders[oid]['runs'])
        raw = (self.base / 'orders.json').read_bytes()
        self.assertTrue(reconcile.apply(self.base, path, self.approval, dry_run=True)['runnable'])
        self.assertEqual((self.base / 'orders.json').read_bytes(), raw)
        result = reconcile.apply(self.base, path, self.approval)
        self.assertEqual(self.st().orders[oid]['runs'], before)
        self.assertEqual(policy.spent(self.st(), self.st().orders[oid]), {})
        self.assertEqual(self.st().orders[ask]['state'], 'done')
        revision = self.st().revision
        self.assertEqual(reconcile.apply(self.base, path, self.approval), result)
        self.assertEqual(self.st().revision, revision)

    def test_reconciliation_refuses_credit_for_real_test_and_preserves_everything(self):
        oid, path, ask = self.prepare()
        from bf.store import transact
        transact(self.base, 'test', 'test', lambda s: s.orders[oid]['runs'][0].update(entry={'exit_code': 1}))
        spec = json.loads(path.read_text()); spec['revision'] = self.st().revision; path.write_text(json.dumps(spec))
        before = (self.base / 'orders.json').read_bytes()
        with self.assertRaises(api.BfError):
            reconcile.apply(self.base, path, self.approval)
        self.assertEqual((self.base / 'orders.json').read_bytes(), before)

    def test_reconciliation_refuses_stale_manifest(self):
        _, path, _ = self.prepare()
        api.stop(self.base)
        with self.assertRaises(api.BfError):
            reconcile.apply(self.base, path, self.approval)
