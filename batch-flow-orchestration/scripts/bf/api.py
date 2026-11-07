"""Public API of batch-flow v2 core (design 17). Every mutation is one transaction = one revision + one event."""
import re
import time
from pathlib import Path

from . import gate, model, policy, register, routing, context
from .store import BfError, State, load, need, now, transact

__all__ = ['driver_event', 'watch', 'watch_lines', 'driver_status', 'retry', 'escalate', 'report_text', 'abandon', 'report', 'BfError', 'init', 'load', 'orders', 'runnable', 'assign', 'start', 'deliver', 'release', 'answer',
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


def _set_stopped(base, value, op, expected_revision, request_id, actor, kill=False):
    def fn(st):
        st.stopped = value
        st.kill = kill
        return value
    return transact(base, op, actor, fn, expected_revision, request_id)


def stop(base, expected_revision=None, request_id=None, actor='user', now=False):
    """Stop dispatching and continuing; now=True also asks the driver to kill in-flight calls."""
    return _set_stopped(base, True, 'stop', expected_revision, request_id, actor, kill=bool(now))


def resume(base, expected_revision=None, request_id=None, actor='user'):
    return _set_stopped(base, False, 'resume', expected_revision, request_id, actor)


def driver_event(base, kind, pid=None):
    """Record driver_start / driver_exit as a (state-neutral) transaction so the event stream shows the driver's life."""
    import os
    return transact(base, kind, 'driver', lambda st: pid or os.getpid())


def abandon(base, order_id, run_id, reason, expected_revision=None, request_id=None, actor='driver'):
    """A run whose process is lost: INVALID and free of charge, the order becomes dispatchable again."""
    def fn(st):
        o = routing.get(st, order_id)
        run = next((r for r in o['runs'] if r['run_id'] == run_id), None)
        need(run is not None and run['ended'] is None, 'no open run %s on %s' % (run_id, order_id))
        run.update(ended=now(), result='INVALID', stop_reason=reason, exempt=True)
        o['state'] = 'open'
        tail = 0
        for r in reversed(o['runs'][o.get('invalid_floor', 0):]):
            if r['result'] != 'INVALID':
                break
            tail += 1
        if tail >= policy.rounds(st.plan, o['target'], 'invalid_retry', 2) and not routing._blocked_by_user_or_lead(st, o):
            if o['type'] in ('decide', 'triage'):
                routing.fail_to_user(st, o, 'tool_failure')
            else:  # operational: the chat front decides between retry and escalate (design 22)
                o['ops'] = {'problem': reason, 'at': now(), 'run': run_id}
        return run_id
    return transact(base, 'abandon', actor, fn, expected_revision, request_id)


def retry(base, order_id, note, expected_revision=None, request_id=None, actor='front'):
    """Operational fault handled (for example the account was switched): clear the mark; failures cost no budget."""
    def fn(st):
        o = routing.get(st, order_id)
        need(o.get('ops'), 'order %s has no pending operational problem' % order_id)
        o.setdefault('ops_log', []).append({'op': 'retry', 'note': note, 'problem': o.pop('ops')['problem'], 'at': now()})
        o['invalid_floor'] = len(o['runs'])
        return order_id
    return transact(base, 'retry', actor, fn, expected_revision, request_id)


def escalate(base, order_id, note, expected_revision=None, request_id=None, actor='front'):
    """The fault looks technical: hand it to the technical lead as a decide (trigger tool_failure)."""
    def fn(st):
        o = routing.get(st, order_id)
        need(o.get('ops'), 'order %s has no pending operational problem' % order_id)
        problem = o.pop('ops')['problem']
        o.setdefault('ops_log', []).append({'op': 'escalate', 'note': note, 'problem': problem, 'at': now()})
        o['invalid_floor'] = len(o['runs'])
        routing.open_decide(st, o, 'tool_failure', {'problem': problem, 'note': note})
        return order_id
    return transact(base, 'escalate', actor, fn, expected_revision, request_id)


VERBS = {'implement': '实施', 'repair': '返修', 'review': '审阅', 'decide': '裁决', 'triage': '分诊', 'release': '收尾'}
ROLE_NAMES = {'executor': '执行者', 'backup_executor': '备用执行者', 'lead': '技术负责人', 'reviewer': '审阅者', 'third_party': '第三方审阅者'}


def label(plan, target):
    """Deliverable id plus its title (the batch title when the title is missing or just the id)."""
    batch = next((b for b in plan['batches'] if b['id'] == model.batch_of(plan, target)), {})
    title = plan['deliverables'].get(target, {}).get('title')
    if target in plan['deliverables'] and title and title != target:
        return '%s %s' % (target, title)
    return '%s %s' % (target, batch.get('title') or '') if target else (batch.get('title') or '')


def _who(st, o):
    try:
        a = policy.assign(st, o['id'])
        return {'role': a['role'], 'tool': a['tool'], 'model': a['model'], 'effort': a['effort']}
    except BfError:
        return {'role': None, 'tool': None, 'model': None, 'effort': None}


def report(base, recent=10):
    """Structured summary for the chat front: in-flight runs, latest finished runs, open questions, budgets left."""
    st = load(base)
    running, ended = [], []
    for o in routing.sorted_orders(st):
        for r in o['runs']:
            item = {'order': o['id'], 'type': o['type'], 'target': o['target'], 'run': r['run_id'],
                    'performer': r['performer'], 'started': r['started']}
            if r['ended']:
                if r['performer'] == 'imported':
                    continue
                item.update(result=r['result'], stop_reason=r['stop_reason'], ended=r['ended'], exempt=r['exempt'])
                ended.append(item)
            else:
                item.update(segments=r['segments'], edited=r['edited'])
                running.append(item)
    ask = [{'order': o['id'], 'target': o['target'], 'question': o['input'].get('question', '')}
           for o in orders(st, type='ask_user', state_='open')]
    plan = st.plan
    budgets, finished = {}, set()
    for d in plan['deliverables']:
        impls = [o for o in orders(st, type='implement', target=d) if o['state'] != 'superseded']
        if impls and all(o['state'] == 'done' for o in impls):
            finished.add(d)
            continue
        budgets[d] = sum(max(n - policy.used(o).get(s, 0), 0) for o in impls for s, n in policy.ladder(plan, o))
    for item in running:
        item['label'] = label(plan, item['target'])
        item.update(_who_run(st, item))
    runnable_ids = routing.runnable(st, 10)
    nxt = []
    for oid in runnable_ids:
        o = st.orders[oid]
        if o['type'] != 'release':
            nxt.append(dict(_who(st, o), label=label(plan, o['target']), type=o['type']))
    inside = plan['envelope']['batches']
    batches, first_open = [], True
    for b in plan['batches']:
        members = [d for d, s in plan['deliverables'].items() if s['batch'] == b['id']]
        done_n = len([d for d in members if d in finished])
        release = routing.by_key(st, 'release:' + b['id'])
        started = done_n or any(o['runs'] for o in st.orders.values() if model.batch_of(plan, o['target']) == b['id'])
        if (release and release['state'] == 'done') or (members and done_n == len(members)):
            state = 'done'
        elif b['id'] not in inside:
            state = 'outside'
        elif started or first_open:
            state, first_open = 'active', False
        else:
            state = 'pending'
        batches.append({'id': b['id'], 'title': b.get('title', ''), 'state': state, 'done': done_n, 'total': len(members)})
    ops = [{'order': o['id'], 'label': label(plan, o['target']), 'problem': o['ops']['problem']}
           for o in routing.sorted_orders(st) if o.get('ops')]
    for item in ask:
        item['label'] = label(plan, item['target'])
    ended.sort(key=lambda x: x['ended'])
    return {'revision': st.revision, 'stopped': st.stopped, 'running': running, 'recent': ended[-recent:], 'ask_user': ask,
            'budgets': budgets, 'runnable': runnable_ids, 'roles': plan['roles'], 'next': nxt, 'batches': batches,
            'ops': ops, 'name': plan.get('name', ''), 'driver': driver_status(base), 'broken_evidence': routing.broken_paths(st)}


def _who_run(st, item):
    role = {'primary': 'executor', 'backup': 'backup_executor', 'lead': 'lead'}.get(item['performer'], item['performer'])
    cfg = st.plan['roles'].get(role) or {}
    return {'role': role, 'model': cfg.get('model'), 'tool': cfg.get('tool')}


def _line(item):
    who = ROLE_NAMES.get(item.get('role'), item.get('role') or '')
    extra = '（%s %s / %s）' % (who, item.get('tool') or '', item.get('model') or '') if item.get('model') else ''
    return item['label'] + extra


def driver_status(base):
    """Heartbeat file written by the runner: alive = pid alive and not exited."""
    import json as _json, os
    try:
        d = _json.loads((Path(str(base)) / 'driver.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'alive': False}
    alive = not d.get('exited')
    if alive:
        try:
            os.kill(d['pid'], 0)
        except (OSError, KeyError, TypeError):
            alive = False
    return dict(d, alive=alive)


def watch(base, since=0):
    """(lines, last revision): one human line per notable event after revision `since`."""
    import json as _json
    st = load(base)
    lines, last = [], since
    try:
        raw = (Path(str(base)) / 'events.jsonl').read_text(encoding='utf-8').splitlines()
    except OSError:
        raw = []
    for line in raw:
        try:
            ev = _json.loads(line)
        except ValueError:
            continue
        if ev['revision'] <= since:
            continue
        last = max(last, ev['revision'])
        text = _event_line(st, ev)
        if text:
            lines.append(' '.join(text.split()))
    return lines, last


def watch_lines(base, since_revision=0):
    return watch(base, since_revision)[0]


def _event_line(st, ev):
    op, res, plan = ev['op'], ev.get('result'), st.plan
    if op == 'driver_start':
        return '驱动器启动'
    if op == 'driver_exit':
        return '驱动器退出'
    if op == 'stop':
        return '已停止'
    if op == 'resume':
        return '已恢复'
    if op == 'answer':
        return '已记录你的回答'
    if op == 'release' and res in st.orders:
        t = st.orders[res]['target']
        title = next((b.get('title') for b in plan['batches'] if b['id'] == t), '')
        return '批次放行：%s %s' % (t, title)
    if op == 'abandon' and isinstance(res, str):
        o = st.orders.get(res.rsplit('-r', 1)[0])
        if o and o.get('ops') and o['ops'].get('run') == res:
            return '运维故障待处理（已暂停）：%s：%s' % (label(plan, o['target']), o['ops']['problem'][:80])
        return None
    if op == 'deliver' and isinstance(res, dict):
        o = st.orders.get(res['run_id'].rsplit('-r', 1)[0])
        if not o:
            return None
        parts = []
        if res.get('outcome') == 'registered' and o['type'] in ('implement', 'repair', 'review'):
            run = next((r for r in o['runs'] if r['run_id'] == res['run_id']), {})
            parts.append('%s %s：%s%s' % (VERBS[o['type']], label(plan, o['target']), run.get('result'),
                                        '（%s）' % run['stop_reason'] if run.get('stop_reason') and run.get('result') != 'VERIFIED' else ''))
        for oid in res.get('opened', []):
            ask = st.orders.get(oid)
            if ask and ask['type'] == 'ask_user':
                parts.append('待你决定：%s：%s' % (label(plan, ask['target']), re.sub(r'WO-\d+', '', ask['input'].get('question', ''))[:80]))
        return '；'.join(parts) or None
    return None


def report_text(base):
    """Short Chinese status note for people: titles, never order ids."""
    r = report(base)
    strip = lambda t: re.sub(r'WO-\d+', '', t or '').strip()[:100]
    out = ['%s · %s' % (r['name'] or '任务', time.strftime('%H:%M'))]
    out.append('已停止（stop），不再派发新工单。' if r['stopped'] else '')
    out.append('驱动器：' + ('运行中' if r['driver'].get('alive') else '未运行'))
    out.append('正在做：' + ('；'.join('%s %s' % (VERBS.get(x['type'], x['type']), _line(x)) for x in r['running']) or '无'))
    nxt = r['next'][:3]
    out.append('下一步：' + ('；'.join('%s %s' % (VERBS.get(x['type'], x['type']), _line(x)) for x in nxt) +
                           ('，等共 %d 项' % len(r['next']) if len(r['next']) > 3 else '') if nxt else '无可派发的工作'))
    ask = ['%s：%s' % (x['label'], strip(x['question'])) for x in r['ask_user']] + \
          ['%s：运维故障待处理（%s）' % (x['label'], strip(x['problem'])) for x in r['ops']]
    out.append('待你决定：' + ('；'.join(ask) if ask else '无'))
    roles = r['roles']
    out.append('角色：' + '；'.join('%s %s / %s' % (ROLE_NAMES.get(k, k), v['tool'], v['model']) for k, v in roles.items()))
    done_n = len([b for b in r['batches'] if b['state'] == 'done'])
    active = ['%s（%d/%d）' % (b['title'] or b['id'], b['done'], b['total']) for b in r['batches'] if b['state'] == 'active']
    pending = len([b for b in r['batches'] if b['state'] == 'pending'])
    outside = len([b for b in r['batches'] if b['state'] == 'outside'])
    out.append('批次：已完成 %d/%d；进行中 %s；待开始 %d；授权范围外 %d' % (done_n, len(r['batches']), '、'.join(active) or '无', pending, outside))
    return '\n'.join(l for l in out if l) + '\n'
