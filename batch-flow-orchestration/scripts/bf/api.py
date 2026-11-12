"""Public API of batch-flow v2 core (design 17). Every mutation is one transaction = one revision + one event."""
import re
import time
from pathlib import Path

from . import evidence as evidence_mod, environment, gate, model, policy, register, routing, context, snap, permissions, review_budget, cycles
from .store import BfError, State, load, need, now, transact

__all__ = ['driver_event', 'watch', 'watch_lines', 'driver_status', 'retry', 'escalate', 'report_text', 'abandon', 'cli_failed', 'report', 'BfError', 'init', 'load', 'orders', 'runnable', 'assign', 'start', 'deliver', 'release', 'answer',
           'revise_plan', 'stop', 'resume', 'broken_evidence']


def init(base, plan, approval, expected_revision=None, request_id=None, actor='user'):
    ref = model.approval_ref(approval)

    def fn(st):
        st.plan = model.normalize(plan)
        environment.register(st.base, st.plan)
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


def start(base, order_id, conversation_id=None, expected_revision=None, request_id=None, actor='driver', dispatch=None):
    def fn(st):
        need(not st.stopped, 'stopped by user')
        o = routing.get(st, order_id)
        need(not permissions.missing(st.plan, o), 'required protected permissions are not installed')
        review_budget.require_dispatch(st, o)
        a = policy.assign(st, order_id)
        if o['type'] == 'review':
            need(conversation_id not in context.author_sessions(st, o), 'a session that authored the work cannot review it (I4)')
        run = new_run(order_id, len(o['runs']) + 1, a['stage'], a['tool'], a['model'], a['effort'], conversation_id,
                      a['session']['mode'], a['session']['rule'])
        if cycles.enabled(st.plan, o['target']):
            run['execution_model'] = cycles.MODEL
        protocol = o.get('control_protocol')
        if protocol in ('intent-v1', 'delivery-v2', 'review-v1'):
            run['delivery_protocol'] = protocol
            run['budget_managed'] = not cycles.enabled(st.plan, o['target'])
        if cycles.enabled(st.plan, o['target']) and o['type'] in ('implement', 'repair'):
            cycles.reserve(st, o, run)
        if dispatch is not None:
            run['dispatch'] = {key: {'path': str(Path(path).resolve()), 'sha256': gate_hash(path)}
                               for key, path in dispatch.items()}
            need({'input', 'card'} <= set(run['dispatch']) <= {'input', 'card', 'context'} and all(x['sha256'] for x in run['dispatch'].values()),
                 'dispatch input and card must exist before starting')
            run['card'] = run['dispatch']['card']['path']
        if st.plan.get('repos'):
            try:
                run['baseline'] = snap.baseline(st.plan)
            except (OSError, BfError) as error:
                run['observations'] = ['基线采集失败：%s' % error]
        o['runs'].append(run)
        o['state'] = 'running'
        o.pop('fresh_next', None)
        return run['run_id']
    return transact(base, 'start', actor, fn, expected_revision, request_id)


def preflight(base):
    """Resolve known dispatch deficiencies without spending a role run."""
    state = load(base)
    candidates = [oid for oid in runnable(state, 1000) if permissions.missing(state.plan, state.orders[oid])
                  or review_budget.dispatch_shortfalls(state, state.orders[oid]) or _environment_drift(state, oid)]
    if not candidates:
        return False
    def fn(st):
        for oid in candidates:
            o = st.orders[oid]
            drift = _environment_drift(st, oid)
            if drift:
                routing.pause(st, o, drift, 'environment_drift',
                              missing='负责人重跑 bf env-resolve --smoke 并经 revise 更新执行环境登记；不消耗执行轮次',
                              waits_on=[{'revision': True}])
                continue
            paths = permissions.missing(st.plan, o)
            if paths:
                routing.open_decide(st, o, 'authorization_required', {'paths': paths})
            deficits = review_budget.dispatch_shortfalls(st, o)
            if deficits:
                routing.pause(st, o, 'review budget coverage: %s' % deficits, 'review_budget_shortfall',
                              missing='补齐独立审阅配套预算后恢复；不先消耗实施或返修轮次')
        return True
    return transact(base, 'preflight', 'driver', fn)


