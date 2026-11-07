"""Structural delivery gate (design 10, I8): parsing and legality only, no model. Adapted from v1 dispatch_card."""
import os
import re
import shlex
from pathlib import Path

from . import evidence as evidence_mod, model, routing

_LEAD = r'^[ \t*#>-]*'
_SEP = r'[ \t]*[:：][ \t]*\**[ \t]*'
FINDING = re.compile(_LEAD + r'阻断' + _SEP + r'(\S+?)[ \t]*[|｜][ \t]*返修[ \t]*[:：][ \t]*(\S+?)[ \t]*[|｜][ \t]*'
                     r'方案级[ \t]*[:：][ \t]*(是|否)[ \t]*[|｜][ \t]*关闭条件[ \t]*[:：][ \t]*(.+?)\s*$', re.M)
SUCCESS = {'implement': ('VERIFIED',), 'repair': ('VERIFIED',), 'review': ('RELEASE', 'FIX_REQUIRED'),
           'decide': ('VERIFIED',), 'triage': ('VERIFIED',)}
ACTIONS = ('open', 'supersede', 'retry', 'plan_revision', 'grant', 'ask_user', 'refresh_design')
OPENABLE = ('implement', 'repair', 'review', 'triage')


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


def cited_evidence(base, text):
    """Existing files under <base>/records cited on the evidence lines."""
    records = os.path.normpath(str(Path(base) / 'records'))
    out = []
    for name in ('验证证据', '复跑与证据'):
        for tok in re.findall(r'(?:/|records/)[^\s,;，；、"\'`()（）|<>]+', field(text, name)):
            path = os.path.normpath(tok.rstrip('.。') if tok.startswith('/') else str(Path(base) / tok.rstrip('.。')))
            if path.startswith(records + os.sep) and os.path.isfile(path) and path not in out and evidence_mod.is_evidence(path):
                out.append(path)
    return out


def parse(text, base):
    m = re.search(_LEAD + r'结果' + _SEP + r'([A-Z_]+)', text or '', re.M)
    issue = field(text, '上升问题')
    return {'result': m.group(1) if m else None, 'why': field(text, '停止原因'), 'issue': issue,
            'has_issue': bool(issue) and not issue.startswith('无'),
            'findings': [{'id': a, 'target': b, 'plan_level': c == '是', 'closure': d}
                         for a, b, c, d in FINDING.findall(text or '')],
            'candidates': candidates(text), 'suggestions': candidates(text, '非阻断建议'), 'evidence': cited_evidence(base, text),
            'fixed': field(text, '固定版本'), 'not_rerun': field(text, '未复跑原因'),
            'version': (field(text, '改动与版本') or field(text, '固定版本'))[:1000]}


# ---- environment evidence -------------------------------------------------------------------------

def direct_entry(command, entries):
    """True only for a bare entry call: compound commands or extra shells are not matched by host allow rules."""
    try:
        words = shlex.split(command)
        if len(words) == 3 and Path(words[0]).name in ('bash', 'sh') and words[1] in ('-c', '-lc'):
            if re.search(r'[;&|<>`$()\n]', words[2]):
                return False
            words = shlex.split(words[2])
    except ValueError:
        return False
    return bool(words) and any(words[0].startswith(e) for e in entries)


def scan(run, events, env):
    """Fold one segment's CLI events into the run (edited / failed / latest controlled-entry call).
    Returns True when this segment edited files."""
    edited = False
    for ev in events or ():
        item = ev.get('item') or ev
        if item.get('type') == 'file_change':
            edited = True
        elif item.get('type') == 'command_execution' and item.get('exit_code') is not None:
            if item['exit_code'] != 0:
                run['failed'] = True
            if env and direct_entry(item.get('command') or '', env.get('entry') or []):
                run['entry'] = {'exit_code': item['exit_code'], 'output': (item.get('aggregated_output') or '')[-4000:]}
    run['edited'] = run.get('edited', False) or edited
    return edited


