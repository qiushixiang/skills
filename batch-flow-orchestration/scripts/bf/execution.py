"""Execution intent and separate technical budget accounting; no quality judgments."""
import time
from . import gate, policy, routing, cycles
from .store import now, sha256_file


def call_facts(run):
    # Never re-inject repository-wide baselines or unrelated run internals into a role card.
    keys = ('execution_cycle', 'execution_model', 'delivery_protocol', 'run_id', 'performer', 'tool', 'model', 'started', 'ended', 'segments', 'continuations',
            'result', 'stop_reason', 'delivery', 'edited', 'failed', 'commands_seen', 'entry',
            'evidence', 'version', 'snapshot', 'observations', 'dispatch')
    return {key: run[key] for key in keys if key in run}


def coordinate(state, order, run, reason, accounting=False):
    if not accounting:
        order['awaiting_disposition'] = True
        if run.get('delivery_protocol') == 'delivery-v2' and run.get('result') == 'HANDOFF':
            key = 'unfinished:' + run['run_id']
            old = routing.by_key(state, key)
            if old:
                return old
            decision = routing.new_order(state, 'decide', order['target'],
                {'by': 'order', 'ref': order['id'], 'reason': 'unfinished'},
                {'trigger': 'unfinished', 'subject': order['id'], 'run': run['run_id'],
                 'issue': reason, 'call_facts': call_facts(run), 'evidence': sorted(run['evidence'])}, key=key)
            order['needs'].append(decision['id'])
            return decision
        return routing.open_decide(state, order, 'handoff', {'issue': reason, 'call_facts': call_facts(run), 'run': run['run_id']})
    key = 'settlement:' + run['run_id']
    old = routing.by_key(state, key)
    if old and old['state'] in ('open', 'running'):
        return old
    return routing.new_order(state, 'decide', order['target'],
        {'by': 'order', 'ref': order['id'], 'reason': 'budget_settlement'},
        {'accounting_only': True, 'subject': order['id'], 'run': run['run_id'],
         'issue': reason, 'call_facts': call_facts(run)}, key=key)


def ensure_settlements(state):
    for order in list(state.orders.values()):
        for run in order['runs']:
            if run.get('budget_managed') and run['ended'] and policy.charge(order, run) is None:
                active = any(o['type'] == 'decide' and o['state'] in ('open', 'running') and
                    o['input'].get('subject') == order['id'] and
                    (o['input'].get('run') == run['run_id'] or not o['input'].get('accounting_only'))
                    for o in state.orders.values())
                if not active:
                    coordinate(state, order, run, '待结算技术调用', accounting=True)


def deliver(state, order, run, parsed, text, version=None):
    legal, reason = gate.judge_intent(order, text, run)
    run.setdefault('deliveries', []).append({'text': text, 'legal': legal, 'intent': parsed['intent']})
    out = {'run_id': run['run_id'], 'legal': legal, 'reason': reason, 'opened': []}
    if legal and parsed['intent'] == '继续' and run['continuations'] < state.plan['policy']['continue_limit']:
        run['continuations'] += 1
        return dict(out, outcome='continue')
    submitted = legal and parsed['intent'] == '交付'
    run.update(ended=now(), ended_ts=time.time(), delivery=text, result='SUBMITTED' if submitted else 'HANDOFF',
               stop_reason=reason, version=parsed['version'] or version or run.get('version'))
    run['evidence'].update((path, sha256_file(path)) for path in parsed['evidence'])
    run['citations'] = parsed['citations']
    order['state'] = 'done' if submitted else 'open'
    order['output'] = {'run': run['run_id'], 'result': run['result'], 'issue': parsed['handoff']}
    if submitted:
        routing.refresh_review_recovery(state, order)
    issue = reason if not legal else ('续作上限已到' if parsed['intent'] == '继续' else
            (parsed['handoff'] or '执行者请求移交') if parsed['intent'] == '移交' else '已交付，待预算结算')
    if not submitted or not cycles.enabled(state.plan, order['target']):
        coordinate(state, order, run, issue, accounting=submitted)
    routing.add_candidates(state, order, parsed['candidates'])
    return dict(out, outcome='registered', result=run['result'], opened=list(state.opened))


def deliver_status(state, order, run, parsed, text, version=None):
    """Delivered work goes directly to review; all unfinished returns go to disposition."""
    legal, reason = gate.judge_delivery(order, text, run)
    submitted = legal and parsed['delivery_status'] == '已交付'
    run.setdefault('deliveries', []).append({'text': text, 'legal': legal, 'status': parsed['delivery_status']})
    run.update(ended=now(), ended_ts=time.time(), delivery=text,
               result='SUBMITTED' if submitted else 'HANDOFF', stop_reason=reason,
               version=parsed['version'] or version or run.get('version'))
    run['evidence'].update((path, sha256_file(path)) for path in parsed['evidence'])
    run['citations'] = parsed['citations']
    issue = parsed['unfinished'] if legal and not submitted else reason if not legal else ''
    order['state'] = 'done' if submitted else 'open'
    order['output'] = {'run': run['run_id'], 'result': run['result'], 'issue': issue}
    if submitted:
        routing.refresh_review_recovery(state, order)
    else:
        coordinate(state, order, run, issue)
    routing.add_candidates(state, order, parsed['candidates'])
    return {'run_id': run['run_id'], 'legal': legal, 'reason': reason,
            'outcome': 'registered', 'result': run['result'], 'opened': list(state.opened)}