def _environment_drift(state, order_id):
    """Pinned environment no longer matches this call (profile, host rules or needs changed)."""
    o = state.orders[order_id]
    if o['type'] not in policy.STAFFED:
        return None
    try:
        assignment = policy.assign(state, order_id)
    except BfError:
        return None
    access = environment.access_for(state.plan, o['type'])
    return environment.call(state.plan, state.base, assignment['tool'], access, o['target'], o['type'])[1]


def deliver(base, order_id, run_id, text, events=(), decision=None, conversation_id=None, session_kb=None,
            version=None, card=None, expected_revision=None, request_id=None, actor='driver', segment=None):
    def register_segment(st):
        previous_pauses = {key: value.get('ops') for key, value in st.orders.items()}
        o = routing.get(st, order_id)
        run = next((r for r in o['runs'] if r['run_id'] == run_id), None)
        need(run is not None and run['ended'] is None, 'no open run %s on %s' % (run_id, order_id))
        p = gate.parse(text, st.base)
        gate.scan(run, events, environment.entries(st.plan))
        legal, reason = gate.judge(st, o, run, p, decision, text)
        _capture(st, run)
        # Changes are observations for reviewers, never an engineering verdict.
        run['segments'] += 1
        if conversation_id:
            run['session'] = conversation_id
        if session_kb is not None:
            run['session_kb'] = (run.get('session_kb') or 0) + session_kb
        run['card'] = card or run['card']
        run['last_ts'] = time.time()
        run['evidence'].update((path, gate_hash(path)) for path in p['evidence'])
        run['citations'] = p['citations']
        run['version'] = p['version'] or version or (' '.join(run['snapshot']) if run['snapshot'] else run.get('version'))
        if run.get('delivery_protocol') == 'delivery-v2':
            from . import execution
            return execution.deliver_status(st, o, run, p, text, version)
        if run.get('delivery_protocol') == 'intent-v1':
            from . import execution
            return execution.deliver(st, o, run, p, text, version)
        if run.get('delivery_protocol') == 'review-v1' and p['result'] not in ('RELEASE', 'FIX_REQUIRED'):
            legal, reason = False, '新审阅协议须交付 RELEASE/FIX_REQUIRED；其他结果需负责人处置'
        if run.get('delivery_protocol') == 'review-v1' and not legal:
            from . import execution
            run.update(ended=now(), ended_ts=time.time(), delivery=text, result='HANDOFF', stop_reason=reason)
            o['state'] = 'open'
            execution.coordinate(st, o, run, reason)
            return {'run_id': run_id, 'legal': False, 'reason': reason, 'outcome': 'registered', 'result': 'HANDOFF', 'opened': list(st.opened)}
        k = run['continuations'] + 1
        out = {'run_id': run_id, 'legal': legal, 'reason': reason if not legal else p['result'], 'opened': []}
        if not legal and k <= st.plan['policy']['continue_limit']:
            run['continuations'] = k
            out['outcome'] = 'continue'
            return out
        run['ended'] = now()
        run['ended_ts'] = time.time()
        run['delivery'] = text
        run['result'] = p['result'] if legal else 'FAILED'
        run['stop_reason'] = p['why'] or ('未完成' if not legal else '')
        run['exempt'] = False  # Transport failures and coordination have explicit accounting rules.
        run['evidence'].update((path, gate_hash(path)) for path in p['evidence'])
        run['citations'] = p['citations']
        run['version'] = p['version'] or version or (' '.join(run['snapshot']) if run['snapshot'] else run.get('version'))
        o['state'] = 'open'
        register.on_registered(st, o, run, p, legal, decision)
        out.update(outcome='registered', result=run['result'], opened=list(st.opened),
                   paused=[key for key, value in st.orders.items() if value.get('ops') and value['ops'] != previous_pauses.get(key)])
        return out
    def fn(st):
        if segment is None:
            return register_segment(st)
        need(type(segment) is int and segment > 0, 'segment must be a positive integer')
        o = routing.get(st, order_id)
        run = next((r for r in o['runs'] if r['run_id'] == run_id), None)
        need(run is not None, 'unknown run')
        receipts = run.setdefault('segment_receipts', {})
        if str(segment) in receipts:
            return receipts[str(segment)]
        need(segment == run['segments'] + 1, 'segment is out of sequence')
        out = register_segment(st)
        receipts[str(segment)] = out
        return out
    return transact(base, 'deliver', actor, fn, expected_revision, request_id)


