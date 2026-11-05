#!/usr/bin/env python3
"""Data-driven advisory workflow. Standard library only; never invokes an agent."""
import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import re
import os
import sys
import base64
import subprocess
from pathlib import Path
import tempfile
import importlib.util
import uuid
import shlex

_tracking_spec = importlib.util.spec_from_file_location("workflow_metrics", Path(__file__).with_name("workflow_metrics.py"))
tracking = importlib.util.module_from_spec(_tracking_spec)
_tracking_spec.loader.exec_module(tracking)

ROLES = {'architect', 'orchestrator', 'executor', 'reviewer', 'third_party'}
KINDS = {'design', 'execute', 'verify', 'review', 'third_party', 'commit', 'acceptance', 'confirmation'}
RESULTS = {'VERIFIED', 'RELEASE', 'FIX_REQUIRED', 'WAIT_EVIDENCE', 'ARCH_DECISION_REQUIRED', 'FAILED', 'INVALID'}
OP_ROLES = {
    'init': {'architect'}, 'plan_update': {'architect'}, 'design_refresh': {'architect'},
    'authorize': {'orchestrator', 'architect'}, 'start': {'orchestrator', 'architect'},
    'finish': ROLES, 'signal': ROLES, 'resolve': {'architect'},
    'decision_progress': {'orchestrator', 'architect'}, 'history': {'orchestrator'},
    'change_subject': {'executor', 'architect'}, 'feedback': ROLES,
    'observe': {'orchestrator', 'architect'}, 'decision_start': {'architect'},
    'repin_requirement': {'architect'}, 'supersede_decision': {'architect'},
    'classify_attempt': {'architect'}, 'resume_attempt': {'architect'},
    'set_models': {'orchestrator'},
    'set_user_mode': {'orchestrator'},
    'register_dispatch': {'orchestrator', 'architect'},
}

class FlowError(ValueError):
    pass

def require(ok, message):
    if not ok:
        raise FlowError(message)

def obj(value, required, optional=(), label='object'):
    require(isinstance(value, dict), f'{label}: expected object')
    require(set(required) <= value.keys(), f'{label}: missing {sorted(set(required)-value.keys())}')
    require(value.keys() <= set(required) | set(optional), f'{label}: unknown fields {sorted(value.keys()-set(required)-set(optional))}')

def string(value, label):
    require(isinstance(value, str) and bool(value.strip()), f'{label}: expected non-empty string')

def strings(value, label, nonempty=False):
    require(isinstance(value, list), f'{label}: expected array')
    require(not nonempty or bool(value), f'{label}: must not be empty')
    for x in value:
        string(x, label)
    require(len(value) == len(set(value)), f'{label}: duplicate entries')

def integer(value, label, minimum=0):
    require(type(value) is int and value >= minimum, f'{label}: expected integer >= {minimum}')

def boolean(value, label):
    require(type(value) is bool, f'{label}: expected boolean')

def timestamp(value):
    string(value, 'timestamp')
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as e:
        raise FlowError('timestamp must be ISO-8601') from e
    require(result.tzinfo is not None, 'timestamp must include timezone')
    return result

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds')

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

def evidence(paths, root):
    strings(paths, 'evidence', nonempty=True)
    hashes = {}
    for name in paths:
        path = Path(name)
        if not path.is_absolute():
            path = root / path
        require(path.is_file(), f'evidence does not exist: {name}')
        hashes[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes

def review_sources(p, review):
    """Explicit repair owners; ordinary reviews also cover direct implementation inputs."""
    ids = set(review.get('repair_tasks', []))
    if review['kind'] == 'review':
        ids.update(review['depends_on'])
    if not review.get('repair_tasks') and review['kind'] in {'review','third_party'}:
        pending=list(review['depends_on']);seen=set()
        while pending:
            tid=pending.pop()
            if tid in seen:
                continue
            seen.add(tid);task=p['tasks'][tid]
            if task['kind'] in {'design','execute'}:
                ids.add(tid)
            else:
                pending.extend(task['depends_on'])
    return sorted(tid for tid in ids if not p['tasks'][tid].get('retired') and p['tasks'][tid]['kind'] in {'design','execute'})


def review_budget_coverage(p, excluded=(), historical_spend=None):
    """Conservative full-path reservation, not a prediction of actual rework."""
    rows=[]; errors=[]; demands=dict(historical_spend or {}); covered=set()
    for rid, review in p['tasks'].items():
        if rid in excluded or review.get('retired') or review['kind'] not in {'review','third_party','verify'}:
            continue
        sources=review_sources(p,review)
        sources=[tid for tid in sources if tid not in excluded]
        if not sources and not review['consumes']:
            continue
        covered.update(sources)
        groups={}; reserves=set()
        for tid in sources:
            source=p['tasks'][tid]
            key=tuple(sorted(set(source['consumes'])))
            if not key:
                errors.append(dict(kind='unbounded_repair_source',review=rid,source=tid))
                continue
            groups.setdefault(key,[]).append(tid)
            h=source.get('handoff',{})
            reserves.update(h[k] for k in ['backup_budget','lead_budget'] if k in h)
        # Identical primary pools and shared handoff reserves are counted once.
        # Partly overlapping primary pools use a conservative upper bound.
        primary=sum(min(p['budgets'][bid]['limit'] for bid in group) for group in groups)
        initial=len(sources)
        required=max(1,1+primary-initial+sum(p['budgets'][bid]['limit'] for bid in reserves))
        rows.append(dict(review=rid,sources=sources,required_rounds=required,
                         consumes=list(review['consumes']),shared_reserves=sorted(reserves)))
        if sources and not review['consumes']:
            errors.append(dict(kind='missing_review_budget',review=rid,required=required))
        for bid in set(review['consumes']):
            demands[bid]=demands.get(bid,0)+required
    for tid,t in p['tasks'].items():
        if tid not in excluded and not t.get('retired') and t.get('handoff') and tid not in covered:
            errors.append(dict(kind='missing_handoff_review',source=tid))
    for bid,required in demands.items():
        if any(not t.get('retired') and t['kind'] in {'design','execute'} and bid in t['consumes'] for t in p['tasks'].values()):
            errors.append(dict(kind='mixed_implementation_review_budget',budget=bid))
        if p['budgets'][bid]['limit']<required:
            errors.append(dict(kind='review_budget_shortfall',budget=bid,required=required,
                               limit=p['budgets'][bid]['limit'],shortfall=required-p['budgets'][bid]['limit']))
    return dict(ok=not errors,policy='full_path',reviews=rows,required_by_budget=demands,errors=errors,excluded_completed_tasks=sorted(excluded))


def active_review_budget_coverage(s, plan=None):
    plan=s['plan'] if plan is None else plan
    done={bid for bid,b in s['plan']['batches'].items() if b.get('retired') or success(s,b['completion_task'])}
    def unchanged_task(tid, task):
        old = s['plan']['tasks'].get(tid)
        # First-time scope annotation on an already completed task is not new delivery work.
        # Any other instruction/config change or later contract revision still gets full checking.
        return task == old or (old is not None and 'goal_contract' not in old and
                               {k:v for k,v in task.items() if k != 'goal_contract'} == old)
    excluded={tid for tid,t in plan['tasks'].items() if unchanged_task(tid,t) and set(t['scope'])<=done}
    spent={}
    for a in s['state']['attempts']:
        if a['task'] not in excluded or s['plan']['tasks'][a['task']]['kind'] not in {'review','third_party','verify'}:
            continue
        for bid in a['consumes']:
            if tracking.round_root(s,a) in tracking.charged_roots(s,bid):
                spent.setdefault(bid,set()).add(tracking.round_root(s,a))
    return review_budget_coverage(plan,excluded,{bid:len(roots) for bid,roots in spent.items()})


def validate_plan(p, enforce_review_budget=True):
    obj(p, ['name', 'roles', 'batches', 'subjects', 'budgets', 'tasks', 'decision_points'], ['feedback_minutes', 'context_groups', 'goal_contract'])
    string(p['name'], 'plan.name')
    if 'goal_contract' in p:
        validate_goal_contract(p['goal_contract'], 'plan.goal_contract')
    require(isinstance(p['roles'], dict), 'plan.roles: expected object')
    require(isinstance(p.get('feedback_minutes', {}), dict), 'feedback_minutes: expected object')
    require('architect' in p['roles'] and set(p['roles']) <= ROLES, 'plan.roles: provide technical lead and supported active roles')
    for role, config in p['roles'].items():
        obj(config, ['tool', 'model', 'effort'], ['principal'])
        for key, val in config.items():
            string(val, f'role.{role}.{key}')
    for field in ['batches', 'subjects', 'budgets', 'tasks', 'decision_points']:
        require(isinstance(p[field], dict), f'plan.{field}: expected object')
    for sid, version in p['subjects'].items():
        string(sid, 'subject id'); string(version, 'subject version')
    for bid, b in p['budgets'].items():
        string(bid, 'budget id')
        obj(b, ['limit']); integer(b['limit'], f'budget.{bid}.limit', 1)
    for bid, b in p['batches'].items():
        obj(b, ['name', 'completion_task'], ['retired'])
        string(b['name'], 'batch name')
        require(b['completion_task'] in p['tasks'], f'{bid}: unknown completion task')
        if 'retired' in b:
            boolean(b['retired'], 'retired')
    for tid, t in p['tasks'].items():
        string(tid, 'task id')
        obj(t, ['kind', 'role', 'scope', 'subjects', 'depends_on', 'consumes', 'instruction'], ['session_policy', 'retry_limit', 'can_close', 'retired', 'requires_authorization', 'decision_only', 'repair_tasks', 'phase', 'handoff', 'environment_retry_limit', 'continuation_limit', 'dispatch_owner', 'goal_contract'])
        require(t['kind'] in KINDS, f'{tid}: unsupported kind')
        require(t['role'] in ROLES, f'{tid}: unsupported role')
        if 'handoff' in t:
            h = t['handoff']
            require(t['role'] == 'executor' and t['kind'] in {'design','execute'}, 'handoff is only for executor design/execute tasks')
            obj(h, ['backup_budget', 'lead_budget'], ['backup'])
            if 'backup' in h:
                obj(h['backup'], ['tool','model','effort'], ['principal'])
                for key, value in h['backup'].items(): string(value, 'handoff.backup.'+key)
            require(bool(t['consumes']), 'handoff requires a primary round budget')
            require(h['backup_budget'] in p['budgets'] and h['lead_budget'] in p['budgets'], 'handoff budget unknown')
            require(h['backup_budget'] != h['lead_budget'] and not {h['backup_budget'],h['lead_budget']} & set(t['consumes']), 'handoff budgets must be distinct from primary budgets')
            require(p['budgets'][h['backup_budget']]['limit'] == 1, 'backup executor has exactly one technical round')
        string(t['instruction'], f'{tid}.instruction')
        if 'goal_contract' in t:
            validate_goal_contract(t['goal_contract'], f'{tid}.goal_contract')
        require('goal_contract' not in p or t.get('retired') or 'goal_contract' in t,
                f'{tid}: missing goal_contract under structured project goals')
        require(t.get('dispatch_owner', 'orchestrator') in {'orchestrator', 'architect'}, f'{tid}: invalid dispatch owner')
        require(t.get('dispatch_owner', 'orchestrator') != 'architect' or t['role'] != 'orchestrator', f'{tid}: local task result needs a non-orchestrator responsible role')
        if 'phase' in t: string(t['phase'], f'{tid}.phase')
        for field in ['scope', 'subjects', 'depends_on', 'consumes', 'can_close', 'repair_tasks']:
            strings(t.get(field, []), f'{tid}.{field}', field == 'scope')
        require(set(t['scope']) <= p['batches'].keys(), f'{tid}: unknown scope')
        require(set(t['subjects']) <= p['subjects'].keys(), f'{tid}: unknown subject')
        require(set(t['depends_on']) <= p['tasks'].keys(), f'{tid}: unknown dependency')
        require(set(t['consumes']) <= p['budgets'].keys(), f'{tid}: unknown budget')
        require(set(t.get('can_close', [])) <= p['tasks'].keys(), f'{tid}: unknown closure task')
        require(set(t.get('repair_tasks', [])) <= p['tasks'].keys(), f'{tid}: unknown repair task')
        require(t.get('session_policy', 'new') in {'new', 'resume', 'independent'}, f'{tid}: unknown session policy')
        integer(t.get('retry_limit', 1), 'retry_limit')
        integer(t.get('environment_retry_limit', 1), 'environment_retry_limit')
        integer(t.get('continuation_limit', 2), 'continuation_limit')
        for flag in ['retired', 'requires_authorization', 'decision_only']:
            if flag in t:
                boolean(t[flag], flag)
    reserved = {}
    for task in p['tasks'].values():
        if 'handoff' in task:
            for stage in ['backup','lead']:
                budget = task['handoff'][stage+'_budget']
                require(budget not in reserved or reserved[budget] == stage, 'handoff budget cannot serve different stages')
                reserved[budget] = stage
    require(not set(reserved) & {b for task in p['tasks'].values() for b in task['consumes']}, 'handoff reserve cannot be a primary or review budget')
    visiting, visited = set(), set()
    def visit(tid):
        require(tid not in visiting, f'dependency cycle at {tid}')
        if tid in visited:
            return
        visiting.add(tid)
        for dep in p['tasks'][tid]['depends_on']:
            visit(dep)
        visiting.remove(tid); visited.add(tid)
    for tid in p['tasks']:
        visit(tid)
    groups = p.get('context_groups', {})
    require(isinstance(groups, dict), 'context_groups must be an object')
    grouped = set()
    for gid, batches in groups.items():
        string(gid, 'context group'); strings(batches, f'context group {gid}', nonempty=True)
        require(set(batches) <= p['batches'].keys() and not grouped.intersection(batches), 'context groups must contain distinct known batches')
        grouped.update(batches)
    for did, point in p['decision_points'].items():
        obj(point, ['signal', 'owner', 'blocks', 'instruction'])
        string(point['signal'], 'decision signal'); string(point['instruction'], 'decision instruction')
        require(point['owner'] in {'architect', 'user'}, f'{did}: invalid owner')
        strings(point['blocks'], f'{did}.blocks')
        require(set(point['blocks']) <= p['tasks'].keys(), f'{did}: unknown blocked task')
    for kind, minutes in p.get('feedback_minutes', {}).items():
        require(kind in KINDS, f'feedback: unsupported kind {kind}')
        integer(minutes, 'feedback threshold', 1)
    if enforce_review_budget:
        coverage=review_budget_coverage(p)
        require(coverage['ok'],'review budget coverage: '+json.dumps(coverage['errors'],ensure_ascii=False))


def validate_context(c):
    obj(c, ['conversation_id','mode','reason'], ['source_conversation_id'])
    string(c['conversation_id'],'context.conversation_id');string(c['reason'],'context.reason')
    require(c['mode'] in {'fresh','resume'}, 'context mode must be fresh or resume')
    if c['mode']=='resume':
        require(c.get('source_conversation_id')==c['conversation_id'],'resume must name the same source conversation')
    else:
        require(not c.get('source_conversation_id'),'fresh context cannot inherit a source conversation')


def tool_family(tool):
    tool=str(tool or '')
    if tool in {'claude-agent','claude-code'} or tool.startswith('claude-agent:'): return 'claude-agent'
    return {'agy-cli':'agy','codex-cli':'codex'}.get(tool,tool)


def same_backend(config, prior_tool):
    """A context can only be resumed by the backend that created it (an AGY/Codex UUID is not a Claude agent ID).
    Bare `claude` is a subagent in a Claude Code host and a CLI elsewhere, so it pairs with either Claude form."""
    now, before = tool_family(config.get('tool')), tool_family(prior_tool)
    if not prior_tool:
        return now not in {'claude','claude-agent'}  # Unknown origin: never hand an unknown ID to SendMessage.
    if 'claude' in {now, before}:
        return {now, before} <= {'claude','claude-agent','claude-cli'}
    return now == before


def cli_context_resumable(config, cid):
    if not cid or cid.startswith('pending:'):
        return False
    if config.get('tool') not in {'codex','codex-cli'}:
        return True
    try:
        return str(uuid.UUID(cid)) == cid
    except (ValueError, AttributeError):
        return False


def check_arch_context(s,c):
    validate_context(c)
    require(not any(a['call']['conversation_id']==c['conversation_id'] and (a.get('performer_role',s['plan']['tasks'][a['task']]['role'])!='architect' or s['plan']['tasks'][a['task']]['kind'] in {'design','execute','review','third_party'}) for a in s['state']['attempts']), 'architecture context cannot inherit author implementation or review session')


def author_contexts(s):
    contexts={x['context']['conversation_id'] for x in s['state'].get('plan_changes',[]) if x.get('context')}
    contexts.update(x['context']['conversation_id'] for x in s['state'].get('design_corrections',[]) if x.get('context'))
    contexts.update(d['resolution']['context']['conversation_id'] for d in s['decisions'].values() if d.get('resolution') and d['resolution'].get('context'))
    contexts.update(d['handling']['call']['conversation_id'] for d in s['decisions'].values() if d.get('handling'))
    contexts.update(a['call']['conversation_id'] for a in s['state']['attempts'] if a.get('performer_role')=='architect' and s['plan']['tasks'][a['task']]['kind'] in {'design','execute'})
    return contexts

def context_group(s, task_ids):
    groups=s['plan'].get('context_groups',{})
    matches={gid for gid,batches in groups.items() if any(set(s['plan']['tasks'][tid]['scope']) & set(batches) for tid in task_ids)}
    return next(iter(matches)) if len(matches)==1 else 'cross-group' if matches else 'ungrouped'

def check_decision_group(s, did, cid):
    if not s['plan'].get('context_groups') or cid.startswith('pending:'): return
    group=context_group(s,s['decisions'][did]['blocks'])
    for other_id,other in s['decisions'].items():
        if other_id==did: continue
        seen=[(other.get('handling') or {}).get('call',{}).get('conversation_id'),((other.get('resolution') or {}).get('context') or {}).get('conversation_id')]
        require(cid not in seen or context_group(s,other['blocks'])==group, 'decision context belongs to another technical group')

def design_link(value, plan, root, version):
    obj(value, ['path', 'version', 'plan_version', 'sections'])
    string(value['path'], 'design.path'); string(value['version'], 'design.version')
    integer(value['plan_version'], 'design.plan_version', 1)
    require(value['plan_version'] == version, 'design and plan version differ')
    sections = value['sections']
    require(isinstance(sections, dict), 'design.sections must be an object')
    active = {tid for tid, t in plan['tasks'].items() if t['kind'] in {'design', 'execute'} and not t.get('retired')}
    require(active <= sections.keys() and sections.keys() <= plan['tasks'].keys(), 'design sections must cover active implementation tasks')
    for tid, section in sections.items():
        string(section, 'design section')
        require(section in plan['tasks'][tid]['instruction'], f'{tid}: instruction must cite its design section')
    path = Path(value['path'])
    path = path if path.is_absolute() else root / path
    require(path.is_file(), 'design document missing')
    content = path.read_bytes()
    for section in sections.values():
        require(section.encode() in content, f'design section missing: {section}')
    return dict(path=str(path.resolve()), version=value['version'], plan_version=version,
                sections=copy.deepcopy(sections), sha256=hashlib.sha256(content).hexdigest(),
                section_hashes={name: section_hash(content, name) for name in sorted(set(sections.values()))})

def section_block(content, name):
    """A cited section is a Markdown heading block, or else the paragraph that contains the name."""
    lines = content.decode('utf-8', 'replace').split('\n')
    def level(line):
        stripped = line.lstrip('#')
        return len(line) - len(stripped) if line.startswith('#') and stripped.startswith(' ') else 0
    for i, line in enumerate(lines):
        if level(line) and line.lstrip('#').strip() == name:
            end = next((j for j in range(i + 1, len(lines)) if level(lines[j]) and level(lines[j]) <= level(line)), len(lines))
            return '\n'.join(lines[i:end])
    for i, line in enumerate(lines):
        if name in line:
            start = i
            while start > 0 and lines[start - 1].strip(): start -= 1
            end = i
            while end + 1 < len(lines) and lines[end + 1].strip(): end += 1
            return '\n'.join(lines[start:end + 1])
    return None

def section_hash(content, name):
    block = section_block(content, name)
    return None if block is None else hashlib.sha256(block.encode()).hexdigest()

def stale_sections(s):
    """Cited sections whose text changed; legacy links without section hashes treat any file change as all sections."""
    link = s.get('design')
    if not link:
        return set()
    path = Path(link['path'])
    if not path.is_file():
        return set(link['sections'].values())
    content = path.read_bytes()
    if 'section_hashes' not in link:
        return set() if hashlib.sha256(content).hexdigest() == link['sha256'] else set(link['sections'].values())
    return {name for name, h in link['section_hashes'].items() if section_hash(content, name) != h}

def design_current(s):
    return not stale_sections(s)

def design_stale_tasks(s):
    stale = stale_sections(s)
    roots = {tid for tid, name in s.get('design', {}).get('sections', {}).items() if name in stale}
    return design_dependents(s, roots) if roots else set()

def design_dependents(s, roots=None):
    """The linked readers and their downstream gates share the design dependency."""
    affected=set(s.get('design',{}).get('sections',{}) if roots is None else roots)
    while True:
        expanded=affected | {tid for tid,t in s['plan']['tasks'].items() if affected.intersection(t['depends_on'])}
        if expanded == affected:
            return affected
        affected=expanded

def locally_owned(s, tasks):
    return bool(tasks) and all(s['plan']['tasks'][tid].get('dispatch_owner','orchestrator')=='architect' for tid in tasks)


def check_task_context(s, tid, cid, exclude=None):
    t=s['plan']['tasks'][tid]
    if cid.startswith('pending:'):
        return
    if t['role']=='executor':
        for a in s['state']['attempts']:
            if a['id']==exclude or a['call']['conversation_id']!=cid: continue
            other=s['plan']['tasks'][a['task']]
            require(other['role']=='executor' and set(other['scope'])==set(t['scope']), 'executor may resume only its own batch/group context')
    if t['kind'] not in {'review','third_party'}:
        return
    require(cid not in author_contexts(s),'review context authored a plan change; create a fresh independent review context')
    for a in s['state']['attempts']:
        if a['id']==exclude or a['call']['conversation_id']!=cid:
            continue
        require(t['kind']=='review' and t.get('session_policy')=='resume' and a['task']==tid,'first/new-node review cannot inherit another role or node context')


def attempts(s, tid):
    return [a for a in s['state']['attempts'] if a['task'] == tid]

def latest(s, tid):
    aa = [a for a in attempts(s, tid) if tracking.accounting_kind(s,a)!='withdrawn']
    return aa[-1] if aa else None

def current_subjects(s, tid):
    return {key: s['plan']['subjects'][key] for key in s['plan']['tasks'][tid]['subjects']}

def success_result(t):
    return 'RELEASE' if t['kind'] in {'review', 'third_party'} else 'VERIFIED'

def used(s, budget):
    return len(tracking.charged_roots(s, budget))


def resumption(s, a):
    record = next((r for r in reversed(s['state'].get('resumptions', [])) if a and r['attempt'] == a['id']), None)
    if not record or not all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p,h in record['evidence_hashes'].items()):
        return None
    if record['kind'] == 'decision_pause':
        classification = next(r for r in s['state']['attempt_accounting'] if r['attempt'] == a['id'])
        if not resumption_decision_ready(s,a,classification['decision_id']): return None
    return record


