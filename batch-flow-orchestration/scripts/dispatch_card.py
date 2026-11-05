#!/usr/bin/env python3
"""Dispatch cards: turn one ledger action into a fixed role prompt, the exact host call and
mechanical ledger steps, so a weak orchestrator copies instead of composing.

make   -> records/dispatch/<card>/{prompt.md,card.json}; prints the steps
show   -> reprint a card and its progress (after context compaction)
start  -> flow.py start / decision_start for the card (actor and call taken from the card)
register -> flow.py register_dispatch with the saved host receipt and timer ticket
finish -> collect the role delivery, parse its result line, flow.py finish

The card never changes technical content: the payload instruction is copied verbatim.
Ledger writes still go through flow.py with an explicit --revision (CAS).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import flow  # noqa: E402

FORMAT = 'batch-flow-dispatch-card/v1'
TASK_KINDS = {'DESIGN', 'EXECUTE', 'VERIFY', 'REVIEW', 'THIRD_PARTY_REVIEW', 'COMMIT', 'RELEASE', 'CONFIRM', 'REPAIR'}
CLI_TOOLS = {'agy': 'agy', 'agy-cli': 'agy', 'codex': 'codex', 'codex-cli': 'codex', 'claude-cli': 'claude'}
AGENT_MODELS = {'haiku', 'sonnet', 'opus', 'fable'}
AGENT_EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}
# Inside a Claude Code host bare `claude` means a subagent; claude-cli is a nested `claude -p` whose auth
# comes from agent_cli.claude_auth (the host strips CLAUDE_CODE_OAUTH_TOKEN from child shells).
AGENT_TYPES = {'executor': 'bf-executor', 'architect': 'bf-architect', 'reviewer': 'bf-reviewer', 'third_party': 'bf-reviewer'}
WRITE_ROLES = {'executor', 'architect'}
ROLE_DOCS = {
    'executor': ['references/roles/executor.md', 'references/engineering.md'],
    'reviewer': ['references/roles/technical-lead.md', 'workflows/review/SKILL.md'],
    'third_party': ['references/roles/third-party.md', 'workflows/review/SKILL.md'],
    'architect': ['references/roles/technical-lead.md'],
}
LEAD_FLOW = {'DESIGN': 'workflows/plan-sync/SKILL.md', 'PLAN_REPAIR': 'workflows/plan-sync/SKILL.md',
             'SYNC_DESIGN': 'workflows/plan-sync/SKILL.md', 'DECIDE': 'workflows/recovery/SKILL.md',
             'PREPARE_RESUMPTION': 'workflows/recovery/SKILL.md', 'CONFIRM_CONDITION': 'workflows/recovery/SKILL.md',
             'REVIEW': 'workflows/review/SKILL.md', 'THIRD_PARTY_REVIEW': 'workflows/review/SKILL.md'}
EXECUTOR_DELIVERY = """结果：VERIFIED | WAIT_EVIDENCE | FAILED | ARCH_DECISION_REQUIRED
改动与版本：<改动文件/范围；git 固定引用或 snapshot 记录路径；需更新的 subject→版本>
验证证据：<flow.py record 生成的记录路径及对应验收项>
未运行/失败：<项目、原因、最小缺项>
上升问题：<矛盾/阻塞、影响、受阻任务、建议处理；无则写“无”>
限制：<已知限制与残余风险>"""
REVIEW_DELIVERY = """结果：RELEASE | FIX_REQUIRED | WAIT_EVIDENCE | ARCH_DECISION_REQUIRED
固定版本：<被审对象及版本>
复跑与证据：<flow.py record 生成的记录路径及对应验收项>
问题与关闭条件：<逐项；无则写“无”>
下一责任人：<角色>"""
LEAD_DELIVERY = """结果：<VERIFIED | WAIT_EVIDENCE | FAILED | ARCH_DECISION_REQUIRED；非任务动作写处理状态>
结论与依据：<裁决/计划/核验结论及原始证据路径>
需登记的操作：<flow.py 操作类型、actor、--data 参数文件路径；由派发责任人原样代交>
下一责任人：<角色>"""


class CardError(ValueError):
    pass


def need(cond, message):
    if not cond:
        raise CardError(message)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def within(path, root):
    path = Path(path).resolve(); root = Path(root).resolve()
    return path == root or root in path.parents


def write_new(path, text):
    with Path(path).open('x', encoding='utf-8') as stream:
        stream.write(text)


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def runtime_spec(store, runtime=None):
    """The launcher spec gives workspace and CLI profiles; never search other launches."""
    if runtime is None:
        launch = os.environ.get('BATCH_FLOW_CLAUDE_LAUNCH')
        if not launch:
            return None
        runtime = Path(store).resolve().parent / '.claude-launch' / launch
    spec = load_json(Path(runtime) / 'spec.json')
    need(Path(spec['store']).resolve() == Path(store).resolve(), 'launcher spec is bound to another ledger')
    return spec


def cards_root(store):
    return Path(store).resolve().parent / 'records' / 'dispatch'


def card_dir(store, card_id):
    need(re.fullmatch(r'[A-Za-z0-9._-]+', card_id or '') is not None, 'invalid card id')
    path = cards_root(store) / card_id
    need((path / 'card.json').is_file(), 'card not found: ' + card_id)
    return path


def is_agent_tool(tool):
    return tool in {'claude', 'claude-agent', 'claude-code'} or str(tool).startswith('claude-agent:')


def lead_dispatchable(s, kind, target):
    """Only formal plan tasks are dispatched by the lead; a confirmation is checked, never redone by a second executor."""
    return kind in TASK_KINDS and target in s['plan']['tasks']


def agent_model(model):
    """Agent takes a family alias; accept a full Claude model ID such as claude-opus-5-5."""
    m = re.fullmatch(r'(?:claude-)?(haiku|sonnet|opus|fable)(?:-[0-9][0-9.-]*)?(?:\[1m\])?', str(model or '').strip().lower())
    return m.group(1) if m else None


def flow_cmd(store, *args):
    return [sys.executable, str(SCRIPTS / 'flow.py'), '--store', str(Path(store).resolve()), *args]


def run_flow(store, *args, data=None):
    argv = flow_cmd(store, *args)
    if data is not None:
        argv += ['--data', json.dumps(data, ensure_ascii=False)]
    proc = subprocess.run(argv, capture_output=True, text=True)
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = dict(ok=False, error=(proc.stdout + proc.stderr).strip())
    if proc.returncode != 0 or not result.get('ok', True):
        raise CardError('flow.py %s failed: %s' % (args[0], result.get('error', result)))
    return result


def find_action(s, kind, target):
    actions = flow.status(s)['actions']
    dispatchable = [a for a in actions if a.get('orchestrator_action', {}).get('mode') in {'dispatch', 'delegate_to_lead'}]
    for a in dispatchable:
        if a['kind'] == kind and a['target'] == target:
            return a
    listing = ', '.join('%s:%s' % (a['kind'], a['target']) for a in dispatchable) or '无'
    raise CardError('当前 status 中没有可派发动作 %s:%s；可派发：%s' % (kind, target, listing))


def prior_run(store, conversation):
    """Locate the successful adapter run that proves a CLI conversation, for --resume-from."""
    import agent_cli
    for run in sorted(cards_root(store).glob('*/run')):
        try:
            report = agent_cli.collect(run)
        except (OSError, ValueError, KeyError):
            continue
        if report.get('usable') and report.get('conversation_id') == conversation:
            return run
    raise CardError('续接会话 %s 没有本台账内成功的 run-dir；请技术负责人决定新建或补证' % conversation)


def render_prompt(card, payload, s, store, task_base, workspace):
    role = card['role']; kind = card['kind']; target = card['target']
    docs = list(ROLE_DOCS[role])
    if role == 'architect' and LEAD_FLOW.get(kind):
        docs.append(LEAD_FLOW[kind])
    design = s.get('design') or {}
    section = (design.get('sections') or {}).get(target)
    lines = ['# batch-flow 派发卡 ' + card['id'], '',
             '身份与职责：' + role + ('（局部技术负责人，承接移交动作）' if card['mode'] == 'delegate_to_lead' else ''),
             '任务：%s %s；台账 %s（revision %s）；任务目录 %s' % (kind, target, store, card['revision'], task_base),
             '实际调用：%s，模型 %s，effort %s；%s' % (card['tool'], card['model'], card['effort'],
                                               '续接会话 ' + card['resume'] if card.get('resume') else '新建会话'),
             '源码工作目录：' + str(workspace),
             '写权限：' + ('仅本卡工作内容所需的源码/测试/设计文件；不改 flow.json、不改技能、不提交 Git（除非工作内容明确要求）'
                        if card['access'] == 'write' else '只读：不修改任何文件；可运行验证命令（经 flow.py record）复核'),
             '', '## 固定输入']
    if design.get('path'):
        lines.append('- 设计书：%s 版本 %s%s' % (design['path'], design.get('version'), '，章节「%s」' % section if section else ''))
    for key, label in [('subjects', '对象版本'), ('remaining', '剩余预算'), ('blocks', '受阻任务'), ('decision', '关联决策'),
                       ('retry_of', '重试来源'), ('continuation_of', '续作来源'), ('review_budget_blockers', '预算缺口')]:
        if payload.get(key) not in (None, [], {}):
            lines.append('- %s：%s' % (label, json.dumps(payload[key], ensure_ascii=False)))
    if payload.get('context_hint'):
        lines.append('- 上下文：' + json.dumps(payload['context_hint'], ensure_ascii=False))
    lines += ['', '## 角色入口（先读）'] + ['- ' + str(SKILL / d) for d in docs]
    lines += ['', '## 工作内容（台账原文，不得改写或缩减）', '', payload.get('instruction', '').strip()]
    if card['mode'] == 'delegate_to_lead' and is_agent_tool(card['tool']) and payload.get('role') not in (None, 'architect') \
            and lead_dispatchable(s, kind, target):
        inner = shlex.join([sys.executable, str(SCRIPTS / 'dispatch_card.py'), 'make', '--store', str(store), '--kind', kind,
                            '--target', target, '--as-lead'])
        lines += ['', '## 你是本动作的派发责任人（dispatch_owner=architect）',
                  '载荷角色是 %s：不要自己实现、修复或审阅。运行下面的命令生成你的派发卡，按其 steps 逐字执行'
                  '（start 以 architect 登记；Agent 前台调用并等待；回收后 finish 以执行角色登记）：' % payload.get('role'),
                  '', '    ' + inner, '',
                  '执行角色交付后，按技术负责人职责核对其原始证据与验收项；台账只经 flow.py 登记，不直接写 flow.json。'
                  '你的交付写明子卡 ID、执行角色结果、核对结论，以及仍需派发责任人代交的操作（参数文件放在 ' + card['dir'] + ' 下）。']
    elif card['mode'] == 'delegate_to_lead' and is_agent_tool(card['tool']):
        lines += ['', '## 你负责核对本动作（不派发执行者）',
                  '这是条件确认/裁决类动作，不是台账正式任务：不要拉起执行者或审阅者，也不要自己实现。实际工作由对应正式任务完成；'
                  '你只核对已有原始证据是否满足条件。依赖未完成就返回 WAIT_EVIDENCE 并写明在等哪个任务或哪项用户实证。'
                  '需要登记的台账操作写成 --data 参数文件放在 ' + card['dir'] + ' 下，并在交付中列出。']
    elif card['mode'] == 'delegate_to_lead':
        lines += ['', '本动作由局部技术负责人处理：按职责实际处理，或经固定入口 ' + str(SCRIPTS / 'agent_cli.py') +
                  ' 派发并回收；需要登记的台账操作写成 --data 参数文件放在 ' + card['dir'] + ' 下，并在交付中列出。']
    delivery = EXECUTOR_DELIVERY if role == 'executor' else REVIEW_DELIVERY if role in {'reviewer', 'third_party'} else LEAD_DELIVERY
    lines += ['', '## 交付（最终回复只给以下结构，首行必须是“结果：<值>”）', '', '```text', delivery, '```',
              '', '不登记台账、不修改 flow.json；原始证据用 flow.py record 生成：',
              shlex.join([sys.executable, str(SCRIPTS / 'flow.py'), '--store', str(store), 'record', '--cwd', str(workspace), '--', '<验证程序>', '<参数>']),
              '', '## 原始载荷', '', '```json', json.dumps(payload, ensure_ascii=False, indent=2), '```', '']
    return '\n'.join(lines)


def make(args):
    store = Path(args.store).resolve()
    s = load_json(store); flow.validate_store(s)
    spec = runtime_spec(store, args.runtime)
    workspace = Path(args.workspace or (spec or {}).get('workspace') or '').resolve() if (args.workspace or spec) else None
    need(workspace is not None and workspace.is_dir(), 'source workspace unknown: pass --workspace or run inside the launcher')
    task_base = Path((spec or {}).get('task_base') or store.parent.parent).resolve()
    a = find_action(s, args.kind, args.target)
    payload = a['subagent_payload']; mode = a['orchestrator_action']['mode']
    as_lead = bool(getattr(args, 'as_lead', False))
    if args.kind == 'CONFIRM_CONDITION' and not getattr(args, 'recheck', False):
        waiting = flow.waiting_conditions(s, store.parent).get(args.target)
        need(not waiting, '上次确认卡 %s 返回 WAIT_EVIDENCE，之后台账没有进展（无新结果/确认/计划）；不重复派发。'
                          '等依赖任务完成或用户补证；确需复核时加 --recheck' % waiting)
    if as_lead:
        # The technical lead subagent dispatches the payload role itself (nested Agent, depth 2).
        need(mode == 'delegate_to_lead', '--as-lead 只用于移交技术负责人的动作（mode=delegate_to_lead）')
        need(payload.get('role') != 'architect', '本动作的责任角色就是技术负责人：由你本人处理，不再派发')
        need(lead_dispatchable(s, args.kind, args.target),
             '%s %s 不是台账正式任务：条件确认只由技术负责人核对，不另派执行者（实际工作走对应正式任务）' % (args.kind, args.target))
        mode = 'dispatch'
    if mode == 'delegate_to_lead':
        role = 'architect'; config = dict(s['plan']['roles'].get('architect') or {})
        need(config, 'plan.roles.architect 未配置，无法移交局部技术负责人')
    else:
        role = payload['role']; config = dict(payload.get('call_config') or {})
    need(role in ROLE_DOCS, 'unsupported role for dispatch: ' + str(role))
    tool, model, effort = config.get('tool'), config.get('model'), config.get('effort', 'default')
    need(tool and model, '调用配置缺 tool/model；先经 set_models 登记用户指定值')
    hint = payload.get('context_hint') or {}
    resume = hint.get('conversation_id') if hint.get('mode') == 'resume' and mode == 'dispatch' else None
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    card_id = re.sub(r'[^A-Za-z0-9._-]', '_', '%s-%s-r%s-%s-%s%s' % (args.target, args.kind, s['revision'], stamp, uuid.uuid4().hex[:4],
                                                               '-lead' if as_lead else ''))
    directory = cards_root(store) / card_id
    directory.mkdir(parents=True)
    card = dict(format=FORMAT, id=card_id, dir=str(directory), store=str(store), revision=s['revision'], kind=args.kind,
                target=args.target, mode=mode, role=role, tool=tool, model=model, effort=effort,
                actor='architect' if as_lead else 'orchestrator', lead=as_lead,
                access='write' if role in WRITE_ROLES else 'read', resume=resume, created_at=now(),
                ledger_start='start' if args.target in s['plan']['tasks'] and args.kind in TASK_KINDS and mode == 'dispatch'
                else 'decision_start' if args.kind == 'DECIDE' and mode == 'dispatch' else None,
                retry_of=payload.get('retry_of'), continuation_of=payload.get('continuation_of'))
    prompt_path = directory / 'prompt.md'
    write_new(prompt_path, render_prompt(card, payload, s, store, task_base, workspace))
    card['prompt_file'] = str(prompt_path); card['prompt_sha256'] = sha(prompt_path)
    pointer = ('你是 batch-flow 角色会话。先用 Read 完整读取派发卡 %s（sha256 %s），按其中身份、范围、角色入口与交付形态工作；'
               '最终回复只给卡中规定的交付结构。' % (prompt_path, card['prompt_sha256']))
    if is_agent_tool(tool):
        alias = agent_model(model)
        need(alias in AGENT_MODELS, 'Agent 子代理模型须为 %s 或其完整 ID（如 claude-opus-5-5），当前 %r' % (sorted(AGENT_MODELS), model))
        need(effort == 'default' or effort in AGENT_EFFORTS, 'Agent effort 须为 default 或 %s，当前 %r' % (sorted(AGENT_EFFORTS), effort))
        subtype = tool.split(':', 1)[1] if ':' in tool else AGENT_TYPES[role]
        card['agent_model'] = alias
        if resume:
            card['route'] = 'send_message'
            card['host_call'] = dict(tool='SendMessage', input=dict(to=resume, message=pointer))
        else:
            card['route'] = 'agent'
            # Lead work runs in the foreground: a background subagent's own Agent calls are forced async
            # (seen on 2.1.318), so a backgrounded lead would end before its executor and never finish it.
            foreground = as_lead or mode == 'delegate_to_lead'
            card['foreground'] = foreground
            call = dict(description='%s %s' % (args.kind, args.target), subagent_type=subtype, model=alias,
                        prompt=pointer, run_in_background=not foreground)
            if effort != 'default':
                call['effort'] = effort
            card['host_call'] = dict(tool='Agent', input=call)
    elif tool in CLI_TOOLS:
        backend = CLI_TOOLS[tool]
        profiles = [p for p in (spec or {}).get('profiles', []) if load_json(p).get('backend') == backend]
        need(len(profiles) == 1, '没有启动器核查过的 %s profile；在启动器会话中生成派发卡' % backend)
        argv = [sys.executable, str(SCRIPTS / 'agent_cli.py'), 'run', '--profile', profiles[0], '--task', args.target,
                '--cwd', str(workspace), '--model', model, '--effort', effort, '--prompt-file', str(prompt_path),
                '--run-dir', str(directory / 'run'), '--access', card['access']]
        if resume:
            argv += ['--conversation', resume, '--resume-from', str(prior_run(store, resume))]
        if not within(task_base, workspace):
            argv += ['--add-dir', str(task_base)]
        card['route'] = 'cli'
        card['host_call'] = dict(tool='Bash', input=dict(command=shlex.join(argv), run_in_background=not as_lead,
                                                          description='%s %s via %s' % (args.kind, args.target, backend)))
    else:
        raise CardError('调用工具 %r 在 Claude Code 编排者中不可派发（支持 claude / claude-agent[:类型]（子代理）、agy、codex、claude-cli（外部进程，认证见 agent_cli token 文件））' % tool)
    write_new(directory / 'card.json', json.dumps(card, ensure_ascii=False, indent=2))
    return card


def state(directory):
    path = Path(directory) / 'state.json'
    return load_json(path) if path.exists() else {}


def save_state(directory, value):
    path = Path(directory) / 'state.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def lead_steps(card, base, store):
    d = card['dir']; call = card['host_call']
    out = []
    if card['ledger_start']:
        out.append('1. 登记开始（actor architect）：' + shlex.join(base + ['start', '--store', store, '--card', card['id'], '--revision', '<当前revision>']))
    else:
        out.append('1. 本动作无 start 登记（%s）；直接派发。' % card['kind'])
    out.append('2. 原样调用 %s（前台，等待结束），参数逐字使用：\n%s' % (call['tool'], json.dumps(call['input'], ensure_ascii=False, indent=2)))
    if card['route'] in {'agent', 'send_message'}:
        out.append('3. 把第 2 步工具返回原文（含 agentId）用 Write 新建为 %s/receipt.txt；把其中子代理的最终报告原文 Write 为 %s/result.md。'
                   '若返回的是 "Async agent launched" 而不是报告，说明宿主改成了后台：等它的完成通知再写 result.md，不要提前结束。' % (d, d))
    else:
        out.append('3. 命令结束后无需另存，finish 会经 agent_cli collect 回收。')
    if card['ledger_start'] == 'start':
        out.append('4. 回收登记：' + shlex.join(base + ['finish', '--store', store, '--card', card['id'], '--revision', '<当前revision>']
                                              + (['--conversation', '<agent ID>'] if card['route'] == 'agent' else [])) +
                   '\n   交付列出 subject→版本时，先对每项运行 flow.py change_subject（--actor 为本卡角色，--evidence 为 result.md），再 finish。')
    else:
        out.append('4. 非任务动作：' + shlex.join(base + ['finish', '--store', store, '--card', card['id'], '--revision', '<当前revision>']
                                             + (['--conversation', '<agent ID>'] if card['route'] == 'agent' else [])) +
                   ' 只回收交付；需登记的操作在你的交付中列出。')
    if card.get('lead'):
        out.append('5. 前台派发不登记 register_dispatch：执行期间你保持等待。核对交付后按你自己卡中的交付结构回复。')
    else:
        out.append('5. 前台调用期间本会话等待技术负责人（含其拉起的执行者）完成；结束后按其交付“需登记的操作”原文代交。')
    return out


def steps(card):
    d = card['dir']; py = sys.executable; me = str(SCRIPTS / 'dispatch_card.py'); store = card['store']
    base = [py, me]
    if card.get('lead') or card.get('foreground'):
        return lead_steps(card, base, store)
    out = []
    if card['ledger_start']:
        out.append('1. 登记开始：' + shlex.join(base + ['start', '--store', store, '--card', card['id'], '--revision', '<当前revision>']))
    else:
        out.append('1. 本动作无 start 登记（%s / %s）；直接派发。' % (card['kind'], card['mode']))
    call = card['host_call']
    out.append('2. 原样调用 %s，参数逐字使用：\n%s' % (call['tool'], json.dumps(call['input'], ensure_ascii=False, indent=2)))
    out.append('3. 把第 2 步工具返回原文用 Write 新建为 %s/receipt.txt（不改写）。' % d)
    handle = '<Agent 返回的 agent ID>' if card['route'] == 'agent' else '<SendMessage 目标 ID>' if card['route'] == 'send_message' else '<Bash 后台任务 ID>'
    out.append('4. 计时（Bash，run_in_background=true）：' + shlex.join(
        [py, str(SCRIPTS / 'progress_timer.py'), 'wait', '--ticket', d + '/timer-1.ticket', '--task', card['target'],
         '--goal', '%s %s' % (card['kind'], card['target']), '--handle', handle]))
    if card['ledger_start'] == 'start':
        out.append('5. 登记回执：' + shlex.join(base + ['register', '--store', store, '--card', card['id'], '--revision', '<当前revision>',
                                                  '--handle', handle, '--timer-handle', '<计时 Bash 后台任务 ID>', '--timer-ticket', d + '/timer-1.ticket']))
    out.append('6. 等完成通知，不轮询、不猜测。600 秒计时到期：汇报在途状态，用新 ticket（timer-2.ticket…）重启计时并再次 register。')
    if card['route'] in {'agent', 'send_message'}:
        out.append('7. 完成通知到达后，把子代理最终报告原文用 Write 新建为 %s/result.md。' % d)
    else:
        out.append('7. 后台命令结束后无需另存，finish 会经 agent_cli collect 回收。')
    if card['ledger_start'] == 'start':
        out.append('8. 回收登记：' + shlex.join(base + ['finish', '--store', store, '--card', card['id'], '--revision', '<当前revision>']
                                              + (['--conversation', '<agent ID>'] if card['route'] == 'agent' else [])) +
                   '\n   交付列出 subject→版本时，先对每项运行 flow.py change_subject（--actor 为本卡角色，--evidence 为 result.md），再 finish。')
    else:
        out.append('8. 非任务动作：finish 只回收并保存交付（不写台账）；交付中“需登记的操作”由派发责任人按原文代交，actor 为原责任角色：' +
                   shlex.join(base + ['finish', '--store', store, '--card', card['id'], '--revision', '<当前revision>']))
    return out


def show(card):
    st = state(card['dir'])
    return dict(card=card['id'], route=card['route'], role=card['role'], kind=card['kind'], target=card['target'],
                prompt_file=card['prompt_file'], state=st, steps=steps(card))


def start(args):
    directory = card_dir(args.store, args.card); card = load_json(directory / 'card.json'); st = state(directory)
    need(not st.get('attempt') and not st.get('started'), '本卡已登记开始；不要重复 start')
    need(card['ledger_start'], '本动作没有 start 登记')
    call = dict(tool=card['tool'], model=card['model'], effort=card['effort'], conversation_id=card.get('resume') or 'pending:' + card['id'])
    if card['ledger_start'] == 'start':
        data = {k: card[k] for k in ['retry_of', 'continuation_of'] if card.get(k)}
        argv = ['start', '--revision', str(args.revision), '--actor', card.get('actor', 'orchestrator'), '--task', card['target'],
                '--tool', call['tool'], '--model', call['model'], '--effort', call['effort'], '--conversation', call['conversation_id']]
        result = run_flow(args.store, *argv, data=data or None)
        s = load_json(args.store)
        mine = [a for a in s['state']['attempts'] if a['task'] == card['target']]
        need(mine and mine[-1].get('result') is None, 'start 已写入但找不到运行中的 attempt；读取 status 核对')
        st.update(started=now(), attempt=mine[-1]['id'], revision=result['revision'], actor=card.get('actor', 'orchestrator'))
    else:
        result = run_flow(args.store, 'decision_start', '--revision', str(args.revision), '--actor', 'architect',
                          data=dict(id=card['target'], call=call))
        st.update(started=now(), revision=result['revision'])
    save_state(directory, st)
    return dict(ok=True, card=card['id'], attempt=st.get('attempt'), revision=st['revision'], next=steps(card)[1:])


def register(args):
    directory = card_dir(args.store, args.card); card = load_json(directory / 'card.json'); st = state(directory)
    need(card['ledger_start'] == 'start' and st.get('attempt'), '先用本卡 start 登记 attempt')
    receipt = directory / 'receipt.txt'
    need(receipt.is_file() and receipt.read_text().strip(), '缺少宿主回执 %s（第 3 步）' % receipt)
    ticket = Path(args.timer_ticket).resolve()
    need(within(ticket, directory) and ticket.is_file(), '计时 ticket 必须是本卡目录内已启动的 ticket')
    data = dict(attempt=st['attempt'], handle=args.handle, evidence=[str(receipt)],
                timer_handle=args.timer_handle, timer_ticket=str(ticket))
    result = run_flow(args.store, 'register_dispatch', '--revision', str(args.revision), '--actor', st.get('actor', 'orchestrator'), data=data)
    st.setdefault('registrations', []).append(dict(at=now(), handle=args.handle, timer_ticket=str(ticket)))
    st['handle'] = args.handle; st['revision'] = result['revision']
    save_state(directory, st)
    return dict(ok=True, card=card['id'], revision=result['revision'])


def parse_result(text):
    match = re.search(r'^\s*[*#>\s]*结果\s*[:：]\s*\**\s*([A-Z_]+)', text, re.M)
    return match.group(1) if match else None


def finish(args):
    directory = card_dir(args.store, args.card); card = load_json(directory / 'card.json'); st = state(directory)
    need(not st.get('finished'), '本卡已回收')
    result_path = directory / 'result.md'
    conversation = None
    evidence = []
    if card['route'] == 'cli':
        import agent_cli
        report = agent_cli.collect(directory / 'run')
        need(report.get('event') != 'CLI_INCOMPLETE', '后台运行尚未结束（无 exit.json）；等待完成通知，不要重派')
        need(report.get('usable'), 'CLI 调用不可用：%s；保留 %s 原始记录，按恢复流程分类，不自动重试' % (report.get('problems'), directory / 'run'))
        if not result_path.exists():
            write_new(result_path, report.get('response') or '')
        conversation = report['conversation_id']
        evidence += [str(directory / 'run' / name) for name in ['stdout', 'exit.json'] if (directory / 'run' / name).exists()]
    else:
        need(result_path.is_file() and result_path.read_text().strip(), '先把子代理最终报告原文 Write 为 %s' % result_path)
        conversation = card.get('resume') or args.conversation
        need(conversation, 'Agent 派发需 --conversation <agent ID>（来自 receipt）')
        receipt = directory / 'receipt.txt'
        need(receipt.is_file() and conversation in receipt.read_text(), 'agent ID 必须出现在本卡 receipt.txt 中')
    for timer in sorted(directory.glob('timer-*.ticket')):
        subprocess.run([sys.executable, str(SCRIPTS / 'progress_timer.py'), 'cancel', '--ticket', str(timer)], capture_output=True, text=True)
    text = result_path.read_text(encoding='utf-8')
    value = parse_result(text)
    out = dict(ok=True, card=card['id'], result=value, result_file=str(result_path), conversation_id=conversation)
    if card['ledger_start'] != 'start':
        st.update(finished=now(), conversation_id=conversation, result=value,
                  progress_marker=flow.progress_marker(load_json(args.store))); save_state(directory, st)
        out['note'] = '非任务动作：未写台账。按 result.md“需登记的操作”原文代交（actor 为原责任角色）。'
        return out
    need(st.get('attempt'), '本卡未登记 start')
    need(value in flow.RESULTS, '交付首行缺少合法“结果：<值>”（%s）；退回原角色补齐，不代填' % sorted(flow.RESULTS))
    s = load_json(args.store)
    attempt = next(a for a in s['state']['attempts'] if a['id'] == st['attempt'])
    actor = attempt.get('performer_role') or card['role']
    data = dict(attempt=st['attempt'], result=value, evidence=[str(result_path), str(directory / 'receipt.txt')] + evidence,
                call=dict(tool=card['tool'], model=card['model'], effort=card['effort'], conversation_id=conversation))
    res = run_flow(args.store, 'finish', '--revision', str(args.revision), '--actor', actor, data=data)
    st.update(finished=now(), conversation_id=conversation, result=value, revision=res['revision']); save_state(directory, st)
    out['revision'] = res['revision']
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('make'); p.add_argument('--store', required=True); p.add_argument('--kind', required=True)
    p.add_argument('--target', required=True); p.add_argument('--workspace'); p.add_argument('--runtime')
    p.add_argument('--as-lead', action='store_true', help='technical lead subagent dispatching the payload role of a delegated action')
    p.add_argument('--recheck', action='store_true', help='dispatch a confirmation again although nothing progressed since its last WAIT_EVIDENCE')
    p = sub.add_parser('show'); p.add_argument('--store', required=True); p.add_argument('--card')
    p = sub.add_parser('start'); p.add_argument('--store', required=True); p.add_argument('--card', required=True)
    p.add_argument('--revision', type=int, required=True)
    p = sub.add_parser('register'); p.add_argument('--store', required=True); p.add_argument('--card', required=True)
    p.add_argument('--revision', type=int, required=True); p.add_argument('--handle', required=True)
    p.add_argument('--timer-handle', required=True); p.add_argument('--timer-ticket', required=True)
    p = sub.add_parser('finish'); p.add_argument('--store', required=True); p.add_argument('--card', required=True)
    p.add_argument('--revision', type=int, required=True); p.add_argument('--conversation')
    args = parser.parse_args()
    try:
        if args.command == 'make':
            card = make(args); out = show(card)
        elif args.command == 'show':
            if args.card:
                out = show(load_json(card_dir(args.store, args.card) / 'card.json'))
            else:
                out = [dict(card=c.parent.name, state=state(c.parent)) for c in sorted(cards_root(args.store).glob('*/card.json'))]
        else:
            out = dict(start=start, register=register, finish=finish)[args.command](args)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except (CardError, flow.FlowError, OSError, KeyError, ValueError) as error:
        print(json.dumps(dict(ok=False, error=str(error)), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
