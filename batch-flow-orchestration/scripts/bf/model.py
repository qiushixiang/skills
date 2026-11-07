"""Plan validation and normalisation (design 4.1) and plan lookups."""
import copy
import hashlib
import json
import re
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
        need(str(spec['instruction']).strip(), 'deliverable %s: instruction is empty' % d)
        need(spec['acceptance'] and all(str(x).strip() for x in spec['acceptance']), 'deliverable %s: acceptance is empty' % d)
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
    for g, spec in groups.items():
        need(isinstance(spec.get('focus', ''), str), 'review group %s: focus must be a string' % g)
    for d, spec in dels.items():
        ng = spec.get('non_goals', [])
        need(isinstance(ng, list) and all(isinstance(x, str) for x in ng), 'deliverable %s: non_goals must be a list of strings' % d)
    covered = set(x for g in groups.values() for x in g['deliverables'])
    for d, spec in dels.items():
        need(d in covered or str(spec.get('no_review') or '').strip(),
             'deliverable %s belongs to no review group; add it to one or write no_review: <reason>' % d)
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
    pin_design(p, strict=True)
    chain = rounds['repair'] + rounds['backup'] + rounds['lead']
    p['warnings'] = (['policy.rounds.review (%d) < 1 + 返修链长度 (%d)：审阅额度可能不够走完返修链，请与用户确认或预留 grant' % (rounds['review'], 1 + chain)]
                     if rounds['review'] < 1 + chain else [])
    holds = p.setdefault('holds', [])
    need(all(h in dels or h in groups or h in ids for h in holds), 'holds: unknown deliverable, group or batch')
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


# ---- design pinning (design 24.3, 24.4) ----------------------------------------------------------

def section_hash(text, ref):
    """sha256 of the section whose heading text equals or starts with ref, up to the next heading of the same or
    higher level; None when no heading matches."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r'^(#+)\s+(.*\S)\s*$', line)
        if m and (m.group(2) == ref or m.group(2).startswith(ref)):
            level, end = len(m.group(1)), len(lines)
            for j in range(i + 1, len(lines)):
                n = re.match(r'^(#+)\s', lines[j])
                if n and len(n.group(1)) <= level:
                    end = j
                    break
            return hashlib.sha256('\n'.join(lines[i:end]).encode('utf-8')).hexdigest()
    return None


def pin_design(plan, strict=False):
    """Record the design file hash and the hash of every referenced section (plan.design.sections)."""
    design = plan.get('design')
    if not (design and design.get('path')):
        return
    path = Path(str(design['path']))
    need(path.is_file(), 'design file does not exist: %s' % path)
    text = path.read_text(encoding='utf-8')
    design['sha256'] = hashlib.sha256(text.encode('utf-8')).hexdigest()
    design['sections'] = {}
    for d, spec in plan['deliverables'].items():
        ref = spec.get('design_ref')
        need(ref or not strict, 'deliverable %s: design_ref is required when plan.design is set' % d)
        if ref and ref not in design['sections']:
            sha = section_hash(text, ref)
            need(sha, 'deliverable %s: design_ref %r matches no heading in the design' % (d, ref))
            design['sections'][ref] = sha


def drifted(plan):
    """Deliverables whose referenced design section changed since it was pinned (read once per call)."""
    design = plan.get('design') or {}
    if not design.get('sections'):
        return []
    try:
        text = Path(str(design['path'])).read_text(encoding='utf-8')
    except OSError:
        return [d for d, s in plan['deliverables'].items() if s.get('design_ref')]
    current = dict((ref, section_hash(text, ref)) for ref in design['sections'])
    return [d for d, s in plan['deliverables'].items() if s.get('design_ref') in current
            and current[s['design_ref']] != design['sections'][s['design_ref']]]
