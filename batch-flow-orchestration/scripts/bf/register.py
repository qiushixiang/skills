"""Registering finished runs and executing decisions: routing table of design 5.2 and decide mechanism of design 7."""
from . import model, policy, routing, permissions, questions, review_budget, cycles
from .store import BfError, need, sha256_file


def on_registered(state, order, run, p, legal, decision):
    t, res = order['type'], run['result']
    if legal:
        order['output'] = {'result': res, 'stop_reason': run['stop_reason'], 'run': run['run_id'], 'issue': p['issue']}
    if t in ('implement', 'repair') and legal:
        if res == 'VERIFIED':
            order['state'] = 'done'
        elif res in ('ARCH_DECISION_REQUIRED', 'WAIT_EVIDENCE'):
            routing.open_decide(state, order, res.lower(), {'issue': p['issue'], 'blocker': p.get('blocker')})
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
    if legal and not order['input'].get('accounting_only'):
        routing.add_candidates(state, order, p['candidates'] + p['suggestions'])
    from . import execution
    if legal and t == 'review' and run.get('budget_managed'):
        execution.coordinate(state, order, run, '审阅调用预算结算', accounting=True)
    execution.ensure_settlements(state)
    # A completed child may unblock another order that shares an exhausted budget.
    for pending in list(state.orders.values()):
        routing.check_exhausted(state, pending)


def _needs_user(state, a):
    plan = state.plan
    if 'plan_revision' in a:
        from . import changes
        return not (a.get('within_authorization') is True and changes.internal_revision(plan, a['plan_revision']))
    if 'grant' in a:
        return True
    if 'authorize' in a:
        obj = state.orders[a['authorize']]
        return any(p not in permissions.allowed(plan, obj) for p in a['paths'])
    if 'supersede' in a and a['supersede'] in state.orders:
        victim = state.orders[a['supersede']]
        if not routing.current_work(state, victim):
            return False  # Retiring an obsolete contract cannot discharge current work or its reviews.
        if victim['type'] in ('review', 'release') or any(
                victim['id'] in o['needs']
                for o in state.orders.values()):
            return True
    requested = a.get('allow_protected') or (a.get('input') or {}).get('allow_protected')
    if requested:
        permitted = plan['envelope'].get('protected', {}).get(a.get('target'), [])
        if any(p not in permitted for p in requested):
            return True
    return 'open' in a and model.batch_of(plan, a['target']) not in plan['envelope']['batches']


def check_retries(state, actions):
    for action in actions:
        if 'retry' in action:
            obj = state.orders[action['retry']]
            if cycles.enabled(state.plan, obj['target']) and obj['type'] in ('implement', 'repair'):
                obj['performer'] = cycles.retry_performer(state, obj)
            need(not permissions.missing(state.plan, obj), 'retry still lacks required protected permissions')
            need(policy.stage(state.plan, obj, state) is not None, 'retry still has no execution allowance')
        elif 'continue_run' in action:
            obj = next(o for o in state.orders.values() if o['input'].get('continuation_of') == action['continue_run'])
            need(not permissions.missing(state.plan, obj), 'continuation still lacks required protected permissions')
            need(policy.stage(state.plan, obj, state) is not None, 'continuation has no eligible original author')


def preview_retries(state, source, actions):
    """Check retry postconditions on an isolated copy, before accepting a decision.

    Apply the whole bundle so grants, settlements and permissions count regardless
    of their position. This never writes the task's state or runs a role.
    """
    import copy
    trial = copy.deepcopy(state)
    src = trial.orders[source['id']] if source else {'id': 'preview', 'input': {}}
    if source:
        src['state'] = 'done'
    try:
        for action in actions:
            _execute(trial, src, action, src.get('input', {}).get('subject'))
        check_retries(trial, actions)
    except BfError as error:
        return str(error)
    return ''


