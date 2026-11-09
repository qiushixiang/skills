"""Full-path independent review reservation; configuration and dispatch use the same rules."""
from . import policy
from .store import need


def coverage(plan, groups=None):
    rows = []
    for group, spec in plan['review_groups'].items():
        if groups is not None and group not in groups:
            continue
        sources = []
        for target in dict.fromkeys(spec['deliverables']):
            limits = {key: policy.rounds(plan, target, key) for key in ('implement', 'repair', 'backup', 'lead')}
            if 'backup_executor' not in plan['roles']:
                limits['backup'] = 0
            # One joint initial review; subsequent opportunities add independently.
            # At least one initial delivery is possible even with a fallback-only author.
            total = max(1, limits['implement']) + limits['repair'] + limits['backup'] + limits['lead']
            sources.append({'target': target, 'rounds': limits, 'delivery_rounds': total})
        required = 1 + sum(row['delivery_rounds'] - 1 for row in sources)
        available = policy.rounds(plan, group, 'review')
        rows.append({'group': group, 'sources': sources, 'required': required, 'available': available,
                     'shortfall': max(0, required - available)})
    return rows


def validate(plan, groups=None):
    errors = [row for row in coverage(plan, groups) if row['shortfall']]
    need(not errors, 'review budget coverage: ' + '; '.join(
        '%s requires %d review rounds, configured %d (shortfall %d)' %
        (row['group'], row['required'], row['available'], row['shortfall']) for row in errors))


def validate_revision(old, new, revalidate=()):
    """Do not retroactively grant rounds to old plans, but never enlarge an existing deficit.

    New/revalidated/restructured review paths require full coverage. Unchanged legacy
    deficits remain visible and are guarded before any further author invocation.
    """
    previous = {row['group']: row for row in coverage(old)}
    for row in coverage(new):
        group = row['group']
        before = previous.get(group)
        structural = ('deliverables', 'reviewer', 'batch')
        changed = (before is None or set(revalidate).intersection(new['review_groups'][group]['deliverables'])
                   or any(old['review_groups'][group].get(k) != new['review_groups'][group].get(k) for k in structural))
        allowed = 0 if changed else before['shortfall']
        need(row['shortfall'] <= allowed, 'review budget coverage: %s requires %d review rounds, configured %d (shortfall %d)' %
             (group, row['required'], row['available'], row['shortfall']))


def dispatch_shortfalls(state, order):
    """Reserve reviews before spending implementation/repair rounds, including pending charges."""
    if order['type'] not in ('implement', 'repair'):
        return []
    errors = []
    groups = [g for g, spec in state.plan['review_groups'].items() if order['target'] in spec['deliverables']]
    for row in coverage(state.plan, groups):
        members = set(state.plan['review_groups'][row['group']]['deliverables'])
        reviews = [o for o in state.orders.values() if o['type'] == 'review' and o['target'] == row['group']]
        if not reviews:
            errors.append(dict(row, reason='missing independent review order'))
            continue
        review = reviews[-1]
        spent = sum(policy.committed(state, review).values())
        # Repair pools belong to this review cycle. Old cycles must not create
        # fictitious credits against new repair opportunities after revalidation.
        from . import routing
        extra_initial, repairs, initial = 0, 0, False
        for target in members:
            work = routing.work_for(state, target)
            implementations = [o for o in work if o['type'] == 'implement']
            pending = [o for o in implementations if o['state'] not in routing.SATISFIED]
            if pending:
                initial = True
                remaining = max(0, policy.rounds(state.plan, target, 'implement') -
                                policy.committed(state, pending[-1]).get('primary', 0))
                extra_initial += max(0, remaining - 1)
            owner = {'type': 'repair', 'target': target, 'input': {'review_order': review['id']}, 'runs': []}
            consumed = policy.committed(state, owner)
            repairs += sum(max(0, count - consumed.get(stage, 0))
                           for stage, count in policy.ladder(state.plan, owner, strict=False))
        # A submitted delivery waiting for review also needs its own review slot.
        if not any(o['type'] == 'repair' and o['state'] not in routing.SATISFIED
                   for o in state.orders.values() if o['id'] in review['needs']):
            initial = True
        required = max(1, int(initial) + extra_initial + repairs)
        available = max(0, row['available'] - spent)
        if available < required:
            errors.append(dict(row, required_remaining=required, available_remaining=available,
                               reason='independent review reservation is insufficient'))
    return errors


def require_dispatch(state, order):
    errors = dispatch_shortfalls(state, order)
    need(not errors, 'review budget coverage before dispatch: %s' % errors)