def resumption_decision_ready(s, a, decision_id):
    decision = s['decisions'][decision_id]
    if not decision.get('resolution'): return False
    if a['task'] in decision['blocks']: return decision_closed(s,decision)
    requirements = decision['resolution']['requirements']
    recovery = [r for r in requirements if r['kind']=='task' and r['target']==a['task']]
    # A recovery task must not wait for the decision that its own work will close.
    return all(requirement_ok(s,next(r for r in requirements if r['id']==dep))
               for requirement in recovery for dep in requirement['depends_on']) if recovery else decision_closed(s,decision)


def awaiting_resumption(s, a):
    return a and tracking.accounting_kind(s, a) in {'environment', 'decision_pause'}


def continuing_charged_round(s, a):
    return resumption(s, a) and all(tracking.round_root(s, a) in tracking.charged_roots(s, b) for b in a['consumes'])

def performer(s, tid, stage=None):
    """Select a preplanned implementation owner without resetting any consumed budget."""
    task = s['plan']['tasks'][tid]
    handoff = task.get('handoff')
    if not handoff:
        return 'primary', task['role'], s['plan']['roles'].get(task['role']), task['consumes']
    if stage is None:
        prev = latest(s, tid)
        # Tool retries belong to the same owner and technical round.
        retryable = prev and prev.get('result') == 'INVALID' and sum(a.get('retry_of') == (prev.get('retry_of') or prev['id']) for a in attempts(s,tid)) < task.get('retry_limit',1)
        if prev and (not prev.get('result') or retryable or awaiting_resumption(s, prev)):
            stage = prev.get('executor_stage','primary')
        elif any(a.get('executor_stage') in {'backup','lead'} for a in attempts(s,tid)):
            stage = 'lead'
        elif all(used(s,b) < s['plan']['budgets'][b]['limit'] for b in task['consumes']):
            stage = 'primary'
        elif used(s,handoff['backup_budget']) < 1:
            stage = 'backup'
        else:
            stage = 'lead'
    if stage == 'primary':
        return stage, 'executor', s['plan']['roles'].get('executor'), task['consumes']
    if stage == 'backup':
        return stage, 'executor', handoff.get('backup'), [handoff['backup_budget']]
    require(stage == 'lead', 'unknown executor handoff stage')
    return stage, 'architect', s['plan']['roles']['architect'], [handoff['lead_budget']]


def unassigned_models(s):
    live=[t for t in s['plan']['tasks'].values() if not t.get('retired')]
    roles=sorted({t['role'] for t in live} - s['plan']['roles'].keys())
    backups=sorted(tid for tid,t in s['plan']['tasks'].items() if not t.get('retired') and t.get('handoff') and 'backup' not in t['handoff'])
    return {'roles': roles, 'backups': backups}


def success(s, tid, seen=None):
    seen = set() if seen is None else set(seen)
    if tid in seen:
        return False
    seen.add(tid)
    t = s['plan']['tasks'][tid]; a = latest(s, tid)
    if blockers(s, tid):
        return False
    if a is None or not a.get('result') or a['result'] == 'INVALID':
        return False
    # Accepted records retain immutable evidence and version bindings.
    intact = all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p,h in a.get('evidence', {}).items())
    if not intact:
        return False
    direct = (a['result'] == success_result(t) and a['subjects'] == current_subjects(s, tid))
    if not direct:
        # A planned regular R2 may explicitly close/revalidate an earlier third-party R1.
        direct = any(tid in ct.get('can_close', []) and ca and a['id'] in ca.get('closes', [])
                     and ca['result'] == success_result(ct) and ca['subjects'] == current_subjects(s, cid) and success(s, cid, seen)
                     for cid, ct in s['plan']['tasks'].items() if (ca := latest(s, cid)))
    return direct and all(success(s, dep, seen) for dep in t['depends_on'])

def requirement_ok(s, r):
    if r['kind'] == 'task':
        return success(s, r['target']) and all(s['plan']['subjects'].get(k) == v for k, v in r['versions'].items())
    record = s['state']['confirmations'].get(r['id'])
    return bool(record) and all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p,h in record['evidence'].items())


def intact_evidence(hashes):
    try:
        return bool(hashes) and all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in hashes.items())
    except OSError:
        return False


def budget_change_set(before, after):
    limits={bid:b['limit'] for bid,b in after['budgets'].items() if bid not in before['budgets'] or b['limit']!=before['budgets'][bid]['limit']}
    tasks={}
    for tid,old in before['tasks'].items():
        new=after['tasks'][tid];change={}
        if old['consumes']!=new['consumes']:change['consumes']=new['consumes']
        for key in ['backup_budget','lead_budget']:
            if old.get('handoff',{}).get(key)!=new.get('handoff',{}).get(key):change[key]=new.get('handoff',{}).get(key)
        for key,default in [('environment_retry_limit',1),('continuation_limit',2)]:
            if new.get(key,default)>old.get(key,default):change[key]=new.get(key,default)
        if change:tasks[tid]=change
    return dict(limits=limits,tasks=tasks)


def validate_budget_approval(scope):
    obj(scope,[],['limits','tasks'])
    require(bool(scope.get('limits') or scope.get('tasks')),'budget approval scope must be non-empty')
    require(isinstance(scope.get('limits',{}),dict) and isinstance(scope.get('tasks',{}),dict),'budget approval needs limits/tasks objects')
    for bid,limit in scope.get('limits',{}).items():
        string(bid,'approved budget id');integer(limit,'approved budget limit',1)
    for tid,change in scope.get('tasks',{}).items():
        string(tid,'approved task id')
        obj(change,[],['consumes','backup_budget','lead_budget','environment_retry_limit','continuation_limit'])
        require(bool(change),'approved task change must be non-empty')
        if 'consumes' in change:strings(change['consumes'],'approved consumes')
        for key in ['backup_budget','lead_budget']:
            if key in change and change[key] is not None:string(change[key],'approved '+key)
        for key in ['environment_retry_limit','continuation_limit']:
            if key in change:integer(change[key],'approved '+key)


def budget_authorization_sources(s, decision, change):
    resolution=decision.get('resolution') or {}
    if intact_evidence(resolution.get('user_evidence',{})):
        return ['resolution.user_evidence']
    requirements={r['id']:r for r in resolution.get('requirements',[])}
    def satisfied(rid, visiting):
        if rid in visiting or rid not in requirements:return False
        r=requirements[rid]
        if r['kind']=='confirmation':
            record=s['state']['confirmations'].get(rid,{})
            if record.get('decision')!=decision['id'] or not intact_evidence(record.get('evidence',{})):return False
        elif not requirement_ok(s,r):return False
        return all(satisfied(dep,visiting|{rid}) for dep in r['depends_on'])
    approvals=[]
    for rid,r in requirements.items():
        if r['kind']!='confirmation' or r.get('owner')!='user' or not satisfied(rid,set()):continue
        scope=s['state']['confirmations'][rid].get('budget_approval')
        if scope:
            validate_budget_approval(scope);approvals.append((rid,scope))
    for bid,limit in change['limits'].items():
        if not any(scope.get('limits',{}).get(bid)==limit for _,scope in approvals):return []
    for tid,fields in change['tasks'].items():
        for key,value in fields.items():
            if not any(key in scope.get('tasks',{}).get(tid,{}) and scope['tasks'][tid][key]==value for _,scope in approvals):return []
    return [rid for rid,_ in approvals]

def decision_closed(s, d):
    return d['status'] == 'closed' and bool(d['resolution']) and all(requirement_ok(s, r) for r in d['resolution']['requirements'])

def blockers(s, tid):
    return [d['id'] for d in s['decisions'].values() if tid in d['blocks'] and not decision_closed(s, d)]

def closure_tasks(s, d):
    # Recovery work is allowed while its dependent implementation remains blocked.
    return {r['target'] for r in (d['resolution'] or {}).get('requirements', []) if r['kind'] == 'task'}

def source_recovered(s, key):
    return any(d.get('source_key') == key and decision_closed(s, d) for d in s['decisions'].values())


def confirmation_wait_pending(s, wait):
    dec = s['decisions'][wait['decision']]
    req = next(r for r in dec['resolution']['requirements'] if r['id'] == wait['requirement'])
    return (dec['status'] != 'superseded' and req['owner'] != 'user'
            and not requirement_ok(s, req) and not source_recovered(s, wait['source_key']))


