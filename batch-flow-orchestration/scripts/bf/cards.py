"""Card rendering (design 10.1): fixed sections, rendered from the order; the gate parses only the fixed delivery lines."""
import json
import os
import sys

from . import model, routing

STOP_RULES = ('停止原因规则：完成=结果 VERIFIED 并引用 records/ 下存在的证据；范围内仍有可试路径=未完成；'
              '技术不可行=FAILED 且上升问题写明已试方案；外部阻塞=WAIT_EVIDENCE，仅当直接调用受控入口出现环境失败。')
BOUNDARY = ('待裁决边界：继续需改动本卡无权改动的内容（验收、冻结资产、范围、他批接口、预算权限、系统资源门限）'
            '时才写 待裁决（结果 ARCH_DECISION_REQUIRED + 上升问题）。')
EXECUTOR = ['结果：VERIFIED | FAILED | ARCH_DECISION_REQUIRED | WAIT_EVIDENCE',
            '停止原因：完成 | 未完成 | 技术不可行 | 待裁决 | 外部阻塞',
            '改动与版本：<提交或快照>', '验证证据：<records/ 下证据文件的绝对路径>', '未运行/失败：...',
            '上升问题：...（没有写“无”）', '候选新项：（可选，下接逐行“- ”）', '限制：...']
REVIEWER = ['结果：RELEASE | FIX_REQUIRED', '固定版本：<被审版本>', '复跑与证据：<records/ 下证据文件的绝对路径>', '未复跑原因：（沿用执行者记录而未亲自复跑时必填）',
            '阻断：B1 | 返修:<交付目标> | 方案级:是/否 | 关闭条件:...（FIX_REQUIRED 时每项一行）',
            '非阻断建议：...', '候选新项：（可选）']
REFS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'references')
NORMS = {
    'implement': [('engineering.md', '必要验证、证据标准、复杂度与最小改动'), ('roles.md', '执行者')],
    'review': [('engineering.md', '审阅边界与问题处置、证据标准'), ('roles.md', '审阅者')],
    'decide': [('technical-lead.md', '裁决（decide）')],
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
    '- {"plan_revision": "<草案路径>"}  修订计划 → 转用户\n'
    '- {"ask_user": "<问题>", "options": [...]}  向用户提问\n'
    '- {"refresh_design": true}  设计文档措辞调整、方案不变时重新固定章节哈希（design_drift 触发用）\n'
    '其余动作在授权包络内由脚本直接执行。')


def _norms(o):
    rows = NORMS.get(o['type'])
    if not rows:
        return ''
    return _lines('%s：%s' % (os.path.join(REFS, f), s) for f, s in rows)


def _lines(xs):
    return '\n'.join('- %s' % x for x in xs) or '- 无'


def _last_done(state, deliverable):
    for o in reversed([o for o in state.orders.values() if o['type'] in ('implement', 'repair') and o['target'] == deliverable]):
        for r in reversed(o['runs']):
            if r['ended'] and r['result'] == 'VERIFIED':
                return r
    return None


def _goal(state, o):
    plan = state.plan
    out = ['项目目标：%s' % plan['goal'].get('objective', '')]
    out += ['约束：%s' % c for c in plan['goal'].get('constraints', [])]
    for d in model.deliverables_of(plan, o['target']):
        spec = plan['deliverables'][d]
        out.append('交付目标 %s %s；验收：%s' % (d, spec['title'], '；'.join(spec['acceptance']) or '见指令'))
        if spec.get('non_goals'):
            out.append('非目标（不要做）：%s' % '；'.join(spec['non_goals']))
    return '\n'.join(out)


