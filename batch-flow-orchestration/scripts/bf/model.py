"""Plan validation and normalisation (design 4.1) and plan lookups."""
import copy
import json
from pathlib import Path

from .store import BfError, need, sha256_file, now

DEFAULT_ROUNDS = {'implement': 3, 'backup': 1, 'lead': 1, 'repair': 2, 'review': 3, 'decide_per_source': 2, 'invalid_retry': 2}
CLI_TOOLS = {'agy': 'agy', 'agy-cli': 'agy', 'codex': 'codex', 'codex-cli': 'codex', 'claude': 'claude',
             'claude-cli': 'claude', 'claude-code': 'claude', 'opencode': 'opencode', 'opencode-cli': 'opencode'}
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
    p.setdefault('workspace', None)
    pol = p.setdefault('policy', {})
    rounds = dict(DEFAULT_ROUNDS)
    rounds.update(pol.get('rounds') or {})
    need(all(isinstance(v, int) and v >= 0 for v in rounds.values()), 'policy.rounds must be non-negative integers')
    pol['rounds'] = rounds
    pol.setdefault('overrides', {})
    pol.setdefault('context', {}).setdefault('max_session_kb', 300)
    pol.setdefault('continue_limit', 2)
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
    groups = p.setdefault('review_groups', {})
    for g, spec in groups.items():
        need(g not in dels, 'name %s is both a deliverable and a review group' % g)
        members = spec.get('deliverables') or []
        need(members and all(m in dels for m in members), 'review group %s: unknown deliverable' % g)
        spans = set(dels[m]['batch'] for m in members)
        need(len(spans) == 1 or spec.get('batch') in ids, 'review group %s spans batches: explicit batch required' % g)
        spec.setdefault('batch', next(iter(spans)))
        need(spec['batch'] in ids, 'review group %s: unknown batch' % g)
        spec.setdefault('reviewer', 'reviewer')
        need(spec['reviewer'] in ('reviewer', 'third_party') and spec['reviewer'] in roles,
             'review group %s: reviewer role %s unavailable' % (g, spec['reviewer']))
    for d, spec in dels.items():
        need(all((x in dels or x in groups) and x != d for x in spec['depends_on']), 'deliverable %s: bad depends_on' % d)
    graph = dict((d, list(spec['depends_on'])) for d, spec in dels.items())
    graph.update((g, list(spec['deliverables'])) for g, spec in groups.items())
    for start in graph:  # cycle check
        seen, todo = set(), list(graph[start])
        while todo:
            x = todo.pop()
            need(x != start, 'dependency cycle through %s' % start)
            if x not in seen:
                seen.add(x)
                todo.extend(graph[x])
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


def groups_of(plan, deliverable):
    return [g for g, spec in plan['review_groups'].items() if deliverable in spec['deliverables']]


def batch_of(plan, target):
    """Batch of a deliverable, review group or batch id; None for anything else."""
    if target in plan['deliverables']:
        return plan['deliverables'][target]['batch']
    if target in plan['review_groups']:
        return plan['review_groups'][target]['batch']
    return target if target in [b['id'] for b in plan['batches']] else None


def deliverables_of(plan, target):
    if target in plan['deliverables']:
        return [target]
    if target in plan['review_groups']:
        return list(plan['review_groups'][target]['deliverables'])
    return []


def backend(tool):
    """Backend a role tool maps to; plan.env is keyed by backend."""
    return CLI_TOOLS.get(tool, tool)


def env_for(plan, tool):
    return plan['env'].get(backend(tool), {})