def issues(s):
    out = []
    for tid, t in s['plan']['tasks'].items():
        if t.get('retired'):
            continue
        a = latest(s, tid)
        if a and a.get('result') == 'ARCH_DECISION_REQUIRED' and not awaiting_resumption(s,a) and not any(d.get('source_key') == f'arch:{a["id"]}' and decision_closed(s,d) for d in s['decisions'].values()):
            out.append({'key': f'arch:{a["id"]}', 'signal': 'technical_decision', 'blocks': [tid], 'reason': f'{tid}: reviewer requested architecture decision'})
        if a and a.get('result') == 'WAIT_EVIDENCE' and not awaiting_resumption(s, a):
            key = 'evidence:' + a['id']
            if not source_recovered(s, key):
                out.append(dict(key=key, signal='technical_decision', blocks=[tid], target=tid,
                                reason=f'{tid}: WAIT_EVIDENCE requires diagnosis; exhausted budgets: ' + str([b for b in performer(s, tid)[3] if used(s, b) >= s['plan']['budgets'][b]['limit']]), evidence=a['evidence']))
                continue  # One diagnosis owns this wait, including its budget consequences.
        if a and a.get('result') and not success(s, tid) and not blockers(s, tid):
            if awaiting_resumption(s, a) and (not resumption(s, a) or continuing_charged_round(s, a)):
                continue
            # Budget only escalates when another technical round is actually required.
            needs = a['result'] in {'FIX_REQUIRED', 'FAILED', 'WAIT_EVIDENCE'} or a['subjects'] != current_subjects(s, tid)
            exhausted = [b for b in performer(s,tid)[3] if used(s, b) >= s['plan']['budgets'][b]['limit']]
            if needs and exhausted:
                out.append({'key': f'budget:{tid}:{a["id"]}', 'signal': 'budget_exhausted', 'blocks': [tid], 'reason': f'{tid}: exhausted {exhausted}; unresolved or stale result'})
    for wait in s['state'].get('confirmation_waits', []):
        if confirmation_wait_pending(s, wait):
            dec = s['decisions'][wait['decision']]
            out.append(dict(key=wait['source_key'], signal='technical_decision', blocks=list(dec['blocks']),
                            target=wait['requirement'], decision=dec['id'], evidence=wait['evidence_hashes'],
                            reason=wait['requirement'] + ': WAIT_EVIDENCE requires diagnosis'))
    return out

def repairs_pending(s):
    result = {}
    for tid,t in s['plan']['tasks'].items():
        a = latest(s,tid)
        if not a or a.get('result') != 'FIX_REQUIRED' or t['kind'] not in {'review','third_party','verify'}:
            continue
        for target in t.get('repair_tasks', []):
            repair = latest(s,target)
            if not repair or int(repair['id'][1:]) < int(a['id'][1:]) or not success(s,target):
                result.setdefault(target, []).append(a['id'])
    return result


def validate_goal_contract(contract, label):
    obj(contract, ['goal', 'acceptance', 'non_goals'])
    string(contract['goal'], label + '.goal')
    strings(contract['acceptance'], label + '.acceptance', nonempty=True)
    strings(contract['non_goals'], label + '.non_goals')


def goal_context(s, item):
    """Copy current authorized scope; never infer acceptance from old findings."""
    plan = s['plan']; tasks = plan['tasks']; target = item['target']
    decision = s['decisions'].get(item.get('decision')) or s['decisions'].get(target)
    tids = {target} if target in tasks else set()
    if decision:
        tids.update(decision['blocks'])
        tids.update(r['target'] for r in (decision.get('resolution') or {}).get('requirements', [])
                    if r['kind'] == 'task')
    # Only direct delivery objects are relevant; prerequisite chains are scheduling context.
    for tid in list(tids):
        task = tasks[tid]
        if task['kind'] in {'review', 'third_party', 'verify', 'acceptance', 'confirmation'}:
            tids.update(dep for dep in task['depends_on'] + task.get('repair_tasks', [])
                        if tasks[dep]['kind'] in {'design', 'execute', 'verify'})
    rows = []
    for tid in sorted(tids):
        task = tasks[tid]
        row = dict(task=tid, kind=task['kind'])
        if 'goal_contract' in task:
            row['contract'] = copy.deepcopy(task['goal_contract'])
        else:
            # Legacy stores remain readable; do not pretend prose was structured/validated.
            row['instruction'] = task['instruction']
        design = s.get('design') or {}
        if tid in design.get('sections', {}):
            row['design'] = dict(path=design['path'], version=design['version'],
                                 section=design['sections'][tid])
        rows.append(row)
    return dict(target=target, plan_version=s['plan_version'], project=copy.deepcopy(plan.get('goal_contract')),
                tasks=rows, legacy=not bool(plan.get('goal_contract')),
                missing=[r['task'] for r in rows if 'contract' not in r])


def action(kind, target, role, instruction, **extra):
    return dict(kind=kind, target=target, role=role, instruction=instruction, **extra)


def decision_owner(decision):
    # Legacy plans may label a technical escalation as user-owned. Diagnosis
    # must still happen before asking the user to change scope or accept risk.
    if decision['signal'] in {'budget_exhausted','scope_blocked','severe_block'} or decision.get('decision_point') == 'SEVERE_BLOCK':
        return 'architect'
    return decision['owner']

def status(s, at=None):
    validate_store(s)
    current_time = timestamp(at or now()); actions = []; task_states = {}; repairs = repairs_pending(s)
    coverage=active_review_budget_coverage(s)
    for recorded in s['state']['attempts']:
        if tracking.accounting_kind(s,recorded)=='withdrawn':continue
        task=s['plan']['tasks'][recorded['task']]
        threshold=s['plan'].get('feedback_minutes', {}).get(task['kind'])
        end=timestamp(recorded['ended_at']) if recorded.get('ended_at') else current_time
        if threshold and (end-timestamp(recorded['started_at'])).total_seconds() >= threshold*60 and recorded['id'] not in s['state']['feedback']:
            actions.append(action('REPORT_PROGRESS',recorded['task'],task['role'],'达到反馈阈值：补记进度/阻塞与下一步；不自动停止或增加预算',attempt=recorded['id']))
    for issue in issues(s):
        if not any(d.get('source_key') == issue['key'] and not decision_closed(s, d) for d in s['decisions'].values()):
            actions.append(action('REGISTER_DECISION', issue.get('target') or issue['blocks'][0], 'orchestrator',
                issue['reason'] + '。只做机械登记：signal --from-issue ' + issue['key'] + '（数据由脚本按本问题生成，不自拟）。'
                '登记后 status 会出现 DECIDE，由技术负责人诊断，按其派发方式交出；编排者不自行诊断、修复或代做 DECIDE，'
                '预算耗尽也不直接请用户批预算（追加与否由技术负责人裁决后才问用户）。', issue=issue, decision=issue.get('decision')))
    for d in s['decisions'].values():
        if decision_closed(s, d) or d['status'] == 'superseded':
            continue
        if not d['resolution']:
            owner = decision_owner(d)
            instruction = d['reason']
            if d['signal'] in {'budget_exhausted','scope_blocked','severe_block'} or d.get('decision_point') == 'SEVERE_BLOCK':
                instruction = '先派技术负责人诊断并登记 resolve；当前不是等待用户确认。只有负责人明确登记用户确认恢复条件后才请求用户决定。问题：' + instruction
            actions.append(action('DECIDE', d['id'], owner, instruction, blocks=d['blocks']))
            continue
        rr = d['resolution']['requirements']; pending = [r for r in rr if not requirement_ok(s, r)]
        for r in pending:
            if any(not requirement_ok(s, next(x for x in rr if x['id'] == dep)) for dep in r['depends_on']):
                continue
            if r['kind'] == 'confirmation':
                if any(w['requirement'] == r['id'] and confirmation_wait_pending(s, w) for w in s['state'].get('confirmation_waits', [])):
                    continue
                actions.append(action('CONFIRM_CONDITION', r['id'], r['owner'], r['instruction'], decision=d['id']))
        if not pending:
            actions.append(action('CLOSE_DECISION', d['id'], 'orchestrator', '恢复条件齐全，用 close 一次检查并闭环；之后重新检查整体门禁'))
    pending_decision_tasks = {x for issue in issues(s) for x in issue['blocks']}
    for tid, t in s['plan']['tasks'].items():
        a = latest(s, tid)
        if t.get('retired'):
            task_states[tid] = 'RETIRED'; continue
        if a and not a.get('result'):
            task_states[tid] = 'RUNNING'
            continue
        deps = [d for d in t['depends_on'] if not success(s, d)]
        blocked = blockers(s, tid)
        if blocked or tid in pending_decision_tasks:
            task_states[tid] = 'DECISION_BLOCKED'; continue
        if deps:
            task_states[tid] = 'WAIT_DEPENDENCY'; continue
        recovery_wait = any(r['kind'] == 'task' and r['target'] == tid and any(not requirement_ok(s, next(x for x in d['resolution']['requirements'] if x['id'] == dep)) for dep in r['depends_on']) for d in s['decisions'].values() if d['resolution'] and not decision_closed(s,d) for r in d['resolution']['requirements'])
        if recovery_wait:
            task_states[tid] = 'WAIT_RECOVERY_DEPENDENCY'; continue
        if t.get('decision_only') and not success(s, tid) and not any(tid in closure_tasks(s,d) and not decision_closed(s,d) for d in s['decisions'].values()):
            task_states[tid] = 'INACTIVE'; continue
        if success(s, tid) and tid not in repairs:
            task_states[tid] = 'SATISFIED'; continue
        if awaiting_resumption(s, a) and not resumption(s, a):
            task_states[tid] = 'WAIT_ENVIRONMENT' if tracking.accounting_kind(s, a) == 'environment' else 'WAIT_RESUMPTION'
            actions.append(action('PREPARE_RESUMPTION', tid, 'architect', '核对实际执行环境恢复证据或已闭环裁决，有限批准原轮次续作', attempt=a['id']))
            continue
        if a and a.get('result') == 'FIX_REQUIRED' and t['kind'] in {'review','third_party','verify'}:
            targets=t.get('repair_tasks', [])
            if not targets:
                task_states[tid]='WAIT_REPAIR_PLAN'
                actions.append(action('PLAN_REPAIR',tid,'architect','审阅/核验有阻断但未配置修复归属，请安排修复任务及共享预算'))
                continue
            if any(target in repairs for target in targets):
                task_states[tid]='WAIT_REPAIR'; continue
        if t.get('requires_authorization', True) and not set(t['scope']) <= set(s['state']['authorized_batches']):
            task_states[tid] = 'WAIT_AUTHORIZATION'
            actions.append(action('REQUEST_AUTHORIZATION', tid, 'orchestrator', '当前授权未覆盖该任务范围', scope=t['scope'])); continue
        if t['role']=='orchestrator' and t['kind'] in {'design','execute','verify','review','third_party','commit'}:
            task_states[tid]='WAIT_ROLE_ASSIGNMENT'
            actions.append(action('PLAN_REPAIR',tid,'architect','技术任务误设为编排者负责；修订计划中的实际责任角色，保留授权、预算和历史。编排者不得自行执行该任务。'))
            continue
        if t['kind'] in {'design','execute'}:
            review_blockers=[]
            bad_budgets={e['budget'] for e in coverage['errors'] if e.get('budget')}
            missing={e.get('source') for e in coverage['errors'] if e['kind']=='missing_handoff_review'}
            if tid in missing:
                review_blockers.append(dict(review=None,budgets=[],reason='missing_handoff_review'))
            for rid,review in s['plan']['tasks'].items():
                if review.get('retired') or review['kind'] not in {'review','third_party','verify'} or tid not in review_sources(s['plan'],review):
                    continue
                unmatched_review=[b for b in review['consumes'] if b in bad_budgets]
                if unmatched_review or not review['consumes']:
                    review_blockers.append(dict(review=rid,budgets=unmatched_review,reason='full_path_coverage'))
                    continue
                exhausted_review=[b for b in review['consumes'] if used(s,b)>=s['plan']['budgets'][b]['limit']]
                ra=latest(s,rid)
                retry_review=ra and ra.get('result')=='INVALID' and sum(x.get('retry_of')==(ra.get('retry_of') or ra['id']) for x in attempts(s,rid))<review.get('retry_limit',1)
                if exhausted_review and not retry_review:
                    review_blockers.append(dict(review=rid,budgets=exhausted_review))
            if review_blockers:
                task_states[tid]='WAIT_REVIEW_BUDGET'
                actions.append(action('PLAN_REPAIR',tid,'architect','实施/返修的独立审阅预算不配套或已耗尽，先修复配套计划再派发；不得先消耗主执行、备用或负责人额度。追加预算须引用用户授权。缺口：'+json.dumps(review_blockers,ensure_ascii=False),review_budget_blockers=review_blockers))
                continue
        exhausted = [b for b in performer(s,tid)[3] if used(s, b) >= s['plan']['budgets'][b]['limit']]
        retry = a and a.get('result') == 'INVALID' and sum(x.get('retry_of') == (a.get('retry_of') or a['id']) for x in attempts(s, tid)) < t.get('retry_limit', 1)
        continuing = awaiting_resumption(s, a) and resumption(s, a)
        if (exhausted and not retry and not continuing_charged_round(s, a)) or (a and a.get('result') == 'INVALID' and not retry and not continuing):
            task_states[tid] = 'BUDGET_BLOCKED'
            if not any(x['target'] == tid and x['kind'] == 'REGISTER_DECISION' for x in actions):
                actions.append(action('REGISTER_DECISION', tid, 'orchestrator', '可用技术轮次或无效调用重试已耗尽', issue={'key': f'budget:{tid}:{a["id"] if a else "none"}', 'signal': 'budget_exhausted', 'blocks': [tid], 'reason': 'remaining budget/retry exhausted'}))
            continue
        stage, responsible, config, active_budgets = performer(s,tid)
        if config is None:
            task_states[tid] = 'WAIT_MODELS'
            missing = {'backup': tid} if stage == 'backup' else {'role': responsible}
            actions.append(action('ASSIGN_MODELS', tid, 'orchestrator', '计划已登记但该职责尚未指定模型；请用户指定 tool/model/effort 后经 set_models 登记', missing=missing, executor_stage=stage)); continue
        task_states[tid] = 'READY'
        kind = {'design': 'DESIGN', 'execute': 'EXECUTE', 'verify': 'VERIFY', 'review': 'REVIEW', 'third_party': 'THIRD_PARTY_REVIEW', 'commit': 'COMMIT', 'acceptance': 'RELEASE', 'confirmation': 'CONFIRM'}[t['kind']]
        if tid in repairs:
            kind='REPAIR'
        actions.append(action(kind, tid, responsible, t['instruction'], executor_stage=stage, call_config=copy.deepcopy(config), retry_of=(a.get('retry_of') or a['id']) if retry and not continuing else None, continuation_of=a['id'] if continuing else None, subjects=current_subjects(s, tid), remaining={b: s['plan']['budgets'][b]['limit']-used(s,b) for b in active_budgets}))
    stale_tasks=design_stale_tasks(s)
    if stale_tasks:
        affected={tid for tid in stale_tasks if task_states.get(tid)=='READY'}
        actions=[a for a in actions if a['target'] not in affected or a['kind'] in {'REGISTER_DECISION','REPORT_PROGRESS'}]
        for tid in sorted(affected):
            task_states[tid]='WAIT_DESIGN_SYNC'
            actions.append(action('SYNC_DESIGN',tid,'architect','设计书与已生效计划关联不符；核对变更后同步更新或受控纯文字刷新'))
    for item in actions:
        role=item['role'];config=item.get('call_config',s['plan']['roles'].get(role,{}))
        item['principal']=config.get('principal','backup_executor' if item.get('executor_stage') == 'backup' else role)
        tid=item['target'];task=s['plan']['tasks'].get(tid)
        item['goal_context'] = goal_context(s, item)
        # role names identify the performer, not permission for the coordinator
        # to do the work. Keep the payload separate from dispatch instructions.
        if role == 'user' or item['kind'] in {'REQUEST_AUTHORIZATION','ASSIGN_MODELS'}:
            mode='request_user'
            guidance='向用户提出当前明确待决项；只登记用户原始决定，不代替用户决定。'
        elif item['kind'] == 'REPORT_PROGRESS':
            mode='collect_progress'
            guidance='向实际责任会话收集可核查进度并登记；不自行探索源码、运行验证或诊断。'
        elif role == 'orchestrator':
            mode='register'
            guidance='执行协调或机械登记；技术内容引用实际责任人的原始交付，不自行补写技术判断。'
        else:
            decision=s['decisions'].get(tid) or s['decisions'].get(item.get('decision'))
            targets=decision['blocks'] if decision else []
            delegated=(task and task.get('dispatch_owner','orchestrator') == 'architect') or (targets and all(s['plan']['tasks'][target].get('dispatch_owner','orchestrator') == 'architect' for target in targets))
            mode='delegate_to_lead' if delegated else 'dispatch'
            guidance=('移交局部技术负责人，由其按职责实际处理或派发并回收。' if delegated else '按指定角色、调用配置及上下文真实派发，回收原始交付后机械登记。') + '编排者不得自行实现、修复、验证、审阅或作技术裁决；不得仅切换 --actor 冒充责任人。角色不可用时报告缺项，不自行接管。'
        item['orchestrator_action']={'mode':mode,'instruction':guidance}
        backend={'agy':'agy','agy-cli':'agy','codex':'codex','codex-cli':'codex',
                 'claude':'claude','claude-cli':'claude'}.get(config.get('tool'))
        if backend and mode in {'dispatch','delegate_to_lead'}:
            item['cli_adapter']={'backend':backend,'tool':config.get('tool'),
                'entrypoint':str(Path(__file__).resolve().parent / 'agent_cli.py'),
                'reference':str(Path(__file__).resolve().parent.parent / 'references' / 'linux-cli.md'),
                'instruction':'Linux CLI 使用此固定入口的 probe/run/collect；已有同版本 profile 时不重复探索调用方式。模型与 effort 使用本动作配置；正式 start 和 finish 仍由派发责任人登记。'}
        if task and item['kind'] in {'REVIEW','THIRD_PARTY_REVIEW','DESIGN','EXECUTE','REPAIR'}:
            prev=latest(s,tid)
            can_resume=task.get('session_policy')=='resume' and prev and prev.get('executor_stage','primary') == item.get('executor_stage','primary') and prev['call']['conversation_id'] not in author_contexts(s) and cli_context_resumable(config,prev['call']['conversation_id']) and same_backend(config,prev['call'].get('tool'))
            item['context_hint']={'mode':'resume' if can_resume else 'fresh','conversation_id':prev['call']['conversation_id'] if can_resume else None,'reason':'same-node follow-up; reread current inputs' if can_resume else 'first/new node, independent review, or prior context authored a changed plan'}
        elif item['kind'] in {'DECIDE','PLAN_REPAIR','PREPARE_RESUMPTION'}:
            dedicated = None
            target_group=context_group(s,s['decisions'][tid]['blocks']) if tid in s['decisions'] else context_group(s,[tid]) if tid in s['plan']['tasks'] else None
            for decision in reversed(list(s['decisions'].values())):
                if s['plan'].get('context_groups') and target_group is not None and context_group(s,decision['blocks'])!=target_group: continue
                context = (decision.get('resolution') or {}).get('context', {})
                cid = context.get('conversation_id') or decision.get('handling', {}).get('call', {}).get('conversation_id')
                if cli_context_resumable(config,cid) and same_backend(config,decision.get('handling',{}).get('call',{}).get('tool')) and not any(a['call']['conversation_id'] == cid for a in s['state']['attempts']):
                    dedicated = cid
                    break
            item['context_hint']={'mode':'resume' if dedicated else 'fresh', 'conversation_id':dedicated, 'reason':'同技术链路续原决策会话；无关域分开，跨组契约总体协调；失效重建保留原因、旧新ID与预算'}
        if backend=='codex' and item.get('context_hint',{}).get('mode')=='fresh':
            item['context_hint']['reason']+='；Codex 仅续接有成功原始调用记录的真实 UUID；旧逻辑名不可用于 resume，重建须保留原因与旧新关联'
        if mode in {'dispatch','delegate_to_lead'}:
            item['subagent_payload']={key:copy.deepcopy(item[key]) for key in
                ['kind','target','role','principal','instruction','call_config','context_hint',
                 'subjects','remaining','executor_stage','retry_of','continuation_of','blocks','decision','review_budget_blockers'] if key in item}
            item['subagent_payload'].setdefault('call_config',copy.deepcopy(config))
            item['subagent_payload']['goal_context'] = copy.deepcopy(item['goal_context'])
            item.pop('instruction')
            item['orchestrator_todo']=guidance + '只将 subagent_payload 交给指定角色；其中的 instruction 不是主会话 TODO。禁止直接执行 git add/commit/merge、产品构建或测试。'
        else:
            item['orchestrator_todo']=guidance
    # Dispatch real work before confirmations: a confirmation checks evidence that pending work produces.
    actions.sort(key=lambda a: a['kind'] == 'CONFIRM_CONDITION')
    batches = {bid: ('RETIRED' if b.get('retired') else 'DONE' if task_states[b['completion_task']] == 'SATISFIED' else 'INCOMPLETE') for bid, b in s['plan']['batches'].items()}
    return {'revision': s['revision'], 'plan_version': s['plan_version'], 'simulation': s['simulation'], 'user_mode': copy.deepcopy(s['state'].get('user_mode', {'mode':'present','authorization':None})), 'batches': batches, 'tasks': task_states, 'actions': actions, 'decision_states': {did: decision_state(s,d) for did,d in s['decisions'].items()}, 'unassigned_models': unassigned_models(s), 'review_budget_coverage':coverage, 'complete': all(v in {'DONE','RETIRED'} for v in batches.values())}