def _environment(state, assignment, run_dir):
    env = model.env_for(state.plan, assignment['tool'])
    out = ['证据与记录文件写入 %s，并在交付中引用绝对路径。' % (state.base / 'records')]
    if run_dir:
        out.append('本次运行目录：%s' % run_dir)
    cli = '%s %s --base %s' % (sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cli.py'), state.base)
    ws = state.plan.get('workspace') or '<工作目录>'
    out.append('留证（只有它生成的文件算证据，手写文件无效）：%s record --cwd %s -- <命令>；代码快照：%s snapshot --repo %s --paths <文件>...' % (cli, ws, cli, ws))
    if env.get('entry'):
        out.append('受控入口（直接调用，不组合命令、不另套 shell）：%s' % '、'.join(env['entry']))
    for key, label in (('preflight', '前检'), ('guide', '说明')):
        if env.get(key):
            out.append('%s：%s' % (label, env[key]))
    return _lines(out)


def _constraints(state, o):
    out = list(o['input'].get('constraints') or []) + list(o['input'].get('answers') or [])
    for n in o['needs']:
        dep = state.orders.get(n)
        if dep and dep['state'] == 'done' and dep['type'] == 'decide' and dep['output']:
            out.append('裁决：%s' % (dep['output'].get('decision') or {}).get('diagnosis', ''))
        if dep and dep['state'] == 'done' and dep['type'] == 'ask_user' and dep['output']:
            out.append('用户答复：%s' % dep['output']['answer'])
    return _lines(out)


def _body(state, o, run_dir):
    plan, t, inp = state.plan, o['type'], o['input']
    if t == 'implement':
        spec = plan['deliverables'].get(o['target'], {})
        out = [inp.get('instruction') or spec.get('instruction', '')]
        if spec.get('design_ref'):
            out.append('设计依据：%s' % spec['design_ref'])
        last = next((r for r in reversed(o['runs']) if r['ended']), None)
        if last:
            out.append('上一轮：结果 %s，停止原因 %s。' % (last['result'], last['stop_reason']))
        return '\n'.join(out)
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
            out.append('“固定版本”行须包含上列每个交付目标的快照路径；你只可写 records 与交付文本，不得改动源码。')
        repairs = [state.orders[n] for n in o['needs'] if n in state.orders and state.orders[n]['type'] == 'repair']
        if repairs:
            out.append('这是复审：只核对下列阻断项与回归；已关闭的写入通过，未关闭的重新列为阻断。')
            for rep in repairs:
                for f in rep['input']['findings']:
                    out.append('- %s（返修:%s）关闭条件：%s' % (f['id'], rep['target'], f['closure']))
        else:
            out.append('首审：独立复跑验收并核对证据，不沿用作者的结论。')
        focus = plan['review_groups'].get(o['target'], {}).get('focus')
        if focus:
            out.append('审阅重点：%s' % focus)
        return '\n'.join(out)
    if t == 'decide':
        out = ['触发：%s；对象工单 %s，运行 %s。' % (inp.get('trigger'), inp.get('subject'), inp.get('run')),
               '问题：%s' % (inp.get('issue') or inp.get('answer') or ''), '证据：%s' % (', '.join(inp.get('evidence', [])) or '无')]
        out.append('把处置写入 %s/decision.json，格式：%s' % (run_dir or '<运行目录>', SCHEMA))
        out.append('不加额度、不改验收与范围的处置直接执行；含 grant、plan_revision 或超出授权包络的由脚本转交用户。')
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
        return '最终回复按下列固定行交付：\n' + '\n'.join(rows) + '\n' + STOP_RULES + '\n' + BOUNDARY
    if o['type'] == 'review':
        return '最终回复按下列固定行交付：\n' + '\n'.join(REVIEWER)
    return '最终回复写：\n结果：VERIFIED\n停止原因：完成\n（处置已写入 decision.json；无法处置时写 待裁决 并说明上升问题。）'


def render(state, order_id, assignment, run_dir=None):
    o = state.orders[order_id]
    parts = [('目标', _goal(state, o)), ('环境', _environment(state, assignment, run_dir)),
             ('本单', _body(state, o, run_dir)), ('约束', _constraints(state, o)), ('交付', _delivery(o))]
    if _norms(o):
        parts.insert(4, ('规范', '按下列文件的对应章节工作：\n' + _norms(o)))
    head = '# 工单 %s（%s，%s）' % (o['id'], o['type'], o['target'] or '-')
    return head + '\n\n' + '\n\n'.join('## %s\n%s' % p for p in parts) + '\n'


def continue_prompt(order_id, reason, k, limit):
    return ('尚未合法结束：工单 %s 的交付不合格（%s）。这是同一轮次续作 %d/%d，不另扣额度。\n'
            '继续完成剩余工作，结束时按原卡“## 交付”的固定行重新给出完整最终回复。\n%s\n'
            % (order_id, reason, k, limit, '环境阻塞只以最近一次直接调用受控入口的环境失败输出为证；你 shell 的沙箱状态不是证据。'
               if '环境' in reason else ''))
