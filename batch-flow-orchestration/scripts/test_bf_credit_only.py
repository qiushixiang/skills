"""Credit-only bookkeeping must never require or grant source modification permission."""
import copy
import json
from pathlib import Path
import sys

from bf import api, front, reconcile, snap
from bf.store import transact
import test_bf_autonomy as autonomy
from test_bf_core import Base as CoreBase
from test_bf_front import Base as FrontBase
from test_bf_integrity import Base as RepoBase, git


class CreditOnly(CoreBase):
    prepare = autonomy.Reconciliation.prepare
    decide = autonomy.Autonomy.decide

    def spec(self):
        oid, path, ask = self.prepare()
        spec = json.loads(path.read_text()); spec['paths'] = []; spec.pop('resolve_asks', None)
        path.write_text(json.dumps(spec))
        return oid, path, ask

    def test_credit_only_preserves_runs_and_permissions_and_is_idempotent(self):
        oid, path, _ = self.spec()
        before = copy.deepcopy(self.st().orders[oid]['runs'])
        raw = (self.base/'orders.json').read_bytes()
        self.assertEqual(reconcile.apply(self.base, path, self.approval, dry_run=True)['paths'], [])
        self.assertEqual((self.base/'orders.json').read_bytes(), raw)
        result = reconcile.apply(self.base, path, self.approval)
        obj = self.st().orders[oid]
        self.assertEqual(obj['runs'], before)
        self.assertNotIn('allow_protected', obj['input'])
        self.assertNotIn('required_protected', obj['input'])
        self.assertEqual(result['stage'], 'primary')
        self.assertEqual(reconcile.apply(self.base, path, self.approval), result)

    def test_credit_only_can_reference_historical_approval_without_granting_permissions(self):
        oid, path, _ = self.spec()
        def revise_contract(st):
            st.plan['deliverables']['D1']['contract_version'] = 2
            st.plan_dirty = True
            st.orders[oid]['contract_version'] = 2
        transact(self.base, 'test', 'test', revise_contract)
        spec = json.loads(path.read_text()); spec['revision'] = self.st().revision; path.write_text(json.dumps(spec))
        self.assertEqual(reconcile.apply(self.base, path, self.approval, dry_run=True)['stage'], 'primary')
        spec['paths'] = ['tests/runner.sh']; path.write_text(json.dumps(spec))
        with self.assertRaises(api.BfError):
            reconcile.apply(self.base, path, self.approval, dry_run=True)

    def test_empty_noop_manifest_and_real_source_work_cannot_be_credited(self):
        oid, path, _ = self.spec()
        spec = json.loads(path.read_text()); spec['credits'] = []; path.write_text(json.dumps(spec))
        with self.assertRaises(api.BfError):
            reconcile.apply(self.base, path, self.approval)
        spec['credits'] = [{'run': oid+'-r1', 'reason': 'diagnosis'}]
        transact(self.base, 'test', 'test', lambda st: st.orders[oid]['runs'][0].update(changed={'repo':['source.c']}))
        spec['revision'] = self.st().revision; path.write_text(json.dumps(spec))
        with self.assertRaises(api.BfError):
            reconcile.apply(self.base, path, self.approval)

    def test_credit_only_cannot_approve_asks_or_change_instructions(self):
        _, path, ask = self.spec()
        original = json.loads(path.read_text())
        before = (self.base/'orders.json').read_bytes()
        for extra in ({'resolve_asks': [ask]}, {'execution_notes': 'new instructions'}):
            spec = dict(original, **extra); path.write_text(json.dumps(spec))
            with self.assertRaises(api.BfError):
                reconcile.apply(self.base, path, self.approval)
            self.assertEqual((self.base/'orders.json').read_bytes(), before)
        spec = dict(original); del spec['paths']; path.write_text(json.dumps(spec))
        with self.assertRaises(api.BfError):
            reconcile.apply(self.base, path, self.approval)


class BoundHelp(FrontBase):
    def test_help_uses_bound_cli_without_approval_flags_but_not_alias_or_python_code(self):
        cli = Path(front.__file__).with_name('cli.py').resolve()
        base = str(self.base.resolve())
        command = '%s %s --base %s reconcile --help' % (sys.executable, cli, base)
        self.assertTrue(front.allowed(command, bound_base=base))
        self.assertFalse(front.allowed(command.replace(base, '/tmp/other-task'), bound_base=base))
        self.assertFalse(front.allowed(command.replace('reconcile --help', 'init --help'), bound_base=base))
        self.assertFalse(front.shell_ok('flow reconcile --help', bound_base=base))
        self.assertFalse(front.shell_ok("python3 - <<'EOF'\nprint('hello')\nEOF", bound_base=base))
        self.assertFalse(front.allowed(command.replace(' --help', ''), bound_base=base))


class DirectoryFacts(RepoBase):
    def test_embedded_git_repo_is_observed_as_gitlink_and_head_change_is_detected(self):
        nested = self.repo/'kickcat'; nested.mkdir()
        git(nested, 'init', '-q'); git(nested, 'config', 'user.email', 'test@example.invalid')
        git(nested, 'config', 'user.name', 'Test')
        (nested/'code.c').write_text('a'); git(nested, 'add', '.'); git(nested, 'commit', '-qm', 'base')
        git(self.repo, 'add', 'kickcat'); git(self.repo, 'commit', '-qm', 'gitlink')
        (nested/'untracked').write_text('dirty')
        before = snap.baseline({'repos':[str(self.repo)]})
        observed = snap.file_state(nested)
        self.assertEqual(observed['type'], 'gitlink')
        self.assertEqual(observed['git_head'], git(nested, 'rev-parse', 'HEAD').strip())
        self.assertEqual(snap.file_state(self.repo/'tests')['type'], 'directory')
        (nested/'code.c').write_text('b'); git(nested, 'add', 'code.c'); git(nested, 'commit', '-qm', 'new head')
        self.assertIn('kickcat', snap.changed({}, before)[str(self.repo)])