def decision_state(s, d):
    if d['status'] == 'superseded': return 'superseded'
    if decision_closed(s, d): return 'closed'
    recovered = bool(d['resolution']) and all(requirement_ok(s,r) for r in d['resolution']['requirements'])
    if d['status'] == 'applied': return 'applied' if recovered else 'recovery_invalidated'
    if d['status'] == 'closed': return 'recovery_ready' if recovered else 'reopened'
    if recovered: return 'recovery_ready'
    if d['status'] == 'pending' and d.get('handling'): return 'handling'
    return d['status']


def progress_marker(s):
    """What a pending confirmation can be waiting on: finished attempts, confirmations and the plan."""
    return dict(results=sum(1 for a in s['state']['attempts'] if a.get('result')),
                confirmations=sorted(s['state']['confirmations']), plan_version=s['plan_version'])


def legacy_confirmation_waits(s, root):
    latest_card = {}
    for path in (Path(root)/'records'/'dispatch').glob('*/card.json'):
        try:
            card = json.loads(path.read_text()); st = json.loads((path.parent/'state.json').read_text())
        except (OSError, ValueError):
            continue
        if card.get('kind') != 'CONFIRM_CONDITION' or not st.get('finished'):
            continue
        target = card.get('target')
        if target not in latest_card or st['finished'] > latest_card[target][1]['finished']:
            latest_card[target] = (card, st, path.parent/'result.md')
    rows = []
    for target, (card, st, path) in latest_card.items():
        if st.get('result') != 'WAIT_EVIDENCE': continue
        if not path.is_file(): path = path.parent/'state.json'
        if any(w['requirement'] == target for w in s['state'].get('confirmation_waits', [])): continue
        for dec in s['decisions'].values():
            if dec['status'] == 'superseded': continue
            req = next((r for r in (dec.get('resolution') or {}).get('requirements', []) if r['id'] == target and r['kind'] == 'confirmation'), None)
            if req and not requirement_ok(s, req) and all(requirement_ok(s, next(r for r in dec['resolution']['requirements'] if r['id'] == dep)) for dep in req['depends_on']):
                rows.append(dict(decision=dec['id'], requirement=target, owner=req['owner'], path=str(path), card=card['id']))
    return rows


def waiting_conditions(s, root=None):
    return {w['requirement']: w['source_key'] for w in s['state'].get('confirmation_waits', []) if confirmation_wait_pending(s, w)}


def pre_yield(s, owner='orchestrator', boundary_audit=None, root=None):
    """Advisory stop-hook result. Never starts work or changes the ledger."""
    report=status(s)
    legacy = legacy_confirmation_waits(s, root) if root is not None else []
    waiting = {w['requirement']: w['card'] for w in legacy}
    for w in legacy:
        if w['owner'] != 'user':
            report['actions'].append(action('REGISTER_CONFIRMATION_WAIT', w['requirement'], 'orchestrator',
                '通过 decision_progress status=waiting 登记原始确认等待，再按 status 派负责人诊断', decision=w['decision'],
                register_data=dict(id=w['decision'], status='waiting', requirement=w['requirement'], evidence=[w['path']])))
    owned={tid for tid,t in s['plan']['tasks'].items() if owner=='orchestrator' or t.get('dispatch_owner','orchestrator')==owner}
    if owner != 'orchestrator':
        owned.difference_update(a['task'] for a in s['state']['attempts'] if not a.get('result') and tracking.accounting_kind(s,a)!='withdrawn' and a.get('dispatch_owner','orchestrator') != owner)
    # Legacy stores may already contain a running attempt whose recorded owner
    # differs from the current plan. The actual dispatcher must still see it.
    owned.update(a['task'] for a in s['state']['attempts'] if not a.get('result') and tracking.accounting_kind(s,a)!='withdrawn' and a.get('dispatch_owner','orchestrator')==owner)
    def relevant(a):
        if owner=='orchestrator': return True
        if a['target'] in owned: return True
        decision=s['decisions'].get(a['target']) or s['decisions'].get(a.get('decision'))
        if decision and decision['blocks']: return set(decision['blocks']) <= owned
        # Empty blocks confer no task authority. Only the explicit architect-owned
        # confirmation and its diagnosis are visible to the local lead.
        origins = [w for w in s['state'].get('confirmation_waits', []) if decision and w['source_key'] == decision.get('source_key')]
        origins += [dict(decision=a.get('decision'), requirement=a['target'])] if a.get('decision') else []
        return any(r['id'] == w['requirement'] and r.get('owner') == 'architect'
                   for w in origins for r in (s['decisions'].get(w['decision'], {}).get('resolution') or {}).get('requirements', []))
    busy=any(report['tasks'].get(tid)=='RUNNING' for tid in owned)
    # A confirmation only checks existing evidence. While owned work is still running, checking now
    # cannot change the answer and may race the running writer, so it waits instead of forcing CONTINUE.
    work=[a for a in report['actions'] if relevant(a) and a['role']!='user' and a['kind'] not in {'REQUEST_AUTHORIZATION','ASSIGN_MODELS'}
          and not (a['kind']=='CONFIRM_CONDITION' and (a['target'] in waiting or busy))]
    problems=[]; running=[]
    for tid,state in report['tasks'].items():
        if state!='RUNNING' or tid not in owned: continue
        a=latest(s,tid);running.append(tid);receipt=a.get('dispatch')
        if owner=='orchestrator' and a.get('dispatch_owner','orchestrator')!='orchestrator':
            continue  # A lead-dispatched attempt (foreground nested call) is tracked by the lead, not by orchestrator timers.
        if not receipt:
            problems.append(dict(task=tid,attempt=a['id'],reason='missing actual dispatch and timer receipt; inspect host before retrying'))
            continue
        try:
            intact=all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in receipt['evidence'].items())
            if receipt.get('timer_kind') == 'native':
                active=bool(receipt.get('timer_id') and receipt.get('owner_conversation_id'))
                deadline=timestamp(receipt['due_at'])
            else:
                ticket=json.loads(Path(receipt['timer_ticket']).read_text())
                active=ticket.get('format')=='batch-flow-progress-timer/v1' and ticket.get('task')==tid and ticket.get('status')=='running'
                deadline=timestamp(ticket['started_at'])+dt.timedelta(seconds=float(ticket['seconds']))
            if not intact or not active or timestamp(now())>=deadline:
                problems.append(dict(task=tid,attempt=a['id'],reason='verify dispatch evidence, report overdue progress, and rearm timer if still running'))
        except (OSError,ValueError,KeyError,TypeError,OverflowError):
            problems.append(dict(task=tid,attempt=a['id'],reason='timer receipt unavailable; verify actual task and timer state'))
    user=[a for a in report['actions'] if (a['role']=='user' or a['kind'] in {'REQUEST_AUTHORIZATION','ASSIGN_MODELS'}) and relevant(a)]
    local_complete=owner=='architect' and all(report['tasks'][tid] in {'SATISFIED','RETIRED','INACTIVE'} for tid in owned)
    disposition='CONTINUE' if work or problems else 'WAIT_HOST' if running else 'WAIT_USER' if user else 'COMPLETE' if (report['complete'] or local_complete) else 'STALLED'
    audit_report=boundary_audit or {'status':'not_configured'}
    if owner=='orchestrator' and audit_report['status'] in {'violation','unavailable'}:
        disposition='ROLE_BOUNDARY_BLOCKED'
    return dict(revision=s['revision'],disposition=disposition,can_yield=disposition in {'WAIT_HOST','WAIT_USER','COMPLETE'},
                actions=work,dispatch_problems=problems,running=running,user_actions=user,boundary_audit=audit_report,
                limit='Receipts do not prove live processes or automatic wakeups; verify host handles. This check requires a host stop hook for enforcement.')


