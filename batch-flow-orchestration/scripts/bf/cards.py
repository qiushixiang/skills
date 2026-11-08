"""Card rendering (design 10.1): fixed sections, rendered from the order; the gate parses only the fixed delivery lines."""
import json
import os
import sys
from pathlib import Path

from . import model, routing, permissions, snap
from .store import BfError, need, sha256_file

EXECUTOR = ['结果：VERIFIED | FAILED | ARCH_DECISION_REQUIRED | WAIT_EVIDENCE',
            '停止原因：完成 | 未完成 | 技术不可行 | 待裁决 | 外部阻塞',
            '改动与版本：<提交或快照>', '验证证据：<records/ 下证据文件的绝对路径>', '未运行/失败：...',
            '上升问题：...（无则写“无”）', '候选新项：（可选，下接逐行“- ”）', '限制：...']
EXECUTOR.insert(-2, '问题标识：<待裁决时可选，复用同一问题标识>')
REVIEWER = ['结果：RELEASE | FIX_REQUIRED', '固定版本：<被审版本>', '复跑与证据：<records/ 下证据文件的绝对路径>', '未复跑原因：（沿用执行者记录而未亲自复跑时必填）',
            '阻断：B1 | 返修:<交付目标> | 方案级:是/否 | 关闭条件:...（FIX_REQUIRED 时每项一行）',
            '非阻断建议：...', '候选新项：（可选）']
REFS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'references')
NORMS = {
    'implement': [('engineering.md', '必要验证、证据标准、复杂度与最小改动'), ('roles.md', '执行者、当前工单与必读')],
    'review': [('engineering.md', '审阅边界与问题处置、证据标准'), ('roles.md', '审阅者')],
    'decide': [('technical-lead.md', '裁决（decide）、工单上下文编制')],
    'triage': [('technical-lead.md', '分诊（triage）')],
}
NORMS['repair'] = NORMS['implement']
SCHEMA = (
    '{"diagnosis": "...", "actions": [ 动作… ]}，每个动作恰有一种键：\n'
    '- {"open": "implement|repair|review|triage", "target": "<交付目标/审阅组/批次>", "input": {"instruction": "..."}, '
    '"allow_protected": ["<glob>"], "fresh_session": false}  开立工单；allow_protected 放开受保护路径 → 转用户；目标在授权包络外 → 转用户\n'
    '- {"supersede": "WO-n", "reason": "..."}  作废工单；对象是审阅、放行或有依赖方的工单 → 转用户\n'
    '- {"retry": "WO-n", "reason": "...", "fresh_session": false}  重新开放失败/开立中的工单\n'
    '- {"grant": {"<目标>": {"implement": 1}}}  追加额度 → 转用户\n'
    '- {"authorize": "WO-n", "paths": ["<精确仓库相对路径>"]}  给原实施/返修单登记限定权限；超出现有授权 → 转用户\n'
    '- {"plan_revision": "<草案路径>", "within_authorization": true, "revalidate": ["<需重做的交付目标>"]}  负责人判断调整是否在授权内，说明依据和影响；脚本仅核对显式策略边界。revalidate 明确选择重做目标，不自动传播；新增承诺转用户\n'
    '- {"ask_user": "<问题>", "user_request": {"category": "commitment|authority|budget|risk|information|checkpoint", '
    '"decision": "<需要用户决定什么>", "recommendation": "<推荐方案>", "impact": "<代价及不决定的影响>"}, "options": [...]}  只问六类新增用户决定，不能只问继续吗\n'
    '- {"blocked": "WO-n", "reason": "<技术阻塞原因>", "missing": "<最小缺项>"}  保留阻塞并报告，不生成用户待办\n'
    '- {"refresh_design": true}  设计文档措辞调整、方案不变时重新固定章节哈希（design_drift 触发用）\n'
    'open 默认成为被裁决工单的前置处置，完成后原工单才恢复；不得反向依赖原工单。新单不增加额度。\n'
    '其余动作在授权包络内由脚本直接执行。')


def _norms(o):
    rows = NORMS.get(o['type'])
    if not rows:
        return ''
    return _lines('%s：%s' % (os.path.join(REFS, f), s) for f, s in rows)


def _lines(xs):
    return '\n'.join('- %s' % x for x in xs) or '- 无'


def _last_done(state, deliverable):
    runs = [r for o in routing.work_for(state, deliverable) for r in o['runs']
            if r['ended'] and r['result'] == 'VERIFIED']
    return max(runs, key=lambda r: r.get('ended_ts', r['started_ts'])) if runs else None


