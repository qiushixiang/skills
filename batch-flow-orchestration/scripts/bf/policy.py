"""Performer chains, budgets, exemptions (design 7, 8)."""
from . import context, cycles
from .store import need

STAGE_ROLE = {'primary': 'executor', 'backup': 'backup_executor', 'lead': 'lead',
              'reviewer': 'reviewer', 'third_party': 'third_party'}
STAFFED = ('implement', 'repair', 'review', 'decide', 'triage')


def rounds(plan, target, key, default=0):
    if cycles.enabled(plan, target) and key == 'review' and target in plan['review_groups']:
        return cycles.review_limit(plan, target)
    if cycles.enabled(plan, target) and target in plan['deliverables'] and key in cycles.STAGES:
        return cycles.config(plan, target)[key]
    override = plan['policy']['overrides'].get(target or '', {})
    return override.get(key, plan['policy']['rounds'].get(key, default))


def ladder(plan, order, strict=True):
    """[(stage, rounds)] in escalation order for this order."""
    t, tg = order['type'], order['target']

    def r(key):
        return rounds(plan, tg, key)
    if t in ('implement', 'repair'):
        if cycles.enabled(plan, tg):
            return cycles.limits(plan, tg)
        chain = [('primary', r('implement' if t == 'implement' else 'repair')), ('backup', r('backup')), ('lead', r('lead'))]
        if t == 'repair' and strict and order['input'].get('plan_level'):
            chain = [('lead', r('lead'))]  # plan-level finding goes straight to the lead
    elif t == 'review':
        stage = plan['review_groups'].get(tg, {}).get('reviewer', 'reviewer')
        chain = [(stage, r('review'))]
    elif t in ('decide', 'triage'):
        # Coordination has its own bounded retry policy, never the implementation fallback budget.
        chain = [('lead', rounds(plan, tg, 'coordination_attempts', 2) + order.get('extra', 0))]
    else:
        chain = []
    return [(s, n) for s, n in chain if n > 0 and STAGE_ROLE[s] in plan['roles']]


def charge(order, run):
    if run.get('execution_cycle'):
        return int(not run.get('exempt') and run['execution_cycle']['phase'] == 'implement')
    entry = order.get('settlements', {}).get(run['run_id'])
    if entry is not None:
        return entry['charge']
    if run.get('budget_managed'):
        return None
    return int(not run.get('exempt') and run['run_id'] not in order.get('budget_credits', {}))


def unsettled(state, order):
    return [(o, r) for o in state.orders.values() if budget_key(o) == budget_key(order)
            for r in o['runs'] if r.get('budget_managed') and charge(o, r) is None]


def committed(state, order):
    out = spent(state, order)
    for o, r in unsettled(state, order):
        out[r['performer']] = out.get(r['performer'], 0) + 1
    return out


def used(order):
    """Charged runs per performer stage (exempt runs are free)."""
    out = {}
    if order['type'] in ('decide', 'triage'):
        return out
    for run in order['runs']:
        if run['ended'] and charge(order, run) == 1:
            out[run['performer']] = out.get(run['performer'], 0) + 1
    return out


def budget_key(order):
    """Stable owner: opening another order or changing a contract never grants rounds."""
    t = order['type']
    if t == 'repair':
        return t, order['target'], order['input']['review_order']
    if t in ('implement', 'review'):
        return t, order['target']
    return t, order['id']


def spent(state, order):
    if cycles.enabled(state.plan, order['target']) and order['type'] in ('implement', 'repair'):
        return cycles.spent(state, order['target'])
    out = {}
    for o in state.orders.values():
        if budget_key(o) == budget_key(order):
            for s, n in used(o).items():
                out[s] = out.get(s, 0) + n
    return out


def stage(plan, order, state=None):
    """Next stage with budget left, or None when the chain is exhausted."""
    if cycles.enabled(plan, order['target']) and order['type'] in ('implement', 'repair'):
        need(state is not None, 'cycle assignment requires the task journal')
        return cycles.stage(state, order)
    if order['type'] in ('decide', 'triage'):
        consumed = {'lead': sum(bool(r['ended']) and not r.get('exempt')
                               for r in order['runs'][order.get('coordination_floor', 0):])}
    else:
        consumed = committed(state, order) if state is not None else used(order)
    if order.get('control_protocol') == 'intent-v1':
        selected = order.get('performer')
        return next((s for s, n in ladder(plan, order) if s == selected and consumed.get(s, 0) < n), None)
    for s, n in ladder(plan, order):
        if consumed.get(s, 0) < n:
            return s
    return None


def over_budget(plan, order, state=None):
    """Stages whose already-used runs exceed what the plan would allow (a revision may not take budget back)."""
    allowed = {}
    for s, n in ladder(plan, order, strict=False):
        allowed[s] = allowed.get(s, 0) + n
    consumed = committed(state, order) if state is not None else used(order)
    return [s for s, n in consumed.items() if n > allowed.get(s, 0)]


def next_performer(state, order, current):
    """Advance the configured ladder; pending charges reserve their original stage only."""
    if cycles.enabled(state.plan, order['target']) and order['type'] in ('implement', 'repair'):
        return cycles.select(state, order, after=current)
    stages = ('primary', 'backup', 'lead')
    consumed = committed(state, order)
    return next((s for s, n in ladder(state.plan, order)
                 if stages.index(s) > stages.index(current) and consumed.get(s, 0) < n), None)


def assign(state, order_id):
    need(order_id in state.orders, 'unknown order %s' % order_id)
    order = state.orders[order_id]
    need(order['type'] in STAFFED, '%s orders are not dispatched to a role' % order['type'])
    need(order['state'] == 'open', 'order %s is %s, not open' % (order_id, order['state']))
    need(not order.get('awaiting_disposition'), 'order awaits technical lead disposition')
    s = stage(state.plan, order, state)
    need(s is not None, 'order %s: performer chain exhausted' % order_id)
    role = STAGE_ROLE[s]
    cfg = state.plan['roles'][role]
    return {'stage': s, 'role': role, 'tool': cfg['tool'], 'model': cfg['model'], 'effort': cfg['effort'],
            'session': context.session_for(state, order, s, cfg)}


def add_rounds(plan, target, increments):
    """One increment path for CLI and role decisions; scoped policies stay local."""
    is_cycle = cycles.enabled(plan, target)
    allowed = set(cycles.STAGES) if is_cycle else {'implement', 'repair', 'backup', 'lead', 'review'}
    need(increments and all(k in allowed and type(v) is int and v > 0 for k, v in increments.items()),
         'invalid execution round increment')
    need(target not in plan['review_groups'] or (not is_cycle and set(increments) == {'review'}),
         'cycle review allowance is derived from author rounds')
    spec = plan['deliverables'].get(target, {})
    destination = (spec['execution'].setdefault('rounds', {}) if is_cycle and 'execution' in spec else
                   plan['policy']['overrides'].setdefault(target, {}))
    values = {k: rounds(plan, target, k) + v for k, v in increments.items()}
    destination.update(values)
    return destination