def validate_store(s):
    obj(s, ['format_version', 'revision', 'plan_version', 'simulation', 'plan', 'state', 'decisions', 'history'], ['design', 'task_mode'])
    require(type(s['format_version']) is int and s['format_version'] == 1, 'unsupported format_version')
    integer(s['revision'],'revision');integer(s['plan_version'],'plan_version',1)
    validate_plan(s['plan'],enforce_review_budget=False); boolean(s['simulation'], 'simulation')
    if 'task_mode' in s:
        require(s['task_mode'] in {'local', 'batched'}, 'invalid task_mode')
        require(s['task_mode'] != 'batched' or 'design' in s, 'batched store requires design')
    if 'design' in s:
        link=s['design'];obj(link,['path','version','plan_version','sections','sha256'],['section_hashes'])
        require(link['plan_version']==s['plan_version'], 'design link plan version mismatch')
    obj(s['state'], ['attempts', 'authorized_batches', 'authorization_evidence', 'confirmations', 'feedback'], ['plan_changes', 'tracking', 'attempt_accounting', 'resumptions', 'user_mode', 'design_corrections', 'confirmation_waits'])
    if 'user_mode' in s['state']:
        mode=s['state']['user_mode']
        obj(mode,['mode','authorization','user_evidence','actor','at'])
        require(mode['mode'] in {'present','away'} and mode['actor']=='orchestrator', 'invalid user mode')
        require(isinstance(mode['user_evidence'],dict) and bool(mode['user_evidence']), 'user mode requires original user evidence')
        timestamp(mode['at'])
        if mode['mode']=='away': string(mode['authorization'],'away authorization')
        else: require(mode['authorization'] is None, 'present mode must clear away authorization')
    if 'tracking' in s['state']:
        observed = s['state']['tracking']
        obj(observed, ['since','current','events']); timestamp(observed['since'])
        require(isinstance(observed['current'],dict) and isinstance(observed['events'],list), 'invalid tracking structure')
        for event in observed['events']:
            obj(event, ['entity','at','value']); string(event['entity'],'tracking.entity'); timestamp(event['at'])
            require(isinstance(event['value'],dict) and bool(event['value'].get('state')), 'invalid tracking state')
    strings(s['state']['authorized_batches'], 'authorized batches')
    require(set(s['state']['authorized_batches']) <= s['plan']['batches'].keys(), 'unknown authorized batch')
    classified = set()
    by_id = {a['id']:a for a in s['state']['attempts']}
    for field in ['attempt_accounting','resumptions']:
        records=s['state'].get(field,[])
        require(isinstance(records,list), field+' must be an array')
        for record in records:
            obj(record, ['attempt','kind','reason','actor','at','revision','evidence_hashes'] + (['work_performed'] if field=='attempt_accounting' else ['task']), ['decision_id','context','source_attempt'])
            require(record['attempt'] in by_id and (by_id[record['attempt']].get('result') or record.get('kind')=='withdrawn'), 'accounting source must be a finished attempt or a withdrawn unlaunched start')
            require(record['actor']=='architect', 'accounting requires architect responsibility')
            string(record['reason'],'accounting reason');timestamp(record['at']);integer(record['revision'],'accounting revision',1)
            require(isinstance(record['evidence_hashes'],dict) and bool(record['evidence_hashes']), 'accounting evidence hashes required')
            if field=='attempt_accounting':
                require(record['attempt'] not in classified and record['kind'] in {'environment','registration','decision_pause','withdrawn'}, 'invalid or duplicate accounting classification')
                boolean(record['work_performed'],'work_performed');classified.add(record['attempt'])
            else:
                require(record['attempt'] in classified and record['kind'] in {'environment','decision_pause'} and record['kind']==tracking.accounting_kind(s,by_id[record['attempt']]), 'invalid resumption classification')
                require(record['task']==by_id[record['attempt']]['task'], 'resumption task mismatch')
    require(isinstance(s['decisions'], dict), 'decisions must be object')
    for d in s['decisions'].values():
        require(set(d['blocks']) <= s['plan']['tasks'].keys(), 'decision blocks unknown task')
        if d['status'] == 'superseded':
            supersession = d.get('supersession', {})
            require(supersession.get('replacement') in s['decisions'] and supersession['replacement'] != d['id'],
                    'superseded decision requires a different recorded replacement')
            require(d['blocks'] and all(s['plan']['tasks'][tid].get('retired') for tid in d['blocks']),
                    'cannot reactivate superseded scope; plan a new explicit recovery path')

    waits = s['state'].get('confirmation_waits', [])
    require(isinstance(waits, list), 'confirmation_waits must be an array')
    keys = set()
    for wait in waits:
        obj(wait, ['decision', 'requirement', 'source_key', 'evidence_hashes', 'actor', 'at', 'revision'])
        string(wait['decision'], 'wait decision'); string(wait['requirement'], 'wait requirement'); string(wait['source_key'], 'wait source key')
        dec = s['decisions'].get(wait['decision'])
        require(dec and dec.get('resolution'), 'wait requires a resolved decision')
        require(any(r['id'] == wait['requirement'] and r['kind'] == 'confirmation' for r in dec['resolution']['requirements']), 'wait requires a confirmation requirement')
        require(wait['actor'] in {'orchestrator', 'architect'}, 'invalid wait actor')
        timestamp(wait['at']); integer(wait['revision'], 'wait revision', 1)
        require(isinstance(wait['evidence_hashes'], dict) and bool(wait['evidence_hashes']), 'wait evidence hashes required')
        require(all(isinstance(p, str) and isinstance(h, str) and re.fullmatch(r'[0-9a-f]{64}', h) for p,h in wait['evidence_hashes'].items()), 'invalid wait evidence hashes')
        require(wait['source_key'] == confirmation_wait_key(wait['decision'], wait['requirement'], wait['evidence_hashes']), 'invalid wait source key')
        require(wait['source_key'] not in keys, 'duplicate confirmation wait'); keys.add(wait['source_key'])


def confirmation_wait_key(decision, requirement, hashes):
    return 'confirmation:' + digest(dict(decision=decision, requirement=requirement, evidence=hashes))


def check_requirements(s, rr):
    require(isinstance(rr, list) and rr, 'resolution needs non-empty structured recovery requirements')
    ids = []
    for r in rr:
        obj(r, ['id', 'kind', 'depends_on', 'instruction'], ['target', 'versions', 'owner'])
        string(r['id'], 'requirement.id'); string(r['instruction'], 'requirement.instruction'); ids.append(r['id'])
        strings(r['depends_on'], 'requirement.depends_on')
        require(r['kind'] in {'task', 'confirmation'}, 'unsupported requirement kind; register explicit role confirmation')
        if r['kind'] == 'task':
            require(r.get('target') in s['plan']['tasks'], 'requirement target unknown')
            require(isinstance(r.get('versions'), dict) and r['versions'] == current_subjects(s, r['target']), 'requirement must pin current target subject versions')
        else:
            require(r.get('owner') in ROLES | {'user'}, 'confirmation needs owner')
    require(len(ids) == len(set(ids)), 'duplicate recovery requirement id')
    seen = set()
    while len(seen) < len(ids):
        ready = [r['id'] for r in rr if r['id'] not in seen and set(r['depends_on']) <= seen]
        require(ready, 'recovery requirements contain cycle or unknown dependency')
        seen.update(ready)


