"""Structural delivery gate (design 10, I8): parsing and legality only, no model. Adapted from v1 dispatch_card."""
import os
import re
import shlex
from pathlib import Path

from . import evidence as evidence_mod, model, routing, permissions, questions

_LEAD = r'^[ \t*#>-]*'
_SEP = r'[ \t]*[:：][ \t]*\**[ \t]*'
FINDING = re.compile(_LEAD + r'阻断' + _SEP + r'(\S+?)[ \t]*[|｜][ \t]*返修[ \t]*[:：][ \t]*(\S+?)[ \t]*[|｜][ \t]*'
                     r'方案级[ \t]*[:：][ \t]*(是|否)[ \t]*[|｜][ \t]*关闭条件[ \t]*[:：][ \t]*(.+?)\s*$', re.M)
SUCCESS = {'implement': ('VERIFIED',), 'repair': ('VERIFIED',), 'review': ('RELEASE', 'FIX_REQUIRED'),
           'decide': ('VERIFIED',), 'triage': ('VERIFIED',)}
ACTIONS = ('settle', 'open', 'supersede', 'retry', 'plan_revision', 'grant', 'ask_user', 'refresh_design', 'authorize', 'blocked')
OPENABLE = ('implement', 'repair', 'review', 'triage')
ACTION_FIELDS = {
    'settle': {'settle', 'charge', 'reason'},
    'open': {'open', 'target', 'input', 'needs', 'allow_protected', 'fresh_session'},
    'authorize': {'authorize', 'paths'}, 'retry': {'retry', 'reason', 'fresh_session', 'performer'},
    'supersede': {'supersede', 'reason'}, 'grant': {'grant'}, 'ask_user': {'ask_user', 'options', 'user_request'},
    'blocked': {'blocked', 'reason', 'missing'},
    'plan_revision': {'plan_revision', 'within_authorization', 'revalidate'}, 'refresh_design': {'refresh_design'},
}


def field(text, name):
    m = re.search(_LEAD + name + _SEP + r'(.*)$', text or '', re.M)
    return m.group(1).strip().strip('*').strip() if m else ''


def candidates(text, label='候选新项'):
    lines = (text or '').splitlines()
    for i, line in enumerate(lines):
        m = re.match(_LEAD + label + _SEP + r'(.*)$', line)
        if not m:
            continue
        out = [m.group(1).strip()] if m.group(1).strip() and not m.group(1).strip().startswith('无') else []
        for nxt in lines[i + 1:]:
            b = re.match(r'^\s*(?:[-*•]|\d+[.)、])\s+(.*\S)\s*$', nxt)
            if not b:
                break
            out.append(b.group(1))
        return out
    return []


def evidence_references(base, text):
    """Preserve cited record paths even when missing or malformed; roles assess their significance."""
    records = os.path.normpath(str(Path(base) / 'records'))
    out = []
    for name in ('验证证据', '复跑与证据'):
        for tok in re.findall(r'(?:/|records/)[^\s,;，；、"\'`()（）|<>]+', field(text, name)):
            path = os.path.normpath(tok.rstrip('.。') if tok.startswith('/') else str(Path(base) / tok.rstrip('.。')))
            if path.startswith(records + os.sep) and path not in out:
                out.append(path)
    return out


def cited_evidence(base, text):
    return [p for p in evidence_references(base, text) if evidence_mod.is_evidence(p)]


def parse(text, base):
    m = re.search(_LEAD + r'结果' + _SEP + r'([A-Z_]+)', text or '', re.M)
    issue = field(text, '上升问题')
    return {'result': m.group(1) if m else None, 'why': field(text, '停止原因'), 'issue': issue,
            'blocker': field(text, '问题标识')[:200],
            'has_issue': bool(issue) and issue not in ('无', '无。'),
            'intent': field(text, '下一步'), 'handoff': field(text, '移交说明'),
            'findings': [{'id': a, 'target': b, 'plan_level': c == '是', 'closure': d}
                         for a, b, c, d in FINDING.findall(text or '')],
            'candidates': candidates(text), 'suggestions': candidates(text, '非阻断建议'), 'evidence': cited_evidence(base, text),
            'citations': [{'path': p, 'valid_record': evidence_mod.is_evidence(p)} for p in evidence_references(base, text)],
            'fixed': field(text, '固定版本'), 'not_rerun': field(text, '未复跑原因'),
            'version': (field(text, '改动与版本') or field(text, '固定版本'))[:1000]}


# ---- environment evidence -------------------------------------------------------------------------

def direct_entry(command, entries):
    """True only for a bare entry call: compound commands or extra shells are not matched by host allow rules."""
    try:
        words = shlex.split(command)
        if len(words) == 3 and Path(words[0]).name in ('bash', 'sh') and words[1] in ('-c', '-lc'):
            command = words[2]
            words = shlex.split(command)
    except ValueError:
        return False
    return bool(words) and not re.search(r'[;&|<>`$()\n]', command) and words[0] in entries


