"""One-time v1 ledger import (design 14, 19.2). The v1 file is only read; flow.py's own predicates decide what is done."""
import json
import sys
from pathlib import Path

from . import api, model, routing
from .store import BfError, need, now, sha256_file, transact

SCRIPTS = str(Path(__file__).resolve().parents[1])
DELIVERABLE_KINDS = ('design', 'execute', 'commit', 'verify', 'confirmation')
REVIEW_KINDS = ('review', 'third_party')
TOOL = {'claude-agent': 'claude'}


def _flow():
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    import flow
    return flow


def _role(cfg):
    return {'tool': TOOL.get(cfg['tool'], cfg['tool']), 'model': cfg['model'], 'effort': cfg.get('effort') or 'default'}


def _workspace(store):
    specs = sorted(Path(store).parent.glob('.claude-launch/*/spec.json'), key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(specs[-1].read_text(encoding='utf-8')).get('workspace') if specs else None
    except (OSError, ValueError):
        return None


def _env(cli_execution):
    out = {}
    for backend, cfg in (cli_execution or {}).items():
        cfg = dict(cfg)
        inner = cfg.pop('env', None) or {}
        cfg.update(inner)
        out[backend] = cfg
    return out


def _done(flow, s, tid, memo):
    """v1 success, except that evidence hashes are not required to be intact: a mismatch is reported (I5) instead of
    silently re-opening the task. flow.success decides first; the loose check repeats its structure without hashes."""
    if tid in memo:
        return memo[tid]
    memo[tid] = False  # cycle guard
    t = s['plan']['tasks'][tid]
    a = flow.latest(s, tid)
    good = bool(flow.success(s, tid)) or bool(
        a and a.get('result') == flow.success_result(t) and a.get('subjects') == flow.current_subjects(s, tid)
        and not flow.blockers(s, tid) and all(_done(flow, s, d, memo) for d in t['depends_on']))
    memo[tid] = good
    return good


def build_plan(flow, s, workspace):
    p = s['plan']
    live = dict((tid, t) for tid, t in p['tasks'].items() if not t.get('retired'))
    batches = [bid for bid, b in p['batches'].items() if not b.get('retired')]
    dropped = dict((tid, 'retired') for tid, t in p['tasks'].items() if t.get('retired'))
    dels, groups = {}, {}
    for tid, t in live.items():
        batch = t['scope'][-1] if t['scope'] else None
        if t['kind'] == 'acceptance' or t.get('decision_only') or (t['kind'] == 'confirmation' and t['role'] == 'orchestrator'):
            dropped[tid] = 'acceptance: release is automatic' if t['kind'] == 'acceptance' else 'decision/orchestrator-only task'
        elif t['kind'] in DELIVERABLE_KINDS:
            if batch not in batches:
                dropped[tid] = 'batch %s retired or missing' % batch
                continue
            gc = t.get('goal_contract') or {}
            dels[tid] = {'batch': batch, 'title': tid, 'instruction': t['instruction'], 'acceptance': list(gc.get('acceptance') or []),
                         'depends_on': [], 'design_ref': (s.get('design') or {}).get('sections', {}).get(tid)}
        elif t['kind'] in REVIEW_KINDS:
            groups[tid] = t
        else:
            dropped[tid] = 'kind %s has no v2 equivalent' % t['kind']
    out_groups = {}
    for gid, t in groups.items():
        members = [m for m in flow.review_sources(p, t) if m in dels]
        if not members:
            dropped[gid] = 'review without surviving members'
            continue
        spans = sorted(set(dels[m]['batch'] for m in members))
        batch = spans[0] if len(spans) == 1 else (t['scope'][-1] if t['scope'] and t['scope'][-1] in batches else spans[-1])
        out_groups[gid] = {'deliverables': members, 'batch': batch,
                           'reviewer': 'third_party' if t['kind'] == 'third_party' else 'reviewer'}

    def expand(dep, seen):
        if dep in dels or dep in out_groups:
            return [dep]
        if dep in seen or dep not in live:
            return []
        seen.add(dep)
        return [x for d in live[dep]['depends_on'] for x in expand(d, seen)]
    for tid in dels:
        deps = []
        for d in live[tid]['depends_on']:
            for x in expand(d, set()):
                if x != tid and x not in deps:
                    deps.append(x)
        dels[tid]['depends_on'] = deps
    roles = dict((new, _role(p['roles'][old])) for old, new in
                 (('executor', 'executor'), ('architect', 'lead'), ('reviewer', 'reviewer'), ('third_party', 'third_party'))
                 if old in p['roles'])
    backup = next((t['handoff']['backup'] for t in live.values() if (t.get('handoff') or {}).get('backup')), None)
    if backup:
        roles['backup_executor'] = _role(backup)
    gc = p.get('goal_contract') or {}
    plan = {'name': p.get('name', ''), 'goal': {'objective': gc.get('goal', ''), 'constraints': list(gc.get('acceptance') or []),
                                               'non_goals': list(gc.get('non_goals') or [])},
            'roles': roles, 'batches': [{'id': b, 'title': p['batches'][b].get('name', b)} for b in batches],
            'deliverables': dels, 'review_groups': out_groups,
            'envelope': {'batches': [b for b in s['state'].get('authorized_batches', []) if b in batches]},
            'env': _env(p.get('cli_execution')), 'workspace': workspace}
    if s.get('design'):
        plan['design'] = {'path': s['design'].get('path'), 'sha256': s['design'].get('sha256')}
    return plan, dropped, live


def import_v1(v1_store, base, approval, workspace=None):
    base = Path(str(base))
    approval_ref = model.approval_ref(approval)
    need(not (base / 'plan.json').exists(), 'v2 already initialised at %s' % base)
    store = Path(str(v1_store))
    flow = _flow()
    s = json.loads(store.read_text(encoding='utf-8'))
    plan, dropped, live = build_plan(flow, s, workspace or _workspace(store))
    api.init(base, plan, approval)
    p = s['plan']
    ok = dict((tid, _done(flow, s, tid, {})) for tid in live)
    carried = {}
    for tid in live:
        if not ok[tid] and live[tid]['kind'] != 'acceptance' and flow.attempts(s, tid):
            carried[tid] = len(flow.attempts(s, tid))
    done_ids, open_ids = [], []

    def fn(st):
        for o in routing.sorted_orders(st):
            if o['type'] == 'release':
                tid = p['batches'][o['target']]['completion_task']
                good = ok.get(tid, False)
                src = None
            else:
                tid = o['target']
                good = ok.get(tid, False)
            if o['type'] in ('implement', 'review') and o['key']:
                (done_ids if good else open_ids).append(o['target'])
            if not good:
                continue
            a = flow.latest(s, tid)
            n = len(o['runs']) + 1
            t = live[tid]
            run = api.new_run(o['id'], n, 'imported', 'v1', 'v1', 'default', (a.get('call') or {}).get('conversation_id'), 'fresh', 'imported')
            run.update(ended=now(), result=flow.success_result(t), stop_reason='完成', exempt=True,
                       evidence=dict(a.get('evidence') or {}), card='v1 attempt %s' % a['id'])
            o['runs'].append(run)
            o['state'] = 'done'
        return None
    transact(base, 'import_v1', 'importer', fn)
    st = api.load(base)
    report = {'source': str(store), 'source_revision': s['revision'], 'approval': approval_ref['sha256'],
              'deliverables': sorted(plan['deliverables']), 'review_groups': sorted(plan['review_groups']),
              'done': done_ids, 'open': open_ids, 'dropped': dropped,
              'v1_open_decisions': sorted(d for d, v in s.get('decisions', {}).items() if v.get('status') != 'closed'),
              'v1_attempts_not_carried': carried, 'broken_evidence': api.broken_evidence(st),
              'workspace': plan['workspace']}
    report['counts'] = {k: len(report[k]) for k in ('deliverables', 'review_groups', 'done', 'open', 'dropped',
                                                  'v1_open_decisions', 'v1_attempts_not_carried', 'broken_evidence')}
    (base / 'import-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    return report