def operate(s, op, actor, root, at):
    obj(op, ['type', 'data']); kind = op['type']; d = op['data']
    require(kind in OP_ROLES and actor in OP_ROLES[kind], f'{actor} cannot perform {kind}')
    if kind == 'init':
        obj(d, ['plan', 'simulation', 'context', 'task_mode'], ['design']); require(s is None, 'store already initialized')
        validate_plan(d['plan'],enforce_review_budget=True); boolean(d['simulation'], 'simulation')
        validate_context(d['context'])
        require(not d['context']['conversation_id'].startswith('pending:'), 'initial author context must be an actual conversation')
        require(d['task_mode'] in {'local', 'batched'}, 'task_mode must be local or batched')
        require(d['task_mode'] != 'batched' or 'design' in d, 'batched initialization requires design')
        link=design_link(d['design'],d['plan'],root,1) if 'design' in d else None
        result={'format_version': 1, 'revision': 0, 'plan_version': 1, 'simulation': d['simulation'], 'plan': copy.deepcopy(d['plan']), 'state': {'attempts': [], 'authorized_batches': [], 'authorization_evidence': {}, 'confirmations': {}, 'feedback': {}, 'plan_changes': [{'plan_version':1,'context':copy.deepcopy(d['context']),'kind':'init'}] if 'context' in d else []}, 'decisions': {}, 'history': []}
        if link: result['design']=link
        result['task_mode']=d['task_mode']
        return result
    require(s is not None, 'initialize first')
    if kind == 'observe':
        obj(d, [])
    elif kind == 'authorize':
        obj(d, ['batches', 'evidence']); strings(d['batches'], 'authorize batches')
        require(set(d['batches']) <= s['plan']['batches'].keys(), 'unknown authorization scope')
        if actor=='architect':
            affected={tid for tid,t in s['plan']['tasks'].items() if set(t['scope']) & set(d['batches']) and not t.get('retired')}
            require(locally_owned(s,affected), 'architect may authorize only wholly delegated batches')
        hashes=evidence(d['evidence'], root)
        if actor=='architect':
            s['state']['authorization_evidence'].update(hashes)
            s['state']['authorized_batches']=sorted(set(s['state']['authorized_batches']) | set(d['batches']))
        else:
            s['state']['authorization_evidence'] = hashes
            s['state']['authorized_batches'] = list(d['batches'])
    elif kind == 'set_user_mode':
        obj(d,['mode','user_evidence'],['authorization'])
        require(d['mode'] in {'present','away'}, 'user mode must be present or away')
        if d['mode']=='away': string(d.get('authorization'),'away authorization')
        else: require(not d.get('authorization'), 'present mode cannot grant away authorization')
        s['state']['user_mode']=dict(mode=d['mode'],authorization=d.get('authorization') if d['mode']=='away' else None,
            user_evidence=evidence(d['user_evidence'],root),actor=actor,at=at)
    elif kind == 'register_dispatch':
        obj(d,['attempt','handle','evidence'],['timer_kind','timer_handle','timer_ticket','timer_id','owner_conversation_id','due_at'])
        a=next((x for x in s['state']['attempts'] if x['id']==d['attempt']),None)
        require(not a or tracking.accounting_kind(s,a)!='withdrawn','withdrawn start cannot be dispatched')
        require(a and not a.get('result'),'dispatch receipt requires a running attempt')
        require(actor==a.get('dispatch_owner','orchestrator'),'only assigned dispatch owner may register this task')
        string(d['handle'],'handle');require(not d['handle'].startswith('pending:'),'record actual host handle')
        require(not any(x is not a and not x.get('result') and x.get('dispatch',{}).get('handle')==d['handle'] for x in s['state']['attempts']), 'work handle already belongs to another running attempt')
        require(not a.get('dispatch') or a['dispatch']['handle']==d['handle'],'cannot replace an active task handle; inspect original call first')
        timer_kind=d.get('timer_kind','script')
        require(timer_kind in {'script','native'}, 'unknown timer kind')
        if timer_kind=='native':
            require(not {'timer_handle','timer_ticket'} & d.keys(), 'native timer cannot include script timer fields')
            for key in ['timer_id','owner_conversation_id','due_at']:
                string(d.get(key),key)
            require(not d['timer_id'].startswith('pending:') and not d['owner_conversation_id'].startswith('pending:'), 'record actual native timer and owner session')
            require(timestamp(at)<timestamp(d['due_at']), 'native timer already due')
            timer=dict(timer_kind='native',timer_id=d['timer_id'],owner_conversation_id=d['owner_conversation_id'],due_at=d['due_at'])
        else:
            require(not {'timer_id','owner_conversation_id','due_at'} & d.keys(), 'script timer cannot include native timer fields')
            string(d.get('timer_handle'),'timer_handle');require(not d['timer_handle'].startswith('pending:'),'record actual timer handle')
            string(d.get('timer_ticket'),'timer_ticket')
            ticket_path=Path(d['timer_ticket']);ticket_path=ticket_path if ticket_path.is_absolute() else root/ticket_path
            ticket=json.loads(ticket_path.read_text())
            require(ticket.get('format')=='batch-flow-progress-timer/v1' and ticket.get('task')==a['task'] and ticket.get('status')=='running','record an active timer for this task')
            seconds=ticket.get('seconds');require(type(seconds) in {int,float} and 0<seconds<float('inf'),'invalid timer interval')
            require(timestamp(at)<timestamp(ticket['started_at'])+dt.timedelta(seconds=seconds),'timer already due')
            timer=dict(timer_kind='script',timer_handle=d['timer_handle'],timer_ticket=str(ticket_path.resolve()))
        hashes=evidence(d['evidence'],root)
        if timer_kind=='script': require(str(ticket_path.resolve()) not in hashes,'use immutable host acknowledgement evidence, not the mutable timer ticket')
        a['dispatch']=dict(handle=d['handle'],evidence=hashes,at=at,**timer)
    elif kind == 'set_models':
        obj(d, ['roles', 'backups', 'reason', 'user_evidence'])
        require(isinstance(d['roles'],dict) and isinstance(d['backups'],dict) and (d['roles'] or d['backups']), 'provide explicit role or backup model updates')
        string(d['reason'],'model update reason')
        user_hashes=evidence(d['user_evidence'],root)
        before_hash=digest(s['plan'])
        changed=False
        backups=dict(d['backups'])
        if '*' in backups:
            # Shorthand for "every unfinished task that has a handoff"; explicit task keys still override it.
            states=status(s)['tasks']
            wildcard=backups.pop('*')
            expanded={tid:copy.deepcopy(wildcard) for tid,t in s['plan']['tasks'].items()
                      if t.get('handoff') and states.get(tid) not in {'SATISFIED','RETIRED'}}
            require(expanded,'backup wildcard matched no unfinished task with a handoff')
            backups={**expanded,**backups}
        for targets, backup in [(d['roles'],False),(backups,True)]:
            for target, patch in targets.items():
                if backup:
                    require(target in s['plan']['tasks'] and s['plan']['tasks'][target].get('handoff'), 'backup target must be a task with an existing handoff')
                    holder,key=s['plan']['tasks'][target]['handoff'],'backup'
                else:
                    require(target in ROLES,'unsupported role')
                    require(target in s['plan']['roles'] or any(x['role']==target for x in s['plan']['tasks'].values()),'role is not used by the plan; add scheduled roles through plan_update')
                    holder,key=s['plan']['roles'],target
                require(isinstance(patch,dict) and bool(patch), 'model configuration patch must be non-empty')
                if key not in holder:
                    require(patch.keys() == {'tool','model','effort'}, 'first assignment must give tool, model and effort')
                    holder[key]={}
                config=holder[key]
                require(patch.keys() <= {'tool','model','effort'}, 'only tool/model/effort may change')
                for key,value in patch.items():
                    string(value,'model configuration '+key)
                    changed=changed or config.get(key)!=value
                config.update(copy.deepcopy(patch))
        require(changed,'model configuration unchanged')
        validate_plan(s['plan'],enforce_review_budget=False)
        s['plan_version']+=1
        if 'design' in s: s['design']['plan_version']=s['plan_version']
        s['state'].setdefault('plan_changes',[]).append(dict(kind='user_model_update',
            plan_version=s['plan_version'],decision_id=None,context=None,reason=d['reason'],
            user_evidence=user_hashes,before_hash=before_hash,after_hash=digest(s['plan'])))
    elif kind == 'plan_update':
        obj(d, ['plan', 'decision_id'], ['context', 'design']); validate_plan(d['plan'],enforce_review_budget=False)
        coverage=active_review_budget_coverage(s,d['plan'])
        require(coverage['ok'],'review budget coverage: '+json.dumps(coverage['errors'],ensure_ascii=False))
        require('design' not in s or 'design' in d, 'linked plan update requires matching design link')
        if s.get('task_mode') == 'local' and d['plan']['batches'].keys() - s['plan']['batches'].keys():
            require('design' in d, 'expanding local work to batches requires design')
        if 'design' in d:
            require('context' in d, 'design update requires author context')
            proposed=Path(d['design']['path']);proposed=proposed if proposed.is_absolute() else root/proposed
            require('design' not in s or proposed.resolve() == Path(s['design']['path']).resolve(), 'design path is fixed for this task')
            link=design_link(d['design'],d['plan'],root,s['plan_version']+1)
        if 'context' in d: check_arch_context(s,d['context'])
        shared=d['plan']['roles']['architect'].get('principal')
        require(not shared or shared!=d['plan']['roles'].get('reviewer',{}).get('principal') or 'context' in d, 'combined technical lead must record plan author context')
        dec = s['decisions'].get(d['decision_id'])
        require(dec is not None, 'plan update requires recorded decision')
        require(dec['status'] != 'superseded', 'superseded decision cannot authorize a plan update')
        for key in ['tasks', 'budgets', 'subjects', 'batches', 'decision_points']:
            require(s['plan'][key].keys() <= d['plan'][key].keys(), f'cannot delete historical {key}; retire instead')
        for tid, task in s['plan']['tasks'].items():
            running = latest(s, tid)
            if running and not running.get('result'):
                require(d['plan']['tasks'][tid].get('dispatch_owner','orchestrator') == running.get('dispatch_owner','orchestrator'), 'running task owner cannot change before recovery')
            if task.get('handoff') and any(a.get('executor_stage') in {'backup','lead'} for a in attempts(s,tid)):
                require(d['plan']['tasks'][tid].get('handoff') == task['handoff'] and d['plan']['tasks'][tid]['consumes'] == task['consumes'], 'cannot reset an activated executor handoff policy')
        require('goal_contract' not in s['plan'] or 'goal_contract' in d['plan'],
                'cannot drop structured project goals; revise the contract explicitly')
        changes=budget_change_set(s['plan'],d['plan'])
        approval_sources=budget_authorization_sources(s,dec,changes) if any(changes.values()) else []
        require(not any(changes.values()) or approval_sources, 'changing budget limits requires referenced user decision with valid evidence and matching budget scope; register decision_progress confirmed with budget_approval, or bind_budget_approval for an existing user confirmation using the same original evidence; do not rewrite resolution.user_evidence')
        for b in d['plan']['budgets']:
            require(d['plan']['budgets'][b]['limit'] >= used(s, b), f'budget {b} below actual consumption')
        for key, old in s['plan']['subjects'].items():
            require(d['plan']['subjects'][key] == old, 'change subjects through change_subject; do not rebind history in plan_update')
        require(all(set(x['blocks']) <= d['plan']['tasks'].keys() for x in s['decisions'].values()), 'invalid decision references')
        before_hash=digest(s['plan'])
        s['plan'] = copy.deepcopy(d['plan']); s['plan_version'] += 1
        if 'design' in d: s['design']=link
        if 'design' in d and s.get('task_mode') == 'local': s['task_mode']='batched'
        s['state'].setdefault('plan_changes', []).append({'plan_version':s['plan_version'],'decision_id':d['decision_id'],'context':copy.deepcopy(d.get('context')),'before_hash':before_hash,'after_hash':digest(s['plan']),'budget_authorization_sources':approval_sources})
    elif kind == 'design_refresh':
        obj(d,['version','reason','context'])
        require('design' in s,'no linked design to refresh')
        string(d['reason'],'design correction reason');check_arch_context(s,d['context'])
        old=s['design']
        link=design_link(dict(path=old['path'],version=d['version'],plan_version=s['plan_version'],sections=old['sections']),s['plan'],root,s['plan_version'])
        s['design']=link
        s['state'].setdefault('design_corrections',[]).append(dict(plan_version=s['plan_version'],before_hash=old['sha256'],after_hash=link['sha256'],reason=d['reason'],context=copy.deepcopy(d['context']),at=at))
    elif kind == 'change_subject':
        obj(d, ['subject', 'version', 'evidence']); require(d['subject'] in s['plan']['subjects'], 'unknown subject')
        string(d['version'], 'new version'); evidence(d['evidence'], root)
        old = s['plan']['subjects'][d['subject']]
        require(d['version'] != old, 'new version equals current')
        require(not any(a['subjects'].get(d['subject']) == d['version'] for a in s['state']['attempts']), 'cannot recycle historical subject version')
        s['plan']['subjects'][d['subject']] = d['version']
    elif kind in {'start', 'history'}:
        required = ['task', 'call'] + (['result', 'evidence', 'source'] if kind == 'history' else [])
        obj(d, required, ['retry_of', 'replacement_reason', 'closes', 'ended_at', 'started_at', 'triggers', 'call_id', 'continuation_of'])
        tid = d['task']; require(tid in s['plan']['tasks'], 'unknown task')
        t = s['plan']['tasks'][tid]; call = d['call']
        if kind == 'start':
            require(actor == t.get('dispatch_owner','orchestrator'), 'only assigned dispatch owner may start this task')
            require(tid not in design_stale_tasks(s), 'design section changed without synchronized plan update')
        obj(call, ['tool', 'model', 'effort', 'conversation_id'])
        for k,v in call.items(): string(v, f'call.{k}')
        stage, responsible, config, active_budgets = performer(s,tid)
        require(config is not None, f'{tid}: {responsible} model not assigned; register user choice with set_models first')
        require(all(call[k] == config[k] for k in ['tool','model','effort']), 'call role/tool/model/effort mismatch')
        aa = attempts(s, tid); prev = latest(s, tid)
        if kind=='start':
            require(call['conversation_id'].startswith('pending:') or cli_context_resumable(call,call['conversation_id']), 'Codex call requires pending or a native UUID, not a logical context name')
            check_task_context(s,tid,call['conversation_id'])
        if kind == 'history':
            require(s['simulation'], 'history replay is simulation-only; production migration must be explicitly designed')
            string(d['source'], 'history source'); evidence([d['source']], root)
        else:
            report = status(s, at)
            require(report['tasks'][tid] == 'READY', f'{tid} not READY: {report["tasks"][tid]}')
        require(not prev or prev.get('result'), 'task already running')
        retry = d.get('retry_of')
        continuation = d.get('continuation_of')
        require(not (retry and continuation), 'retry_of and continuation_of are mutually exclusive')
        if continuation:
            require(kind == 'start' and prev and prev['id'] == continuation and awaiting_resumption(s, prev) and resumption(s, prev), 'continuation requires a permit for the latest classified attempt')
            require(active_budgets == prev['consumes'], 'continuation cannot change budget identity')
        if retry:
            target = next((a for a in aa if a['id'] == retry), None)
            require(target and target.get('result') == 'INVALID' and prev.get('result') == 'INVALID', 'retry requires latest invalid call and valid root attempt')
            require(sum(a.get('retry_of') == retry for a in aa) < t.get('retry_limit', 1), 'invalid retry budget exhausted')
        elif not (continuation and continuing_charged_round(s, prev)):
            for b in active_budgets:
                require(used(s,b) < s['plan']['budgets'][b]['limit'], f'budget exhausted: {b}')
        if kind == 'start':
            offered = next(a for a in report['actions'] if a['target'] == tid and 'retry_of' in a)
            require(offered['retry_of'] == retry, 'use retry_of proposed by status')
            require(offered.get('continuation_of') == continuation, 'use continuation_of proposed by status')
        if t.get('handoff') and stage != (prev.get('executor_stage','primary') if prev else 'primary'):
            require(not any(a['call']['conversation_id'] == call['conversation_id'] for a in s['state']['attempts']) or call['conversation_id'].startswith('pending:'), 'executor handoff requires a fresh implementation context')
        if t.get('session_policy') == 'resume' and prev and call['conversation_id'] != prev['call']['conversation_id']:
            string(d.get('replacement_reason'), 'new context requires replacement_reason')
        if t.get('session_policy', 'new') in {'new','independent'} and not retry:
            require(not any(a['call']['conversation_id'] == call['conversation_id'] for a in s['state']['attempts']), 'new/independent context reused')
        started = d.get('started_at', at); timestamp(started)
        a = {'id': f'A{len(s["state"]["attempts"])+1:04}', 'task': tid, 'subjects': current_subjects(s, tid), 'plan_version': s['plan_version'], 'consumes': list(active_budgets), 'call': copy.deepcopy(call), 'started_at': started, 'result': None, 'retry_of': retry, 'replacement_reason': d.get('replacement_reason'), 'historical_simulation': kind == 'history'}
        a.update(performer_role=responsible, performer_config=copy.deepcopy(config), dispatch_owner=t.get('dispatch_owner','orchestrator'))
        if continuation:
            a['continuation_of'] = continuation
        if t.get('handoff'):
            a['executor_stage'] = stage
        if 'call_id' in d:
            string(d['call_id'], 'call_id'); a['call_id'] = d['call_id']
        triggers = copy.deepcopy(d.get('triggers', {'attempts': [], 'decisions': []}))
        obj(triggers, ['attempts', 'decisions'])
        for key in triggers: strings(triggers[key], 'triggers.'+key)
        require(set(triggers['attempts']) <= {x['id'] for x in s['state']['attempts']}, 'trigger attempt unknown')
        require(all(x.get('result') for x in s['state']['attempts'] if x['id'] in triggers['attempts']), 'trigger attempt is unfinished')
        require(set(triggers['decisions']) <= s['decisions'].keys(), 'trigger decision unknown')
        inferred = set(repairs_pending(s).get(tid, []))
        if prev and not retry and prev.get('result') in {'FIX_REQUIRED','WAIT_EVIDENCE','FAILED','ARCH_DECISION_REQUIRED'}:
            inferred.add(prev['id'])
            for repair_tid in t.get('repair_tasks', []):
                repaired = latest(s, repair_tid)
                if repaired and int(repaired['id'][1:]) > int(prev['id'][1:]): inferred.add(repaired['id'])
        triggers['attempts'] = sorted(set(triggers['attempts']) | inferred)
        for dec in s['decisions'].values():
            if tid in closure_tasks(s, dec) and not decision_closed(s, dec):
                triggers['decisions'] = sorted(set(triggers['decisions']) | {dec['id']})
        a['triggers'] = triggers
        a['task_snapshot'] = tracking.snapshot(s['plan'], tid)
        a['technical_round'] = prev['technical_round'] if continuation else target.get('technical_round', len({tracking.round_root(s,x) for x in aa})) if retry else 1+len(tracking.charged_roots(s) & {tracking.round_root(s,x) for x in aa})
        s['state']['attempts'].append(a)
        if kind == 'history':
            finish(s, {'attempt': a['id'], 'result': d['result'], 'evidence': d['evidence'], 'closes': d.get('closes', []), 'ended_at': d.get('ended_at', at)}, responsible, root, at)
    elif kind == 'classify_attempt':
        obj(d, ['attempt', 'kind', 'work_performed', 'reason', 'evidence'], ['decision_id', 'context', 'source_attempt'])
        a = next((x for x in s['state']['attempts'] if x['id'] == d['attempt']), None)
        require(a and (a.get('result') or d['kind']=='withdrawn'), 'classification requires a finished attempt')
        require(not any(r['attempt'] == a['id'] for r in s['state'].get('attempt_accounting', [])), 'attempt already classified')
        require(d['kind'] in {'environment','registration','decision_pause','withdrawn'}, 'unknown accounting kind')
        boolean(d['work_performed'], 'work_performed'); string(d['reason'], 'accounting reason')
        if 'context' in d: check_arch_context(s, d['context'])
        if d['kind'] == 'withdrawn':
            require(not d['work_performed'] and a.get('result') is None and a['call']['conversation_id'].startswith('pending:'),'withdrawal requires an unfinished unlaunched placeholder without substantive work')
            require(a==latest(s,a['task']) and not a.get('dispatch') and not any(a.get(k) for k in ['call_id','timing','retry_of','continuation_of']),'withdrawal requires the latest standalone start without dispatch/call/timing evidence')
            previous=[x for x in attempts(s,a['task']) if int(x['id'][1:])<int(a['id'][1:]) and tracking.accounting_kind(s,x)!='withdrawn']
            source=previous[-1] if previous else None
            require(source and source['id']==d.get('source_attempt') and source.get('result')==success_result(s['plan']['tasks'][a['task']]),'withdrawal must name the immediately preceding successful delivery')
            require(source['subjects']==current_subjects(s,a['task']) and intact_evidence(source.get('evidence',{})),'prior delivery versions and immutable evidence must remain intact')
        elif d['kind'] == 'environment':
            require(not d['work_performed'] and a['result'] in {'INVALID','WAIT_EVIDENCE'}, 'environment exemption requires no substantive work')
            require(a.get('started_subjects', a['subjects']) == a['subjects'], 'environment exemption cannot hide a subject change')
        elif d['kind'] == 'registration':
            require(not d['work_performed'] and a['result'] == success_result(s['plan']['tasks'][a['task']]), 'registration exemption requires a successful metadata-only correction')
            source = next((x for x in attempts(s,a['task']) if x['id'] == d.get('source_attempt')),None)
            require(source and int(source['id'][1:]) < int(a['id'][1:]) and source.get('result') == a['result'], 'registration correction requires an earlier successful delivery')
            require(bool(set(source.get('evidence',{}).items()) & set(a.get('evidence',{}).items())), 'registration correction must reference unchanged original delivery evidence')
            require(all(Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in list(source.get('evidence',{}).items())+list(a.get('evidence',{}).items())), 'registration correction evidence is no longer intact')
        else:
            require(d['work_performed'] and a['result'] in {'WAIT_EVIDENCE','ARCH_DECISION_REQUIRED'}, 'decision pause cannot waive a technical failure')
            require(d.get('decision_id') in s['decisions'], 'decision pause requires a recorded decision')
            task = s['plan']['tasks'][a['task']]
            require(task['role'] == 'executor' and task['kind'] in {'design','execute'}, 'decision continuation is only for executor design/execute tasks')
            decision = s['decisions'][d['decision_id']]
            require(a['task'] in set(decision['blocks']) | closure_tasks(s,decision) or d['decision_id'] in a.get('triggers',{}).get('decisions',[]), 'pause decision is unrelated to the attempt')
            require(a == latest(s,a['task']), 'only the latest attempt can pause for continuation')
        record = {k:copy.deepcopy(v) for k,v in d.items() if k != 'evidence'}
        record.update(actor=actor, at=at, revision=s['revision']+1, evidence_hashes=evidence(d['evidence'],root))
        s['state'].setdefault('attempt_accounting', []).append(record)
    elif kind == 'resume_attempt':
        obj(d, ['attempt', 'reason', 'evidence'], ['context'])
        a = next((x for x in s['state']['attempts'] if x['id'] == d['attempt']), None)
        require(a and a == latest(s,a['task']) and a.get('result') and awaiting_resumption(s,a), 'resume requires the latest environment failure or decision pause')
        require(not resumption(s,a), 'attempt already has a valid resumption permit')
        string(d['reason'], 'resumption reason')
        if 'context' in d: check_arch_context(s,d['context'])
        category = tracking.accounting_kind(s,a); task=s['plan']['tasks'][a['task']]
        limit_key, default = ('environment_retry_limit',1) if category == 'environment' else ('continuation_limit',2)
        consumed = sum(r['task'] == a['task'] and r['kind'] == category for r in s['state'].get('resumptions',[]))
        require(consumed < task.get(limit_key,default), 'resumption limit exhausted; no automatic further retry')
        if category == 'decision_pause':
            classification = next(r for r in s['state']['attempt_accounting'] if r['attempt']==a['id'])
            require(resumption_decision_ready(s,a,classification['decision_id']), 'continuation decision or recovery prerequisites are not ready')
        record = {k:copy.deepcopy(v) for k,v in d.items() if k != 'evidence'}
        hashes = evidence(d['evidence'],root)
        require(any(a.get('evidence',{}).get(p) != h for p,h in hashes.items()), 'resumption requires new readiness or recovery evidence')
        record.update(task=a['task'],kind=category,actor=actor,at=at,revision=s['revision']+1,evidence_hashes=hashes)
        s['state'].setdefault('resumptions',[]).append(record)
    elif kind == 'finish':
        finish(s, d, actor, root, at)
    elif kind == 'signal':
        obj(d, ['id', 'signal', 'reason', 'blocks', 'evidence'], ['decision_point', 'source_key'])
        require(d['id'] not in s['decisions'], 'duplicate decision id')
        string(d['id'], 'decision id'); string(d['signal'], 'signal'); string(d['reason'], 'reason')
        strings(d['blocks'], 'blocks'); require(set(d['blocks']) <= s['plan']['tasks'].keys(), 'unknown blocked task')
        point = s['plan']['decision_points'].get(d.get('decision_point'))
        require(not d.get('decision_point') or point, 'unknown decision point')
        if point:
            require(d['signal'] == point['signal'] and set(point['blocks']) <= set(d['blocks']), 'signal/blocks do not cover configured decision point')
        source_key = d.get('source_key')
        require(not source_key or not any(x.get('source_key') == source_key and not decision_closed(s,x) for x in s['decisions'].values()), 'duplicate unresolved signal')
        owner=decision_owner(dict(d,owner=point['owner'] if point else 'architect'))
        s['decisions'][d['id']] = dict(d, owner=owner, reported_by=actor, status='pending', resolution=None, reported_at=at, reported_plan_version=s['plan_version'], reported_subjects=copy.deepcopy(s['plan']['subjects']), evidence_hashes=evidence(d['evidence'],root))
    elif kind == 'decision_start':
        obj(d, ['id', 'call'], ['replacement_reason','evidence'])
        dec = s['decisions'].get(d['id'])
        require(dec and dec['status']=='pending', 'decision not pending')
        obj(d['call'], ['tool','model','effort','conversation_id'])
        for key,value in d['call'].items(): string(value, 'decision.call.'+key)
        require(all(d['call'][k] == s['plan']['roles']['architect'][k] for k in ['tool','model','effort']), 'decision call configuration mismatch')
        require(d['call']['conversation_id'].startswith('pending:') or cli_context_resumable(d['call'],d['call']['conversation_id']), 'Codex decision context must be pending or a native UUID, not a logical name')
        check_arch_context(s, {'conversation_id':d['call']['conversation_id'],'mode':'fresh','reason':'decision handling'})
        check_decision_group(s,d['id'],d['call']['conversation_id'])
        if dec.get('handling'):
            previous=dec['handling']['call']
            require(previous['tool'] in {'codex','codex-cli'} and not previous['conversation_id'].startswith('pending:') and not cli_context_resumable(previous,previous['conversation_id']), 'handling already started; only an invalid legacy Codex context can be replaced here')
            string(d.get('replacement_reason'),'replacement_reason')
            require(bool(d.get('evidence')), 'legacy context replacement requires original failure evidence')
            dec.setdefault('handling_history',[]).append(dict(previous=copy.deepcopy(dec['handling']),replacement_reason=d['replacement_reason'],evidence_hashes=evidence(d['evidence'],root),replaced_at=at))
        else:
            require(not {'replacement_reason','evidence'} & d.keys(), 'replacement fields require legacy handling')
        dec['handling'] = {'started_at':at, 'call':copy.deepcopy(d['call'])}
    elif kind == 'resolve':
        obj(d, ['id', 'summary', 'requirements', 'evidence', 'user_evidence'], ['context','timing','call_id','clarification'])
        if 'context' in d:
            check_arch_context(s,d['context'])
            require(cli_context_resumable(s['plan']['roles']['architect'],d['context']['conversation_id']), 'decision result requires a real native context, not a logical name or pending ID')
            check_decision_group(s,d['id'],d['context']['conversation_id'])
        shared=s['plan']['roles']['architect'].get('principal')
        require(not shared or shared!=s['plan']['roles'].get('reviewer',{}).get('principal') or 'context' in d, 'combined technical lead must record decision context')
        dec = s['decisions'].get(d['id']); require(dec and dec['status'] == 'pending', 'decision is not pending')
        string(d['summary'], 'decision summary')
        clarification = d.get('clarification', False)
        boolean(clarification, 'clarification')
        if clarification:
            require(d['requirements'] == [] and not d['user_evidence'], 'clarification cannot contain recovery work or user approval')
            require(decision_owner(dec) == 'architect' and dec['signal'] != 'budget_exhausted' and not dec.get('source_key', '').startswith(('evidence:', 'confirmation:')), 'this decision requires explicit recovery conditions')
            require(dec.get('reported_plan_version') == s['plan_version'] and dec.get('reported_subjects') == s['plan']['subjects'], 'plan or subjects changed; record explicit recovery conditions')
        else:
            check_requirements(s, d['requirements'])
        existing_ids = {r['id'] for other in s['decisions'].values() if other['resolution'] for r in other['resolution']['requirements']}
        require(not existing_ids & {r['id'] for r in d['requirements']}, 'recovery requirement ids must be unique across decisions')
        user_hashes = evidence(d['user_evidence'], root) if d['user_evidence'] else {}
        require(decision_owner(dec) != 'user' or user_hashes, 'user-owned decision requires referenced user response')
        # Recovery work cannot block itself; blocks denote downstream actions, not every affected activity.
        require(not set(dec['blocks']) & {r['target'] for r in d['requirements'] if r['kind']=='task'}, 'recovery task blocks itself; scope blocks to downstream steps')
        if dec.get('handling') and d.get('context'):
            registered = dec['handling']['call']['conversation_id']
            require(registered.startswith('pending:') or registered == d['context']['conversation_id'], 'decision context differs from handling session')
            dec['handling']['call']['conversation_id'] = d['context']['conversation_id']
        dec['resolution'] = dict(d, evidence_hashes=evidence(d['evidence'], root), user_evidence=user_hashes)
        if 'timing' in d:
            require(dec.get('handling'), 'record decision_start before actual decision timing')
            dec['timing'] = checked_timing(d['timing'], dec['handling']['started_at'], at, root)
        if 'call_id' in d:
            string(d['call_id'], 'call_id'); dec['call_id'] = d['call_id']
        dec['status'] = 'decided'
        dec['resolved_at'] = at
        if clarification:
            dec.update(status='closed', applied_at=at, closed_at=at)
    elif kind == 'supersede_decision':
        obj(d, ['id', 'replacement', 'reason', 'evidence', 'context'])
        check_arch_context(s, d['context']); string(d['reason'], 'supersession reason')
        old = s['decisions'].get(d['id']); new = s['decisions'].get(d['replacement'])
        require(old and old.get('resolution') and old['status'] != 'superseded' and not decision_closed(s, old),
                'supersession requires an unresolved effective decision')
        require(new and new is not old and new.get('resolution') and new['status'] != 'superseded',
                'replacement must be a different resolved decision')
        require(old['blocks'] and all(s['plan']['tasks'][tid].get('retired') for tid in old['blocks']),
                'only a fully retired blocked scope can be superseded; active recovery gates remain mandatory')
        require(not any(not task.get('retired') and set(task['depends_on']) & set(old['blocks'])
                        for task in s['plan']['tasks'].values()), 'live tasks still depend on the old blocked scope')
        require(intact_evidence(new['resolution'].get('user_evidence', {})),
                'replacement requires intact original user scope authorization')
        require(intact_evidence(new['resolution'].get('evidence_hashes', {})),
                'replacement resolution evidence is missing or changed')
        require(any(c.get('decision_id') == d['replacement'] and c.get('plan_version', 0) > old.get('reported_plan_version', 0)
                    for c in s['state'].get('plan_changes', [])),
                'replacement must have an effective subsequent plan update')
        hashes = evidence(d['evidence'], root)
        old['supersession'] = dict(replacement=d['replacement'], reason=d['reason'], evidence_hashes=hashes,
                                   context=copy.deepcopy(d['context']), actor=actor, at=at, revision=s['revision']+1)
        old['status'] = 'superseded'
    elif kind == 'repin_requirement':
        obj(d, ['id', 'requirement', 'versions', 'evidence'], ['context', 'reason'])
        string(d['id'], 'decision id'); string(d['requirement'], 'requirement id')
        require('context' in d or 'reason' in d, 'repin requires context or reason')
        if 'context' in d: check_arch_context(s, d['context'])
        if 'reason' in d: string(d['reason'], 'repin reason')
        dec = s['decisions'].get(d['id'])
        require(dec and dec.get('resolution'), 'repin requires a resolved decision')
        require(dec['status'] != 'superseded', 'superseded decision cannot be repinned')
        req = next((r for r in dec['resolution']['requirements'] if r['id'] == d['requirement']), None)
        require(req and req['kind'] == 'task', 'repin requires an existing task requirement')
        require(isinstance(d['versions'], dict) and d['versions'] == current_subjects(s, req['target']),
                'repin versions must equal current target subject versions')
        require(d['versions'] != req['versions'], 'repin versions unchanged')
        hashes = evidence(d['evidence'], root)
        before = copy.deepcopy(req)
        req['versions'] = copy.deepcopy(d['versions'])
        audit = {'requirement': req['id'], 'before': before, 'after': copy.deepcopy(req),
                 'actor': actor, 'at': at, 'revision': s['revision'] + 1, 'evidence_hashes': hashes}
        for key in ['context', 'reason']:
            if key in d: audit[key] = copy.deepcopy(d[key])
        dec.setdefault('requirement_repins', []).append(audit)
    elif kind == 'decision_progress':
        obj(d, ['id', 'status', 'evidence'], ['requirement','budget_approval'])
        dec = s['decisions'].get(d['id']); require(dec and dec['resolution'], 'decision has no resolution')
        require(dec['status'] != 'superseded', 'superseded decision cannot confirm or close recovery conditions')
        if actor=='architect':
            local_wait = d['status'] == 'waiting' and not dec['blocks'] and any(r['id'] == d.get('requirement') and r.get('owner') == 'architect' for r in dec['resolution']['requirements'])
            require(locally_owned(s,dec['blocks']) or local_wait, 'architect may close only locally delegated decisions')
        hashes = evidence(d['evidence'], root)
        if d['status'] in {'waiting','confirmed','bind_budget_approval'}:
            req = next((r for r in dec['resolution']['requirements'] if r['id'] == d.get('requirement')), None)
            require(req and req['kind'] == 'confirmation', 'unknown confirmation requirement')
            if d['status'] == 'waiting':
                require('budget_approval' not in d, 'waiting cannot register budget approval')
                require(not requirement_ok(s, req), 'confirmation already satisfied')
                require(all(requirement_ok(s, next(r for r in dec['resolution']['requirements'] if r['id'] == dep)) for dep in req['depends_on']), 'confirmation dependencies not satisfied')
                waits = s['state'].setdefault('confirmation_waits', [])
                key = confirmation_wait_key(d['id'], req['id'], hashes)
                require(not any(w['requirement'] == req['id'] and confirmation_wait_pending(s, w) for w in waits), 'unresolved confirmation diagnosis; do not repeat check')
                require(not any(w['source_key'] == key for w in waits), 'unchanged wait evidence already registered')
                waits.append(dict(decision=d['id'], requirement=req['id'], source_key=key,
                                  evidence_hashes=hashes, actor=actor, at=at, revision=s['revision']+1))
                return s
            previous=s['state']['confirmations'].get(req['id'])
            if 'budget_approval' in d:
                require(req.get('owner')=='user','budget approval requires a user confirmation')
                validate_budget_approval(d['budget_approval'])
            if d['status']=='bind_budget_approval':
                require('budget_approval' in d and previous and previous.get('decision')==d['id'] and previous.get('evidence')==hashes,'bind an existing confirmation using the same intact original evidence')
                require(not previous.get('budget_approval'),'budget approval scope is already bound; do not overwrite historical authorization')
                previous['budget_approval']=copy.deepcopy(d['budget_approval'])
            else:
                require(not previous or not previous.get('budget_approval'),'budget approval scope is immutable; use a new user condition for new authorization')
                s['state']['confirmations'][req['id']] = {'decision': d['id'], 'evidence': hashes}
                if 'budget_approval' in d:s['state']['confirmations'][req['id']]['budget_approval']=copy.deepcopy(d['budget_approval'])
        else:
            require('budget_approval' not in d,'budget approval is only for confirmation registration')
            require(d['status'] in {'applied','closed'}, 'unknown decision status')
            require(all(requirement_ok(s,r) for r in dec['resolution']['requirements']), 'recovery requirements not satisfied')
            require(dec['status'] in {'decided', 'applied', 'closed'}, 'decision has not been resolved')
            if d['status'] == 'closed':
                dec.setdefault('applied_at', at)
            dec['status'] = d['status']
            dec[d['status']+'_at'] = at
    elif kind == 'feedback':
        obj(d, ['attempt', 'summary', 'evidence']); string(d['summary'], 'feedback summary')
        require(any(a['id']==d['attempt'] for a in s['state']['attempts']), 'unknown feedback attempt')
        s['state']['feedback'][d['attempt']] = {'at': at,'summary':d['summary'],'evidence':evidence(d['evidence'],root)}
    return s