def controlled_wrapper(command, entries):
    """Recognize execution wrappers, without confusing reads of a runner file with executing it."""
    try:
        words = shlex.split(command)
        if len(words) == 3 and Path(words[0]).name in ('bash', 'sh') and words[1] in ('-c', '-lc'):
            command = words[2]
            words = shlex.split(command)
        if not words or direct_entry(command, entries):
            return False
        if words[0] in entries:
            return True
        wrappers = ('python', 'python3', 'python3.8', 'bash', 'sh', 'env', 'timeout', 'sudo')
        return Path(words[0]).name in wrappers and any(e in words[1:] for e in entries)
    except ValueError:
        return False


def scan(run, events, env):
    """Fold one segment's CLI events into the run (edited / failed / latest controlled-entry call).
    Returns True when this segment edited files."""
    edited = False
    for ev in events or ():
        item = ev.get('item') or ev
        if item.get('type') == 'file_change':
            edited = True
        elif item.get('type') == 'command_execution' and item.get('exit_code') is not None:
            run['commands_seen'] = run.get('commands_seen', 0) + 1
            if item['exit_code'] != 0:
                run['failed'] = True
            if env and direct_entry(item.get('command') or '', env.get('entry') or []):
                run['entry'] = {'exit_code': item['exit_code'], 'output': (item.get('aggregated_output') or '')[-4000:]}
                run.pop('controlled_wrapper', None)
            elif env and controlled_wrapper(item.get('command') or '', env.get('entry') or []):
                run['controlled_wrapper'] = True
                run['entry'] = None
    run['edited'] = run.get('edited', False) or edited
    return edited


# ---- legality -------------------------------------------------------------------------------------

