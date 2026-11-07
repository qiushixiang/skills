"""Public API of batch-flow v2 core (design 17). Every mutation is one transaction = one revision + one event."""
import time

from . import gate, model, policy, register, routing, context
from .store import BfError, State, load, need, now, transact

__all__ = ['abandon', 'report', 'BfError', 'init', 'load', 'orders', 'runnable', 'assign', 'start', 'deliver', 'release', 'answer',
           'revise_plan', 'stop', 'resume', 'broken_evidence']


def init(base, plan, approval, expected_revision=None, request_id=None, actor='user'):
    ref = model.approval_ref(approval)

    def fn(st):
        st.plan = model.normalize(plan)
        st.plan['revision'] = 1
        st.plan['approval'] = ref
        st.plan_dirty = True
        routing.sync_plan(st)
        return st.revision + 1
    return transact(base, 'init', actor, fn, expected_revision, request_id, init=True)


def orders(state, type=None, target=None, state_=None):
    return [o for o in routing.sorted_orders(state) if (type is None or o['type'] == type)
            and (target is None or o['target'] == target) and (state_ is None or o['state'] == state_)]


def runnable(state, parallel=1):
    return routing.runnable(state, parallel)


def assign(state, order_id):
    return policy.assign(state, order_id)


def broken_evidence(state):
    return routing.broken_paths(state)


def new_run(order_id, n, performer, tool, model_name, effort, session, mode, rule):
    return {'run_id': '%s-r%d' % (order_id, n), 'performer': performer, 'tool': tool, 'model': model_name, 'effort': effort,
            'session': session, 'session_mode': mode, 'session_rule': rule, 'started': now(), 'started_ts': time.time(),
            'last_ts': time.time(), 'ended': None, 'result': None, 'stop_reason': None, 'evidence': {}, 'version': None,
            'card': None, 'segments': 0, 'continuations': 0, 'exempt': False, 'edited': False, 'failed': False,
            'entry': None, 'session_kb': None, 'env_rejected': False}


def start(base, order_id, conversation_id=None, expected_revision=None, request_id=None, actor='driver'):
    def fn(st):
        need(not st.stopped, 'stopped by user')
        o = routing.get(st, order_id)
        a = policy.assign(st, order_id)
        if o['type'] == 'review':
            need(conversation_id not in context.author_sessions(st, o), 'a session that authored the work cannot review it (I4)')
        run = new_run(order_id, len(o['runs']) + 1, a['stage'], a['tool'], a['model'], a['effort'], conversation_id,
                      a['session']['mode'], a['session']['rule'])
        o['runs'].append(run)
        o['state'] = 'running'
        o.pop('fresh_next', None)
        return run['run_id']
    return transact(base, 'start', actor, fn, expected_revision, request_id)


def deliver(base, order_id, run_id, text, events=(), decision=None, conversation_id=None, session_kb=None,
            version=None, card=None, expected_revision=None, request_id=None, actor='driver'):
    def fn(st):
        o = routing.get(st, order_id)
        run = next((r for r in o['runs'] if r['run_id'] == run_id), None)
        need(run is not None and run['ended'] is None, 'no open run %s on %s' % (run_id, order_id))
        p = gate.parse(text, st.base)
        edited_now = gate.scan(run, events, model.env_for(st.plan, run['tool']))
        legal, reason = gate.judge(st, o, run, p, decision, text)
        run['segments'] += 1
        if conversation_id:
            run['session'] = conversation_id
        if session_kb is not None:
            run['session_kb'] = session_kb
        run['card'] = card or run['card']
        if not legal and p['why'].startswith('外部阻塞'):
            run['env_rejected'] = True
        since, run['last_ts'] = run['last_ts'], time.time()
        k = run['continuations'] + 1
        out = {'run_id': run_id, 'legal': legal, 'reason': reason if not legal else p['result'], 'opened': []}
        if not legal and k <= st.plan['policy']['continue_limit'] and (k == 1 or edited_now or gate.new_records(st.base, since)):
            run['continuations'] = k
            out['outcome'] = 'continue'
            return out
        run['ended'] = now()
        run['result'] = p['result'] if legal else 'FAILED'
        run['stop_reason'] = p['why'] or ('未完成' if not legal else '')
        run['exempt'] = bool(legal and run['result'] == 'WAIT_EVIDENCE' and not run['edited']
                             and not gate.new_records(st.base, run['started_ts']))
        run['evidence'] = dict((path, gate_hash(path)) for path in p['evidence'])
        run['version'] = p['version'] or version
        o['state'] = 'open'
        register.on_registered(st, o, run, p, legal, decision)
        out.update(outcome='registered', result=run['result'], opened=list(st.opened))
        return out
    return transact(base, 'deliver', actor, fn, expected_revision, request_id)