def checked_timing(timing, registered_start, registered_end, root):
    obj(timing, ['started_at','ended_at','evidence'])
    begin, end = timestamp(timing['started_at']), timestamp(timing['ended_at'])
    require(timestamp(registered_start) <= begin <= end <= timestamp(registered_end), 'actual timing must lie within registration interval')
    return {'started_at':timing['started_at'], 'ended_at':timing['ended_at'], 'evidence':evidence(timing['evidence'],root)}


def finish(s, d, actor, root, at):
    obj(d, ['attempt', 'result', 'evidence'], ['closes', 'ended_at', 'call', 'subject_versions', 'timing', 'cause', 'call_id'])
    a = next((x for x in s['state']['attempts'] if x['id'] == d['attempt']), None)
    require(a is not None and a['result'] is None, 'attempt missing or already finished')
    require(tracking.accounting_kind(s,a)!='withdrawn','withdrawn start cannot be finished as a technical call')
    t = s['plan']['tasks'][a['task']]
    require(actor == a.get('performer_role',t['role']), 'result must be registered under its responsible role')
    if 'call' in d:
        obj(d['call'], ['tool','model','effort','conversation_id'])
        require(all(d['call'][k] == a.get('performer_config',s['plan']['roles'][t['role']])[k] for k in ['tool','model','effort']), 'actual call configuration mismatch')
        string(d['call']['conversation_id'], 'actual conversation id')
        require(a['call']['conversation_id'].startswith('pending:') or a['call']['conversation_id'] == d['call']['conversation_id'], 'actual session differs from registered session')
        require(not any(other['id'] != a['id'] and other['call']['conversation_id'] == d['call']['conversation_id'] for other in s['state']['attempts']) or t.get('session_policy') == 'resume', 'independent actual conversation reused')
        check_task_context(s,a['task'],d['call']['conversation_id'],exclude=a['id'])
        if a.get('executor_stage') in {'backup','lead'}:
            require(not any(other['id'] != a['id'] and other['call']['conversation_id'] == d['call']['conversation_id'] and other.get('executor_stage','primary') != a['executor_stage'] for other in s['state']['attempts']), 'actual executor handoff reused previous owner context')
        a['call'] = copy.deepcopy(d['call'])
    require(d['result'] in RESULTS, 'unsupported result')
    require(d['result'] == 'INVALID' or cli_context_resumable(a['call'],a['call']['conversation_id']), 'record actual conversation id before accepting a result')
    require(d['result'] not in {'VERIFIED','RELEASE'} or d['result'] == success_result(t), 'wrong successful result for task kind')
    if 'subject_versions' in d:
        require(t['role'] in {'executor', 'architect'} and t['kind'] in {'design','execute'}, 'only executor or architect delivery may declare updated subject versions')
        require(d['subject_versions'] == current_subjects(s,a['task']), 'delivery versions do not match current subjects')
        a['started_subjects']=copy.deepcopy(a['subjects']);a['subjects']=copy.deepcopy(d['subject_versions'])
    closes = d.get('closes', []); strings(closes, 'closes')
    for aid in closes:
        other = next((x for x in s['state']['attempts'] if x['id']==aid), None)
        require(other and other['task'] in t.get('can_close', []) and other['result'] not in {None,'INVALID'}, 'unplanned or invalid closure reference')
    ended = d.get('ended_at', at); require(timestamp(ended) >= timestamp(a['started_at']), 'end precedes start')
    if 'timing' in d:
        a['timing'] = checked_timing(d['timing'], a['started_at'], ended, root)
    if 'cause' in d:
        obj(d['cause'], ['category','summary'])
        require(d['cause']['category'] in tracking.CAUSES, 'unknown cause category')
        string(d['cause']['summary'], 'cause.summary'); a['cause'] = copy.deepcopy(d['cause'])
    if 'call_id' in d:
        string(d['call_id'], 'call_id')
        require(not a.get('call_id') or a['call_id']==d['call_id'], 'actual call_id differs from registered id')
        a['call_id'] = d['call_id']
    a.update(result=d['result'], ended_at=ended, evidence=evidence(d['evidence'], root), closes=closes)


def apply_request(s, request, root):
    obj(request, ['request_id', 'expected_revision', 'actor', 'operations'], ['at'])
    string(request['request_id'], 'request_id'); integer(request['expected_revision'], 'expected_revision')
    require(request['actor'] in ROLES, 'unknown actor')
    require(isinstance(request['operations'], list) and request['operations'], 'operations must be non-empty')
    fingerprint = digest(request)
    if s:
        validate_store(s)
        prior = next((h for h in s['history'] if h['request_id']==request['request_id']), None)
        if prior:
            require(prior['request_hash']==fingerprint, 'request_id reused with different payload')
            return s, False
    require(request['expected_revision'] == (s['revision'] if s else 0), 'revision conflict: reread status and rebase request')
    at = request.get('at', now()); timestamp(at)
    if s and not s['simulation']:
        require('at' not in request and not any('started_at' in op.get('data', {}) or 'ended_at' in op.get('data', {}) for op in request['operations']), 'backdated timestamps are simulation-only')
    draft = copy.deepcopy(s)
    if draft: tracking.observe(draft, status(draft, at), at)
    for op in request['operations']:
        draft = operate(draft, op, request['actor'], root, at)
        tracking.observe(draft, status(draft, at), at)
    validate_store(draft)
    require(draft['simulation'] or 'at' not in request, 'backdated timestamps are simulation-only')
    draft['revision'] += 1
    draft['history'].append({'revision':draft['revision'], 'request_id':request['request_id'], 'request_hash':fingerprint, 'actor':request['actor'], 'at':at, 'operations':copy.deepcopy(request['operations']), 'previous_store_hash':digest(s) if s else None})
    return draft, True


def locate():
    for parent in [Path.cwd(), *Path.cwd().parents]:
        candidate = parent/'.batch-flow'/'flow.json'
        if candidate.exists():
            return candidate
    return Path.cwd()/'.batch-flow'/'flow.json'


