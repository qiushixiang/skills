"""Performer chains, budgets, exemptions (design 7, 8)."""
from . import context
from .store import need

STAGE_ROLE = {'primary': 'executor', 'backup': 'backup_executor', 'lead': 'lead',
              'reviewer': 'reviewer', 'third_party': 'third_party'}
STAFFED = ('implement', 'repair', 'review', 'decide', 'triage')


def rounds(plan, target, key, default=0):
    override = plan['policy']['overrides'].get(target or '', {})
    return override.get(key, plan['policy']['rounds'].get(key, default))


def ladder(plan, order, strict=True):
    """[(stage, rounds)] in escalation order for this order."""
    t, tg = order['type'], order['target']

    def r(key):
        return rounds(plan, tg, key)
    if t in ('implement', 'repair'):
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


def used(order):
    """Charged runs per performer stage (exempt runs are free)."""
    out = {}
    if order['type'] in ('decide', 'triage'):
        return out
    for run in order['runs']:
        if run['ended'] and not run.get('exempt') and run['run_id'] not in order.get('budget_credits', {}):
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
    out = {}
    for o in state.orders.values():
        if budget_key(o) == budget_key(order):
            for s, n in used(o).items():
                out[s] = out.get(s, 0) + n
    return out


def stage(plan, order, state=None):
    """Next stage with budget left, or None when the chain is exhausted."""
    if order['type'] in ('decide', 'triage'):
        consumed = {'lead': sum(bool(r['ended']) and not r.get('exempt')
                               for r in order['runs'][order.get('coordination_floor', 0):])}
    else:
        consumed = spent(state, order) if state is not None else used(order)
    for s, n in ladder(plan, order):
        if consumed.get(s, 0) < n:
            return s
    return None


def over_budget(plan, order, state=None):
    """Stages whose already-used runs exceed what the plan would allow (a revision may not take budget back)."""
    allowed = {}
    for s, n in ladder(plan, order, strict=False):
        allowed[s] = allowed.get(s, 0) + n
    consumed = spent(state, order) if state is not None else used(order)
    return [s for s, n in consumed.items() if n > allowed.get(s, 0)]


def assign(state, order_id):
    need(order_id in state.orders, 'unknown order %s' % order_id)
    order = state.orders[order_id]
    need(order['type'] in STAFFED, '%s orders are not dispatched to a role' % order['type'])
    need(order['state'] == 'open', 'order %s is %s, not open' % (order_id, order['state']))
    s = stage(state.plan, order, state)
    need(s is not None, 'order %s: performer chain exhausted' % order_id)
    role = STAGE_ROLE[s]
    cfg = state.plan['roles'][role]
    return {'stage': s, 'role': role, 'tool': cfg['tool'], 'model': cfg['model'], 'effort': cfg['effort'],
            'session': context.session_for(state, order, s, cfg)}
