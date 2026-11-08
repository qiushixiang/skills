"""Explicit, approved repair of legacy dispatch bookkeeping. Never rewrites a historical run."""
import hashlib
import json
from pathlib import Path

from . import model, permissions, policy, routing
from .store import load, need, sha256_file, transact


def apply(base, manifest, approval, dry_run=False):
    manifest, approval = Path(manifest).resolve(), Path(approval).resolve()
    spec = json.loads(Path(manifest).read_text(encoding='utf-8'))
    approval_ref = model.approval_ref(approval)
    manifest_ref = model.approval_ref(manifest)
    request_id = 'reconcile:' + hashlib.sha256(json.dumps(
        [spec, approval_ref['path'], approval_ref['sha256']], sort_keys=True).encode()).hexdigest()

    def repair(st):
        need(sha256_file(manifest) == manifest_ref['sha256'] and sha256_file(approval) == approval_ref['sha256'],
             'reconciliation inputs changed')
        need(not any(o['state'] == 'running' for o in st.orders.values()), 'collect running work before reconciliation')
        need(spec.get('revision') == st.revision, 'reconciliation revision changed; review a fresh manifest')
        obj = routing.get(st, spec['order'])
        need(obj['type'] in ('implement', 'repair') and obj['state'] in ('open', 'failed') and
             routing.current_work(st, obj), 'reconciliation requires an unfinished current work order')
        paths = spec.get('paths', [])
        need(isinstance(paths, list) and (permissions.validate_paths(paths) if paths else bool(spec.get('credits'))),
             'exact permission paths or an explicit credit-only repair required')
        if not paths:
            need(spec.get('paths') == [] and 'paths' in spec, 'credit-only repair requires explicit paths=[]')
            need(not spec.get('resolve_asks') and 'execution_notes' not in spec,
                 'credit-only repair cannot approve asks or change plan instructions')
        refs = spec.get('prior_approvals', [])
        need(bool(refs), 'cite existing approvals before installing legacy permissions')
        for key in refs:
            ask = routing.get(st, key)
            need(ask['type'] == 'ask_user' and ask['target'] == obj['target'] and ask['state'] == 'done' and
                 ask['output'].get('approve') is True and (not paths or routing.current_work(st, ask)),
                 'invalid prior approval: ' + key)
        if paths:
            permissions.install(st, obj, paths, {'approval': approval_ref, 'prior_approvals': refs})
            obj['input']['required_protected'] = list(dict.fromkeys(obj['input'].get('required_protected', []) + paths))
        credits = obj.setdefault('budget_credits', {})
        for credit in spec.get('credits', []):
            run = next((r for r in obj['runs'] if r['run_id'] == credit['run']), None)
            need(run is not None and run['ended'] and run['result'] in ('ARCH_DECISION_REQUIRED', 'WAIT_EVIDENCE'),
                 'credit must identify a completed blocked run')
            need(not run.get('changed') and not run.get('entry') and not run.get('snapshot'),
                 'cannot credit work with source changes or controlled test execution')
            need(bool(credit.get('reason')) and run['run_id'] not in credits and not run.get('exempt'),
                 'credit requires a reason and cannot be duplicated')
            need(all(sha256_file(p) == h for p, h in run['evidence'].items()), 'credit evidence drifted')
            credits[run['run_id']] = {'reason': credit['reason'], 'approval': approval_ref,
                                     'manifest': manifest_ref, 'prior_charge': run['performer']}
        need(not permissions.missing(st.plan, obj) and policy.stage(st.plan, obj, st) is not None,
             'reconciliation did not restore permissions and execution allowance')
        for key in spec.get('resolve_asks', []):
            ask = routing.get(st, key)
            need(ask['type'] == 'ask_user' and ask['state'] == 'open' and
                 ask['input'].get('subject') == obj['id'] and not ask['input'].get('pending'),
                 'only an empty legacy escalation for this subject can be resolved')
            ask['state'] = 'done'
            ask['output'] = {'resolution': 'legacy dispatch bookkeeping repaired', 'approval': approval_ref,
                             'manifest': manifest_ref, 'approve': True,
                             'answer': Path(approval).read_text(encoding='utf-8')}
        obj['state'] = 'open'
        obj['input'].setdefault('constraints', []).append(
            '本单已通过 reconcile 落实既有批准并冲正协调运行；以当前权限登记为准。保持原验收、独立审阅和范围限制。')
        constraints = spec.get('constraints', [])
        need(isinstance(constraints, list) and all(isinstance(x, str) and x.strip() for x in constraints),
             'dispatch constraints must be nonempty strings')
        for item in constraints:
            if item not in obj['input']['constraints']:
                obj['input']['constraints'].append(item)
        if 'execution_notes' in spec:
            need(isinstance(spec['execution_notes'], str) and bool(spec['execution_notes'].strip()),
                 'execution_notes must contain the approved operational instructions')
            st.plan['execution_notes'] = spec['execution_notes']
            st.plan['revision'] += 1
            st.plan_dirty = True
        if 'fresh_session' in spec:
            need(type(spec['fresh_session']) is bool, 'fresh_session must be boolean')
            obj['fresh_next'] = spec['fresh_session']
        obj.pop('ops', None)
        return {'order': obj['id'], 'paths': paths, 'credits': list(credits),
                'stage': policy.stage(st.plan, obj, st), 'runnable': obj['id'] in routing.runnable(st, 1000),
                'approval': approval_ref, 'manifest': manifest_ref, 'dry_run': dry_run,
                'fresh_session': obj.get('fresh_next', False), 'execution_notes_updated': 'execution_notes' in spec}

    if dry_run:
        return repair(load(base))
    return transact(base, 'reconcile', 'user', repair, request_id=request_id)
