"""Compact observations and derived metrics; no agent or product commands."""
import copy
import datetime as dt
from collections import Counter

CAUSES = {'implementation_defect', 'design_defect', 'missing_evidence', 'requirement_change',
          'dependency_failure', 'tool_failure', 'other', 'unknown'}


def accounting_kind(store, attempt):
    return next((r['kind'] for r in store['state'].get('attempt_accounting', [])
                 if r['attempt'] == attempt['id']), 'technical')


def round_root(store, attempt):
    parent = attempt.get('continuation_of') or attempt.get('retry_of')
    return round_root(store, next(a for a in store['state']['attempts'] if a['id'] == parent)) if parent else attempt['id']


def charged_roots(store, budget=None):
    return {round_root(store, a) for a in store['state']['attempts']
            if (budget is None or budget in a['consumes']) and accounting_kind(store, a) not in {'environment', 'registration','withdrawn'}}


def instant(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def seconds(start, end):
    if not start or not end:
        return None
    value = (instant(end)-instant(start)).total_seconds()
    return value if value >= 0 else None


def phase(plan, tid):
    task = plan['tasks'][tid]
    if 'phase' in task:
        return task['phase']
    if task['kind'] == 'review':
        return 'design_review' if any(plan['tasks'][d]['kind'] == 'design' for d in task['depends_on']) else 'implementation_review'
    return {'execute': 'implementation'}.get(task['kind'], task['kind'])


def snapshot(plan, tid):
    task = plan['tasks'][tid]
    return {'kind': task['kind'], 'role': task['role'], 'scope': list(task['scope']), 'phase': phase(plan, tid)}


def observe(store, report, at):
    """Persist only changed states, using first observation rather than inferred dates."""
    tracking = store['state'].setdefault('tracking', {'since': at, 'current': {}, 'events': []})
    values = {}
    for tid, state in report['tasks'].items():
        task = store['plan']['tasks'][tid]
        values['task:'+tid] = {'state': state, 'scope': list(task['scope']),
                              'dependencies': {d: report['tasks'][d] for d in task['depends_on'] if report['tasks'][d] != 'SATISFIED'},
                              'decisions': sorted(did for did, d in store['decisions'].items() if tid in d['blocks'] and report['decision_states'][did] != 'closed')}
    for bid, state in report['batches'].items():
        values['batch:'+bid] = {'state': state}
    for did, decision in store['decisions'].items():
        state = report['decision_states'][did]
        values['decision:'+did] = {'state': state, 'blocks': list(decision['blocks'])}
    detected = {a['issue']['key']: a['issue'] for a in report['actions'] if a['kind']=='REGISTER_DECISION' and a.get('issue')}
    for key in set(detected) | {k[7:] for k in tracking['current'] if k.startswith('signal:')}:
        values['signal:'+key] = {'state': 'detected' if key in detected else 'registered' if any(d.get('source_key')==key for d in store['decisions'].values()) else 'cleared'}
    changed = False
    for key, value in values.items():
        if tracking['current'].get(key) != value:
            tracking['events'].append({'entity': key, 'at': at, 'value': copy.deepcopy(value)})
            tracking['current'][key] = copy.deepcopy(value)
            changed = True
    return changed


def intervals(store, entity, at):
    events = [e for e in store['state'].get('tracking', {}).get('events', []) if e['entity'] == entity]
    return [{'state': e['value']['state'], 'start': e['at'],
             'end': events[i+1]['at'] if i+1 < len(events) else at,
             'seconds': seconds(e['at'], events[i+1]['at'] if i+1 < len(events) else at),
             'open': i+1 == len(events)} for i, e in enumerate(events)]


def summary(rows):
    known = [r['work_seconds'] for r in rows if r['work_seconds'] is not None]
    rework = [r for r in rows if r['is_rework']]
    rework_known = [r['work_seconds'] for r in rework if r['work_seconds'] is not None]
    return {'calls': sum(r['accounting_kind']!='withdrawn' for r in rows), 'technical_rounds': len({r['budget_root'] for r in rows if r['chargeable']}),
            'withdrawn_starts': sum(r['accounting_kind']=='withdrawn' for r in rows),
            'invalid_calls': sum(r['result'] == 'INVALID' for r in rows),
            'retry_calls': sum(bool(r['retry_of']) for r in rows),
            'environment_calls': sum(r['accounting_kind'] == 'environment' for r in rows),
            'registration_calls': sum(r['accounting_kind'] == 'registration' for r in rows),
            'continuation_calls': sum(bool(r['continuation_of']) for r in rows),
            'work_seconds': sum(known) if known else None,
            'unknown_work_calls': sum(r['work_seconds'] is None and r['accounting_kind']!='withdrawn' for r in rows),
            'rework_calls': sum(r['is_rework'] for r in rows),
            'rework_seconds': sum(rework_known) if rework_known or not rework else None,
            'unknown_rework_calls': sum(r['work_seconds'] is None for r in rework),
            'dispatch_seconds': sum(r['dispatch_seconds'] for r in rows if r['dispatch_seconds'] is not None) if known else None,
            'result_registration_seconds': sum(r['result_registration_seconds'] for r in rows if r['result_registration_seconds'] is not None) if known else None,
            'unclassified_calls': sum(not r.get('cause') for r in rows),
            'causes': dict(Counter(r['cause']['category'] for r in rows if r.get('cause')))}


def metrics(store, report, at):
    rows = []
    for a in store['state']['attempts']:
        withdrawn_at=next((r['at'] for r in store['state'].get('attempt_accounting',[]) if r['attempt']==a['id'] and r['kind']=='withdrawn'),None)
        snap = a.get('task_snapshot') or snapshot(store['plan'], a['task'])
        timing = a.get('timing', {})
        predecessors = [x for x in store['state']['attempts'] if x['task'] == a['task'] and int(x['id'][1:]) < int(a['id'][1:])]
        root = next((x for x in store['state']['attempts'] if x['id'] == a.get('retry_of')), None)
        round_no = a.get('technical_round') or (root.get('technical_round') if root else None) or sum(not x.get('retry_of') for x in predecessors)+(not a.get('retry_of'))
        call = a.get('call', {})
        executor_stage = a.get('executor_stage', 'primary' if snap['role'] == 'executor' and snap['kind'] in {'design','execute'} else None)
        rows.append({'attempt': a['id'], 'task': a['task'], **snap,
                     'snapshot_source': 'recorded' if a.get('task_snapshot') else 'current_plan_fallback',
                     'round': round_no, 'retry_of': a.get('retry_of'), 'result': a.get('result'),
                     'continuation_of': a.get('continuation_of'), 'budget_root': round_root(store, a),
                     'accounting_kind': accounting_kind(store, a),
                     'withdrawn_at': withdrawn_at,
                     'chargeable': accounting_kind(store, a) not in {'environment','registration','withdrawn'},
                     'executor_stage': executor_stage, 'performer_role': a.get('performer_role',snap['role']),
                     'tool': call.get('tool'), 'model': call.get('model'), 'effort': call.get('effort'),
                     'model_source': 'planned_unlaunched' if accounting_kind(store,a)=='withdrawn' else 'recorded_call' if call.get('model') else 'unknown',
                     'call': call, 'call_id': a.get('call_id'), 'subjects': a['subjects'], 'plan_version': a['plan_version'],
                     'consumes': a['consumes'], 'timing_evidence': list(timing.get('evidence', {})),
                     'registered_start': a['started_at'], 'registered_end': a.get('ended_at') or withdrawn_at,
                     'registration_seconds': seconds(a['started_at'], a.get('ended_at') or withdrawn_at),
                     'work_seconds': seconds(timing.get('started_at'), timing.get('ended_at')),
                     'dispatch_seconds': seconds(a['started_at'], timing.get('started_at')),
                     'result_registration_seconds': seconds(timing.get('ended_at'), a.get('ended_at')),
                     'triggers': a.get('triggers', {'attempts': [], 'decisions': []}),
                     'is_rework': accounting_kind(store,a) not in {'environment','registration','withdrawn'} and not (a.get('retry_of') or a.get('continuation_of')) and bool(a.get('triggers', {}).get('attempts') or a.get('triggers', {}).get('decisions')),
                     'cause': a.get('cause'), 'evidence': list(a.get('evidence', {})),
                     'historical_simulation': a.get('historical_simulation', False)})
    # One row per actual attempt; tool retries are calls, not extra technical rounds.
    groups = {}
    fields = ('performer_role','executor_stage','tool','model','effort')
    for row in rows:
        key = tuple(row[field] for field in fields)
        groups.setdefault(key, []).append(row)
    model_usage = [{**dict(zip(fields,key)), **summary(group), 'attempts':[row['attempt'] for row in group]}
                   for key, group in groups.items()]
    decisions = []
    for did, d in store['decisions'].items():
        events = [e for e in store['state'].get('tracking', {}).get('events', []) if e['entity'] == 'decision:'+did]
        spans = intervals(store, 'decision:'+did, at)
        detected = next((e['at'] for e in store['state'].get('tracking', {}).get('events', []) if e['entity']=='signal:'+str(d.get('source_key')) and e['value']['state']=='detected'), None)
        decisions.append({'id': did, 'signal': d['signal'], 'source_key': d.get('source_key'), 'owner': d['owner'],
                          'detected_observed_at': detected, 'detection_to_registration_seconds': seconds(detected, d.get('reported_at')),
                          'blocks': d['blocks'], 'reported_at': d.get('reported_at'),
                          'handling_started_at': d.get('handling', {}).get('started_at'),
                          'resolved_at': d.get('resolved_at'), 'applied_at': d.get('applied_at'), 'closed_at': d.get('closed_at'),
                          'status': events[-1]['value']['state'] if events else d['status'],
                          'registration_to_handling_seconds': seconds(d.get('reported_at'), d.get('handling', {}).get('started_at')),
                          'handling_to_resolution_seconds': seconds(d.get('handling', {}).get('started_at'), d.get('resolved_at')),
                          'observed_blocked_seconds': sum(x['seconds'] for x in spans if x['state'] not in {'closed','superseded'} and x['seconds'] is not None) if spans else None,
                          'work_seconds': seconds(d.get('timing',{}).get('started_at'),d.get('timing',{}).get('ended_at')),
                          'call': d.get('handling',{}).get('call'), 'call_id': d.get('call_id'),
                          'timeline': spans})
    batches = {}
    for bid in store['plan']['batches']:
        related = [r for r in rows if bid in r['scope']]
        exclusive = [r for r in related if len(r['scope']) == 1]
        shared = [r for r in related if len(r['scope']) > 1]
        spans = intervals(store, 'batch:'+bid, at)
        done = [x['start'] for x in spans if x['state'] == 'DONE']
        start = min((r['registered_start'] for r in related), key=instant, default=None)
        end = done[-1] if report['batches'][bid] == 'DONE' and done else None
        tids = [tid for tid, t in store['plan']['tasks'].items() if bid in t['scope']]
        task_spans = {tid: intervals(store, 'task:'+tid, at) for tid in tids}
        waits = [(instant(x['start']), instant(x['end'])) for spans_ in task_spans.values() for x in spans_ if x['state'] == 'READY' and x['seconds'] is not None]
        # Union prevents simultaneous ready tasks from multiplying batch waiting time.
        merged = []
        for left, right in sorted(waits):
            if merged and left <= merged[-1][1]: merged[-1][1] = max(right, merged[-1][1])
            else: merged.append([left, right])
        stages = {}
        for name in sorted({r['phase'] for r in related} | {phase(store['plan'],tid) for tid in tids}):
            stages[name] = {'exclusive': summary([r for r in exclusive if r['phase'] == name]),
                            'shared_attempts': [r['attempt'] for r in shared if r['phase'] == name]}
        batches[bid] = {'status': report['batches'][bid], 'started_at': start, 'completed_observed_at': end,
                        'elapsed_seconds': seconds(start, end or at) if report['batches'][bid] != 'DONE' or end else None,
                        'elapsed_open': report['batches'][bid] != 'DONE', 'exclusive': summary(exclusive),
                        'shared_attempts': [r['attempt'] for r in shared], 'stages': stages,
                        'observed_ready_wait_seconds': sum((r-l).total_seconds() for l, r in merged) if any(task_spans.values()) else None,
                        'task_timelines': task_spans,
                        'decisions': [d['id'] for d in decisions if set(d['blocks']) & set(tids)]}
    signals = []
    for key, value in store['state'].get('tracking', {}).get('current', {}).items():
        if not key.startswith('signal:'): continue
        first = next(e['at'] for e in store['state']['tracking']['events'] if e['entity']==key)
        signals.append({'source_key':key[7:], 'status':value['state'], 'first_observed_at':first,
                        'unregistered_seconds': seconds(first,at) if value['state']=='detected' else None})
    return {'name': store['plan']['name'], 'revision': store['revision'], 'as_of': at,
            'simulation': store['simulation'], 'tracking_since': store['state'].get('tracking', {}).get('since'),
            'totals': summary(rows), 'model_usage': model_usage, 'signals':signals,
            'budgets': {bid: {'limit':b['limit'], 'used':len(charged_roots(store, bid)),
                              'attempts':[a['attempt'] for a in rows if bid in a['consumes']]} for bid,b in store['plan']['budgets'].items()}, 'batches': batches, 'attempts': rows, 'decisions': decisions,
            'shared_attempts': [r['attempt'] for r in rows if len(r['scope']) > 1],
            'limits': ['Work uses actual CLI timing only; unknown is null, not registration duration.',
                       'State intervals start at observations; status/metrics are read-only.',
                       'Shared calls occur once in totals and are referenced, not allocated, per batch.',
                       'Decision blocking and readiness intervals may overlap work and each other.']}