def _goal(state, o):
    targets = model.deliverables_of(state.plan, o['target'])
    lead = '本次仅审阅下列交付目标；不承担实施工作：' if o['type'] == 'review' else '本次负责下列交付目标：'
    return lead + '\n' + _lines('%s：%s' % (d, state.plan['deliverables'][d]['title'] or d) for d in targets)

def _acceptance(state, o):
    return '\n'.join('%s.A%d 验收：%s' % (d, i, text) for d in model.deliverables_of(state.plan, o['target'])
                     for i, text in enumerate(state.plan['deliverables'][d]['acceptance'], 1))

def _contracts(state, o):
    if o['type'] not in ('review', 'repair') and not (o['type'] == 'implement' and o['origin'].get('by') != 'plan'):
        return ''
    return '\n\n'.join('### %s\n%s' % (d, state.plan['deliverables'][d]['instruction'])
                        for d in model.deliverables_of(state.plan, o['target']))

def _non_goals(state, o):
    return '\n'.join('- %s：%s' % (d, text) for d in model.deliverables_of(state.plan, o['target'])
                     for text in state.plan['deliverables'][d].get('non_goals', []))

def _review_scope(state, o):
    if o['type'] != 'review':
        return ''
    out = [
        '本组范围：%s。仅按上述逐项验收和下列实施约定核对交付；实施约定是符合性依据，不是要求审阅者重新实施。' %
        '、'.join(model.deliverables_of(state.plan, o['target'])),
        '可阻断：本组验收未满足或必需证据不足；本组改动导致已支持场景的实际回归；本组改动引入可复现的实际缺陷。'
        '每项阻断必须绑定本组交付目标，说明对应验收或本次改动、实际影响、证据和最小关闭条件。',
        '不可扩张：不得从历史失败、旧裁决、通用规范或参考材料推导新的验收；不要求其他批次的目标提前完成。'
        '范围外发现只列非阻断建议，交技术负责人决定去向。',
        '复跑服务于本组验收和本次改动的直接影响；结合现有证据决定必要复跑，并说明未复跑原因。'
        '历史失败保留，不能用新通过覆盖；证据异常如实说明影响，不自动扩大到全项目复验。',
        '审阅者只读被审对象；确有阻断时提出返修目标和关闭条件，不自行修改产品、测试判据或验收。']
    focus = state.plan['review_groups'].get(o['target'], {}).get('focus')
    if focus:
        out.append('本组重点（不新增验收）：' + focus)
    return _lines(out)

