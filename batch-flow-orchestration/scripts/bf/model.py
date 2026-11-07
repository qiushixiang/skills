"""Plan validation and normalisation (design 4.1) and plan lookups."""
import copy
import json
from pathlib import Path

from .store import BfError, need, sha256_file, now

DEFAULT_ROUNDS = {'implement': 3, 'backup': 1, 'lead': 1, 'repair': 2, 'review': 3, 'decide_per_source': 2}
ORDER_TYPES = ('implement', 'review', 'repair', 'decide', 'ask_user', 'triage', 'release')


def _tool(role, name):
    need(isinstance(role, dict) and role.get('tool') and role.get('model'), 'roles.%s needs tool and model' % name)
    role.setdefault('effort', 'default')


def normalize(plan):
    """Validate a plan and return a normalised deep copy (defaults filled in)."""
    need(isinstance(plan, dict), 'plan must be an object')
    p = copy.deepcopy(plan)
    p.setdefault('name', '')
    need(isinstance(p.get('goal'), dict), 'plan.goal required')
    roles = p.get('roles') or {}
    for name in ('executor', 'lead', 'reviewer'):
        _tool(roles.get(name), name)
    for name in ('backup_executor', 'third_party'):
        if name in roles:
            _tool(roles[name], name)
    p['roles'] = roles
    pol = p.setdefault('policy', {})
    rounds = dict(DEFAULT_ROUNDS)
    rounds.update(pol.get('rounds') or {})
    need(all(isinstance(v, int) and v >= 0 for v in rounds.values()), 'policy.rounds must be non-negative integers')
    pol['rounds'] = rounds
    pol.setdefault('overrides', {})
    pol.setdefault('context', {}).setdefault('max_session_kb', 300)
    pol.setdefault('continue_limit', 2)
    pol.setdefault('third_party_reviews', [])
    batches = p.get('batches') or []
    ids = [b.get('id') for b in batches]
    need(ids and len(set(ids)) == len(ids), 'plan.batches needs unique ids')
    dels = p.get('deliverables') or {}
    need(dels, 'plan.deliverables required')
    for d, spec in dels.items():
        need(spec.get('batch') in ids, 'deliverable %s: unknown batch' % d)
        spec.setdefault('title', '')
        spec.setdefault('instruction', '')
        spec.setdefault('acceptance', [])
        spec.setdefault('depends_on', [])
        spec.setdefault('resources', [])
        need(all(x in dels and x != d for x in spec['depends_on']), 'deliverable %s: bad depends_on' % d)
    for d in dels:  # cycle check
        seen, todo = set(), list(dels[d]['depends_on'])
        while todo:
            x = todo.pop()
            need(x != d, 'dependency cycle through %s' % d)
            if x not in seen:
                seen.add(x)
                todo.extend(dels[x]['depends_on'])
    groups = p.setdefault('review_groups', {})
    grouped = set()
    for g, spec in groups.items():
        members = spec.get('deliverables') or []
        need(members and all(m in dels for m in members), 'review group %s: unknown deliverable' % g)
        need(len(set(dels[m]['batch'] for m in members)) == 1, 'review group %s spans batches' % g)
        need(not grouped & set(members), 'review group %s: deliverable already grouped' % g)
        grouped |= set(members)
    env = p.setdefault('envelope', {})
    env.setdefault('batches', list(ids))
    need(all(b in ids for b in env['batches']), 'envelope: unknown batch')
    p.setdefault('env', {})
    return p


def approval_ref(path):
    path = Path(str(path))
    need(path.is_file() and path.read_text(encoding='utf-8', errors='replace').strip(),
         'user approval file missing or empty: %s' % path)
    return {'path': str(path), 'sha256': sha256_file(path), 'at': now()}


def load_plan_file(path):
    try:
        return json.loads(Path(str(path)).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise BfError('cannot read plan draft: %s' % path)


def group_of(plan, deliverable):
    for g, spec in plan['review_groups'].items():
        if deliverable in spec['deliverables']:
            return g
    return None


def batch_of(plan, target):
    """Batch of a deliverable, review group or batch id; None for anything else."""
    if target in plan['deliverables']:
        return plan['deliverables'][target]['batch']
    if target in plan['review_groups']:
        return plan['deliverables'][plan['review_groups'][target]['deliverables'][0]]['batch']
    return target if target in [b['id'] for b in plan['batches']] else None


def deliverables_of(plan, target):
    if target in plan['deliverables']:
        return [target]
    if target in plan['review_groups']:
        return list(plan['review_groups'][target]['deliverables'])
    return []