def _capture(st, run):
    """Append version observations on every exit, including interrupted calls. Never decide acceptance."""
    run.setdefault('observations', [])
    run.setdefault('evidence', {})
    run.setdefault('snapshot', [])
    if run.get('baseline') is None:
        return run.get('changed', {})
    try:
        changes = snap.changed(st.plan, run['baseline'])
    except (OSError, BfError) as error:
        run['observations'].append('变更采集失败：%s' % error)
        return run.get('changed', {})
    # Do not discard observations from earlier segments of the same call.
    run['changed'] = {repo: sorted(set(paths) | set(run.get('changed', {}).get(repo, [])))
                      for repo, paths in dict(run.get('changed', {}), **changes).items()}
    for repo, rels in sorted(run['changed'].items()):
        try:
            path = evidence_mod.snapshot(st.base, repo, rels)['evidence']
            run['snapshot'].append(path)
            run['evidence'][path] = gate_hash(path)
        except (OSError, BfError) as error:
            run['observations'].append('快照采集失败 %s：%s' % (repo, error))
    return run['changed']


def dispatch_error(base, order_id, reason):
    """An incomplete dispatch is a tool fault, before a model run or budget charge."""
    return transact(base, 'dispatch_error', 'driver', lambda st: routing.pause(
        st, routing.get(st, order_id), reason, 'tool_failure', '修复工单导出/适配器输入后重试'))


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
        o['state'] = 'done'
        return order_id
    return transact(base, 'release', actor, fn, expected_revision, request_id)


def answer(base, order_id, text, approve, expected_revision=None, request_id=None, actor='user'):
    def fn(st):
        register.answer(st, order_id, text, approve, now())
        return order_id
    return transact(base, 'answer', actor, fn, expected_revision, request_id)


def revise_plan(base, plan, approval, expected_revision=None, request_id=None, actor='user', revalidate=()):
    ref = model.approval_ref(approval)

    def fn(st):
        register.revise(st, plan, ref, revalidate=revalidate)
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


def abandon(base, order_id, run_id, reason, expected_revision=None, request_id=None, actor='driver', not_started=False):
    """A lost process is INVALID; prior received segments retain their charge and normal recovery routing."""
    def fn(st):
        o = routing.get(st, order_id)
        run = next((r for r in o['runs'] if r['run_id'] == run_id), None)
        need(run is not None and run['ended'] is None, 'no open run %s on %s' % (run_id, order_id))
        _capture(st, run)
        run.update(ended=now(), result='INVALID', stop_reason=reason, exempt=not bool(run.get('segments')))
        o['state'] = 'open'
        if run.get('execution_cycle'):
            run['exempt'] = bool(not_started and not run.get('segments'))
        if run.get('budget_managed'):
            from . import execution
            run['exempt'] = bool(not_started and not run.get('segments'))
            if run['exempt']:
                o.setdefault('settlements', {})[run_id] = {'charge': 0, 'reason': reason, 'source': 'mechanical_not_started'}
            else:
                execution.coordinate(st, o, run, reason)
                return run_id
        tail = 0
        for r in reversed(o['runs'][o.get('invalid_floor', 0):]):
            if r['result'] != 'INVALID':
                break
            tail += 1
        if tail >= policy.rounds(st.plan, o['target'], 'invalid_retry', 2) and not routing._blocked_by_user_or_lead(st, o):
            routing.pause(st, o, reason, 'tool_failure')
            o['ops']['run'] = run_id
        routing.check_exhausted(st, o)
        return run_id
    return transact(base, 'abandon', actor, fn, expected_revision, request_id)