def gate_hash(path):
    from .store import sha256_file
    return sha256_file(path)


def release(base, order_id, expected_revision=None, request_id=None, actor='driver'):
    def fn(st):
        o = routing.get(st, order_id)
        need(o['type'] == 'release' and o['state'] == 'open', '%s is not an open release order' % order_id)
        need(not st.stopped, 'stopped by user')
        pending = [n for n in o['needs'] if st.orders[n]['state'] not in routing.SATISFIED]
        need(not pending, 'release blocked by %s' % ', '.join(pending))
        need(not routing.broken_paths(st), 'evidence hash mismatch: release refused')
        o['state'] = 'done'
        return order_id
    return transact(base, 'release', actor, fn, expected_revision, request_id)


def answer(base, order_id, text, approve, expected_revision=None, request_id=None, actor='user'):
    def fn(st):
        register.answer(st, order_id, text, approve, now())
        return order_id
    return transact(base, 'answer', actor, fn, expected_revision, request_id)


def revise_plan(base, plan, approval, expected_revision=None, request_id=None, actor='user'):
    ref = model.approval_ref(approval)

    def fn(st):
        register.revise(st, plan, ref)
        return st.plan['revision']
    return transact(base, 'revise_plan', actor, fn, expected_revision, request_id)


def _set_stopped(base, value, op, expected_revision, request_id, actor):
    def fn(st):
        st.stopped = value
        return value
    return transact(base, op, actor, fn, expected_revision, request_id)


def stop(base, expected_revision=None, request_id=None, actor='user'):
    return _set_stopped(base, True, 'stop', expected_revision, request_id, actor)


def resume(base, expected_revision=None, request_id=None, actor='user'):
    return _set_stopped(base, False, 'resume', expected_revision, request_id, actor)


def abandon(base, order_id, run_id, reason, expected_revision=None, request_id=None, actor='driver'):
    """A run whose process is lost: INVALID and free of charge, the order becomes dispatchable again."""
    def fn(st):
        o = routing.get(st, order_id)
        run = next((r for r in o['runs'] if r['run_id'] == run_id), None)
        need(run is not None and run['ended'] is None, 'no open run %s on %s' % (run_id, order_id))
        run.update(ended=now(), result='INVALID', stop_reason=reason, exempt=True)
        o['state'] = 'open'
        tail = 0
        for r in reversed(o['runs']):
            if r['result'] != 'INVALID':
                break
            tail += 1
        if tail >= policy.rounds(st.plan, o['target'], 'invalid_retry', 2) and not routing._blocked_by_user_or_lead(st, o):
            if o['type'] in ('decide', 'triage'):
                routing.fail_to_user(st, o, 'tool_failure')
            else:
                routing.open_decide(st, o, 'tool_failure', {'problem': reason})
        return run_id
    return transact(base, 'abandon', actor, fn, expected_revision, request_id)


def report(base, recent=10):
    """Structured summary for the chat front: in-flight runs, latest finished runs, open questions, budgets left."""
    st = load(base)
    running, ended = [], []
    for o in routing.sorted_orders(st):
        for r in o['runs']:
            item = {'order': o['id'], 'type': o['type'], 'target': o['target'], 'run': r['run_id'],
                    'performer': r['performer'], 'started': r['started']}
            if r['ended']:
                item.update(result=r['result'], stop_reason=r['stop_reason'], ended=r['ended'], exempt=r['exempt'])
                ended.append(item)
            else:
                item.update(segments=r['segments'], edited=r['edited'])
                running.append(item)
    ask = [{'order': o['id'], 'target': o['target'], 'question': o['input'].get('question', '')}
           for o in orders(st, type='ask_user', state_='open')]
    budgets = {}
    for d in st.plan['deliverables']:
        left = 0
        for o in orders(st, type='implement', target=d):
            if o['state'] != 'superseded':
                spent = policy.used(o)
                left += sum(max(n - spent.get(s, 0), 0) for s, n in policy.ladder(st.plan, o))
        budgets[d] = left
    ended.sort(key=lambda x: x['ended'])
    return {'revision': st.revision, 'stopped': st.stopped, 'running': running, 'recent': ended[-recent:], 'ask_user': ask,
            'budgets': budgets, 'runnable': routing.runnable(st, 10),
            'broken_evidence': routing.broken_paths(st)}
