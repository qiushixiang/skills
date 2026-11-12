"""User adjustments (design 21): each is one transaction (one revision, one event named after the function) that
stores the approval (the user's words file) reference in plan.adjustments. Plan content other than these knobs
(deliverables, acceptance) is never touched here; that stays with the technical lead's revise."""
import json
import sys
import time
from pathlib import Path

from . import adapter, model, policy, routing, cycles
from .store import BfError, need, now, transact

ROLES = ('executor', 'backup_executor', 'lead', 'reviewer', 'third_party')
ROUND_KEYS = ('implement', 'backup', 'lead', 'repair', 'review')


def _catalog_module():
    scripts = str(Path(__file__).resolve().parents[1])
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import model_catalog
    return model_catalog


def _edit(base, op, approval, fn, targets=None, reason=None):
    ref = model.approval_ref(approval)

    def run(st):
        import copy
        from . import review_budget
        prior = copy.deepcopy(st.plan)
        result = fn(st)
        review_budget.validate_revision(prior, st.plan)
        from . import environment
        if op in ('set_role', 'grant') and not environment.is_legacy(st.plan.get('env')):
            environment.register(st.base, st.plan)  # only these change who executes what; hold/stop never block
        st.plan['revision'] += 1
        entry = {'revision': st.plan['revision'], 'op': op, 'approval': ref}
        if targets:
            entry['targets'] = list(targets)
        if reason:
            entry['reason'] = reason
        st.plan.setdefault('adjustments', []).append(entry)
        st.plan_dirty = True
        changed = {'unhold': 'hold', 'grant': 'grant'}.get(op)
        if changed and targets:
            routing.condition_changed(st, changed, set(targets), '用户调整 %s：%s（批准 %s）' % (op, '、'.join(targets), ref['path']))
        return result
    return transact(base, op, 'user', run)


def _profile(base, backend, agent_cli=None):
    path = adapter.ensure_profile(base, backend, agent_cli)
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _save_catalog(base, backend, catalog):
    path = Path(str(base)) / 'catalogs' / ('%s-%s-%d.json' % (backend, time.strftime('%Y%m%dT%H%M%S'), int(time.time() * 1000) % 1000))
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding='utf-8')
    return str(path)


def models(base, backend, query=None, agent_cli=None):
    """Read-only: query the backend's model catalogue and keep it under <base>/catalogs/."""
    profile = _profile(base, backend, agent_cli)
    catalog = (query or _catalog_module().query)(profile)
    return {'catalog': _save_catalog(base, backend, catalog), 'models': catalog.get('models', [])}


def set_role(base, role, backend, name, approval, effort='default', query=None, resolve=None, agent_cli=None):
    """Change a role's model through catalogue resolution; only a unique match is written."""
    need(role in ROLES, 'unknown role %s (one of %s)' % (role, ', '.join(ROLES)))
    model.approval_ref(approval)
    profile = _profile(base, backend, agent_cli)
    catalog = (query or _catalog_module().query)(profile)
    path = _save_catalog(base, backend, catalog)
    result = (resolve or _catalog_module().resolve)(profile, catalog, name, effort)
    if result.get('status') != 'resolved':
        return {'status': result.get('status'), 'candidates': [c['id'] if isinstance(c, dict) else c for c in result.get('candidates', [])],
                'catalog': path}

    def fn(st):
        st.plan['roles'][role] = dict(result['config'])
        return None
    _edit(base, 'set_role', approval, fn)
    return {'status': 'resolved', 'config': result['config'], 'catalog': path}


def envelope(base, approval, add=(), remove=()):
    def fn(st):
        ids = [b['id'] for b in st.plan['batches']]
        need(all(b in ids for b in list(add) + list(remove)), 'unknown batch')
        current = (set(st.plan['envelope']['batches']) | set(add)) - set(remove)
        st.plan['envelope']['batches'] = [b for b in ids if b in current]
        return st.plan['envelope']['batches']
    return _edit(base, 'envelope', approval, fn, targets=list(add) + list(remove))


def grant(base, target, approval, **rounds):
    asked = dict((k, v) for k, v in rounds.items() if v is not None)

    def fn(st):
        need(target in st.plan['deliverables'] or target in st.plan['review_groups'], 'unknown target %s' % target)
        return policy.add_rounds(st.plan, target, asked)
    return _edit(base, 'grant', approval, fn, targets=[target])


def _holds(base, targets, approval, op, hold, reason=None):
    def fn(st):
        p = st.plan
        p.setdefault('holds', [])  # plans written before holds existed
        known = set(p['deliverables']) | set(p['review_groups']) | set(b['id'] for b in p['batches'])
        need(targets and all(t in known for t in targets), 'unknown hold target')
        if not hold:
            need(all(t in p['holds'] for t in targets), 'not held')
        p['holds'] = [h for h in p['holds'] if h not in targets] + (list(targets) if hold else [])
        notes = p.setdefault('hold_notes', {})
        for t in targets:
            if hold:
                notes[t] = {'reason': reason or '', 'at': now()}
            else:
                notes.pop(t, None)
        return p['holds']
    return _edit(base, op, approval, fn, targets=targets, reason=reason)


def hold(base, targets, approval, reason=None):
    return _holds(base, list(targets), approval, 'hold', True, reason)


def unhold(base, targets, approval):
    return _holds(base, list(targets), approval, 'unhold', False)


def supersede(base, order_id, approval, reason):
    def fn(st):
        o = routing.get(st, order_id)
        need(o['state'] == 'open', 'order %s is %s, only open orders can be superseded' % (order_id, o['state']))
        o['state'] = 'superseded'
        o['output'] = {'superseded_by': 'user', 'reason': reason, 'at': now()}
        return order_id
    return _edit(base, 'supersede', approval, fn, targets=[order_id], reason=reason)