def validate_decision(state, decision, order=None):
    plan = state.plan
    if not isinstance(decision, dict) or not isinstance(decision.get('actions'), list) or not decision.get('diagnosis'):
        return 'decision.json 需要 diagnosis 与 actions'
    if order and order.get('input', {}).get('subject') and not decision['actions']:
        return '解除阻塞的裁决须有结构化处置；空动作不能宣布恢复'
    if len(decision['actions']) > 1 and any(isinstance(a, dict) and 'ask_user' in a for a in decision['actions']):
        return 'ask_user 用于独立方向问题；状态变更用结构化动作整包自动请求批准，不能混合重复提问'
    settlements = {}
    for a in decision['actions']:
        if isinstance(a, dict) and 'settle' in a:
            rid = a['settle']
            if not isinstance(rid, str) or type(a.get('charge')) is not int or a['charge'] not in (0, 1):
                return 'settle 需要 run_id 与 charge 整数 0/1'
            if rid in settlements and settlements[rid] != a['charge']:
                return '同一运行结算冲突'
            settlements[rid] = a['charge']
    if order and order['input'].get('accounting_only'):
        if len(decision['actions']) != 1 or list(settlements) != [order['input']['run']]:
            return '预算结算单仅可结算指定运行，不得审阅或恢复工程工作'
    for a in decision['actions']:
        kinds = [k for k in ACTIONS if isinstance(a, dict) and k in a]
        if len(kinds) != 1:
            return '每个处置动作须恰有一种动作键: %s' % '/'.join(ACTIONS)
        k = kinds[0]
        if set(a) - ACTION_FIELDS[k]:
            return '%s 含不支持的动作字段，不能静默忽略处置要求' % k
        if k == 'settle':
            from . import policy
            found = [(o, r) for o in state.orders.values() for r in o['runs'] if r['run_id'] == a[k]]
            subject = state.orders.get(order['input'].get('subject')) if order else None
            if len(found) != 1 or not subject:
                return 'settle 须引用本裁决对象的运行'
            owner, run = found[0]
            if owner['type'] not in ('implement', 'repair', 'review') or not run.get('budget_managed') or not run['ended'] or policy.budget_key(owner) != policy.budget_key(subject):
                return 'settle 须为同预算已结束的新预算协议技术运行；旧记录沿用历史修复流程'
            old = owner.get('settlements', {}).get(run['run_id'])
            if old and old['charge'] != a['charge']:
                return '已结算运行不能改写'
            if not isinstance(a.get('reason'), str) or not a['reason'].strip():
                return '结算须说明依据'
        if k == 'ask_user':
            reason = questions.validate(a.get('user_request'))
            if reason:
                return reason
            if not isinstance(a['ask_user'], str) or not a['ask_user'].strip():
                return 'ask_user 须有明确问题'
        if k == 'blocked':
            if not isinstance(a[k], str) or a[k] not in state.orders:
                return 'blocked 须指向现有工单'
            if state.orders[a[k]]['state'] not in ('open', 'failed'):
                return 'blocked 只能暂停未完成且未运行的工单'
            if not all(isinstance(a.get(x), str) and a[x].strip() for x in ('reason', 'missing')):
                return 'blocked 须记录原因和最小缺项'
        if k == 'authorize':
            if not isinstance(a['authorize'], str):
                return 'authorize 须指定工单号'
            obj = state.orders.get(a['authorize'])
            if not obj or obj['type'] not in ('implement', 'repair') or obj['state'] not in ('open', 'failed'):
                return 'authorize 须指向未完成且未运行的实施/返修原单'
            if not routing.current_work(state, obj) or not permissions.validate_paths(a.get('paths')):
                return 'authorize 须为当前契约的精确仓库相对路径，不允许目录通配符'
        if k == 'retry' and a.get('allow_protected'):
            return 'retry 不登记权限；请使用 authorize 动作'
        if k == 'open' and (a['open'] not in OPENABLE or a.get('target') not in
                            list(plan['deliverables']) + list(plan['review_groups']) + [b['id'] for b in plan['batches']]):
            return 'open 动作的类型或目标无效'
        if k == 'open':
            target, kind, inp = a['target'], a['open'], a.get('input') or {}
            known = {'implement': plan['deliverables'], 'repair': plan['deliverables'],
                     'review': plan['review_groups'], 'triage': {b['id'] for b in plan['batches']}}
            if target not in known[kind] or not isinstance(inp, dict):
                return 'open 动作的类型与目标不匹配'
            if 'accounting_only' in inp:
                return 'accounting_only 是内部结算单标记，open 不能声明'
            for paths in (a.get('allow_protected'), inp.get('allow_protected'), inp.get('required_protected')):
                if paths is not None and (not isinstance(paths, list) or any(not model.valid_exact_path(p) for p in paths)):
                    return 'open 的权限清单须为精确仓库相对路径'
            deps = a.get('needs', [])
            if not isinstance(deps, list) or any(n not in state.orders for n in deps):
                return 'open.needs 须引用已存在的工单'
            if kind == 'repair' and (inp.get('review_order') not in state.orders or not inp.get('findings') or
                                     not inp.get('review_run')):
                return 'repair 须指定 review_order、review_run 与 findings'
            # A disposition cannot depend on the waiter it is meant to unblock.
            waiter = order['input'].get('subject') if order else None
            pending = list(deps)
            if kind == 'review':
                pending += routing.review_needs(state, target)
            else:
                for d in plan['deliverables'].get(target, {}).get('depends_on', []):
                    pending += [o['id'] for o in state.orders.values() if o['target'] == d and o['type'] in ('implement', 'review')]
            seen = set()
            while pending:
                n = pending.pop()
                if n == waiter:
                    return '裁决处置依赖被裁决工单，会形成等待环；请调整处置方案'
                if n not in seen and n in state.orders:
                    seen.add(n)
                    pending.extend(state.orders[n]['needs'])
        if k in ('supersede', 'retry') and a[k] not in state.orders:
            return '%s 指向不存在的工单' % k
        if k == 'retry':
            from . import policy
            obj = state.orders[a[k]]
            if obj['state'] not in ('open', 'failed'):
                return 'retry 只能恢复未完成且未运行的工单'
            if any(not r['ended'] or r['run_id'] not in settlements for _, r in policy.unsettled(state, obj)):
                return 'retry 前须明确结算全部待定运行'
            if obj.get('control_protocol') == 'intent-v1' or 'performer' in a:
                if obj['type'] not in ('implement', 'repair') or a.get('performer') not in dict(policy.ladder(plan, obj)) or not isinstance(a.get('reason'), str) or not a['reason'].strip():
                    return '实施/返修 retry 须显式选择 performer 并说明继续依据'
        if k == 'grant' and not isinstance(a['grant'], dict):
            return 'grant 须为 {目标: {额度项: 增量}}'
        if k == 'grant':
            for owner, values in a['grant'].items():
                target = state.orders[owner]['target'] if owner in state.orders else owner
                if target not in plan['deliverables'] and target not in plan['review_groups']:
                    return 'grant 目标无效'
                allowed = {'review'} if target in plan['review_groups'] else {'implement', 'repair', 'backup', 'lead', 'review'}
                if not isinstance(values, dict) or not values or any(
                        key not in allowed or type(n) is not int or n <= 0 for key, n in values.items()):
                    return 'grant 须指定正整数额度；审阅组只能追加 review'
        if k == 'refresh_design' and a[k] is not True:
            return 'refresh_design 须为 true'
        if k == 'plan_revision' and not isinstance(a['plan_revision'], str):
            return 'plan_revision 须为草案路径'
        if k == 'plan_revision' and ('revalidate' in a):
            if not isinstance(a['revalidate'], list) or any(d not in plan['deliverables'] for d in a['revalidate']):
                return 'revalidate 须列出现有交付目标，影响范围由负责人明确选择'
        if k == 'plan_revision' and 'within_authorization' in a:
            from . import changes
            if a['within_authorization'] is not True or not changes.internal_revision(plan, a['plan_revision']):
                return '包络内计划调整不得改变目标、验收、权限、预算、资源或必需审阅；实质变化转用户'
    subject = state.orders.get(order['input'].get('subject')) if order else None
    if subject and subject.get('awaiting_disposition') and not order['input'].get('accounting_only'):
        if not any(a.get(k) == subject['id'] for a in decision['actions'] for k in ('retry', 'blocked', 'supersede')) and not any('ask_user' in a for a in decision['actions']):
            return '移交须明确恢复、保留阻塞或替换原单，不能只结算后自动恢复'
    return ''


