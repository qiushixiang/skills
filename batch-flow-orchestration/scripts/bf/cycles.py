"""cycles-v1: initial delivery and at most one repair share one author round.

The run journal is the ledger; only initial calls reserve configurable rounds.
Legacy journals stay on their original policy, never silently reinterpreted.
"""
from .store import need

MODEL = 'cycles-v1'
STAGES = ('primary', 'backup', 'lead')
ROLES = {'primary': 'executor', 'backup': 'backup_executor', 'lead': 'lead'}
DEFAULTS = {'primary': 3, 'backup': 1, 'lead': 1, 'decide_per_source': 2,
            'invalid_retry': 2, 'coordination_attempts': 2, 'coordination_no_progress': 6}


def model_for(plan, target=None):
    default = plan.get('policy', {}).get('execution_model', 'legacy-v1')
    if target in plan.get('deliverables', {}):
        return plan['deliverables'][target].get('execution', {}).get('model', default)
    if target in plan.get('review_groups', {}):
        members = plan['review_groups'][target]['deliverables']
        models = {model_for(plan, d) for d in members}
        need(len(models) == 1, 'review group cannot mix execution accounting models')
        return models.pop()
    return default


def enabled(plan, target=None):
    return model_for(plan, target) == MODEL


def config(plan, target):
    values = dict(DEFAULTS)
    if enabled(plan):
        values.update(plan['policy']['rounds'])
        values.update(plan['policy']['overrides'].get(target, {}))
    values.update(plan['deliverables'][target].get('execution', {}).get('rounds', {}))
    return values


def limits(plan, target):
    values = config(plan, target)
    return [(s, values[s]) for s in STAGES if values.get(s, 0) > 0 and ROLES[s] in plan['roles']]


def calls(state, target):
    return [(o, r) for o in state.orders.values() if o['target'] == target and o['type'] in ('implement', 'repair')
            for r in o['runs'] if r.get('execution_cycle') and not r.get('exempt')]


def spent(state, target):
    out = {}
    for _, run in calls(state, target):
        if run['execution_cycle']['phase'] == 'implement':
            s = run['performer']
            out[s] = out.get(s, 0) + 1
    return out


def latest(state, target):
    rows = [(o, r) for o, r in calls(state, target) if r.get('result') == 'SUBMITTED']
    return max(rows, key=lambda row: row[1]['ended_ts'], default=None)


def repair_owner(state, order):
    cycle = order['input'].get('execution_cycle')
    if not cycle:
        return None
    rows = calls(state, order['target'])
    root = next((r for _, r in rows if r['run_id'] == cycle), None)
    used = any(r['execution_cycle']['id'] == cycle and r['execution_cycle']['phase'] == 'repair' for _, r in rows)
    return root['performer'] if root and not used else None


def continuation_source(state, order):
    rid = order['input'].get('continuation_of')
    return next((r for r in order['runs'] if r['run_id'] == rid), None) if rid else None


def continuation_context(state, source):
    if source and source.get('type') == 'ask_user' and (source.get('output') or {}).get('approve') is True:
        original = state.orders.get(source.get('origin', {}).get('ref'))
        if original and original['type'] == 'decide':
            return original
    return source


def validate_continuation(state, source, action):
    source = continuation_context(state, source)
    subject = state.orders.get((source or {}).get('input', {}).get('subject'))
    rid = action.get('continue_run')
    if not subject or not enabled(state.plan, subject['target']) or subject['type'] not in ('implement', 'repair'):
        return 'continue_run 仅用于 cycles-v1 原实施/返修的未交付裁决'
    if not isinstance(action.get('reason'), str) or not action['reason'].strip():
        return 'continue_run 须给出同轮继续的可执行下一步'
    run = next((r for r in subject['runs'] if r['run_id'] == rid), None)
    if (not run or run is not subject['runs'][-1] or not run['ended'] or run['result'] != 'HANDOFF'
            or run.get('delivery_protocol') != 'delivery-v2' or not run.get('execution_cycle')
            or source['input'].get('run') != rid or source['input'].get('accounting_only')):
        return 'continue_run 须绑定本裁决对应的最新未交付运行'
    if (subject['state'] not in ('open', 'failed') or not subject.get('awaiting_disposition')
            or rid in subject.get('continued_runs', []) or subject['input'].get('continuation_of')):
        return 'continue_run 已处置或原工单不能恢复'
    return ''


