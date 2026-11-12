#!/usr/bin/env python3
"""Task environment split (design authorization-conveyance-and-env-split.md, A3).

A call = the adapter's fixed part + the plan's backend-neutral needs. Needs are mapped by the capability
table, smoke-tested before registration, pinned into the plan, and only hash-compared at dispatch.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_bf_core import make_plan, ENTRY, NEEDS_DECISION  # noqa: F401  (also sets the suite-wide flag)
from bf import api, adapter, adjust, environment, runner
from bf.store import BfError

HOST = {'id': 'host', 'kind': 'host_parent', 'reason': '入口需 sudo 端点',
        'applies': {'deliverables': '*', 'access': ['read', 'write']}}
ENTRY_REQ = {'id': 'board', 'kind': 'host_entry', 'entries': [ENTRY], 'preflight': ENTRY + ' preflight',
             'smoke': [ENTRY, 'selfcheck'], 'reason': '板测须在沙箱外执行',
             'applies': {'deliverables': '*', 'access': ['write']}}
ESCALATE = {'id': 'escalate', 'kind': 'sandbox_escalation', 'mode': 'auto_review', 'reason': '写入时自动审阅提权',
            'applies': {'deliverables': '*', 'access': ['write']}}


def plan_with(*reqs, **over):
    p = make_plan(**over)
    p['env'] = {'requirements': [copy.deepcopy(r) for r in reqs], 'blocked_output': 'Sandbox detected'}
    return p


def roles(executor='codex', backup='opencode', lead='codex', reviewer='codex'):
    return {'executor': {'tool': executor, 'model': 'exec-m', 'effort': 'medium'},
            'backup_executor': {'tool': backup, 'model': 'backup-m', 'effort': 'default'},
            'lead': {'tool': lead, 'model': 'lead-m', 'effort': 'medium'},
            'reviewer': {'tool': reviewer, 'model': 'rev-m', 'effort': 'medium'}}


class FakeRunners(object):
    """Host commands and model smoke calls without real processes; records what ran."""

    def __init__(self, host_code=0, model_code=0, skip=()):
        self.host, self.model = [], []
        self.host_code, self.model_code, self.skip = host_code, model_code, set(skip)

    def run_host(self, argv):
        self.host.append(list(argv))
        return self.host_code, 'host ok' if self.host_code == 0 else 'ERROR: Sandbox detected'

    def run_model(self, base, plan, spec, token):
        self.model.append(spec)
        events = []
        for argv in environment.model_commands(spec, token):
            if argv[0] in self.skip:
                continue
            events.append({'type': 'command_execution', 'command': "/bin/bash -lc '%s'" % ' '.join(argv),
                           'exit_code': self.model_code if argv[0] == ENTRY else 0,
                           'aggregated_output': 'Operation not permitted' if self.model_code else 'ok'})
        return events, [], str(base)


class EnvironmentBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / '.batch-flow'
        (self.base / 'records').mkdir(parents=True)
        self.approval = self.root / 'approval.txt'
        self.approval.write_text('用户：批准')
        self.dirs = self.root / 'cache'
        self.dirs.mkdir()
        previous = environment.ASSUME_SMOKE_FOR_TESTS
        environment.ASSUME_SMOKE_FOR_TESTS = False
        self.addCleanup(setattr, environment, 'ASSUME_SMOKE_FOR_TESTS', previous)
        rules = patch.object(environment, 'rule_files', return_value=[])
        rules.start()
        self.addCleanup(rules.stop)
        host = patch.object(environment, '_check_host_parent', return_value=(True, 'NoNewPrivs:0'))
        self.host_check = host.start()
        self.addCleanup(host.stop)

    def smoke(self, plan, runners=None):
        runners = runners or FakeRunners()
        from bf import model
        normalized = model.normalize(copy.deepcopy(plan), enforce_review_budget=False)
        return environment.smoke(self.base, normalized, run_host=runners.run_host, run_model=runners.run_model), runners


class Structure(EnvironmentBase):
    def test_env_is_requirements_only(self):
        from bf import model
        for env, words in [({'codex': {'entry': [ENTRY]}}, 'migrate-env'),
                           ({'requirements': [], 'approval_policy': 'on-request'}, 'adapter'),
                           ({}, 'requirements'),
                           ({'requirements': [dict(ENTRY_REQ, smoke=['/bin/true'])]}, 'smoke'),
                           ({'requirements': [{k: v for k, v in ENTRY_REQ.items() if k != 'smoke'}]}, 'smoke'),
                           ({'requirements': [dict(ENTRY_REQ, applies={'deliverables': ['DX'], 'access': ['write']})]}, 'deliverables'),
                           ({'requirements': [dict(ENTRY_REQ, reason='')]}, 'reason'),
                           ({'requirements': [dict(ESCALATE, mode='never')]}, 'mode'),
                           ({'requirements': [HOST, HOST]}, 'duplicated')]:
            p = make_plan()
            p['env'] = env
            with self.assertRaisesRegex(BfError, words):
                model.normalize(p)
        p = make_plan()
        del p['env']
        with self.assertRaisesRegex(BfError, 'plan.env is required'):
            model.normalize(p)

    def test_capability_table_refuses_unverified_backends_where_they_would_work(self):
        from bf import model
        claude_lead = model.normalize(plan_with(ENTRY_REQ, ESCALATE, roles=roles(lead='claude')))
        failures = environment.resolve(claude_lead, self.base)['failures']
        self.assertTrue(any('lead（claude）' in f and 'board' in f for f in failures), failures)
        agy = model.normalize(plan_with(ENTRY_REQ, ESCALATE, roles=roles(backup='agy')))
        self.assertTrue(any('backup_executor（agy）' in f for f in environment.resolve(agy, self.base)['failures']))

    def test_lead_coordination_gets_only_supported_needs(self):
        from bf import model
        p = plan_with(ENTRY_REQ, ESCALATE, roles=roles(lead='claude'))
        p['policy']['rounds']['lead'] = 0          # the lead never implements, only coordinates
        resolution = environment.resolve(model.normalize(p), self.base)
        self.assertEqual(resolution['failures'], [])
        lead = [c for c in resolution['combos'].values() if 'lead' in c['roles']]
        self.assertEqual(lead, [])

    def test_opencode_needs_are_native_and_noted(self):
        from bf import model
        p = model.normalize(plan_with(ENTRY_REQ, ESCALATE, {'id': 'dirs', 'kind': 'extra_dirs', 'dirs': [str(self.dirs)],
                                                            'reason': '缓存', 'applies': {'deliverables': '*', 'access': ['write']}}))
        resolution = environment.resolve(p, self.base)
        self.assertEqual(resolution['failures'], [])
        self.assertTrue(any('OpenCode 无 OS 隔离' in n for n in resolution['notes']))
        opencode = [c for c in resolution['combos'].values() if c['backend'] == 'opencode']
        self.assertEqual([c['launch'] for c in opencode], [{'require_host': False, 'add_dirs': [], 'approval_policy': None}])
        codex = [c for c in resolution['combos'].values() if c['backend'] == 'codex' and c['access'] == 'write']
        self.assertEqual(codex[0]['launch']['add_dirs'], [str(self.dirs)])
        self.assertEqual(codex[0]['launch']['approval_policy'], 'on-request')

    def test_codex_entry_needs_rules_or_escalation(self):
        from bf import model
        p = model.normalize(plan_with(ENTRY_REQ))
        self.assertTrue(any('rules' in f for f in environment.resolve(p, self.base)['failures']))
        with patch.object(environment, 'rule_files', return_value=['/r']), \
                patch.object(environment, '_policy_allows', return_value=True):
            self.assertEqual(environment.resolve(p, self.base)['failures'], [])


class Registration(EnvironmentBase):
    def test_init_refused_until_smoke_passes_then_pinned(self):
        p = plan_with(HOST, ENTRY_REQ, ESCALATE)
        with self.assertRaisesRegex(BfError, '冒烟'):
            api.init(self.base, p, self.approval)
        result, runners = self.smoke(p)
        self.assertTrue(all(v['ok'] for v in result['smoke'].values()))
        self.assertTrue(runners.model, 'codex write combo is proven by a real call')
        self.assertIn([ENTRY, 'selfcheck'], runners.host, 'opencode proves the entry on the host')
        api.init(self.base, p, self.approval)
        pinned = api.load(self.base).plan['env_resolved']
        self.assertTrue(pinned['combos'])
        for combo in pinned['combos'].values():
            self.assertTrue(Path(combo['receipt']).is_file())

    def test_failed_smoke_blocks_registration(self):
        p = plan_with(ENTRY_REQ, ESCALATE)
        result, _ = self.smoke(p, FakeRunners(model_code=1))
        self.assertFalse(all(v['ok'] for v in result['smoke'].values()))
        with self.assertRaisesRegex(BfError, '冒烟未通过'):
            api.init(self.base, p, self.approval)

    def test_command_not_seen_in_cli_events_fails(self):
        p = plan_with(ENTRY_REQ, ESCALATE)
        result, _ = self.smoke(p, FakeRunners(skip=[ENTRY]))
        failed = [c for v in result['smoke'].values() for c in v['failed']]
        self.assertTrue(any(c.get('detail') == 'not executed' for c in failed))

    def test_host_parent_failure_is_reported_before_any_model_call(self):
        self.host_check.return_value = (False, 'NoNewPrivs!=0')
        result, runners = self.smoke(plan_with(HOST, ENTRY_REQ, ESCALATE))
        self.assertFalse(any(v['ok'] for v in result['smoke'].values()))
        self.assertEqual(runners.model, [])

    def test_passing_receipts_are_reused(self):
        p = plan_with(ENTRY_REQ, ESCALATE)
        self.smoke(p)
        result, runners = self.smoke(p)
        self.assertEqual(runners.model, [])
        self.assertTrue(all(v.get('reused') for v in result['smoke'].values()))

    def test_superset_call_proves_its_subsets(self):
        dirs = {'id': 'dirs', 'kind': 'extra_dirs', 'dirs': [str(self.dirs)], 'reason': '缓存',
                'applies': {'deliverables': '*', 'access': ['write']}}
        p = plan_with(dirs, dict(ENTRY_REQ, applies={'deliverables': ['D1'], 'access': ['write']}), ESCALATE)
        result, runners = self.smoke(p)
        self.assertEqual(len([s for s in runners.model if s['access'] == 'write']), 1)
        self.assertTrue(any(v.get('covered_by') for v in result['smoke'].values()))
        api.init(self.base, p, self.approval)

    def test_no_requirements_need_no_smoke(self):
        api.init(self.base, plan_with(), self.approval)
        self.assertEqual(api.load(self.base).plan['env_resolved']['combos'], {})

    def test_adjustment_that_makes_an_unsupported_role_implement_is_refused(self):
        p = plan_with(ENTRY_REQ, ESCALATE, roles=roles(lead='claude'))
        p['policy']['rounds']['lead'] = 0
        self.smoke(p)
        api.init(self.base, p, self.approval)
        with self.assertRaisesRegex(BfError, 'lead（claude）'):
            adjust.grant(self.base, 'D1', self.approval, lead=1)


class ReviewFindings(EnvironmentBase):
    def test_launch_only_combo_is_proven_by_a_real_call(self):
        result, runners = self.smoke(plan_with(ESCALATE))
        self.assertTrue(runners.model, 'approval policy alone must still be exercised')
        self.assertTrue(all(v['ok'] for v in result['smoke'].values()))

    def test_every_dispatchable_call_has_a_pinned_combo(self):
        from bf import model
        r1 = dict(ENTRY_REQ, applies={'deliverables': ['D1'], 'access': ['write']})
        r2 = dict(ESCALATE, applies={'deliverables': ['D1', 'D2'], 'access': ['write']})
        p = plan_with(r1, r2, repos=[str(self.root)])
        self.smoke(p)
        api.init(self.base, p, self.approval)
        st = api.load(self.base)
        plan = st.plan
        for target in list(plan['deliverables']) + list(plan['review_groups']) + [None]:
            for kind in ('implement', 'repair', 'review', 'decide', 'triage'):
                if kind == 'review' and target not in plan['review_groups']:
                    continue
                if kind in ('implement', 'repair') and target not in plan['deliverables']:
                    continue
                role = {'review': 'reviewer', 'decide': 'lead', 'triage': 'lead'}.get(kind, 'executor')
                for r in ([role] if kind not in ('implement', 'repair') else ['executor', 'backup_executor', 'lead']):
                    tool = plan['roles'][r]['tool']
                    access = environment.access_for(plan, kind)
                    launch, drift = environment.call(plan, self.base, tool, access, target, kind)
                    self.assertIsNone(drift, (r, kind, target))
        self.assertEqual(environment.access_for(plan, 'review'), 'write')

    def test_hold_and_supersede_never_wait_for_smoke(self):
        p = plan_with(ENTRY_REQ, ESCALATE)
        self.smoke(p)
        api.init(self.base, p, self.approval)
        with patch.object(environment, 'rules_digest', return_value='changed'):
            adjust.hold(self.base, ['B2'], self.approval)
            adjust.unhold(self.base, ['B2'], self.approval)
            with self.assertRaisesRegex(BfError, '冒烟'):
                adjust.grant(self.base, 'D1', self.approval, backup=1)


class Dispatch(EnvironmentBase):
    def ready(self, *reqs):
        p = plan_with(*reqs, workspace=str(self.root))
        self.smoke(p)
        api.init(self.base, p, self.approval)
        return p

    def test_adapter_uses_pinned_launch(self):
        self.ready(HOST, ENTRY_REQ, ESCALATE, {'id': 'dirs', 'kind': 'extra_dirs', 'dirs': [str(self.dirs)],
                                              'reason': '缓存', 'applies': {'deliverables': '*', 'access': ['write']}})
        st = api.load(self.base)
        oid = api.orders(st, type='implement', target='D1')[0]['id']
        ad = adapter.AgentCliAdapter(self.base, {'codex': '/p/codex.json', 'opencode': '/p/opencode.json'})
        argv = ad.argv(st.orders[oid], {'tool': 'codex', 'model': 'm', 'effort': 'e'}, '/card', '/dir')
        self.assertIn('--require-host', argv)
        self.assertEqual(argv[argv.index('--add-dir') + 1], str(self.dirs))
        self.assertEqual(argv[argv.index('--approval-policy') + 1], 'on-request')
        backup = ad.argv(st.orders[oid], {'tool': 'opencode', 'model': 'p/m', 'effort': 'default'}, '/card', '/dir2')
        self.assertNotIn('--approval-policy', backup)
        self.assertNotIn('--add-dir', backup)

    def test_drift_pauses_only_affected_work_before_any_run(self):
        self.ready(ENTRY_REQ, ESCALATE)
        with patch.object(environment, 'rules_digest', return_value='changed'):
            self.assertTrue(api.preflight(self.base))
        st = api.load(self.base)
        paused = [o for o in st.orders.values() if (o.get('ops') or {}).get('kind') == 'environment_drift']
        self.assertTrue(paused)
        self.assertTrue(all(not o['runs'] for o in paused))
        self.assertIn('env-resolve', paused[0]['ops']['missing'])

    def test_card_states_granted_environment(self):
        from bf import cards
        self.ready(ENTRY_REQ, ESCALATE)
        st = api.load(self.base)
        oid = api.orders(st, type='implement', target='D1')[0]['id']
        text = cards.render(st, oid, {'tool': 'opencode', 'model': 'm', 'effort': 'e'})
        self.assertIn('OpenCode 无 OS 隔离', text)
        self.assertIn(ENTRY, text)

    def test_legacy_plan_cannot_drive(self):
        self.ready()
        st = api.load(self.base)
        st.plan['env'] = {'codex': {'require_host': True}}
        with self.assertRaisesRegex(BfError, 'migrate-env'):
            runner.check_driver_environment(st)


class Migration(unittest.TestCase):
    def test_legacy_env_converts_with_report(self):
        p = make_plan()
        p['env'] = {'codex': {'require_host': True, 'approval_policy': 'on-request', 'entry': [ENTRY],
                              'preflight': ENTRY + ' preflight', 'add_dirs': ['/c'], 'guide': 'g1', 'blocked_output': 'x'},
                    'opencode': {'require_host': True, 'approval_policy': 'on-request', 'entry': [ENTRY],
                                 'add_dirs': ['/c'], 'guide': 'g2', 'blocked_output': 'x'}}
        env, report = environment.migrate(p)
        kinds = {r['kind']: r for r in env['requirements']}
        self.assertEqual(set(kinds), {'host_parent', 'extra_dirs', 'host_entry', 'sandbox_escalation'})
        self.assertIsNone(kinds['host_entry']['smoke'])
        self.assertTrue(any('smoke' in line for line in report))
        self.assertTrue(any('guide' in line and '差异' in line for line in report))
        self.assertFalse(any('approval_policy' in r for r in env['requirements']))
        self.assertEqual(set(env), {'requirements', 'blocked_output', 'guide'})

    def test_cli_migrate_writes_new_file_only(self):
        from bf import cli
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / 'plan.json'
            p = make_plan()
            p['env'] = {'codex': {'entry': [ENTRY]}}
            old.write_text(json.dumps(p))
            new = Path(tmp) / 'plan.new.json'
            self.assertEqual(cli.main(['--base', tmp, 'migrate-env', '--plan', str(old), '--out', str(new)]), 0)
            self.assertEqual(json.loads(new.read_text())['env']['requirements'][0]['kind'], 'host_entry')
            self.assertEqual(cli.main(['--base', tmp, 'migrate-env', '--plan', str(old), '--out', str(new)]), 2)


if __name__ == '__main__':
    unittest.main()