def cli_failed(base, order_id, run_id, reason, segment, not_started=False, source=None):
    """Confirmed failed CLI invocation: retry once in this run, then advance the executor ladder.

    Unknown/lost processes and explicit user stops still use abandon, never this recovery path.
    """
    def fn(st):
        from . import execution
        o = routing.get(st, order_id)
        need(o.get('control_protocol') in ('intent-v1', 'delivery-v2'), 'CLI recovery requires an execution order')
        run = next((r for r in o['runs'] if r['run_id'] == run_id), None)
        need(run is not None, 'unknown run')
        receipts = run.setdefault('segment_receipts', {})
        if str(segment) in receipts:
            return receipts[str(segment)]
        need(run['ended'] is None and o['state'] == 'running', 'no running call')
        need(type(segment) is int and segment == run['segments'] + 1, 'segment is out of sequence')
        _capture(st, run)
        failures = run.setdefault('cli_failures', [])
        consecutive = bool(failures and failures[-1]['segment'] == segment - 1)
        failures.append({'segment': segment, 'reason': reason, 'not_started': bool(not_started),
                         'source': source, 'ended': now()})
        run['segments'] = segment
        if not consecutive:
            out = {'outcome': 'continue', 'kind': 'cli_retry', 'reason': reason}
        else:
            whole_unstarted = len(failures) == run['segments'] and all(f['not_started'] for f in failures)
            run.update(ended=now(), ended_ts=time.time(), result='INVALID', stop_reason=reason, exempt=whole_unstarted)
            o['state'] = 'open'
            if whole_unstarted and not cycles.enabled(st.plan, o['target']):
                o.setdefault('settlements', {})[run_id] = {
                    'charge': 0, 'reason': reason, 'source': 'mechanical_not_started'}
            selected = policy.next_performer(st, o, run['performer'])
            if selected:
                if not whole_unstarted and not cycles.enabled(st.plan, o['target']):
                    execution.coordinate(st, o, run, reason, accounting=True)
                o['performer'] = selected
                o['fresh_next'] = True
                o['input']['recovery'] = {'source': run_id, 'instruction':
                    '上一执行者连续两次 CLI 调用异常结束；按计划阶梯接手原工单。'
                    '先核对原始调用记录、当前文件及可能遗留的任务，确认已做与未完成事项后继续；'
                    '不把异常结束视作交付或验证通过。调用记录：%s' % (st.base.resolve() / 'records' / 'runs' / run_id)}
            else:
                execution.coordinate(st, o, run, '连续两次 CLI 调用异常结束，已无下一执行者：' + reason)
            out = {'outcome': 'registered', 'result': 'INVALID', 'next_performer': selected}
        receipts[str(segment)] = out
        return out
    return transact(base, 'cli_failed', 'driver', fn)


def restore_review(base, order_id, run_id, approval, expected_revision=None, request_id=None, actor='user'):
    """Recover a submitted repair reopened by a review pinned to a pre-repair version.

    This repairs routing only: the submitted work still requires independent review.
    Run facts, settlements, findings and all budget limits remain unchanged.
    """
    ref = model.approval_ref(approval)

    def fn(st):
        repair = routing.get(st, order_id)
        need(repair['type'] == 'repair' and repair['state'] == 'open', 'expected an open repair')
        need(not any(o['state'] == 'running' for o in st.orders.values()), 'collect running calls first')
        need(repair['runs'] and repair['runs'][-1]['run_id'] == run_id, 'only the latest repair call can be restored')
        run = repair['runs'][-1]
        need(run['ended'] and run['result'] == 'SUBMITTED', 'latest repair must already be submitted')
        need(not policy.unsettled(st, repair), 'settle pending calls first')
        need((repair.get('output') or {}).get('run') == run_id, 'repair output does not match submitted call')
        review = routing.get(st, repair['input']['review_order'])
        need(review['type'] == 'review' and review['state'] == 'open', 'expected an open independent review')
        need(routing.current_work(st, repair) and routing.current_work(st, review), 'obsolete contract')
        recovery = review['input'].get('recovery') or {}
        source = st.orders.get(recovery.get('source'))
        need(source is not None and source['type'] == 'decide' and source['state'] == 'done' and source['runs'],
             'missing prior recovery instruction source')
        need(source['runs'][-1]['ended'] <= run['started'], 'recovery instruction is not older than repair')
        need(review['runs'] and review['runs'][-1]['result'] == 'FIX_REQUIRED', 'expected a failed stale review')
        fixed = re.search(r'固定版本[：:]\s*([^\n]+)', review['runs'][-1].get('delivery', ''))
        def refs(text):
            return set(re.findall(r'/[^\s\[\]()，；。]+', text or ''))
        reviewed = refs(fixed.group(1)) if fixed else set()
        submitted = refs(run.get('version')) | set(run.get('snapshot', []))
        need(reviewed and submitted and not reviewed & submitted,
             'review is not demonstrably pinned to a different version')
        older = [r for o in st.orders.values() if o['target'] == repair['target'] and o['type'] in ('implement', 'repair')
                 for r in o['runs'] if r.get('ended') and r['ended'] <= run['started'] and r.get('version')]
        need(any(reviewed & (refs(r['version']) | set(r.get('snapshot', []))) for r in older),
             'review version is not a known older delivery')
        need(all(st.orders[n]['state'] in routing.SATISFIED for n in repair['needs'] if n in st.orders),
             'repair still has unresolved dependencies')
        prior = repair.pop('ops', None)
        repair.pop('awaiting_disposition', None)
        repair['state'] = 'done'
        repair.setdefault('ops_log', []).append({'op': 'restore_review', 'run': run_id,
            'review': review['id'], 'prior_problem': prior, 'approval': ref, 'at': now()})
        routing.refresh_review_recovery(st, repair)
        return {'repair': order_id, 'run': run_id, 'review': review['id'], 'budget_changed': False}
    return transact(base, 'restore_review', actor, fn, expected_revision, request_id)


