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
        chain = [('lead', rounds(plan, tg, 'decide', r('lead')) + order.get('extra', 0))]
    else:
        chain = []
    return [(s, n) for s, n in chain if n > 0 and STAGE_ROLE[s] in plan['roles']]


def used(order):
    """Charged runs per performer stage (exempt runs are free)."""
    out = {}
    for run in order['runs']:
        if run['ended'] and not run.get('exempt'):
            out[run['performer']] = out.get(run['performer'], 0) + 1
    return out


def stage(plan, order):
    """Next stage with budget left, or None when the chain is exhausted."""
    spent = used(order)
    for s, n in ladder(plan, order):
        if spent.get(s, 0) < n:
            return s
    return None


def over_budget(plan, order):
    """Stages whose already-used runs exceed what the plan would allow (a revision may not take budget back)."""
    allowed = {}
    for s, n in ladder(plan, order, strict=False):
        allowed[s] = allowed.get(s, 0) + n
    return [s for s, n in used(order).items() if n > allowed.get(s, 0)]


def assign(state, order_id):
    need(order_id in state.orders, 'unknown order %s' % order_id)
    order = state.orders[order_id]
    need(order['type'] in STAFFED, '%s orders are not dispatched to a role' % order['type'])
    need(order['state'] == 'open', 'order %s is %s, not open' % (order_id, order['state']))
    s = stage(state.plan, order)
    need(s is not None, 'order %s: performer chain exhausted' % order_id)
    role = STAGE_ROLE[s]
    cfg = state.plan['roles'][role]
    return {'stage': s, 'role': role, 'tool': cfg['tool'], 'model': cfg['model'], 'effort': cfg['effort'],
            'session': context.session_for(state, order, s)}
