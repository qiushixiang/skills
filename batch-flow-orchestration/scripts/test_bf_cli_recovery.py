"""Confirmed CLI failures retry once, then advance the authorized execution ladder."""
import copy
from pathlib import Path
from unittest.mock import Mock

from bf import api, cards, policy, runner
from bf.adapter import AgentCliAdapter, LaunchNotStarted
from test_bf_core import Base
from test_bf_runner import FakeAdapter, release


class CliRecovery(Base):
    legacy_protocol = False

    def fail(self, oid, rid, segment, not_started=False):
        return api.cli_failed(self.base, oid, rid, 'arbitrary transport failure', segment,
                              not_started=not_started, source='cli-%d' % segment)

    def test_one_retry_same_round_and_second_failure_switches_without_decision(self):
        self.reinit(policy={'rounds': {'implement': 1, 'backup': 1, 'lead': 1, 'review': 3}})
        oid = self.impl('D1')
        before = copy.deepcopy(self.st().orders[oid])
        rid = api.start(self.base, oid)
        first = self.fail(oid, rid, 1)
        self.assertEqual(first['outcome'], 'continue')
        self.assertEqual(first, self.fail(oid, rid, 1))
        self.assertEqual(self.st().orders[oid]['runs'][0]['segments'], 1)
        self.assertEqual(policy.committed(self.st(), self.st().orders[oid]), {'primary': 1})
        second = self.fail(oid, rid, 2)
        self.assertEqual(second['next_performer'], 'backup')
        self.assertEqual(second, self.fail(oid, rid, 2))
        st = self.st()
        order = st.orders[oid]
        self.assertEqual(len(order['runs']), 1)
        self.assertNotIn('awaiting_disposition', order)
        self.assertEqual(order['needs'], before['needs'])
        self.assertEqual(st.plan['policy']['rounds']['implement'], 1)
        self.assertEqual(api.assign(st, oid)['stage'], 'backup')
        self.assertEqual(api.assign(st, oid)['session']['mode'], 'fresh')
        accounting = api.orders(st, type='decide', target='D1')
        self.assertEqual(len(accounting), 1)
        self.assertTrue(accounting[0]['input']['accounting_only'])
        self.assertIn(oid, api.runnable(st, 10))
        self.assertNotIn(self.one('review', 'R12')['id'], api.runnable(st, 10))
        card = cards.render(st, oid, api.assign(st, oid))
        self.assertIn('do d1', card)
        self.assertIn('先核对原始调用记录', card)
        self.assertIn(rid, card)
        api.start(self.base, oid)  # Pending primary accounting does not block backup.

    def test_all_stages_bounded_even_when_nothing_started(self):
        oid = self.impl('D1')
        for expected in ('primary', 'backup', 'lead'):
            self.assertEqual(api.assign(self.st(), oid)['stage'], expected)
            rid = api.start(self.base, oid)
            self.fail(oid, rid, 1, True)
            out = self.fail(oid, rid, 2, True)
            self.assertEqual(policy.charge(self.st().orders[oid], self.st().orders[oid]['runs'][-1]), 0)
        self.assertIsNone(out['next_performer'])
        self.assertTrue(self.st().orders[oid]['awaiting_disposition'])
        self.assertNotIn(oid, api.runnable(self.st(), 10))
        self.assertEqual(len(self.st().orders[oid]['runs']), 3)
        self.assertEqual(len(api.orders(self.st(), type='decide', target='D1')), 1)

    def test_success_resets_consecutive_failure_count(self):
        oid = self.impl('D1'); rid = api.start(self.base, oid)
        self.fail(oid, rid, 1)
        api.deliver(self.base, oid, rid, '下一步：继续', segment=2)
        self.assertEqual(self.fail(oid, rid, 3)['outcome'], 'continue')
        api.deliver(self.base, oid, rid, '下一步：交付', segment=4)
        order = self.st().orders[oid]
        self.assertEqual(order['state'], 'done')
        self.assertEqual(len(order['runs']), 1)
        self.assertEqual(order['runs'][0]['continuations'], 1)

    def test_unstarted_retry_does_not_erase_earlier_execution(self):
        oid = self.impl('D1'); rid = api.start(self.base, oid)
        api.deliver(self.base, oid, rid, '下一步：继续', segment=1)
        self.fail(oid, rid, 2, True); self.fail(oid, rid, 3, True)
        run = self.st().orders[oid]['runs'][0]
        self.assertFalse(run['exempt'])
        self.assertIsNone(policy.charge(self.st().orders[oid], run))

    def test_no_backup_allowance_skips_to_lead(self):
        self.reinit(policy={'rounds': {'implement': 1, 'backup': 0, 'lead': 1, 'review': 3}})
        oid = self.impl('D1'); rid = api.start(self.base, oid)
        self.fail(oid, rid, 1); self.fail(oid, rid, 2)
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'lead')

    def test_uncertain_call_is_not_automatically_retried(self):
        oid = self.impl('D1'); a = api.assign(self.st(), oid)
        rid = api.start(self.base, oid)
        fake = FakeAdapter(self.base, {})
        r = runner.Runner(self.base, fake)
        r.inflight[rid] = {'handle': 'unknown', 'order': oid, 'assignment': a, 'n': 1}
        fake.pending['unknown'] = {'invalid': True, 'uncertain': True, 'problems': ['no exit receipt']}
        r._collect()
        self.assertEqual(fake.launches, [])
        self.assertTrue(self.st().orders[oid]['awaiting_disposition'])
        self.assertNotIn('cli_failures', self.st().orders[oid]['runs'][0])

    def test_restart_after_failure_receipt_preserves_retry_count(self):
        oid = self.impl('D1'); assignment = api.assign(self.st(), oid)
        rid = api.start(self.base, oid)
        first = runner.Runner(self.base, FakeAdapter(self.base, {}))
        first.inflight[rid] = {'handle': None, 'launching': True, 'order': oid,
                               'assignment': assignment, 'n': 1}
        first._persist(rid)
        self.fail(oid, rid, 1)
        fake = FakeAdapter(self.base, {('implement', 'D1'): [{'invalid': True}]})
        second = runner.Runner(self.base, fake)
        second._recover(self.st())
        self.assertEqual(second.inflight[rid]['n'], 2)
        second._launch_pending(); second._collect()
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'backup')
        self.assertEqual(len(self.st().orders[oid]['runs'][0]['cli_failures']), 2)

    def test_stop_prevents_retry_launch(self):
        oid = self.impl('D1'); a = api.assign(self.st(), oid)
        rid = api.start(self.base, oid)
        fake = FakeAdapter(self.base, {})
        r = runner.Runner(self.base, fake)
        r._cli_failed(oid, rid, a, 1, 'failed')
        api.stop(self.base)
        self.assertFalse(r._launch_pending())
        self.assertEqual(fake.launches, [])

    def test_real_driver_switches_then_independent_review_releases(self):
        self.reinit(envelope={'batches': ['B1']})
        d1, d2, rev = self.impl('D1'), self.impl('D2'), self.one('review', 'R12')['id']
        def account(rid):
            return {'text': '结果：VERIFIED', 'decision': {'diagnosis': '有效技术尝试',
                    'actions': [{'settle': rid, 'charge': 1, 'reason': '已执行工作'}]}}
        fake = FakeAdapter(self.base, {
            ('implement', 'D1'): [{'invalid': True, 'problems': ['error A']},
                                     {'invalid': True, 'problems': ['unrelated error B']}, {'text': '下一步：交付'}],
            ('implement', 'D2'): [{'text': '下一步：交付'}],
            ('decide', 'D1'): [account(d1+'-r1'), account(d1+'-r2')],
            ('decide', 'D2'): [account(d2+'-r1')],
            ('review', 'R12'): [release()], ('decide', 'R12'): [account(rev+'-r1')]})
        runner.Runner(self.base, fake).run_until_idle()
        launches = [x for x in fake.launches if x['type'] == 'implement' and x['target'] == 'D1']
        self.assertEqual([x['assignment']['stage'] for x in launches], ['primary', 'primary', 'backup'])
        self.assertEqual(launches[0]['run_dir'], launches[1]['run_dir'])
        self.assertIsNone(launches[1]['resume'])
        self.assertIn('重试一次', launches[1]['card'])
        self.assertEqual(self.one('release', 'B1')['state'], 'done')
        self.assertEqual(len(self.st().orders[d1]['runs']), 2)

    def test_launch_failure_uses_same_bounded_path(self):
        class FailingLaunch:
            def launch(self, *args, **kwargs):
                raise LaunchNotStarted('spawn', OSError('unavailable'))
        oid = self.impl('D1'); a = api.assign(self.st(), oid)
        rid = api.start(self.base, oid)
        r = runner.Runner(self.base, FailingLaunch())
        r._launch(self.st(), oid, rid, a, 'original task', None, 1)
        self.assertEqual(r.inflight[rid]['n'], 2)
        r._launch_pending()
        self.assertEqual(api.assign(self.st(), oid)['stage'], 'backup')
        self.assertEqual(policy.charge(self.st().orders[oid], self.st().orders[oid]['runs'][0]), 0)

    def test_adapter_waits_for_live_call_and_marks_missing_exit_uncertain(self):
        ad = AgentCliAdapter(self.base, {})
        cli = Path(self.tmp.name) / 'cli'; cli.mkdir()
        handle = {'pid': 123, 'dir': str(cli)}
        proc = Mock(); ad.procs[123] = proc
        proc.poll.return_value = None
        self.assertIsNone(ad.poll(handle))
        proc.poll.return_value = 3
        result = ad.poll(handle)
        self.assertTrue(result['invalid'])
        self.assertTrue(result['uncertain'])


if __name__ == '__main__':
    import unittest
    unittest.main()
