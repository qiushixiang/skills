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
    if type_ in ('implement', 'repair', 'review'):
        o['control_protocol'] = 'review-v1' if type_ == 'review' else 'intent-v1'
        if type_ != 'review':
            o['performer'] = next((s for s, n in policy.ladder(state.plan, o)), None)
    if target in state.plan['deliverables']:
        o['contract_version'] = state.plan['deliverables'][target].get('contract_version', 1)
        if type_ in ('implement', 'repair') and state.plan['deliverables'][target].get('required_protected'):
            o['input']['required_protected'] = list(state.plan['deliverables'][target]['required_protected'])
    if target in state.plan['review_groups']:
        o['contract'] = model.review_contract(state.plan, target)
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


def _resources(plan, order):
    if order['input'].get('accounting_only'):
        return set()
    members = model.deliverables_of(plan, order['target'])
    return set('deliverable:' + d for d in members) | set(
        x for d in members for x in plan['deliverables'][d]['resources'])


def current_work(state, order):
    """Historical deliveries remain intact, but cannot satisfy a changed contract."""
    if order['input'].get('accounting_only'):
        return True
    if order['target'] in state.plan['deliverables'] or order['type'] in ('implement', 'repair'):
        spec = state.plan['deliverables'].get(order['target'])
        return bool(spec) and order.get('contract_version', 1) == spec.get('contract_version', 1)
    if order['target'] in state.plan['review_groups'] or order['type'] == 'review':
        return order['target'] in state.plan['review_groups'] and order.get('contract') in (
            None, model.review_contract(state.plan, order['target']))
    return True


def work_for(state, target):
    return [o for o in sorted_orders(state) if o['target'] == target and o['type'] in ('implement', 'repair')
            and o['state'] != 'superseded' and current_work(state, o)]


def review_needs(state, group):
    """A review always waits for all current work on its declared deliverables."""
    return [o['id'] for d in state.plan['review_groups'][group]['deliverables'] for o in work_for(state, d)]


def refresh_review_recovery(state, submitted):
    """A new delivery ends recovery instructions for an earlier review version."""
    for review in state.orders.values():
        if (review['type'] == 'review' and review['state'] == 'open'
                and review['target'] in state.plan['review_groups']
                and submitted['target'] in state.plan['review_groups'][review['target']]['deliverables']):
            recovery = review['input'].pop('recovery', None)
            if recovery:
                review.setdefault('recovery_history', []).append(recovery)


def _position(plan, order):
    dels, tg = list(plan['deliverables']), order['target']
    members = model.deliverables_of(plan, tg)
    batch = model.batch_of(plan, tg)
    ids = [b['id'] for b in plan['batches']]
    return (ids.index(batch) if batch else len(ids), dels.index(members[0]) if members else len(dels))


def _busy_target(order):
    return None if order['input'].get('accounting_only') else order['target']


def runnable(state, parallel=1):
    if state.stopped:
        return []
    plan = state.plan
    held = set(plan.get('holds') or [])
    running = [o for o in state.orders.values() if o['state'] == 'running']
    busy_targets = set(_busy_target(o) for o in running if _busy_target(o))
    busy_res = set()
    for o in running:
        busy_res |= _resources(plan, o)
    deciding = set(o['target'] for o in state.orders.values() if o['type'] == 'decide' and o['state'] in ('open', 'running') and not o['input'].get('accounting_only'))

    def dep_ok(d):
        if d in plan['review_groups']:
            impls = [o for o in state.orders.values() if o['type'] == 'review' and o['target'] == d and current_work(state, o)]
            return bool(impls) and all(i['state'] in SATISFIED for i in impls)
        impls = [o for o in work_for(state, d) if o['type'] == 'implement']
        return bool(impls) and all(i['state'] in SATISFIED for i in impls)
    cands = []
    for o in state.orders.values():
        if o['state'] != 'open' or o['type'] == 'ask_user' or o.get('ops') or o.get('awaiting_disposition'):
            continue
        if not current_work(state, o):
            continue
        needs = o['needs'] + (review_needs(state, o['target']) if o['type'] == 'review' else [])
        if not all(state.orders[n]['state'] in SATISFIED for n in needs if n in state.orders):
            continue
        batch = model.batch_of(plan, o['target'])
        if batch and batch not in plan['envelope']['batches']:
            continue
        if held & set(model.deliverables_of(plan, o['target']) + [o['target'], batch]):
            continue
        if not o['input'].get('accounting_only') and o['target'] in plan['deliverables'] and not all(dep_ok(d) for d in plan['deliverables'][o['target']]['depends_on']):
            continue
        if o['type'] != 'decide' and o['target'] in deciding:
            continue
        if o['type'] in policy.STAFFED and policy.stage(plan, o, state) is None:
            continue
        cands.append(o)
    cands.sort(key=lambda o: (PRIORITY.index(o['type']), _position(plan, o), seqno(o)))
    chosen = []
    decision_targets = {o['target'] for o in running if o['type'] == 'decide'}
    for o in cands:
        if len(chosen) + len(running) >= parallel:
            break
        if o['type'] == 'decide' and o['target'] in decision_targets:
            continue  # Technical decisions and accounting share one target session.
        res = _resources(plan, o)
        if (_busy_target(o) and _busy_target(o) in busy_targets) or res & busy_res:
            continue
        chosen.append(o['id'])
        if o['type'] == 'decide':
            decision_targets.add(o['target'])
        busy_targets.add(_busy_target(o))
        busy_res |= res
    return chosen


