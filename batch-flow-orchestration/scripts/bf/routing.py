"""Order creation, selection (design 5.4) and event -> order openers (design 5.2)."""
from . import model, policy
from .store import need, sha256_file

PRIORITY = ['decide', 'repair', 'review', 'implement', 'triage', 'release']
SATISFIED = ('done', 'superseded')


def get(state, order_id):
    need(order_id in state.orders, 'unknown order %s' % order_id)
    return state.orders[order_id]


def seqno(order):
    return int(order['id'][3:])


def sorted_orders(state):
    return sorted(state.orders.values(), key=seqno)


def by_key(state, key):
    for o in state.orders.values():
        if o.get('key') == key:
            return o
    return None


def new_order(state, type_, target, origin, input_=None, needs=None, key=None):
    state.seq += 1
    o = {'id': 'WO-%04d' % state.seq, 'key': key, 'type': type_, 'target': target, 'origin': origin,
         'input': input_ or {}, 'needs': list(needs or []), 'runs': [], 'state': 'open', 'output': None}
    state.orders[o['id']] = o
    state.opened.append(o['id'])
    return o


# ---- selection ------------------------------------------------------------------------------------

def broken_paths(state):
    out = []
    for o in sorted_orders(state):
        for r in o['runs']:
            for path, digest in r['evidence'].items():
                if sha256_file(path) != digest and path not in out:
                    out.append(path)
    return out


def _broken_orders(state):
    return set(o['id'] for o in state.orders.values()
               if any(sha256_file(p) != h for r in o['runs'] for p, h in r['evidence'].items()))


def _resources(plan, order):
    return set(x for d in model.deliverables_of(plan, order['target']) for x in plan['deliverables'][d]['resources'])


def _position(plan, order):
    dels, tg = list(plan['deliverables']), order['target']
    members = model.deliverables_of(plan, tg)
    batch = model.batch_of(plan, tg)
    ids = [b['id'] for b in plan['batches']]
    return (ids.index(batch) if batch else len(ids), dels.index(members[0]) if members else len(dels))


def runnable(state, parallel=1):
    if state.stopped:
        return []
    plan = state.plan
    bad = _broken_orders(state)
    running = [o for o in state.orders.values() if o['state'] == 'running']
    busy_targets = set(o['target'] for o in running if o['target'])
    busy_res = set()
    for o in running:
        busy_res |= _resources(plan, o)
    deciding = set(o['target'] for o in state.orders.values() if o['type'] == 'decide' and o['state'] in ('open', 'running'))

    def dep_ok(d):
        impls = [o for o in state.orders.values() if o['type'] == 'implement' and o['target'] == d]
        return bool(impls) and all(i['state'] in SATISFIED and i['id'] not in bad for i in impls)
    cands = []
    for o in state.orders.values():
        if o['state'] != 'open' or o['type'] == 'ask_user':
            continue
        if not all(state.orders[n]['state'] in SATISFIED and n not in bad for n in o['needs'] if n in state.orders):
            continue
        batch = model.batch_of(plan, o['target'])
        if batch and batch not in plan['envelope']['batches']:
            continue
        if o['target'] in plan['deliverables'] and not all(dep_ok(d) for d in plan['deliverables'][o['target']]['depends_on']):
            continue
        if o['type'] != 'decide' and o['target'] in deciding:
            continue
        if o['type'] in policy.STAFFED and policy.stage(plan, o) is None:
            continue
        cands.append(o)
    cands.sort(key=lambda o: (PRIORITY.index(o['type']), _position(plan, o), seqno(o)))
    chosen = []
    for o in cands:
        if len(chosen) + len(running) >= parallel:
            break
        res = _resources(plan, o)
        if (o['target'] and o['target'] in busy_targets) or res & busy_res:
            continue
        chosen.append(o['id'])
        busy_targets.add(o['target'])
        busy_res |= res
    return chosen


# ---- openers --------------------------------------------------------------------------------------

def _blocked_by_user_or_lead(state, order):
    return any(state.orders[n]['type'] in ('decide', 'ask_user') and state.orders[n]['state'] not in SATISFIED
               for n in order['needs'] if n in state.orders)


def open_ask(state, ref, waiter, question, pending=None, options=None):
    ask = new_order(state, 'ask_user', ref['target'], {'by': 'order', 'ref': ref['id'], 'reason': 'needs user'},
                    {'question': question, 'pending': pending or [], 'options': options or [], 'subject': waiter})
    if waiter in state.orders:
        state.orders[waiter]['needs'].append(ask['id'])
    return ask


def open_decide(state, subject, trigger, extra=None, force=False):
    """One decide per trigger; past decide_per_source for the same subject the user is asked instead."""
    plan = state.plan
    count = len([o for o in state.orders.values() if o['type'] == 'decide' and o['origin'].get('ref') == subject['id']])
    if count >= policy.rounds(plan, subject['target'], 'decide_per_source', 2) and not force:
        return open_ask(state, subject, subject['id'], '同一触发源已裁决 %d 次仍未解决（%s）' % (count, trigger))
    last = next((r for r in reversed(subject['runs']) if r['ended']), None)
    inp = {'trigger': trigger, 'subject': subject['id'], 'run': last['run_id'] if last else None,
           'evidence': sorted(last['evidence']) if last else [], 'subject_input': subject['input']}
    inp.update(extra or {})
    d = new_order(state, 'decide', subject['target'], {'by': 'order', 'ref': subject['id'], 'reason': trigger}, inp)
    subject['needs'].append(d['id'])
    return d