def _environment(state, assignment, run_dir):
    env = model.env_for(state.plan, assignment['tool'])
    out = ['工作目录：%s；证据目录：%s' % (state.plan.get('workspace') or '<工作目录>', state.base.resolve() / 'records')]
    if run_dir:
        out.append('本次运行目录：%s' % run_dir)
    cli = '%s %s --base %s' % (sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cli.py'), state.base.resolve())
    ws = state.plan.get('workspace') or '<工作目录>'
    out.append('BF命令前缀：%s\n  留证：<BF> record --cwd %s -- <命令>；快照：<BF> snapshot --repo <仓库绝对路径> --paths <文件>...' % (cli, ws))
    if env.get('entry'):
        out.append('受控入口（直接调用，不组合命令、不另套 shell）：%s' % '、'.join(env['entry']))
        out.append('受控入口例外：入口自身负责 bf record 留证。调用时 cmd 直接写入口绝对路径和固定参数，禁止外套 bf record、Python 或 timeout；上述通用留证模板只用于其他命令。')
        out.append('require_host 只检查 CLI 父进程，不代表工具已出沙箱；以受控入口的实际前检为准。包装调用失败先纠正调用，不能据此认定宿主权限失效。')
    for key, label in (('preflight', '前检'), ('guide', '说明')):
        if env.get(key):
            out.append('%s：%s' % (label, env[key]))
    return _lines(out)


def _constraints(state, o):
    out = list(state.plan['goal'].get('constraints', []))
    for d in model.deliverables_of(state.plan, o['target']):
        out += list(state.plan['deliverables'][d].get('constraints', []))
    if o['target'] in state.plan['review_groups']:
        out += list(state.plan['review_groups'][o['target']].get('constraints', []))
    out += list(o['input'].get('constraints') or [])
    allowed = permissions.allowed(state.plan, o)
    if allowed:
        out.append('当前生效的受保护路径权限：%s' % json.dumps(allowed, ensure_ascii=False))
    return _lines(dict.fromkeys(out))


def required_read(state, o):
    spec = state.plan['deliverables'].get(o['target'], state.plan['review_groups'].get(o['target'], {}))
    rows = o['input'].get('required_read', spec.get('required_read', []))
    model.validate_required_read(rows)
    resolved = []
    for row in rows:
        path = Path(row['path'])
        if not path.is_absolute():
            path = Path(state.plan.get('workspace') or state.base.parent) / path
        need(path.is_file(), 'required reading file unavailable: %s' % path)
        resolved.append(dict(row, path=str(path.resolve())))
    return resolved


def _body(state, o, run_dir):
    plan, t, inp = state.plan, o['type'], o['input']
    if t == 'implement':
        return ''
    if t == 'repair':
        out = ['只修复审阅（%s，运行 %s）列出的阻断项；已审阅通过的部分不得改动。' % (inp['review_order'], inp['review_run'])]
        out += ['- %s：关闭条件：%s%s' % (f['id'], f['closure'], '（方案级）' if f['plan_level'] else '') for f in inp['findings']]
        out.append('交付须含“阻断关闭：Bn=已关闭 说明”，逐项一条。')
        return '\n'.join(out)
    if t == 'review':
        out = ['被审版本：']
        for d in model.deliverables_of(plan, o['target']):
            r = _last_done(state, d)
            snaps = ' '.join(routing.latest_snapshots(state, d))
            out.append('- %s：版本 %s；证据 %s' % (d, snaps or (r or {}).get('version') or '未记录', ', '.join(sorted((r or {}).get('evidence', {}))) or '无'))
        if state.plan.get('repos'):
            out.append('快照及 SHA 用于追溯：请判断当前版本差异是否影响证据、是否需补测；不要求逐路径复述。只读审阅被审对象，验证副作用按工单说明处理并如实报告。')
        repairs = [state.orders[n] for n in o['needs'] if n in state.orders and state.orders[n]['type'] == 'repair']
        if repairs:
            out.append('这是复审：只核对下列阻断项与回归；已关闭的写入通过，未关闭的重新列为阻断。')
            for rep in repairs:
                for f in rep['input']['findings']:
                    out.append('- %s（返修:%s）关闭条件：%s' % (f['id'], rep['target'], f['closure']))
        elif not inp.get('instruction'):
            out.append('首审：独立核对验收和证据，判断必要复跑，不沿用作者结论。')
        return '\n'.join(out)
    if t == 'decide':
        out = ['触发：%s；对象工单 %s，运行 %s。' % (inp.get('trigger'), inp.get('subject'), inp.get('run')),
               '问题：%s' % (inp.get('issue') or inp.get('answer') or ''), '证据：%s' % (', '.join(inp.get('evidence', [])) or '无')]
        facts = {k: v for k, v in inp.items() if k != 'subject_input'}
        # Trigger details are already present in the resolved input.
        out.append('把处置写入 %s/decision.json，格式：%s' % (run_dir or '<运行目录>', SCHEMA))
        out.append('包络内技术处置直接执行；只有六类新增用户决定才提问。次数用尽或暂时无解不是用户问题；用 blocked 保留原因和最小缺项。')
        return '\n'.join(out)
    if t == 'triage':
        out = ['为每个候选项给出去向：merge（须给 target 交付目标）、defer、drop。', '候选项：']
        out += ['- %s：%s' % (i['id'], i['text']) for i in inp['items']]
        out.append('把结果写入 %s/decision.json，格式：{"diagnosis": "...", "items": {"C1": {"disposition": "merge|defer|drop", '
                   '"target": "D?", "reason": "..."}}}，须覆盖全部候选项。' % (run_dir or '<运行目录>'))
        return '\n'.join(out)
    return json.dumps(inp, ensure_ascii=False)


def _delivery(o):
    if o['type'] in ('implement', 'repair'):
        rows = EXECUTOR[:]
        if o['type'] == 'repair':
            rows.insert(3, '阻断关闭：B1=已关闭 说明')
        return '按固定行回复，无内容写“无”；可选项无内容可省略：\n' + '\n'.join(rows)
    if o['type'] == 'review':
        return '最终回复按下列固定行交付：\n' + '\n'.join(REVIEWER)
    return '最终回复写：\n结果：VERIFIED\n停止原因：完成\n（处置已写入 decision.json；无法处置时写 待裁决 并说明上升问题。）'


def _current_input(state, o):
    """Resolve this order only; neither ancestors nor historical caches are active inputs."""
    inp = dict(o['input'])
    if o['type'] == 'implement' and o['origin'].get('by') == 'plan':
        spec = state.plan['deliverables'][o['target']]
        inp.update(instruction=spec['instruction'], acceptance=spec['acceptance'])
    inp.pop('authorization_sources', None)
    inp.pop('subject_input', None)
    return inp


def prepare(state, order_id):
    """One resolved input for every role. The Markdown card is only its export."""
    o = state.orders[order_id]
    need(isinstance(o['input'], dict), 'order.input must be an object')
    inp = _current_input(state, o)
    if o['type'] == 'decide':
        historical_input = o['input'].get('subject_input')
        subject = state.orders.get(inp.get('subject'))
        if subject:
            target = state.plan['deliverables'].get(subject['target'], {})
            inp['subject_task'] = {'instruction': target.get('instruction'), 'acceptance': target.get('acceptance'),
                'constraints': _constraints(state, subject),
                'current_work': _current_input(state, subject)}
        elif historical_input:
            inp['historical_subject'] = {'notice': '历史输入，仅供诊断；当前任务不可用，不作为执行要求', 'input': historical_input}
    if 'instruction' in inp:
        need(isinstance(inp['instruction'], str) and bool(inp['instruction'].strip()), 'instruction must be nonempty text')
    for key in ('constraints', 'answers'):
        need(isinstance(inp.get(key, []), list) and all(isinstance(x, str) for x in inp.get(key, [])), key + ' must be text list')
    if o['type'] == 'repair':
        need(isinstance(inp.get('findings'), list) and bool(inp['findings']), 'repair requires findings')
        need(all(isinstance(f, dict) and all(k in f for k in ('id', 'closure', 'plan_level')) for f in inp['findings']),
             'repair findings require id, closure and plan_level')
        need(inp.get('review_order') in state.orders and isinstance(inp.get('review_run'), str), 'repair requires review provenance')
    if o['type'] == 'triage':
        need(isinstance(inp.get('items'), list) and all(isinstance(i, dict) and 'id' in i and 'text' in i for i in inp['items']),
             'triage requires complete items')
    facts = []
    targets = set(model.deliverables_of(state.plan, o['target']))
    for obj in routing.sorted_orders(state):
        if obj['id'] != o['id'] and not (obj['type'] in ('implement', 'repair') and obj['target'] in targets):
            continue
        for run in obj['runs']:
            refs = [{'path': path, 'registered_sha256': digest, 'current_sha256': sha256_file(path)}
                    for path, digest in sorted(run['evidence'].items())]
            if run.get('ended'):
                facts.append({'order': obj['id'], 'run': run['run_id'], 'version': run.get('version'),
                              'changed': run.get('changed', {}), 'evidence': refs,
                              'started': run.get('started'), 'ended': run.get('ended'),
                              'dispatch': run.get('dispatch'),
                              'observations': run.get('observations', []), 'citations': run.get('citations', [])})
    return {'goal': _goal(state, o), 'order': o['id'], 'type': o['type'], 'target': o['target'], 'input': inp,
            'acceptance': _acceptance(state, o), 'contracts': _contracts(state, o),
            'non_goals': _non_goals(state, o), 'review_scope': _review_scope(state, o),
            'constraints': _constraints(state, o), 'versions_and_evidence': facts if o['type'] in ('review', 'decide') else [],
            'required_read': required_read(state, o),
            'control': {'held': bool({o['target'], model.batch_of(state.plan, o['target'])} & set(state.plan.get('holds', []))),
                        'in_envelope': model.batch_of(state.plan, o['target']) in state.plan['envelope']['batches'],
                        'dependencies': {n: state.orders[n]['state'] for n in o['needs'] if n in state.orders}},
            'design_changes': model.drifted(state.plan),
            'plan_revision': state.plan.get('revision')}


def export_input(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def _input_text(data):
    inp = dict(data['input'])
    instruction = inp.pop('instruction', '')
    # These fields have a single presentation in their dedicated sections.
    for key in ('acceptance', 'constraints', 'required_read'):
        inp.pop(key, None)
    # Membership and protected-path declarations are presented once in scope/constraints.
    if data['type'] == 'review':
        inp.pop('deliverables', None)
    inp.pop('required_protected', None)
    out = [instruction] if instruction else []
    if inp:
        out.append('本次输入：\n' + export_input(inp))
    out.append('派发状态：%s；%s。限定路径的修改许可见范围与约束，不等同于启动执行。' %
               ('已暂停' if data['control']['held'] else '未暂停',
                '自主执行包络内' if data['control']['in_envelope'] else '自主执行包络外'))
    if data['design_changes']:
        out.append('设计变化记录：' + export_input(data['design_changes']))
    return '\n'.join(out)


def _reading_text(data):
    # Version digests stay in dispatch.json; the work instructions show only selected reading.
    return _lines('%s — %s；%s' % (r['path'], r['section'], r['purpose']) for r in data['required_read'])


def _facts_text(data):
    """Keep full observations in dispatch.json; expose a usable provenance index, never raw history as instructions."""
    if not data['versions_and_evidence']:
        return ''
    out = ['以下是历史运行来源，仅供核对事实；完整版本观察、变更路径与登记/当前哈希见本次结构化派发记录 dispatch.json 的 versions_and_evidence。']
    for fact in data['versions_and_evidence']:
        refs = fact.get('evidence', [])
        out.append('- %s / %s：证据 %d 条；完整输入来源 %s' % (
            fact['order'], fact['run'], len(refs),
            (fact.get('dispatch') or {}).get('input', {}).get('path') or '旧记录未登记派发输入'))
        for ref in refs:
            out.append('  - %s（登记SHA-256：%s）%s' % (ref['path'], ref.get('registered_sha256') or '未登记', '（登记/当前哈希不同或文件不可读，须判断影响）'
                       if ref.get('registered_sha256') != ref.get('current_sha256') or not ref.get('current_sha256') else ''))
    return '\n'.join(out)

def validate_export(data, text):
    for key in ('goal', 'acceptance', 'contracts', 'non_goals', 'review_scope', 'constraints'):
        need(data.get(key, '') in text, 'card export omitted or changed ' + key)
    need(_input_text(data) in text, 'card export omitted or changed the resolved order input')
    if data['required_read']:
        need(_reading_text(data) in text, 'card export omitted required reading')
    if data['versions_and_evidence']:
        need(_facts_text(data) in text, 'card export omitted evidence provenance')


def render(state, order_id, assignment, run_dir=None, prepared=None):
    o = state.orders[order_id]
    data = prepared if prepared is not None else prepare(state, order_id)
    bounds = '\n'.join(x for x in [data['non_goals'], data['constraints']] if x)
    if o['type'] == 'review':
        parts = [('审阅目标', data['goal']), ('验收依据', data['acceptance']),
                 ('审阅边界', data['review_scope']), ('范围与约束', bounds),
                 ('本次审阅要求', _input_text(data)), ('被审交付与证据', _body(state, o, run_dir)),
                 ('实施约定（供核对，不是审阅者待办）', data['contracts'])]
    else:
        parts = [('目标', data['goal']), ('验收条件', data['acceptance']),
                 ('范围与约束', bounds), ('本次工作', _input_text(data)),
                 ('本单', _body(state, o, run_dir)), ('目标实施约定', data['contracts'])]
    if data['required_read']:
        parts.append(('输入材料', '只读指定范围；历史材料用于所列用途，不自动成为当前命令：\n' + _reading_text(data)))
    if data['versions_and_evidence']:
        parts.append(('历史证据索引（按需核对）', _facts_text(data)))
    parts += [('环境与留证', _environment(state, assignment, run_dir)), ('交付', _delivery(o))]
    if _norms(o):
        parts.append(('规范', '按下列文件的对应章节工作；通用规范不增加本组验收或审阅范围：\n' + _norms(o)))
    head = '# 工单 %s（%s，%s）' % (o['id'], o['type'], o['target'] or '-')
    text = head + '\n\n' + '\n\n'.join('## %s\n%s' % item for item in parts if item[1]) + '\n'
    validate_export(data, text)
    return text


def continue_prompt(order_id, reason, k, limit, original=None):
    return ('尚未合法结束：工单 %s 的协议交付不完整（%s）。这是同一轮次续作 %d/%d，不另扣额度。\n'
            '完整原工单与约束：%s；继续完成剩余工作，按原工单交付格式回复。\n'
            % (order_id, reason, k, limit, original or '当前会话的原始工单'))