# ---- openers --------------------------------------------------------------------------------------

def _blocked_by_user_or_lead(state, order):
    return any(state.orders[n]['type'] in ('decide', 'ask_user') and state.orders[n]['state'] not in SATISFIED
               for n in order['needs'] if n in state.orders)


def open_ask(state, ref, waiter, question, pending=None, options=None, request=None):
    from . import questions
    need(not questions.validate(request), questions.validate(request))
    question = '%s\n类别：%s\n需决定：%s\n推荐：%s\n影响：%s' % (
        question, questions.CATEGORIES[request['category']], request['decision'], request['recommendation'], request['impact'])
    ask = new_order(state, 'ask_user', ref['target'], {'by': 'order', 'ref': ref['id'], 'reason': 'needs user'},
                    {'question': question, 'pending': pending or [], 'options': options or [], 'subject': waiter,
                     'user_request': request})
    if waiter in state.orders:
        state.orders[waiter]['needs'].append(ask['id'])
    return ask


def open_decide(state, subject, trigger, extra=None, force=False):
    """Bound consecutive repetitions of the same issue at the same material state."""
    plan = state.plan
    issue = [subject['type'], subject['target'], subject.get('contract_version'), trigger]
    if subject['type'] == 'repair':
        issue.append(subject['input'].get('review_order'))
    details = extra or {}
    issue.append(details.get('blocker') or sorted(details.get('paths') or []))
    count = len([o for o in state.orders.values() if o['type'] == 'decide'
                 and not o['input'].get('accounting_only') and o['input'].get('issue_key') == issue])
    stalled = len([o for o in state.orders.values() if o['type'] == 'decide'
                   and not o['input'].get('accounting_only') and o['input'].get('issue_key', [])[:3] == issue[:3]])
    active = next((o for o in state.orders.values() if o['type'] == 'decide' and
                   o['state'] in ('open', 'running') and o['input'].get('subject') == subject['id'] and
                   o['input'].get('issue_key') == issue), None)
    if active and not force:
        return active
    if not force and stalled >= policy.rounds(plan, subject['target'], 'coordination_no_progress', 6):
        return pause(state, subject, '本目标协调调用达到配置上限 %d 次' % stalled, 'coordination_limit')
    if count >= policy.rounds(plan, subject['target'], 'decide_per_source', 2) and not force:
        return pause(state, subject, '同类触发的协调调用达到配置上限 %d 次（%s）' % (count, trigger), 'coordination_limit')
    last = next((r for r in reversed(subject['runs']) if r['ended']), None)
    inp = {'trigger': trigger, 'subject': subject['id'], 'run': last['run_id'] if last else None,
           'evidence': sorted(last['evidence']) if last else [],
           'issue_key': issue}
    inp.update(extra or {})
    d = new_order(state, 'decide', subject['target'], {'by': 'order', 'ref': subject['id'], 'reason': trigger}, inp)
    subject['needs'].append(d['id'])
    return d


def check_exhausted(state, order):
    """Chain used up: the only exit is decide (decide/triage themselves exit to the user)."""
    if order.get('awaiting_disposition') or (order['type'] in ('implement', 'repair', 'review') and policy.unsettled(state, order)):
        return
    if order['state'] != 'open' or order.get('ops') or order['type'] not in policy.STAFFED or policy.stage(state.plan, order, state) is not None:
        return
    if any(state.orders[n]['state'] not in SATISFIED for n in order['needs'] if n in state.orders):
        return
    if order['type'] in ('decide', 'triage'):
        pause(state, order, '协调重试次数用尽', 'coordination_exhausted')
    else:
        open_decide(state, order, 'exhausted')