def new_records(base, since):
    try:
        return any(p.stat().st_mtime >= since for p in (Path(base) / 'records').iterdir() if p.is_file())
    except OSError:
        return False


def environment_blocked(env, run):
    """The claim stands only on the latest direct controlled-entry run failing with the entry's environment output."""
    if not env:
        return bool(run.get('failed'))
    entry = run.get('entry')
    if not entry or entry['exit_code'] == 0:
        return False
    return not env.get('blocked_output') or bool(re.search(env['blocked_output'], entry['output']))


# ---- legality -------------------------------------------------------------------------------------

def validate_decision(state, decision):
    plan = state.plan
    if not isinstance(decision, dict) or not isinstance(decision.get('actions'), list) or not decision.get('diagnosis'):
        return 'decision.json 需要 diagnosis 与 actions'
    for a in decision['actions']:
        kinds = [k for k in ACTIONS if isinstance(a, dict) and k in a]
        if len(kinds) != 1:
            return '每个处置动作须恰有一种动作键: %s' % '/'.join(ACTIONS)
        k = kinds[0]
        if k == 'open' and (a['open'] not in OPENABLE or a.get('target') not in
                            list(plan['deliverables']) + list(plan['review_groups']) + [b['id'] for b in plan['batches']]):
            return 'open 动作的类型或目标无效'
        if k in ('supersede', 'retry') and a[k] not in state.orders:
            return '%s 指向不存在的工单' % k
        if k == 'grant' and not isinstance(a['grant'], dict):
            return 'grant 须为 {目标: {额度项: 增量}}'
        if k == 'refresh_design' and a[k] is not True:
            return 'refresh_design 须为 true'
        if k == 'plan_revision' and not isinstance(a['plan_revision'], str):
            return 'plan_revision 须为草案路径'
    return ''


def judge(state, order, run, p, decision, text):
    """Returns (legal, reason)."""
    t, res, why = order['type'], p['result'], p['why']
    if res in SUCCESS.get(t, ()):
        reason = ''
        if t in ('implement', 'repair') or (t == 'review' and res == 'RELEASE'):
            reason = '' if p['evidence'] else '须引用 bf record / bf snapshot 生成的 records/ 证据文件（手写文件不算证据）'
            elsewhere = set(path for o in state.orders.values() for r in o['runs'] if r['run_id'] != run['run_id'] for path in r['evidence'])
            if not reason and not (res == 'RELEASE' and p['not_rerun']) and not evidence_mod.fresh_pass(
                    p['evidence'], run['started_ts'] - 2, elsewhere):
                reason = '须至少引用一份本次运行内生成、退出码为 0 的 record（审阅 RELEASE 不能只引用他人记录，否则写“未复跑原因：”）'
        if not reason and t == 'review':
            need_paths = [x for d in state.plan['review_groups'].get(order['target'], {}).get('deliverables', [])
                          for x in routing.latest_snapshots(state, d)]
            missing = [x for x in need_paths if x not in p['fixed']]
            reason = '固定版本须包含被审交付目标的最近快照路径：%s' % '、'.join(missing) if missing else ''
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
            reason = validate_decision(state, decision)
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
    if why.startswith('待裁决'):
        return (res == 'ARCH_DECISION_REQUIRED' and p['has_issue']), '待裁决须为 ARCH_DECISION_REQUIRED 且写明上升问题'
    if why.startswith('技术不可行'):
        return (res == 'FAILED' and p['has_issue']), '技术不可行须为 FAILED 且在上升问题写明已试方案'
    if why.startswith('外部阻塞'):
        if res != 'WAIT_EVIDENCE':
            return False, '外部阻塞须为 WAIT_EVIDENCE'
        return (environment_blocked(model.env_for(state.plan, run['tool']), run),
                '最近一次直接调用受控入口未运行、已成功或不是环境失败，环境阻塞依据不足')
    if why.startswith('完成'):
        return False, '停止原因为完成但结果不符'
    return False, '停止原因为“%s”：尚未完成' % (why or '缺失')