def retry(base, order_id, note, expected_revision=None, request_id=None, actor='front'):
    """Operational fault handled (for example the account was switched): clear the mark; failures cost no budget."""
    def fn(st):
        o = routing.get(st, order_id)
        need(isinstance(note, str) and note.strip(), 'recovery requires a concrete operational note')
        cycle_work = (cycles.enabled(st.plan, o['target']) and o['type'] in ('implement', 'repair')
                      and o['state'] == 'open' and not o.get('awaiting_disposition'))
        stranded = (cycle_work and o.get('performer') is None
                    and cycles.retry_performer(st, o) is not None)
        need(o.get('ops') or stranded, 'order %s has no pending operational problem' % order_id)
        lead_owned = bool(o.get('ops')) and routing.ops_owner(o) == 'lead'
        ops = o.pop('ops', {'problem': 'execution performer unset after exhausted rounds'})
        o.setdefault('ops_log', []).append({'op': 'retry', 'note': note, 'problem': ops['problem'], 'at': now()})
        o['invalid_floor'] = len(o['runs'])
        if o['type'] in ('decide', 'triage'):
            o['coordination_floor'] = len(o['runs'])
        elif lead_owned or ops.get('kind') in ('no_progress', 'coordination_limit') or o.get('awaiting_disposition'):
            # The lead still owns this order's disposition: hand the operational fact back, never just clear it.
            routing.open_decide(st, o, 'operational_recovery', {'note': note, 'problem': ops['problem']}, force=True)
        if stranded:
            o['performer'] = cycles.retry_performer(st, o)
        return order_id
    return transact(base, 'retry', actor, fn, expected_revision, request_id)