def check_exhausted(state, order):
    """Chain used up: the only exit is decide (decide/triage themselves exit to the user)."""
    if order['state'] != 'open' or order['type'] not in policy.STAFFED or policy.stage(state.plan, order) is not None:
        return
    if _blocked_by_user_or_lead(state, order):
        return
    if order['type'] == 'decide':
        order['state'] = 'failed'
        open_ask(state, order, order['origin']['ref'], '裁决未能产出有效处置')
    elif order['type'] == 'triage':
        order['state'] = 'failed'
        release = by_key(state, 'release:' + model.batch_of(state.plan, order['target'])) or {}
        open_ask(state, order, release.get('id'), '分诊未能产出有效处置')
    else:
        open_decide(state, order, 'exhausted')


def add_candidates(state, order, items):
    plan = state.plan
    batch = model.batch_of(plan, order['target'])
    if not batch or not items:
        return
    triages = [o for o in state.orders.values() if o['type'] == 'triage' and o['target'] == batch]
    open_t = next((o for o in triages if o['state'] == 'open' and not o['runs']), None)
    n = len(open_t['input']['items']) if open_t else 0
    new = [{'id': 'C%d' % (n + i + 1), 'text': t, 'source': order['id']} for i, t in enumerate(items)]
    if open_t:
        open_t['input']['items'].extend(new)
        return
    needs = [o['id'] for o in state.orders.values() if o['type'] in ('implement', 'review', 'repair')
             and model.batch_of(plan, o['target']) == batch and o['state'] != 'superseded']
    t = new_order(state, 'triage', batch, {'by': 'order', 'ref': order['id'], 'reason': 'candidates'}, {'items': new}, needs)
    release = by_key(state, 'release:' + batch)
    if release and release['state'] == 'open':
        release['needs'].append(t['id'])


def open_repairs(state, review, run, p):
    targets = []
    for f in p['findings']:
        if f['target'] not in targets:
            targets.append(f['target'])
    for tg in targets:
        fs = [f for f in p['findings'] if f['target'] == tg]
        key = 'repair:%s:%s' % (review['id'], tg)
        rep = by_key(state, key)
        inp = {'findings': [{'id': f['id'], 'closure': f['closure'], 'plan_level': f['plan_level']} for f in fs],
               'review_order': review['id'], 'review_run': run['run_id'], 'plan_level': any(f['plan_level'] for f in fs)}
        if rep:  # same order reopened: the chain keeps escalating
            inp['cycle'] = rep['input'].get('cycle', 1) + 1
            rep['input'] = inp
            rep['state'] = 'open'
        else:
            inp['cycle'] = 1
            rep = new_order(state, 'repair', tg, {'by': 'order', 'ref': review['id'], 'reason': 'FIX_REQUIRED'}, inp, key=key)
        if rep['id'] not in review['needs']:
            review['needs'].append(rep['id'])
        check_exhausted(state, rep)


def link(state, order):
    """A later-opened implement joins the open review of its group and the batch release."""
    plan = state.plan
    if order['type'] != 'implement' or order['target'] not in plan['deliverables']:
        return
    g = model.group_of(plan, order['target'])
    for key in (g and 'review:' + g, 'release:' + plan['deliverables'][order['target']]['batch']):
        target = key and by_key(state, key)
        if target and target['state'] == 'open':
            target['needs'].append(order['id'])


def sync_plan(state):
    """Open the orders a plan (revision) calls for; idempotent through order keys. Supersede orders of removed items."""
    plan = state.plan
    for d, spec in plan['deliverables'].items():
        if not by_key(state, 'impl:' + d):
            new_order(state, 'implement', d, {'by': 'plan', 'ref': 'r%d' % plan['revision'], 'reason': 'plan'},
                      {'instruction': spec['instruction'], 'acceptance': spec['acceptance']}, key='impl:' + d)
    for g, spec in plan['review_groups'].items():
        if not by_key(state, 'review:' + g):
            new_order(state, 'review', g, {'by': 'plan', 'ref': 'r%d' % plan['revision'], 'reason': 'plan'},
                      {'deliverables': list(spec['deliverables'])},
                      [by_key(state, 'impl:' + d)['id'] for d in spec['deliverables']], key='review:' + g)
    for b in plan['batches']:
        if not by_key(state, 'release:' + b['id']):
            new_order(state, 'release', b['id'], {'by': 'plan', 'ref': 'r%d' % plan['revision'], 'reason': 'plan'}, key='release:' + b['id'])
    for o in sorted_orders(state):
        if o['state'] in ('done', 'superseded'):
            continue
        batch = model.batch_of(plan, o['target'])
        if o['type'] in ('implement', 'review', 'release') and o['key'] and batch is None:
            o['state'] = 'superseded'  # target removed by a plan revision
    for b in plan['batches']:
        release = by_key(state, 'release:' + b['id'])
        if release['state'] == 'open':
            for o in sorted_orders(state):
                if o['type'] in ('implement', 'review') and o['key'] and model.batch_of(plan, o['target']) == b['id'] \
                        and o['state'] != 'superseded' and o['id'] not in release['needs']:
                    release['needs'].append(o['id'])