def pause(state, order, why, kind='technical_block', missing=''):
    """Bound a stalled path without fabricating a user decision or satisfying dependencies."""
    from .store import now
    if not missing:
        missing = {'coordination_exhausted': '修复协调交付并给出合法、可执行的处置',
                   'tool_failure': '恢复当前 CLI 调用环境或账号',
                   'no_progress': '负责人核查历史暂停并确定恢复方案',
                   'coordination_limit': '负责人或运维核查协调调用次数及恢复条件'}.get(kind, '负责人核实具体恢复条件')
    order['state'] = 'open'
    order['ops'] = {'problem': why, 'kind': kind, 'missing': missing, 'at': now()}
    return order


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
            if rep['state'] == 'done' and rep.get('control_protocol') == 'intent-v1':
                # A new review cycle starts with the executor; old fallback selection
                # is not an instruction to keep the lead implementing indefinitely.
                rep['performer'] = 'primary'
            inp['cycle'] = rep['input'].get('cycle', 1) + 1
            rep['input'] = inp
            rep['state'] = 'open'
        else:
            inp['cycle'] = 1
            rep = new_order(state, 'repair', tg, {'by': 'order', 'ref': review['id'], 'reason': 'FIX_REQUIRED'}, inp, key=key)
        if rep['id'] not in review['needs']:
            review['needs'].append(rep['id'])
        link(state, rep)
        check_exhausted(state, rep)


def latest_snapshots(state, deliverable):
    """All captured versions, in run order; partial and interrupted work is relevant evidence too."""
    paths = []
    for o in sorted_orders(state):
        if o['target'] == deliverable and o['type'] in ('implement', 'repair'):
            for r in o['runs']:
                for path in r.get('snapshot', []):
                    if path not in paths:
                        paths.append(path)
    return paths


def _review_family(state, group):
    return [o for o in sorted_orders(state) if o['type'] == 'review' and o['target'] == group]


def ensure_review(state, group, work=None):
    family = _review_family(state, group)
    last = family[-1] if family else None
    if last and last['state'] == 'open' and current_work(state, last):
        review = last
    else:
        if last and last['state'] == 'open':
            last['state'] = 'superseded'
        needs = review_needs(state, group)
        key = 'review:' + group + (':%d' % (len(family) + 1) if family else '')
        review = new_order(state, 'review', group, {'by': 'plan', 'ref': 'r%d' % state.plan['revision'],
                           'reason': 'current contract and work'},
                           {'deliverables': list(state.plan['review_groups'][group]['deliverables'])}, needs, key)
    if work and work['id'] not in review['needs']:
        review['needs'].append(work['id'])
    release = by_key(state, 'release:' + model.batch_of(state.plan, group))
    if release:
        release['state'] = 'open'
        if review['id'] not in release['needs']:
            release['needs'].append(review['id'])
    return review


def link(state, order):
    """All implementation and repair work invalidates every covering review and release."""
    plan = state.plan
    if order['type'] not in ('implement', 'repair') or order['target'] not in plan['deliverables']:
        return
    release = by_key(state, 'release:' + plan['deliverables'][order['target']]['batch'])
    for g in model.groups_of(plan, order['target']):
        ensure_review(state, g, order)
    if release:
        release['state'] = 'open'
        if order['id'] not in release['needs']:
            release['needs'].append(order['id'])


def sync_plan(state):
    """Open the orders a plan (revision) calls for; idempotent through order keys. Supersede orders of removed items."""
    plan = state.plan
    for d, spec in plan['deliverables'].items():
        if not any(o['type'] == 'implement' and current_work(state, o) and o['target'] == d
                   for o in state.orders.values()):
            version = spec.get('contract_version', 1)
            key = 'impl:' + d + (':r%d' % version if version != 1 else '')
            new_order(state, 'implement', d, {'by': 'plan', 'ref': 'r%d' % plan['revision'], 'reason': 'plan'},
                      {}, key=key)
    for g, spec in plan['review_groups'].items():
        family = _review_family(state, g)
        if not family or not current_work(state, family[-1]):
            ensure_review(state, g)
        else:
            for d in spec['deliverables']:
                for work in work_for(state, d):
                    if family[-1]['state'] == 'open' and work['id'] not in family[-1]['needs']:
                        family[-1]['needs'].append(work['id'])
    for b in plan['batches']:
        if not by_key(state, 'release:' + b['id']):
            new_order(state, 'release', b['id'], {'by': 'plan', 'ref': 'r%d' % plan['revision'], 'reason': 'plan'}, key='release:' + b['id'])
    for o in sorted_orders(state):
        if o['state'] in ('done', 'superseded'):
            continue
        batch = model.batch_of(plan, o['target'])
        if not o['input'].get('accounting_only') and o['target'] is not None and (batch is None or not current_work(state, o)):
            o['state'] = 'superseded'  # target removed by a plan revision
    for b in plan['batches']:
        release = by_key(state, 'release:' + b['id'])
        if release['state'] == 'open':
            for o in sorted_orders(state):
                if o['type'] in ('implement', 'review') and o['key'] and model.batch_of(plan, o['target']) == b['id'] \
                        and o['state'] != 'superseded' and o['id'] not in release['needs']:
                    release['needs'].append(o['id'])
