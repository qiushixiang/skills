"""Context policy (design 9): one function decides fresh vs resumed session, with a rule name."""


def _fresh(rule):
    return {'mode': 'fresh', 'conversation_id': None, 'rule': rule}


def _resume(cid, rule):
    return {'mode': 'resume', 'conversation_id': cid, 'rule': rule}


def author_sessions(state, order):
    """Sessions that authored (implemented or repaired) what this review/decide order is about (I4)."""
    plan = state.plan
    if order['type'] == 'review':
        group = plan['review_groups'].get(order['target'])
        targets = set(group['deliverables']) if group else set()
    else:
        targets = set([order['target']])
    return set(r['session'] for o in state.orders.values() if o['type'] in ('implement', 'repair')
               and o['target'] in targets for r in o['runs'] if r.get('session'))


def _last_session(runs, kb, cfg):
    """(session, rule-if-unusable) of the latest finished run."""
    if not runs:
        return None, 'first'
    last = runs[-1]
    if not last.get('session'):
        return None, 'no-session'
    if cfg and (last['tool'], last['model']) != (cfg['tool'], cfg['model']):
        return None, 'tool-change'
    if (last.get('session_kb') or 0) > kb:
        return None, 'size-limit'
    return last['session'], None


def session_for(state, order, stage, cfg=None):
    t = order['type']
    kb = state.plan['policy']['context']['max_session_kb']
    runs = [r for r in order['runs'] if r['ended'] and r['result'] != 'INVALID']
    if order.get('fresh_next'):
        return _fresh('fresh-requested')
    if t == 'implement':
        if not runs:
            return _fresh('implement-first')
        if runs[-1]['performer'] != stage:
            return _fresh('stage-change')
        if runs[-1].get('env_rejected'):
            return _fresh('env-misjudged')
        cid, why = _last_session(runs, kb, cfg)
        return _resume(cid, 'implement-continue') if cid else _fresh(why)
    if t == 'review':
        cid, why = _last_session(runs, kb, cfg)
        if cid and cid in author_sessions(state, order):
            return _fresh('review-author-session')
        return _resume(cid, 'review-resume') if cid else _fresh('review-first' if why == 'first' else why)
    if t == 'decide':
        prior = [o for o in state.orders.values() if o['type'] == 'decide' and o['target'] == order['target']
                 and o['id'] != order['id']]
        finished = [r for o in prior for r in o['runs'] if r['ended'] and r['result'] != 'INVALID']
        cid, why = _last_session(finished, kb, cfg)
        if cid and cid in author_sessions(state, order):
            return _fresh('decide-author-session')
        return _resume(cid, 'decide-resume') if cid else _fresh('decide-first' if why == 'first' else why)
    return _fresh('%s-fresh' % t)  # repair, triage