def escalate(base, order_id, note, expected_revision=None, request_id=None, actor='front'):
    """The fault looks technical: hand it to the technical lead as a decide (trigger tool_failure)."""
    def fn(st):
        o = routing.get(st, order_id)
        stuck = routing.disposition(st, o) is None   # historical state without any exit
        need(o.get('ops') or stuck, 'order %s has no pending operational problem' % order_id)
        need(isinstance(note, str) and note.strip(), 'recovery requires a concrete operational note')
        trigger = 'tool_failure' if o.get('ops') and routing.ops_owner(o) == 'front' else 'operational_recovery'
        problem = o.pop('ops')['problem'] if o.get('ops') else '等待负责人处置但无裁决单（历史状态）'
        o.setdefault('ops_log', []).append({'op': 'escalate', 'note': note, 'problem': problem, 'at': now()})
        o['invalid_floor'] = len(o['runs'])
        if o['type'] in ('decide', 'triage'):
            o['coordination_floor'] = len(o['runs'])
            o['fresh_next'] = True
        else:
            routing.open_decide(st, o, trigger, {'problem': problem, 'note': note}, force=True)
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
            item = {'observations': r.get('observations', []), 'order': o['id'], 'type': o['type'], 'target': o['target'], 'run': r['run_id'],
                    'performer': r['performer'], 'started': r['started'],
                    'execution_cycle': r.get('execution_cycle'),
                    'budget_pending': bool(r.get('budget_managed') and policy.charge(o, r) is None)}
            if r['ended']:
                if r['performer'] == 'imported':
                    continue
                charge = ('coordination' if o['type'] in ('decide', 'triage') else
                          'credited' if r['run_id'] in o.get('budget_credits', {}) else
                          'pending' if r.get('budget_managed') and policy.charge(o, r) is None else
                          'exempt' if policy.charge(o, r) == 0 else 'technical')
                item.update(result=r['result'], stop_reason=r['stop_reason'], ended=r['ended'], exempt=r['exempt'],
                            budget_charge=charge)
                ended.append(item)
            else:
                item.update(segments=r['segments'], edited=r['edited'])
                running.append(item)
    ask = [{'order': o['id'], 'target': o['target'], 'question': o['input'].get('question', ''),
            'user_request': o['input'].get('user_request')}
           for o in orders(st, type='ask_user', state_='open')]
    plan = st.plan
    budgets, finished = {}, set()
    for d in plan['deliverables']:
        impls = [o for o in routing.work_for(st, d) if o['type'] == 'implement']
        complete = impls and all(o['state'] == 'done' for o in impls)
        if cycles.enabled(plan, d):
            complete = complete and all(o['state'] == 'done' for o in routing.work_for(st, d)) and all(
                o['state'] == 'done' for o in orders(st, type='review')
                if o['target'] in plan['review_groups'] and routing.current_work(st, o)
                and d in plan['review_groups'][o['target']]['deliverables'])
        if complete:
            finished.add(d)
            continue
        owner = impls[-1] if impls else next((o for o in orders(st, type='implement', target=d)), None)
        budgets[d] = sum(max(n - policy.committed(st, owner).get(s, 0), 0) for s, n in policy.ladder(plan, owner)) if owner else 0
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
        if release and release['state'] == 'done':
            state = 'done'
        elif b['id'] not in inside:
            state = 'outside'
        elif started or first_open:
            state, first_open = 'active', False
        else:
            state = 'pending'
        batches.append({'id': b['id'], 'title': b.get('title', ''), 'state': state, 'done': done_n, 'total': len(members)})
    ops = []
    for o in routing.sorted_orders(st):
        where = routing.disposition(st, o)
        if where == 'ops':
            ops.append({'order': o['id'], 'label': label(plan, o['target']), 'problem': o['ops']['problem'],
                        'kind': o['ops'].get('kind', 'tool_failure'), 'missing': o['ops'].get('missing', ''),
                        'owner': routing.ops_owner(o)})
        elif where is None:   # committed before invariant I-D existed; only escalate can repair it
            ops.append({'order': o['id'], 'label': label(plan, o['target']), 'kind': 'no_disposition', 'owner': 'lead',
                        'problem': '工单等待负责人处置，但没有待处理裁决或受阻记录（历史状态）',
                        'missing': '前端 escalate 带事实重开负责人裁决'})
    for item in ask:
        item['label'] = label(plan, item['target'])
    ended.sort(key=lambda x: x['ended'])
    return {'revision': st.revision, 'stopped': st.stopped, 'running': running, 'recent': ended[-recent:], 'ask_user': ask,
            'execution_rounds': cycles.report(st),
            'budgets': budgets, 'budget_pending': [r for r in running + ended if r['budget_pending']], 'runnable': runnable_ids, 'roles': plan['roles'], 'next': nxt, 'batches': batches,
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
        for key in dict.fromkeys(res.get('paused', []) + ([o['id']] if o.get('ops') else [])):
            paused = st.orders.get(key)
            if paused and paused.get('ops'):
                parts.append('受阻待处理：%s：%s' % (label(plan, paused['target']), paused['ops']['problem']))
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
    ask = ['%s：%s' % (x['label'], strip(x['question'])) for x in r['ask_user']]
    out.append('待你决定：' + ('；'.join(ask) if ask else '无'))
    out.append('受阻待处理：' + ('；'.join('%s：%s%s' % (x['label'], strip(x['problem']),
               '；最小缺项：' + strip(x['missing']) if x.get('missing') else '') for x in r['ops']) or '无'))
    roles = r['roles']
    out.append('角色：' + '；'.join('%s %s / %s' % (ROLE_NAMES.get(k, k), v['tool'], v['model']) for k, v in roles.items()))
    done_n = len([b for b in r['batches'] if b['state'] == 'done'])
    active = ['%s（%d/%d）' % (b['title'] or b['id'], b['done'], b['total']) for b in r['batches'] if b['state'] == 'active']
    pending = len([b for b in r['batches'] if b['state'] == 'pending'])
    outside = len([b for b in r['batches'] if b['state'] == 'outside'])
    out.append('批次：已完成 %d/%d；进行中 %s；待开始 %d；授权范围外 %d' % (done_n, len(r['batches']), '、'.join(active) or '无', pending, outside))
    return '\n'.join(l for l in out if l) + '\n'
