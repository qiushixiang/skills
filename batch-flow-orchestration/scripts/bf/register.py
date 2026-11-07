"""Registering finished runs and executing decisions: routing table of design 5.2 and decide mechanism of design 7."""
from . import model, policy, routing
from .store import need, sha256_file


def on_registered(state, order, run, p, legal, decision):
    t, res = order['type'], run['result']
    if legal:
        order['output'] = {'result': res, 'stop_reason': run['stop_reason'], 'run': run['run_id'], 'issue': p['issue']}
    if t in ('implement', 'repair') and legal:
        if res == 'VERIFIED':
            order['state'] = 'done'
        elif res in ('ARCH_DECISION_REQUIRED', 'WAIT_EVIDENCE'):
            routing.open_decide(state, order, res.lower(), {'issue': p['issue']})
    elif t == 'review' and legal:
        if res == 'RELEASE':
            order['state'] = 'done'
        else:
            routing.open_repairs(state, order, run, p)
    elif t == 'decide' and legal:
        order['state'] = 'done'
        order['output']['decision'] = decision
        apply_actions(state, order, decision['actions'], order['origin']['ref'])
    elif t == 'triage' and legal:
        order['state'] = 'done'
        apply_triage(state, order, decision)
    if legal:
        routing.add_candidates(state, order, p['candidates'] + p['suggestions'])
    routing.check_exhausted(state, order)


def _needs_user(state, a):
    plan = state.plan
    if 'grant' in a or 'plan_revision' in a:
        return True
    if 'supersede' in a and a['supersede'] in state.orders:
        victim = state.orders[a['supersede']]
        if victim['type'] in ('review', 'release') or any(
                victim['id'] in o['needs'] and o['state'] not in routing.SATISFIED and o['type'] not in ('review', 'release')
                for o in state.orders.values()):
            return True
    if a.get('allow_protected'):
        return True
    return 'open' in a and model.batch_of(plan, a['target']) not in plan['envelope']['batches']


def apply_actions(state, source, actions, waiter, forced=False, diagnosis=''):
    """Execute decision actions; those beyond the envelope (grant, plan revision, out-of-envelope open) wait for the user (I6)."""
    deferred = []
    for a in actions:
        if _needs_user(state, a) and not forced:
            deferred.append(a)
        else:
            _execute(state, source, a, waiter)
    if deferred:
        routing.open_ask(state, source, waiter, '处置超出授权包络，需用户批准：%s' % (diagnosis or source['output'] and
                                                    source['output'].get('decision', {}).get('diagnosis', '')), pending=deferred)


def _execute(state, source, a, waiter):
    plan = state.plan
    if 'open' in a:
        inp = dict(a.get('input') or {})
        if a.get('allow_protected'):
            inp['allow_protected'] = list(a['allow_protected'])
        o = routing.new_order(state, a['open'], a.get('target'), {'by': 'order', 'ref': source['id'], 'reason': 'decision'},
                              inp, a.get('needs'))
        if a.get('fresh_session'):
            o['fresh_next'] = True
        routing.link(state, o)
    elif 'supersede' in a:
        o = routing.get(state, a['supersede'])
        if o['state'] != 'done':
            o['state'] = 'superseded'
            o['output'] = {'superseded_by': source['id'], 'reason': a.get('reason', '')}
    elif 'refresh_design' in a:
        model.pin_design(plan)
        plan['revision'] += 1
        state.plan_dirty = True
    elif 'retry' in a:
        o = routing.get(state, a['retry'])
        if o['state'] in ('open', 'failed'):
            o['state'] = 'open'
            o['fresh_next'] = bool(a.get('fresh_session'))
    elif 'grant' in a:
        for key, spec in a['grant'].items():
            target = state.orders[key]['target'] if key in state.orders else key
            need(target in plan['deliverables'], 'grant: unknown target %s' % key)
            ov = plan['policy']['overrides'].setdefault(target, {})
            for rkey, n in spec.items():
                ov[rkey] = ov.get(rkey, plan['policy']['rounds'].get(rkey, 0)) + int(n)
        plan['revision'] += 1
        state.plan_dirty = True
    elif 'plan_revision' in a:
        revise(state, model.load_plan_file(a['plan_revision']), None, 'user answer')
    elif 'ask_user' in a:
        routing.open_ask(state, source, waiter, a['ask_user'], options=a.get('options'))


def apply_triage(state, order, decision):
    items = {i['id']: i for i in order['input']['items']}
    release = routing.by_key(state, 'release:' + model.batch_of(state.plan, order['target']))
    acts = [{'open': 'implement', 'target': d['target'], 'input': {'instruction': items[i]['text'], 'candidate': i}}
            for i, d in sorted(decision['items'].items()) if d['disposition'] == 'merge']
    order['output']['items'] = decision['items']
    apply_actions(state, order, acts, release['id'] if release else None, diagnosis='分诊合并项')


def answer(state, order_id, text, approve, at):
    ask = routing.get(state, order_id)
    need(ask['type'] == 'ask_user' and ask['state'] == 'open', '%s is not an open ask_user order' % order_id)
    ask['state'] = 'done'
    ask['output'] = {'answer': text, 'approve': bool(approve), 'at': at}
    ref = state.orders.get(ask['origin']['ref'])
    if ref and ref['state'] == 'failed':  # decide/triage ran out: the user's answer buys one more round
        ref['state'] = 'open'
        ref['extra'] = ref.get('extra', 0) + 1
        ref['input'].setdefault('answers', []).append(text)
    elif approve:
        apply_actions(state, ref or ask, ask['input']['pending'], ask['input'].get('subject'), forced=True)
    else:
        subject = state.orders.get(ask['input'].get('subject') or '')
        if subject and subject['state'] in ('open', 'running'):
            routing.open_decide(state, subject, 'user_rejected', {'answer': text, 'question': ask['input']['question']}, force=True)


def revise(state, new_plan, approval, source='user'):
    """Install a revised plan: budgets never shrink below what was used (I2); orders for new items are opened."""
    old = state.plan
    plan = model.normalize(new_plan)
    plan['revision'] = old['revision'] + 1
    plan['approval'] = approval or old.get('approval')
    state.plan = plan
    for o in state.orders.values():
        if o['type'] in policy.STAFFED:
            over = policy.over_budget(plan, o)
            need(not over, 'revision would take back used budget of %s (%s)' % (o['id'], ', '.join(over)))
    state.plan_dirty = True
    routing.sync_plan(state)