def apply_actions(state, source, actions, waiter, forced=False, diagnosis=''):
    """Execute decision actions; those beyond the envelope (grant, plan revision, out-of-envelope open) wait for the user (I6)."""
    import copy
    prior_plan = copy.deepcopy(state.plan)
    from .gate import validate_decision
    need(not validate_decision(state, {'diagnosis': diagnosis or '处置', 'actions': actions}, source),
         'invalid decision action bundle')
    # Approval is for the entire disposition, not a partially executed subset.
    if not forced and any(_needs_user(state, a) for a in actions):
        explanation = diagnosis or (source.get('output') or {}).get('decision', {}).get('diagnosis', '')
        routing.open_ask(state, source, waiter, '处置超出授权包络，需用户批准：%s' % explanation,
                         pending=actions, request=questions.for_actions([a for a in actions if _needs_user(state, a)], explanation))
        return
    for a in actions:
        _execute(state, source, a, waiter)
    for a in actions:
        if 'authorize' in a:
            obj = state.orders[a['authorize']]
            need(all(p in permissions.allowed(state.plan, obj) for p in a['paths']), 'authorization postcondition failed')
    check_retries(state, actions)
    for obj in state.orders.values():
        if obj['type'] in ('implement', 'repair', 'review'):
            need(not policy.over_budget(state.plan, obj, state), 'settlement exceeds authorized technical budget')
    review_budget.validate_revision(prior_plan, state.plan)
    from . import execution
    execution.ensure_settlements(state)
    for obj in list(state.orders.values()):
        routing.check_exhausted(state, obj)


def _execute(state, source, a, waiter):
    plan = state.plan
    if 'continue_run' in a:
        reason = cycles.validate_continuation(state, source, a)
        need(not reason, reason)
        ctx = cycles.continuation_context(state, source)
        o = state.orders[ctx['input']['subject']]
        run = o['runs'][-1]
        o['input']['continuation_of'] = run['run_id']
        o['input']['recovery'] = {'instruction': a['reason'], 'source': source['id']}
        o['performer'] = run['performer']
        o['state'] = 'open'
        o.pop('awaiting_disposition', None)
        o.pop('ops', None)
    elif 'settle' in a:
        owner, run = next((o, r) for o in state.orders.values() for r in o['runs'] if r['run_id'] == a['settle'])
        owner.setdefault('settlements', {}).setdefault(run['run_id'], {'charge': a['charge'], 'reason': a['reason'], 'source': source['id']})
        for pending in state.orders.values():
            if pending['input'].get('accounting_only') and pending['input'].get('run') == run['run_id'] and pending['state'] == 'open':
                pending['state'] = 'done'
    elif 'authorize' in a:
        permissions.install(state, routing.get(state, a['authorize']), a['paths'], source['id'])
    elif 'blocked' in a:
        routing.pause(state, routing.get(state, a['blocked']), a['reason'], missing=a['missing'], waits_on=a.get('waits_on'))
    elif 'open' in a:
        inp = dict(a.get('input') or {})
        if a.get('allow_protected'):
            inp['allow_protected'] = list(a['allow_protected'])
        needs = list(a.get('needs') or [])
        if a['open'] == 'review':
            needs = list(dict.fromkeys(needs + routing.review_needs(state, a['target'])))
        o = routing.new_order(state, a['open'], a.get('target'), {'by': 'order', 'ref': source['id'], 'reason': 'decision'},
                              inp, needs)
        if a.get('fresh_session'):
            o['fresh_next'] = True
        if waiter in state.orders:
            state.orders[waiter]['needs'].append(o['id'])
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
            o.pop('awaiting_disposition', None)
            problem = o.pop('ops', None)
            if problem:
                o.setdefault('ops_log', []).append({'op': 'decide_retry', 'note': a.get('reason', ''),
                    'problem': problem['problem'], 'source': source['id']})
            if a.get('reason'):
                o['input']['recovery'] = {'instruction': a['reason'], 'source': source['id']}

            if cycles.enabled(plan, o['target']) and o['type'] in ('implement', 'repair'):
                o['performer'] = cycles.retry_performer(state, o)
            elif 'performer' in a:
                o['control_protocol'] = 'intent-v1'
                o['performer'] = a['performer']
    elif 'grant' in a:
        for key, spec in a['grant'].items():
            target = state.orders[key]['target'] if key in state.orders else key
            need(target in plan['deliverables'] or target in plan['review_groups'], 'grant: unknown target %s' % key)
            policy.add_rounds(plan, target, spec)
        from . import environment
        if not environment.is_legacy(plan.get('env')):
            environment.register(state.base, plan)  # new lead rounds may put the lead on implementation
        plan['revision'] += 1
        state.plan_dirty = True
        targets = {state.orders[k]['target'] if k in state.orders else k for k in a['grant']}
        routing.condition_changed(state, 'grant', targets, '负责人追加额度：%s' % '、'.join(sorted(targets)))
    elif 'plan_revision' in a:
        from . import changes
        candidate = model.load_plan_file(a['plan_revision'])
        if a.get('within_authorization'):
            need(changes.internal_revision(plan, candidate), 'internal plan changed outside the authorized boundary')
        revise(state, candidate, None, 'internal technical decision' if a.get('within_authorization') else 'user answer',
               revalidate=a.get('revalidate', []))
    elif 'ask_user' in a:
        routing.open_ask(state, source, waiter, a['ask_user'], options=a.get('options'), request=a['user_request'])


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
        if ask['input']['pending']:
            apply_actions(state, ask, ask['input']['pending'], ask['input'].get('subject'), forced=True)
        else:
            # A directional answer needs interpretation; it is not a successful implementation retry.
            anchor = state.orders.get(ask['input'].get('subject')) or ref or ask
            routing.open_decide(state, anchor, 'user_answer',
                                {'answer': text, 'question': ask['input']['question']}, force=True)
    else:
        subject = state.orders.get(ask['input'].get('subject') or '')
        # A superseded/completed subject must not swallow a user's changed direction.
        # Keep its history intact; let the lead interpret the answer and hold its live consumers.
        anchor = subject or ref or ask
        waiters = [o for o in state.orders.values() if o['state'] == 'open' and anchor['id'] in o['needs']]
        decision = routing.open_decide(state, anchor, 'user_rejected',
                                       {'answer': text, 'question': ask['input']['question']}, force=True)
        for waiter in waiters:
            if decision['id'] not in waiter['needs']:
                waiter['needs'].append(decision['id'])