def stage(state, order):
    allowed = dict(limits(state.plan, order['target']))
    owner = repair_owner(state, order)
    selected = order.get('performer')
    continued = continuation_source(state, order)
    if continued and selected == continued['performer'] and selected in allowed:
        return selected
    if owner and selected == owner and owner in allowed:
        return owner
    used = spent(state, order['target'])
    return next((s for s in STAGES if s == selected
                 and used.get(s, 0) < allowed.get(s, 0)), None)


def select(state, order, after=None):
    allowed = dict(limits(state.plan, order['target']))
    # Only automatic CLI recovery advances strictly beyond the failing backend.
    floor = STAGES.index(after) + 1 if after else 0
    used = spent(state, order['target'])
    return next((s for s in STAGES if STAGES.index(s) >= floor and used.get(s, 0) < allowed.get(s, 0)), None)


def retry_performer(state, order):
    """A technical disposition resumes the included repair or selects an unspent round."""
    return repair_owner(state, order) or select(state, order)


def reserve(state, order, run):
    continued = continuation_source(state, order)
    if continued:
        run['execution_cycle'] = {'id': continued['execution_cycle']['id'], 'phase': 'continue',
                                  'continuation_of': continued['run_id']}
        run['budget_managed'] = False
        order.setdefault('continued_runs', []).append(continued['run_id'])
        order['input'].pop('continuation_of')
        return
    owner = repair_owner(state, order)
    phase = 'repair' if owner == run['performer'] else 'implement'
    run['execution_cycle'] = {'id': order['input']['execution_cycle'] if phase == 'repair' else run['run_id'],
                              'phase': phase}
    run['budget_managed'] = False


def reopen(state, order):
    """First rejection offers the author one repair; rejection after repair advances."""
    previous = latest(state, order['target'])
    order['input'].pop('execution_cycle', None)
    if previous:
        _, run = previous
        order['input']['execution_cycle'] = run['execution_cycle']['id']
        owner = repair_owner(state, order)
        if owner:
            order['performer'] = owner
            return
        order['input'].pop('execution_cycle', None)
    order['performer'] = select(state, order)


def review_limit(plan, group):
    members = set(plan['review_groups'][group]['deliverables'])
    return 1 + sum(2 * sum(n for _, n in limits(plan, d)) - 1 for d in members)


def require_same_model(old, new, orders):
    if enabled(old) != enabled(new):
        need(not orders, 'execution model change requires a fresh task; historical runs cannot be reinterpreted')
    for order in orders.values():
        target = order['target']
        if (order['type'] in ('implement', 'repair') and target in new.get('deliverables', {})) or (
                order['type'] == 'review' and target in new.get('review_groups', {})):
            previous = order.get('execution_model', model_for(old, target))
            need(previous == model_for(new, target),
                 'existing target execution model cannot change: ' + target)



def options(state, order):
    """An unspent author round and an included repair are distinct capabilities."""
    used = spent(state, order['target'])
    return {'model': MODEL, 'owner': order['target'],
            'limits': dict(limits(state.plan, order['target'])), 'used': used,
            'remaining': {s: max(0, n - used.get(s, 0)) for s, n in limits(state.plan, order['target'])},
            'repair_owner': repair_owner(state, order), 'repair_of': order['input'].get('execution_cycle')}


def report(state):
    from . import routing
    out = {}
    for target in state.plan['deliverables']:
        work = routing.work_for(state, target)
        if work and enabled(state.plan, target):
            current = next((o for o in reversed(work) if o['state'] != 'done'), work[-1])
            out[target] = options(state, current)
    return out