def judge_intent(order, text, run=None):
    if run and run.get('controlled_wrapper'):
        return False, '受控入口须按工单直接调用；包装调用先纠正，不能据此判定环境故障'
    rows = re.findall(_LEAD + '下一步' + _SEP + r'(.*)$', text or '', re.M)
    intent = field(text, '下一步')
    if len(rows) != 1 or intent not in ('继续', '交付', '移交') or field(text, '结果') or field(text, '停止原因'):
        return False, '须恰有一个下一步：继续/交付/移交，不混用旧协议'
    if order['type'] == 'repair' and intent == '交付':
        tail = text[text.find('阻断关闭'):] if '阻断关闭' in text else ''
        if any(not re.search(r'(?<![\w])%s\s*=' % re.escape(f['id']), tail) for f in order['input'].get('findings', [])):
            return False, '交付须逐项报告阻断关闭'
    return True, intent


def judge(state, order, run, p, decision, text):
    """Returns (legal, reason)."""
    t, res, why = order['type'], p['result'], p['why']
    if run.get('controlled_wrapper'):
        return False, '受控入口必须以绝对路径直接调用；不要外套 bf record、Python、timeout 或 shell。入口自行留证；请纠正调用后重试，包装调用不能证明宿主环境故障。'
    if res in SUCCESS.get(t, ()):
        reason = ''
        if not reason and t == 'repair':
            tail = text[text.find('阻断关闭'):] if '阻断关闭' in text else ''
            missing = [f['id'] for f in order['input'].get('findings', [])
                       if not re.search(r'(?<![\w])%s\s*=' % re.escape(f['id']), tail)]
            reason = '缺少阻断关闭行: %s' % ', '.join(missing) if missing else ''
        if not reason and t == 'review' and res == 'FIX_REQUIRED':
            members = state.plan['review_groups'].get(order['target'], {}).get('deliverables', [])
            if not p['findings']:
                reason = 'FIX_REQUIRED 须含结构化阻断行（阻断：Bn | 返修:目标 | 方案级:是/否 | 关闭条件:...）'
            elif [f for f in p['findings'] if f['target'] not in members]:
                reason = '返修目标不在审阅组内'
        if not reason and t == 'decide':
            reason = validate_decision(state, decision, order)
        if not reason and t == 'triage':
            ids = [i['id'] for i in order['input'].get('items', [])]
            items = (decision or {}).get('items') if isinstance(decision, dict) else None
            if not isinstance(items, dict) or sorted(items) != sorted(ids):
                reason = '分诊须逐项给出去向'
            elif [i for i in items.values() if not isinstance(i, dict) or i.get('disposition') not in ('merge', 'defer', 'drop')
                  or (i['disposition'] == 'merge' and i.get('target') not in state.plan['deliverables'])]:
                reason = '去向须为 merge(含目标)/defer/drop'
        return (not reason), (reason or res)
    if res == 'VERIFIED' or res == 'RELEASE' or res == 'FIX_REQUIRED':
        return False, '结果 %s 不适用于 %s 工单' % (res, t)
    if t in ('decide', 'triage'):
        return False, '协调工单须交付 VERIFIED 与合法结构化处置；需要用户时用 ask_user 动作'
    if why.startswith('待裁决'):
        return (res == 'ARCH_DECISION_REQUIRED' and p['has_issue']), '待裁决须为 ARCH_DECISION_REQUIRED 且写明上升问题'
    if why.startswith('技术不可行'):
        return (res == 'FAILED' and p['has_issue']), '技术不可行须为 FAILED 且在上升问题写明已试方案'
    if why.startswith('外部阻塞'):
        if res != 'WAIT_EVIDENCE':
            return False, '外部阻塞须为 WAIT_EVIDENCE'
        return bool(p['has_issue']), '外部阻塞须说明原因；诊断与恢复由技术负责人判断'
    if why.startswith('完成'):
        return False, '停止原因为完成但结果不符'
    return False, '停止原因为“%s”：尚未完成' % (why or '缺失')
