"""User-approved role/automation boundary. Real Git and driver, fake model adapter only."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from bf import api, cards, gate, model, register, routing, runner
from test_bf_core import Base, ENV_BLOCK, NEEDS_DECISION
from test_bf_integrity import Base as RepoBase, git
from test_bf_runner import FakeAdapter, done


class RoleJudgment(Base):
    def test_expected_failure_and_old_evidence_are_accepted_without_fake_rerun(self):
        evidence = self.evidence()
        data = json.loads(Path(evidence).read_text()); data['exit_code'] = 7
        Path(evidence).write_text(json.dumps(data))
        result = self.go(self.impl('D1'), '结果：VERIFIED\n停止原因：完成\n验证证据：' + evidence)
        self.assertTrue(result['legal'])
        self.assertEqual(json.loads(Path(evidence).read_text())['exit_code'], 7)

    def test_blocker_is_reported_to_lead_without_log_regex_diagnosis(self):
        result = self.go(self.impl('D1'), ENV_BLOCK)
        self.assertTrue(result['legal'])
        self.assertEqual(self.one('decide', 'D1')['input']['trigger'], 'wait_evidence')

    def test_textual_revision_preserves_completed_work_until_lead_requests_redo(self):
        for d in ('D1', 'D2'):
            self.go(self.impl(d), self.done())
        self.go(self.one('review', 'R12')['id'], self.release_text())
        before = copy.deepcopy(self.st().orders)
        plan = copy.deepcopy(self.st().plan)
        plan['deliverables']['D1']['instruction'] = '澄清文档，不要求重做'
        plan['deliverables']['D1']['acceptance'] = ['负责人已核对等价表述']
        api.revise_plan(self.base, plan, self.approval)
        for oid, o in before.items():
            self.assertEqual(self.st().orders[oid]['runs'], o['runs'])
        self.assertEqual(len(api.orders(self.st(), type='implement', target='D1')), 1)
        self.assertEqual(self.one('review', 'R12')['state'], 'done')
        api.revise_plan(self.base, plan, self.approval, revalidate=['D1'])
        self.assertEqual(len(api.orders(self.st(), type='implement', target='D1')), 2)
        self.assertEqual(self.one('implement', 'D2')['state'], 'done')
        self.assertEqual(self.one('implement', 'D3')['state'], 'open')
        self.assertEqual(len(api.orders(self.st(), type='implement', target='D3')), 1)
        self.assertTrue(api.orders(self.st(), type='review', target='R12', state_='open'))

    def test_coordination_limit_counts_calls_without_claiming_no_progress(self):
        s = self.st(); obj = s.orders[self.impl('D1')]
        for _ in range(2):
            routing.open_decide(s, obj, 'diagnose')['state'] = 'done'
        obj['input']['answers'] = ['网络已经恢复，有新的诊断证据']
        routing.open_decide(s, obj, 'diagnose')
        self.assertEqual(obj['ops']['kind'], 'coordination_limit')
        self.assertNotIn('无进展', obj['ops']['problem'])

    def test_invalid_dispatch_is_rejected_before_model_or_budget(self):
        from bf.store import transact
        oid = self.impl('D1')
        transact(self.base, 'test', 'test', lambda s: s.orders[oid]['input'].update(constraints='lost type'))
        fake = FakeAdapter(self.base, {('implement', 'D2'): [{'hang': True}]})
        r = runner.Runner(self.base, fake)
        try:
            r.step()
            self.assertEqual(self.st().orders[oid]['runs'], [])
            self.assertEqual(self.st().orders[oid]['ops']['kind'], 'tool_failure')
            self.assertFalse(any(x['order'] == oid for x in fake.launches))
        finally:
            r.close()

    def test_renderer_cannot_drop_resolved_input_at_real_dispatch_boundary(self):
        fake = FakeAdapter(self.base, {('implement', 'D2'): [{'hang': True}]})
        r = runner.Runner(self.base, fake)
        with patch.object(cards, 'render', return_value='missing order input'):
            try:
                r.step()
                self.assertEqual(fake.launches, [])
                self.assertEqual(self.st().orders[self.impl('D1')]['runs'], [])
            finally:
                r.close()

    def test_actual_adapter_receives_extra_fields_and_current_user_constraints(self):
        from bf.store import transact
        oid = self.impl('D1')
        extra = {'instruction': '专项执行完整指令', 'constraints': ['必须保留拒绝决定'],
                 'handoff_artifact': {'path': 'specific-artifact', 'purpose': 'reason-to-read'}}
        def update(s):
            s.orders[oid]['origin']['by'] = 'order'
            s.orders[oid]['input'].update(extra)
        transact(self.base, 'test', 'test', update)
        fake = FakeAdapter(self.base, {('implement', 'D1'): [{'hang': True}]})
        r = runner.Runner(self.base, fake)
        try:
            r.step()
            card = fake.launches[0]['card']
            data = json.loads((Path(fake.launches[0]['run_dir']) / 'dispatch.json').read_text())
            cards.validate_export(data, card)
            for k, v in extra.items():
                self.assertEqual(data['input'][k], v)
            self.assertIn('专项执行完整指令', card)
        finally:
            r.close()

    def test_special_review_and_repair_instructions_use_same_export(self):
        s = self.st(); review = s.orders[self.one('review', 'R12')['id']]
        review['input'].update(instruction='只核对三个提交，不构建不板测', custom='附加工作信息')
        text = cards.render(s, review['id'], {'tool': 'claude'})
        self.assertIn(review['input']['instruction'], text)
        self.assertNotIn('首审：', text)
        self.assertNotIn('来源结论：', text)
        # The same common export is used for repair, not a review-only patch.
        rep = routing.new_order(s, 'repair', 'D1', {'by': 'order', 'ref': review['id']},
             {'instruction': '只修复闭环，不调整其他项', 'review_order': review['id'], 'review_run': 'rv1',
              'findings': [{'id': 'B1', 'closure': '负例返回失败', 'plan_level': False}]})
        text = cards.render(s, rep['id'], {'tool': 'codex'})
        self.assertIn(rep['input']['instruction'], text)
        self.assertIn('负例返回失败', text)

    def test_continuation_references_persisted_original_and_keeps_session(self):
        fake = FakeAdapter(self.base, {('implement', 'D1'): [
            {'text': '结果：FAILED\n停止原因：未完成'}, {'hang': True}]})
        r = runner.Runner(self.base, fake)
        try:
            r.step(); r.step()
            self.assertEqual(len(fake.launches), 2)
            self.assertEqual(fake.launches[1]['resume'], 'conv-1')
            original = Path(fake.launches[0]['run_dir']) / 'card-1.md'
            self.assertIn(str(original), fake.launches[1]['card'])
            self.assertTrue(original.is_file())
            self.assertEqual(len(self.st().orders[self.impl('D1')]['runs']), 1)
        finally:
            r.close()


    def test_all_staffed_roles_reach_adapter_with_complete_input(self):
        from bf.store import transact
        for kind in ('implement', 'repair', 'review', 'decide', 'triage'):
            with self.subTest(kind=kind):
                self.reinit(envelope={'batches': ['B1']})
                if kind in ('review', 'repair', 'triage'):
                    self.verify_batch1()
                    rv = self.one('review', 'R12')['id']
                if kind == 'repair':
                    self.go(rv, self.fix('阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:修复实际问题'))
                    oid = self.one('repair', 'D1')['id']
                elif kind == 'triage':
                    self.go(rv, self.release_text() + '非阻断建议：\n- 后续核查\n')
                    oid = self.one('triage', 'B1')['id']
                elif kind == 'review':
                    oid = rv
                elif kind == 'decide':
                    self.go(self.impl('D1'), NEEDS_DECISION)
                    oid = self.one('decide', 'D1')['id']
                else:
                    oid = self.impl('D1')
                def update(st):
                    st.orders[oid]['input'].update(instruction='专属范围-' + kind, retained_field={'meaning': '不得丢失'})
                    if kind == 'implement':
                        st.orders[oid]['origin']['by'] = 'order'
                transact(self.base, 'test', 'test', update)
                target = self.st().orders[oid]['target']
                fake = FakeAdapter(self.base, {(kind, target): [{'hang': True}]})
                r = runner.Runner(self.base, fake)
                try:
                    r.step()
                    self.assertEqual(fake.launches[0]['order'], oid)
                    self.assertIn('专属范围-' + kind, fake.launches[0]['card'])
                    self.assertIn('不得丢失', fake.launches[0]['card'])
                    run = self.st().orders[oid]['runs'][-1]
                    for ref in run['dispatch'].values():
                        self.assertEqual(api.gate_hash(ref['path']), ref['sha256'])
                finally:
                    r.close()

    def test_parallel_dispatch_resolves_each_order_against_its_own_revision(self):
        fake = FakeAdapter(self.base, {('implement', 'D1'): [{'hang': True}],
                                       ('implement', 'D2'): [{'hang': True}]})
        r = runner.Runner(self.base, fake, parallel=2)
        try:
            r.step()
            self.assertEqual(len(fake.launches), 2)
            self.assertEqual({x['target'] for x in fake.launches}, {'D1', 'D2'})
            for launch in fake.launches:
                data = json.loads((Path(launch['run_dir']) / 'dispatch.json').read_text())
                self.assertEqual(data['order'], launch['order'])
        finally:
            r.close()

    def test_focus_wording_does_not_destroy_review_history(self):
        self.verify_batch1()
        rv = self.one('review', 'R12')['id']
        self.go(rv, self.release_text())
        original = copy.deepcopy(self.st().orders[rv]['runs'])
        plan = copy.deepcopy(self.st().plan)
        plan['review_groups']['R12']['focus'] = '澄清原审阅重点'
        api.revise_plan(self.base, plan, self.approval)
        self.assertEqual(self.st().orders[rv]['runs'], original)
        self.assertEqual(len(api.orders(self.st(), type='review', target='R12')), 1)
        self.assertEqual(self.st().orders[rv]['state'], 'done')


class VersionFacts(RepoBase):
    def test_committing_reviewed_protected_bytes_is_not_a_modification_verdict(self):
        self.init()
        f = self.repo / 'tests/run_board_test.sh'; f.write_text('approved bytes\n')
        before = f.read_bytes()
        def commit():
            git(self.repo, 'add', 'tests/run_board_test.sh'); git(self.repo, 'commit', '-qm', 'approved')
        out = self.run_order(self.one('implement', 'D1')['id'], self.done(), edit=commit)
        self.assertTrue(out['legal'])
        self.assertEqual(f.read_bytes(), before)
        self.assertFalse(api.orders(self.st(), type='decide'))
        run = self.one('implement', 'D1')['runs'][-1]
        self.assertIn('tests/run_board_test.sh', run['changed'][str(self.repo)])
        self.assertEqual(run['evidence'][run['snapshot'][0]], api.gate_hash(run['snapshot'][0]))

    def test_abort_captures_changes_and_next_review_sees_all_versions_and_sha(self):
        self.init(); oid = self.one('implement', 'D1')['id']
        run = api.start(self.base, oid)
        (self.repo / 'app.c').write_text('interrupted\n')
        api.abandon(self.base, oid, run, 'CLI disappeared')
        interrupted = self.one('implement', 'D1')['runs'][0]
        self.assertEqual(interrupted['changed'][str(self.repo)], ['app.c'])
        self.run_order(oid, self.done(), edit=self.touch('other.c', 'second\n'))
        for d in ('D2',):
            self.run_order(self.one('implement', d)['id'], self.done())
        s = self.st(); rv = self.one('review', 'R12')['id']
        text = cards.render(s, rv, api.assign(s, rv))
        for r in self.one('implement', 'D1')['runs']:
            for path in r['snapshot']:
                self.assertIn(path, text)
                self.assertIn(r['evidence'][path], text)
        Path(interrupted['snapshot'][0]).write_text('{}')
        text = cards.render(self.st(), rv, api.assign(self.st(), rv))
        self.assertIn(interrupted['snapshot'][0], text)
        self.assertIn(rv, api.runnable(self.st(), 10))
        self.assertTrue(self.run_order(rv, self.release_text())['legal'])
        # Historical evidence damage does not veto a registered role conclusion at release.
        api.release(self.base, self.one('release', 'B1')['id'])

    def test_mode_and_symlink_are_captured_as_facts(self):
        self.init(); f = self.repo / 'app.c'; f.write_text('dirty before\n')
        def edit():
            f.chmod(0o755); (self.repo / 'external-link').symlink_to('/nonexistent/target')
        self.run_order(self.one('implement', 'D1')['id'], self.done(), edit=edit)
        run = self.one('implement', 'D1')['runs'][-1]
        data = json.loads(Path(run['snapshot'][0]).read_text())
        self.assertEqual(data['files']['app.c']['mode'], '100755')
        self.assertEqual(data['files']['external-link']['target'], '/nonexistent/target')

    def test_design_text_drift_is_presented_without_pausing(self):
        self.init(); self.design.write_text(self.design.read_text().replace('方案 A。', '方案 A，标点调整。'))
        oid = self.one('implement', 'D1')['id']
        self.assertIn(oid, api.runnable(self.st(), 10))
        self.assertIn('D1', cards.prepare(self.st(), oid)['design_changes'])

    def test_capture_failure_is_visible_and_does_not_replace_role_result(self):
        self.init(); oid = self.one('implement', 'D1')['id']
        with patch('bf.api.snap.changed', side_effect=api.BfError('repository unavailable')):
            out = self.run_order(oid, self.done())
        self.assertTrue(out['legal'])
        self.assertIn('repository unavailable', self.one('implement', 'D1')['runs'][-1]['observations'][0])