def revise(state, new_plan, approval, source='user', revalidate=()):
    """Install a revised plan: budgets never shrink below what was used (I2); orders for new items are opened."""
    old = state.plan
    # Freeze the pre-revision review basis, including stores created before this field existed.
    for o in state.orders.values():
        if o['target'] in old['review_groups']:
            o.setdefault('contract', model.review_contract(old, o['target']))
    plan = model.normalize(new_plan, enforce_review_budget=False)
    from . import environment
    environment.register(state.base, plan)
    cycles.require_same_model(old, plan, state.orders)
    review_budget.validate_revision(old, plan, revalidate)
    plan['revision'] = old['revision'] + 1
    plan['approval'] = approval or old.get('approval')
    need(isinstance(revalidate, (list, tuple)) and all(d in plan['deliverables'] for d in revalidate),
         'revalidate must list deliverables explicitly selected by the lead')
    # The lead selects what must be done again; textual changes never infer invalidation.
    changed = set(revalidate)
    plan['revision_notes'] = list(old.get('revision_notes', [])) + [{
        'revision': plan['revision'], 'source': source, 'revalidate': sorted(changed),
        'instruction_changes': [d for d in plan['deliverables'] if d in old['deliverables']
                                and plan['deliverables'][d] != old['deliverables'][d]]}]
    available = set(plan['deliverables']) | set(plan['review_groups']) | {None}
    for o in state.orders.values():
        need(o['state'] != 'running' or (o['target'] not in changed and o['target'] in available),
             'revision affects running order %s; stop and collect it before revising' % o['id'])
    for d, spec in plan['deliverables'].items():
        spec['contract_version'] = (plan['revision'] if d in changed else
                                    old['deliverables'].get(d, {}).get('contract_version', 1))
    for obj in state.orders.values():
        group = obj['target']
        if obj['type'] == 'review' and group in old['review_groups'] and group in plan['review_groups']:
            before, after = old['review_groups'][group], plan['review_groups'][group]
            structural = ('deliverables', 'batch', 'reviewer')
            if not changed.intersection(after['deliverables']) and all(before.get(k) == after.get(k) for k in structural):
                current = model.review_contract(plan, group)
                if obj.get('contract') == model.review_contract(old, group) and obj['contract'] != current:
                    obj.setdefault('contract_history', []).append({'revision': plan['revision'], 'prior': obj['contract']})
                    obj['contract'] = current
    state.plan = plan
    routing.condition_changed(state, 'revision', (), '计划已修订为第 %d 版' % plan['revision'])
    for o in state.orders.values():
        if o['type'] in policy.STAFFED:
            over = policy.over_budget(plan, o, state)
            need(not over, 'revision would take back used budget of %s (%s)' % (o['id'], ', '.join(over)))
    state.plan_dirty = True
    routing.sync_plan(state)
    from . import execution
    execution.ensure_settlements(state)
    for pending in list(state.orders.values()):
        routing.check_exhausted(state, pending)