def write_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.flow-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value,out,ensure_ascii=False,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def save_record(root, value):
    """One automatically named result; callers reuse this path instead of copying it."""
    path = root / 'records' / (os.urandom(12).hex() + '.json')
    write_atomic(path, value)
    return str(path)


def issue_signal(s, key, root):
    """Mechanical signal data for a status issue, so the orchestrator copies nothing technical."""
    issue = next((i for i in issues(s) if i['key'] == key), None)
    require(issue is not None, 'no current issue with key ' + key + '; reread status')
    numbers = [int(m.group(1)) for d in s['decisions'] for m in [re.fullmatch(r'DEC-(\d+)', d)] if m]
    latest_attempt = latest(s, issue['blocks'][0]) if issue['blocks'] else None
    hashes = issue.get('evidence') or (latest_attempt or {}).get('evidence', {})
    require(not hashes or intact_evidence(hashes), 'issue evidence is missing or changed; preserve original evidence')
    evidence = list(hashes) or [save_record(root, {'issue': issue})]
    return dict(id='DEC-%02d' % (max(numbers, default=0) + 1), signal=issue['signal'], source_key=key,
                blocks=list(issue['blocks']), reason=issue['reason'], evidence=evidence)


def shortcut(args, root, s=None):
    require(args.revision is not None, 'mutation requires --revision from the inspected status')
    data = {}
    if getattr(args, 'from_issue', None):
        require(args.command == 'signal' and not args.data and s is not None, '--from-issue is only for signal on an existing store')
        data = issue_signal(s, args.from_issue, root)
    if args.data:
        data = json.loads(sys.stdin.read() if args.data == '-' else Path(args.data[1:]).read_text() if args.data.startswith('@') else args.data)
        require(isinstance(data, dict), '--data must contain an operation parameter object')
    for key in ['attempt', 'task', 'result', 'id', 'signal', 'reason', 'summary']:
        value = getattr(args, key)
        if value is not None:
            require(key not in data, 'duplicate parameter: ' + key)
            data[key] = value
    if args.blocks is not None:
        require('blocks' not in data, 'duplicate parameter: blocks')
        data['blocks'] = args.blocks
    if args.evidence:
        data.setdefault('evidence', []).extend(args.evidence)
    if args.note is not None:
        data.setdefault('evidence', []).append(save_record(root, {'note': args.note}))
    if args.git_ref:
        require(args.repo is not None, '--git-ref requires --repo')
        repo = args.repo.resolve()
        commit = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--end-of-options', args.git_ref + '^{commit}'], capture_output=True, text=True)
        require(commit.returncode == 0, 'cannot resolve Git commit: ' + commit.stderr.strip())
        data.setdefault('evidence', []).append(save_record(root, {'repository': str(repo), 'commit': commit.stdout.strip()}))
    if args.command == 'start':
        require(all([args.tool, args.model, args.effort, args.conversation]), 'start requires actual --tool --model --effort --conversation')
        require('call' not in data, 'use start flags for call, not --data.call')
        data['call'] = dict(tool=args.tool, model=args.model, effort=args.effort, conversation_id=args.conversation)
    command = args.command
    if command == 'resolve':
        data.setdefault('user_evidence', [])
        if args.clarification:
            data['clarification'] = True
            data.setdefault('requirements', [])
        if args.conversation:
            require('context' not in data, 'duplicate parameter: context')
            data['context'] = dict(conversation_id=args.conversation, mode=args.context_mode, reason='decision')
            if args.context_mode == 'resume':
                data['context']['source_conversation_id'] = args.conversation
    if command == 'close':
        command = 'decision_progress'
        require('status' not in data, 'close supplies status')
        data['status'] = 'closed'
    default_actor = {'start': 'orchestrator', 'close': 'orchestrator', 'resolve': 'architect'}.get(args.command)
    actor = args.actor or default_actor
    require(actor in ROLES, 'provide --actor for finish/signal or generic operation')
    return {'request_id': args.request_id or os.urandom(12).hex(), 'expected_revision': args.revision,
            'actor': actor, 'operations': [{'type': command, 'data': data}]}


def host_capabilities(store, environ=None):
    """Recover only the launch bound to this process and store; never search other sessions."""
    env=os.environ if environ is None else environ
    host='agy' if env.get('BATCH_FLOW_AGY_LAUNCH') else 'claude' if env.get('BATCH_FLOW_CLAUDE_LAUNCH') else None
    launch_id=env.get('BATCH_FLOW_AGY_LAUNCH') or env.get('BATCH_FLOW_CLAUDE_LAUNCH')
    if not launch_id:
        return dict(status='unbound',profiles={},reason='not running in an AGY/Claude Code launcher session; this does not mean a CLI is uninstalled')
    try:
        require(len(launch_id)==32 and all(c in '0123456789abcdef' for c in launch_id),'invalid launcher identity')
        store=Path(store).resolve();root=store.parent/('.agy-launch' if host=='agy' else '.claude-launch');runtime=(root/launch_id).resolve()
        require(runtime.parent==root.resolve(),'runtime escaped the bound management directory')
        spec=json.loads((runtime/'spec.json').read_text())
        require(spec.get('launch_id')==launch_id and Path(spec['store']).resolve()==store and Path(spec['runtime']).resolve()==runtime,'launcher/store binding mismatch')
        # Claude Code sessions are pre-bound with --session-id; AGY reports its ID at SessionStart.
        owner=json.loads((runtime/'owner.json').read_text()) if host=='agy' else {'conversation_id':spec.get('session_id')}
        require(bool(owner.get('conversation_id')),'SessionStart binding missing')
        profiles={}
        for name in spec.get('profiles',[]):
            path=Path(name).resolve()
            require(path.parent==runtime,'profile is outside the protected launch directory')
            if not path.is_file():continue
            profile=json.loads(path.read_text());backend=profile.get('backend')
            require(profile.get('format')=='batch-flow-cli/v1' and backend in {'agy','codex','claude'},'invalid launcher CLI profile')
            require(backend not in profiles,'ambiguous launcher backend profile')
            profiles[backend]=dict(path=str(path),version=profile['version'],binary=profile['binary'])
        return dict(status='bound',host=host,launch_id=launch_id,workspace=spec['workspace'],conversation_id=owner['conversation_id'],
                    task_base=spec.get('task_base'),store=str(store),python=spec['python'],profiles=profiles,
                    query_command=shlex.join([spec['python'],str(Path(__file__).resolve()),'capabilities','--store',str(store)]),
                    limit='Launcher metadata only; use agent_cli.py check/run for actual CLI availability. Never guess a profile or switch models because startup context was lost.')
    except (FlowError,OSError,ValueError,KeyError,TypeError) as error:
        return dict(status='unavailable',profiles={},reason=str(error))


def attach_host_capabilities(result, store):
    if not (os.environ.get('BATCH_FLOW_AGY_LAUNCH') or os.environ.get('BATCH_FLOW_CLAUDE_LAUNCH')):return result
    host=host_capabilities(store)
    result['host_capabilities']=host
    target=result.get('next',result)
    if target is not result:target['host_capabilities']=host
    for action in target.get('actions',[]):
        if host['status']=='bound' and action.get('orchestrator_action',{}).get('mode') in {'dispatch','delegate_to_lead'}:
            # Orchestrators (Claude Code and AGY) never compose a dispatch: `dispatch_card.py next` makes, starts and collects cards.
            action['next_command']=shlex.join([host['python'],str(Path(__file__).resolve().parent/'dispatch_card.py'),'next',
                                               '--store',str(Path(store).resolve())])
        if action.get('kind')=='CLOSE_DECISION':
            action['register_command']=shlex.join([host.get('python') or sys.executable,str(Path(__file__).resolve()),'--store',str(Path(store).resolve()),
                'close','--actor','orchestrator','--revision','<当前revision>','--id',action['target'],'--note','恢复条件已由 status 判定齐全，机械闭环'])
        if action.get('kind')=='REGISTER_DECISION' and action.get('issue'):
            action['register_command']=shlex.join([host.get('python') or sys.executable,str(Path(__file__).resolve()),'--store',str(Path(store).resolve()),
                'signal','--actor','orchestrator','--revision','<当前revision>','--from-issue',action['issue']['key']])
        adapter=action.get('cli_adapter')
        if adapter and host.get('host')=='claude' and adapter.get('tool')=='claude':
            # In a Claude Code host bare `claude` is a subagent (see dispatch_card.py); only claude-cli is a nested CLI.
            action.pop('cli_adapter');continue
        if not adapter:continue
        profile=host.get('profiles',{}).get(adapter['backend'])
        if host['status']=='bound' and profile:
            adapter['profile']=profile['path']
            adapter['capabilities_command']=host['query_command']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',nargs='?',default='status',choices=['status','capabilities','pre_yield','apply','validate','render','observe','metrics','start','finish','signal','resolve','repin_requirement','supersede_decision','classify_attempt','resume_attempt','set_models','set_user_mode','register_dispatch','close','record','snapshot','authorize','change_subject','feedback','decision_progress','decision_start','init','plan_update','design_refresh'])
    parser.add_argument('--store',type=Path)
    parser.add_argument('--request',type=Path)
    parser.add_argument('--revision', type=int)
    parser.add_argument('--request-id')
    parser.add_argument('--actor', choices=sorted(ROLES))
    parser.add_argument('--data', help='operation parameters as JSON, @file, or - for stdin; no request envelope')
    for flag in ['attempt', 'task', 'id', 'signal', 'reason', 'summary', 'note', 'tool', 'model', 'effort', 'conversation']:
        parser.add_argument('--' + flag)
    parser.add_argument('--result', choices=sorted(RESULTS))
    parser.add_argument('--evidence', action='append', default=[])
    parser.add_argument('--blocks', nargs='*')
    parser.add_argument('--from-issue', help='signal: build the decision from this status issue key (REGISTER_DECISION)')
    parser.add_argument('--clarification', action='store_true')
    parser.add_argument('--context-mode', choices=['fresh','resume'], default='fresh')
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--git-ref')
    parser.add_argument('--cwd', type=Path)
    parser.add_argument('--audit-log',type=Path,help='bound role audit JSONL; unavailable audit blocks orchestrator yield')
    parser.add_argument('--audit-management-root',type=Path,action='append',default=[])
    parser.add_argument('--paths', nargs='+')
    # Separate the child argv before argparse; never invoke a shell.
    argv = sys.argv[1:]
    child = []
    if '--' in argv:
        split = argv.index('--'); child = argv[split+1:]; argv = argv[:split]
    args=parser.parse_args(argv);path=(args.store or locate()).resolve()
    try:
        if args.command=='capabilities':
            result=host_capabilities(path)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result['status']=='bound' else 2
        if args.command == 'snapshot':
            require(not child and args.repo is not None and args.paths, 'snapshot requires --repo and explicit --paths')
            repo = args.repo.resolve()
            # Literal pathspecs: user-selected files, never repository-wide implicit capture.
            selected = []
            for name in args.paths:
                selected.append(str((repo / name).resolve().relative_to(repo)))
            def git(*parts):
                run = subprocess.run(['git', '--literal-pathspecs', '-C', str(repo), *parts], capture_output=True)
                require(run.returncode == 0, run.stderr.decode(errors='replace'))
                return run.stdout
            head = git('rev-parse', '--verify', 'HEAD').decode().strip()
            diff = git('diff', '--binary', 'HEAD', '--', *selected).decode(errors='replace')
            untracked = {}
            for raw in git('ls-files', '-z', '--others', '--exclude-standard', '--', *selected).split(b'\0'):
                if raw:
                    name = os.fsdecode(raw)
                    candidate = (repo / name).resolve()
                    candidate.relative_to(repo)
                    require(not (repo / name).is_symlink(), 'untracked symlink: use an explicit export tool')
                    untracked[name] = base64.b64encode(candidate.read_bytes()).decode('ascii')
            result_path = save_record(path.parent, dict(repository=str(repo), base_commit=head, paths=selected, diff=diff, untracked_base64=untracked))
            print(json.dumps({'ok':True,'evidence':result_path}, ensure_ascii=False))
            return 0
        if args.command == 'record':
            require(child, 'record requires -- COMMAND [ARG ...]')
            cwd = (args.cwd or Path.cwd()).resolve()
            started = now()
            run = subprocess.run(child, cwd=cwd, capture_output=True, text=True, errors='replace')
            result_path = save_record(path.parent, dict(command=child, cwd=str(cwd), started_at=started, ended_at=now(), exit_code=run.returncode, stdout=run.stdout, stderr=run.stderr))
            print(json.dumps({'ok':run.returncode == 0,'evidence':result_path,'exit_code':run.returncode}, ensure_ascii=False))
            return run.returncode if 0 <= run.returncode <= 125 else 1
        require(not child, '-- COMMAND is only supported by record')
        if args.command not in {'status','validate','render','metrics','pre_yield'}:
            require(args.command!='apply' or args.request is not None,'apply requires --request FILE')
            path.parent.mkdir(parents=True,exist_ok=True)
            with open(str(path)+'.lock','a+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                s=json.loads(path.read_text()) if path.exists() else None
                if args.command=='observe':
                    require(s is not None, 'initialize workflow before observe')
                    at=now(); probe=copy.deepcopy(s)
                    changed=tracking.observe(probe,status(probe,at),at)
                    if not changed:
                        print(json.dumps({'ok':True,'changed':False,'revision':s['revision'],'next':status(s,at)},ensure_ascii=False,indent=2))
                        return 0
                    request={'request_id':'observe-'+os.urandom(12).hex(),'expected_revision':s['revision'],'actor':args.actor or 'orchestrator','operations':[{'type':'observe','data':{}}]}
                elif args.command == 'apply':
                    request=json.loads(args.request.read_text())
                else:
                    # Check CAS before creating any automatic evidence files.
                    require(args.revision == (s['revision'] if s else 0), 'revision conflict: reread status and rebase request')
                    request=shortcut(args,path.parent,s)
                new,changed=apply_request(s,request,path.parent)
                if changed:write_atomic(path,new)
            result={'ok':True,'changed':changed,'revision':new['revision'],'next':status(new)}
        else:
            require(path.is_file(),f'no workflow store: {path}; initialize with architect init request')
            # Atomic replacement ensures readers observe an entire old/new document; queries do not write lockfiles.
            s=json.loads(path.read_text());validate_store(s)
            if args.command=='validate':
                coverage=active_review_budget_coverage(s)
                print(json.dumps(dict(ok=coverage['ok'],revision=s['revision'],review_budget_coverage=coverage),ensure_ascii=False,indent=2))
                return 0 if coverage['ok'] else 2
            if args.command=='pre_yield':
                boundary_audit=None
                if args.audit_log and (args.actor or 'orchestrator')=='orchestrator':
                    from role_audit import audit
                    boundary_audit=audit(args.audit_log,args.conversation,[path.parent]+args.audit_management_root)
                result=attach_host_capabilities(pre_yield(s,args.actor or 'orchestrator',boundary_audit,path.parent),path)
                print(json.dumps(result,ensure_ascii=False,indent=2))
                return 0 if result['can_yield'] else 1
            result={'ok':True,'revision':s['revision']} if args.command=='validate' else tracking.metrics(s,status(s),now()) if args.command=='metrics' else status(s)
            if args.command=='render':
                print(f'# {s["plan"]["name"]}\n\nRevision: {s["revision"]}\n\n| 批次 | 状态 |\n|---|---|')
                for b,st in result['batches'].items():print(f'| {b} | {st} |')
                print('\n下一步：\n')
                for a in result['actions']:print(f'- {a["kind"]} / {a["target"]} / {a["role"]}: {a.get("orchestrator_todo",a.get("instruction",""))}')
                return 0
        print(json.dumps(attach_host_capabilities(result,path),ensure_ascii=False,indent=2));return 0
    except (FlowError,ValueError,OSError,KeyError,TypeError,OverflowError) as e:
        print(json.dumps({'ok':False,'error':str(e)},ensure_ascii=False));return 2

if __name__=='__main__':
    raise SystemExit(main())
