"""Selected context survives dispatch; archival history never becomes a new command."""
import copy
import json
from pathlib import Path
from bf import api, cards, model, runner
from bf.store import BfError, transact
from test_bf_core import Base
from test_bf_runner import FakeAdapter


class SelectedContext(Base):
    def test_owner_selected_reading_and_constraints_survive_backup_and_fresh_dispatch(self):
        oid = self.impl('D1')
        reading = {'path': str(Path(self.base).parent / 'contract.md'),
                   'section': '§2 current API', 'purpose': 'retain synchronous semantics'}
        Path(reading['path']).write_text('current API')
        def update(s):
            s.plan['execution_notes'] = 'S1 remains paused OLD-COMMAND'
            s.plan_dirty = True
            s.plan['deliverables']['D1']['required_read'] = [reading]
            s.plan['deliverables']['D1']['constraints'] = ['preserve authorized boundary']
            s.orders[oid]['fresh_next'] = True
        transact(self.base, 'test', 'test', update)
        fake = FakeAdapter(self.base, {('implement', 'D1'): [{'hang': True}]})
        r = runner.Runner(self.base, fake)
        try:
            r.step()
            call = fake.launches[0]
            self.assertIn('retain synchronous semantics', call['card'])
            self.assertIn('preserve authorized boundary', call['card'])
            self.assertNotIn('OLD-COMMAND', call['card'])
            run = self.st().orders[oid]['runs'][-1]
            archived = json.loads(Path(run['dispatch']['context']['path']).read_text())
            self.assertIn('OLD-COMMAND', archived['execution_notes'])
            self.assertEqual(cards.prepare(self.st(), oid)['required_read'], [reading])
        finally:
            r.close()

    def test_global_repository_observations_do_not_enlarge_implementation_card(self):
        from unittest.mock import patch
        s = self.st(); oid = self.impl('D1')
        with patch.object(cards.snap, 'baseline', return_value={'unrelated': 'x' * 1000000}) as sample:
            text = cards.render(s, oid, api.assign(s, oid))
        sample.assert_not_called()
        self.assertLess(len(text.encode()), 12000)

    def test_special_role_reading_replaces_default_without_inheriting_ancestor(self):
        s = self.st(); oid = self.impl('D1'); obj = s.orders[oid]
        a = Path(self.base).parent / 'a'; a.write_text('base')
        b = Path(self.base).parent / 'b'; b.write_text('specific')
        s.plan['deliverables']['D1']['required_read'] = [dict(path=str(a), section='§1', purpose='base')]
        obj['input']['required_read'] = [dict(path=str(b), section='§2', purpose='specific')]
        data = cards.prepare(s, oid)
        self.assertEqual([x['path'] for x in data['required_read']], [str(b)])
        for bad in [None, ['all history'], [dict(path='a', section='§2')]]:
            obj['input']['required_read'] = bad
            with self.assertRaises(BfError):
                cards.prepare(s, oid)

    def test_hold_is_read_from_current_structured_state(self):
        s = self.st(); oid = self.impl('D1')
        s.plan['holds'] = ['D1']
        self.assertTrue(cards.prepare(s, oid)['control']['held'])
        s.plan['holds'] = []
        s.plan['execution_notes'] = 'D1 held'
        self.assertFalse(cards.prepare(s, oid)['control']['held'])

    def test_compact_body_preserves_commands_and_archival_sha_without_repeating_instruction(self):
        import hashlib
        s = self.st(); oid = self.impl('D1')
        f = Path(self.base).parent / 'contract.md'; f.write_text('actual contract')
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        s.plan['deliverables']['D1']['instruction'] = 'single-current-instruction'
        s.plan['deliverables']['D1']['required_read'] = [dict(path=str(f), section='§2', purpose='preserve ABI', sha256=digest)]
        data = cards.prepare(s, oid)
        text = cards.render(s, oid, api.assign(s, oid), prepared=data)
        cards.validate_export(data, text)
        self.assertEqual(text.count('single-current-instruction'), 1)
        self.assertNotIn('## 本单', text)
        self.assertEqual(data['required_read'][0]['sha256'], digest)
        self.assertNotIn(digest, text)
        self.assertIn('preserve ABI', text)
        self.assertIn('snapshot --repo <仓库绝对路径>', text)
        self.assertIn(str(self.base.resolve()), text)

    def test_automatic_review_and_repair_receive_current_contract_and_target_constraints(self):
        def update(s):
            s.plan['deliverables']['D1']['instruction'] = 'retain-sync-and-shared-types'
            s.plan['deliverables']['D1']['constraints'] = ['do-not-edit-contract-document']
            s.plan_dirty = True
        transact(self.base, 'test', 'test', update)
        self.verify_batch1()
        review = self.one('review', 'R12')
        text = cards.render(self.st(), review['id'], api.assign(self.st(), review['id']))
        self.assertIn('retain-sync-and-shared-types', text)
        self.assertIn('do-not-edit-contract-document', text)
        self.go(review['id'], self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:fix-direct-regression'))
        rep = self.one('repair', 'D1')
        text = cards.render(self.st(), rep['id'], {'tool':'codex'})
        self.assertIn('retain-sync-and-shared-types', text)
        self.assertIn('do-not-edit-contract-document', text)
        self.assertIn('fix-direct-regression', text)
        s = self.st(); impl = s.orders[self.impl('D1')]
        impl['origin']['by'] = 'order'; impl['input']['instruction'] = 'verify-existing-commit-only'
        text = cards.render(s, impl['id'], {'tool':'codex'})
        self.assertIn('verify-existing-commit-only', text)
        self.assertIn('retain-sync-and-shared-types', text)

    def test_decision_keeps_current_subject_work_without_replaying_stale_cached_instruction(self):
        from bf import routing
        s = self.st(); impl = s.orders[self.impl('D1')]
        impl['input']['instruction'] = 'obsolete-pause-S17-through-S20'
        s.plan['deliverables']['D1']['instruction'] = 'current-authorized-S17'
        decision = routing.open_decide(s, impl, 'tool_failure', {'problem':'real-launch-failure'})
        self.assertNotIn('subject_input', decision['input'])
        # Legacy stores still contain this field and must remain readable, never active.
        decision['input']['subject_input'] = dict(impl['input'])
        data = cards.prepare(s, decision['id'])
        text = cards.render(s, decision['id'], {'tool':'codex'}, prepared=data)
        self.assertNotIn('obsolete-pause-S17-through-S20', text)
        self.assertIn('current-authorized-S17', text)
        self.assertIn('real-launch-failure', text)
        self.assertIn('subject_input', decision['input'])

    def test_outer_cli_spawn_failure_is_reclaimed_as_unstarted_transport_failure(self):
        import errno
        from unittest.mock import patch
        from bf.adapter import AgentCliAdapter
        def setup(s):
            s.plan['workspace'] = str(Path(self.base).parent); s.plan_dirty = True
        transact(self.base, 'test', 'test', setup)
        actual = AgentCliAdapter(self.base, {'codex':'unused-profile'})
        r = runner.Runner(self.base, actual)
        try:
            with patch('bf.adapter.subprocess.Popen', side_effect=OSError(errno.EAGAIN, 'cannot spawn')):
                r.step()
            self.assertFalse(api.orders(self.st(), state_='running'))
            run = self.st().orders[self.impl('D1')]['runs'][-1]
            self.assertEqual(run['result'], 'INVALID')
            self.assertTrue(run['exempt'])
            f = Path(self.base) / 'records/runs' / run['run_id'] / 'launch-error-1.json'
            failure = json.loads(f.read_text())
            self.assertEqual(failure['phase'], 'supervisor_spawn')
            self.assertFalse(failure['started'])
            self.assertEqual(failure['errno'], errno.EAGAIN)
        finally:
            r.close()

    def test_failed_continuation_launch_preserves_charge_for_received_work(self):
        from bf.adapter import LaunchNotStarted
        from test_bf_core import UNFINISHED
        from unittest.mock import patch
        fake = FakeAdapter(self.base, {('implement','D1'):[{'text':UNFINISHED}],
                                      ('implement','D2'):[{'hang':True}]})
        r = runner.Runner(self.base, fake, parallel=1)
        try:
            r.step()
            with patch.object(fake, 'launch', side_effect=LaunchNotStarted('supervisor_spawn', OSError('failed'))):
                r.step()
            run = self.st().orders[self.impl('D1')]['runs'][0]
            self.assertEqual(run['segments'], 1)
            self.assertEqual(run['result'], 'INVALID')
            self.assertFalse(run['exempt'])
        finally:
            r.close()

    def test_last_technical_round_continuation_failure_routes_to_exhaustion_decision(self):
        from bf.adapter import LaunchNotStarted
        from test_bf_core import UNFINISHED
        from unittest.mock import patch
        self.reinit(policy={'rounds':{'implement':1, 'backup':0, 'lead':0}})
        fake = FakeAdapter(self.base, {('implement','D1'):[{'text':UNFINISHED}]})
        r = runner.Runner(self.base, fake)
        try:
            r.step()
            with patch.object(fake, 'launch', side_effect=LaunchNotStarted('supervisor_spawn', OSError('failed'))):
                r._collect()
            obj = self.st().orders[self.impl('D1')]
            self.assertEqual(obj['runs'][0]['segments'], 1)
            self.assertFalse(obj['runs'][0]['exempt'])
            decision = self.one('decide', 'D1')
            self.assertEqual(decision['input']['trigger'], 'exhausted')
            self.assertIn(decision['id'], obj['needs'])
        finally:
            r.close()

    def test_automatic_repair_decision_retains_live_closures_and_review_provenance(self):
        from bf import routing
        self.verify_batch1()
        rv = self.one('review', 'R12')
        self.go(rv['id'], self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:retain-current-closure'))
        rep = self.one('repair', 'D1')
        s = self.st(); d = routing.open_decide(s, s.orders[rep['id']], 'exhausted')
        data = cards.prepare(s, d['id'])
        work = data['input']['subject_task']['current_work']
        self.assertEqual(work['findings'], rep['input']['findings'])
        self.assertEqual(work['review_order'], rv['id'])
        self.assertEqual(work['review_run'], rep['input']['review_run'])
        self.assertIn('retain-current-closure', cards.render(s, d['id'], {'tool':'codex'}, prepared=data))
