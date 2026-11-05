#!/usr/bin/env python3
"""Dispatch cards: turn one ledger action into a fixed role prompt, the exact host call and
mechanical ledger steps, so a weak orchestrator copies instead of composing.

next   -> the only orchestrator command: reconcile every card (collect finished subagents and CLI runs,
          record work interrupted by an orchestrator restart as INVALID), run mechanical registrations,
          then return exactly one host call to copy, or say wait / ask the user / report
make   -> records/dispatch/<card>/{prompt.md,card.json} (used by next; the technical lead uses --as-lead)
show   -> reprint a card and its progress
start / finish -> ledger steps of a lead (--as-lead) card, run by the technical lead subagent

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
（结果只按本卡工作内容和验收判定：本卡要求的改动、构建与验证都完成即 VERIFIED。由后续审阅、板测等其他节点产生的实证不属本卡，
不因其尚未运行写 WAIT_EVIDENCE；WAIT_EVIDENCE 只用于本卡要求的验证因外部条件无法完成。）
改动与版本：<改动文件/范围；git 固定引用或 snapshot 记录路径；需更新的 subject→版本>
验证证据：<flow.py record 生成的记录路径及对应验收项>
未运行/失败：<项目、原因、最小缺项>
上升问题：<矛盾/阻塞、影响、受阻任务、建议处理；无则写“无”>
限制：<已知限制与残余风险>"""
REVIEW_DELIVERY = """结果：RELEASE | FIX_REQUIRED | WAIT_EVIDENCE | ARCH_DECISION_REQUIRED
固定版本：<被审对象及版本>
复跑与证据：<原始记录路径、命令、工作目录、退出码及对应验收项>
问题与关闭条件：<逐项；无则写“无”>
下一责任人：<角色>"""
LEAD_DELIVERY = """结果：<VERIFIED | WAIT_EVIDENCE | FAILED | ARCH_DECISION_REQUIRED；非任务动作写处理状态>
结论与依据：<裁决/计划/核验结论及原始证据路径>
需登记的操作：<flow.py 操作类型、actor、--data 参数文件路径；由派发责任人原样代交>
下一责任人：<角色>"""


# AGY native subagents (role tool agy-native): fixed definitions, the same ones the AGY host guide verified.
NATIVE_TOOLS = {'agy-native', 'agy-agent'}
AGY_TYPES = {'executor': 'batch_flow_executor', 'reviewer': 'batch_flow_reviewer', 'third_party': 'batch_flow_third_party',
             'architect': 'batch_flow_technical_lead'}
AGY_DEFS = {
    'batch_flow_executor': ('Dedicated implementation executor for batch-flow tasks', True,
                            '你是 batch-flow 的执行者。严格在本次批次允许范围内修改代码并运行测试，交付结果与原始证据。只处理收到的任务，不派生代理，不修改正式台账、管理脚本或授权。'),
    'batch_flow_reviewer': ('Read-only independent reviewer for batch-flow tasks', False,
                            '你是 batch-flow 的独立审阅者。只读复核派发卡指定的固定版本，可运行验证命令，不修改任何文件，按卡中结构交付。'),
    'batch_flow_third_party': ('Read-only third-party reviewer for batch-flow tasks', False,
                               '你是 batch-flow 的第三方审阅者。只读复核派发卡指定的对象，不修改任何文件，按卡中结构交付。'),
    'batch_flow_technical_lead': ('Technical lead for batch-flow decisions and checks', True,
                                  '你是 batch-flow 的技术负责人。按派发卡处理裁决、方案或条件核对，不修改正式台账（只经 flow.py 登记），按卡中结构交付。'),
}


def agy_definition(type_name):
    description, write, prompt = AGY_DEFS[type_name]
    return dict(Name=type_name, Description=description, SystemPrompt=prompt, EnableWriteTools=write,
                EnableSubagentTools=False, EnableMCPTools=False)


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


HOSTS = [('claude', 'BATCH_FLOW_CLAUDE_LAUNCH', '.claude-launch'), ('agy', 'BATCH_FLOW_AGY_LAUNCH', '.agy-launch')]


def runtime_spec(store, runtime=None):
    """The launcher spec gives host, workspace and CLI profiles; never search other launches."""
    if runtime is None:
        found = [(key, folder) for _, key, folder in HOSTS if os.environ.get(key)]
        if not found:
            return None
        runtime = Path(store).resolve().parent / found[0][1] / os.environ[found[0][0]]
    runtime = Path(runtime)
    spec = load_json(runtime / 'spec.json')
    need(Path(spec['store']).resolve() == Path(store).resolve(), 'launcher spec is bound to another ledger')
    spec.setdefault('host', 'agy' if runtime.parent.name == '.agy-launch' else 'claude')
    spec.setdefault('runtime', str(runtime))
    if not spec.get('session_id') and (runtime / 'owner.json').is_file():
        spec['owner_conversation'] = load_json(runtime / 'owner.json').get('conversation_id')  # AGY binds at SessionStart
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


# An executor keeps its context only while it is small: a long transcript dilutes attention, anchors the
# executor on its own earlier approach and gets compacted lossily. Rework state lives in delivery files.
EXECUTOR_RESUME_MAX_BYTES = 300 * 1024


def agent_transcript(spec, agent_id):
    if not spec or not spec.get('session_id') or not spec.get('workspace'):
        return None
    project = Path.home() / '.claude' / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', spec['workspace'])
    return project / spec['session_id'] / 'subagents' / ('agent-%s.jsonl' % agent_id)


def agent_in_session(spec, agent_id):
    """SendMessage resumes a finished subagent from its transcript only within the session that spawned it
    (verified on 2.1.318 across `claude --resume`). A new launch has a new session and cannot reach old agents."""
    path = agent_transcript(spec, agent_id)
    return True if path is None else path.is_file()  # Outside the launcher nothing can be checked.


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
    records = Path(store).resolve().parent / 'records'
    # Pre-card invocations also saved native CLI records directly under records/<name>.
    # Locate adapter request metadata, then verify its actual completion/session below.
    runs = {request.parent for request in records.rglob('request.json')
            if within(request.parent, records)}
    for run in sorted(runs):
        try:
            report = agent_cli.collect(run)
        except (OSError, ValueError, KeyError):
            continue
        if report.get('usable') and report.get('conversation_id') == conversation:
            return run
    raise CardError('续接会话 %s 没有本台账内成功的 run-dir；请技术负责人决定新建或补证' % conversation)


def render_goal_context(context):
    lines = ['', '## 当前目标与验收边界']
    seen = set()
    def items(label, values):
        values = [v for v in values if v not in seen]
        if values:
            lines.append(label)
            for value in values:
                lines.append('- ' + value); seen.add(value)
    target = context.get('target')
    primary = [r for r in context['tasks'] if r['task'] == target]
    related = [r for r in context['tasks'] if r['task'] != target]
    for row in primary + related:
        if row in related:
            lines.append('直接覆盖：' + row['task'])
        if 'contract' in row:
            value = row['contract']
            if row in primary or not primary:
                lines.append('目标：' + value['goal'])
            items('验收：', value['acceptance'])
            items('范围外：', value['non_goals'])
        elif row in primary:
            lines.append('旧节点未结构化；目标与验收见下方工作指令。')
        else:
            lines.append('覆盖对象原始指令：' + row['instruction'])
    if context.get('project'):
        value = context['project']
        lines.append('项目目标：' + value['goal'])
        items('项目约束：', value['acceptance'] + value['non_goals'])
    elif context.get('legacy'):
        lines.append('旧计划目标未结构化；未自动提取或补造验收标准。')
    if not context['tasks']:
        lines.append('无绑定节点；按本动作指令处理，缺验收依据时先澄清。')
    return lines


def render_prompt(card, payload, s, store, task_base, workspace):
    role = card['role']; kind = card['kind']; target = card['target']
    docs = list(ROLE_DOCS[role])
    if role == 'architect' and LEAD_FLOW.get(kind):
        docs.append(LEAD_FLOW[kind])
    design = s.get('design') or {}
    section = (design.get('sections') or {}).get(target)
    lines = ['# %s · %s' % (role, target),
             '动作：%s；计划 %s。' % (kind, s['plan_version'])]
    lines += render_goal_context(flow.goal_context(s, payload))
    lines += ['', '## 执行', payload.get('instruction', '').strip()]
    lines += ['', '## 输入与约束', '工作目录：' + str(workspace), '台账：' + str(store)]
    if task_base != workspace:
        lines.append('任务目录：' + str(task_base))
    lines.append('权限：' + ('按本节点允许范围修改；不直接改台账或技能。提交须本节点明确授权。'
                              if card['access'] == 'write' else '只读待审产物；可复跑验证。'))
    if design.get('path'):
        lines.append('设计：%s%s（%s）' % (design['path'], ' §' + section if section else '', design.get('version')))
    for key, label in [('subjects', '固定版本'), ('remaining', '剩余预算'), ('blocks', '受阻任务'),
                       ('decision', '关联决策'), ('retry_of', '重试来源'), ('continuation_of', '续作来源'),
                       ('review_budget_blockers', '预算缺口')]:
        if payload.get(key) not in (None, [], {}):
            lines.append(label + '：' + json.dumps(payload[key], ensure_ascii=False))
    lines += ['必读：'] + ['- ' + str(SKILL / d) for d in docs]
    if role in {'reviewer', 'third_party', 'architect'}:
        lines.append('先判断发现是否违反当前目标；阻断须给验收项、支持场景、实际证据、影响与最小关闭条件。范围外建议不阻断。')
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
    lines += ['', '## 交付（最终回复只给以下结构，首行必须是“结果：<值>”）', '', '```text', delivery, '```']
    if role in {'reviewer', 'third_party'} and card['access'] == 'read':
        lines += ['', '只读审阅：直接复跑可在本环境执行的代表性验证；报告命令、工作目录、退出码和关键输出。',
                  '可写 records 时用 flow.py record；只读时由宿主保存工具原始输出，不因无法自行写记录而重复运行或判验证未完成。']
        if card['tool'] in CLI_TOOLS:
            lines.append('本卡 CLI 原始输出：' + str(Path(card.get('dir', '<派发卡目录>')) / 'run' / 'stdout') + '（宿主保存，回收时引用）。')
        else:
            lines.append('原生子代理的工具原始回执由派发责任人原样保存；不能用最终报告摘要代替运行记录。')
        lines.append('测试本身若因夹具/构建目录不可写而未运行，仍报环境缺项；读取代码或 record 落盘失败都不冒充测试通过。')
    else:
        lines += ['', '不登记台账、不修改 flow.json；原始证据用 flow.py record 生成：',
                  shlex.join([sys.executable, str(SCRIPTS / 'flow.py'), '--store', str(store), 'record', '--cwd', str(workspace), '--', '<验证程序>', '<参数>'])]
    lines += ['', '派发卡：' + card['id'], '']
    return '\n'.join(lines)


def make(args):
    store = Path(args.store).resolve()
    s = load_json(store); flow.validate_store(s)
    spec = runtime_spec(store, args.runtime)
    workspace = Path(args.workspace or (spec or {}).get('workspace') or '').resolve() if (args.workspace or spec) else None
    need(workspace is not None and workspace.is_dir(), 'source workspace unknown: pass --workspace or run inside the launcher')
    task_base = Path((spec or {}).get('task_base') or store.parent.parent).resolve()
    if args.kind == 'CONFIRM_CONDITION':
        need(not any(w['requirement'] == args.target and w['owner'] != 'user' for w in flow.legacy_confirmation_waits(s, store.parent)),
             '旧确认卡等待尚未登记；先运行 next 经正式操作登记并诊断，--recheck 不绕过恢复条件')
    a = find_action(s, args.kind, args.target)
    payload = a['subagent_payload']; mode = a['orchestrator_action']['mode']
    as_lead = bool(getattr(args, 'as_lead', False))
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
    replacement = None
    host = (spec or {}).get('host', 'claude')
    native = host == 'agy' and tool in NATIVE_TOOLS
    if native and resume:
        replacement = 'AGY 原生子代理按新上下文派发（原上下文 %s）；交接靠台账与交付文件' % resume
        resume = None
    if resume and is_agent_tool(tool) and not agent_in_session(spec, resume):
        # Continuing is an optimisation; the ledger and delivery files carry the state. Fall back to a fresh
        # context instead of blocking, and record why (the ledger requires it for resume-policy tasks).
        replacement = '原上下文 %s 不在本编排会话（新会话或其他宿主），改用新上下文；交接靠台账与交付文件' % resume
        resume = None
    if resume and is_agent_tool(tool) and role == 'executor':
        path = agent_transcript(spec, resume)
        size = path.stat().st_size if path is not None and path.is_file() else 0
        if size > EXECUTOR_RESUME_MAX_BYTES:
            replacement = ('执行者原上下文 %s 记录 %d KB，超过续接上限 %d KB；新上下文按交付文件交接，避免长上下文干扰'
                           % (resume, size // 1024, EXECUTOR_RESUME_MAX_BYTES // 1024))
            resume = None
    task = s['plan']['tasks'].get(args.target)
    prev = flow.latest(s, args.target) if task else None
    if not resume and not replacement and task and task.get('session_policy') == 'resume' and prev and mode == 'dispatch':
        replacement = '按 context_hint 新建（%s）' % (hint.get('reason') or '原上下文不可续')
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    card_id = re.sub(r'[^A-Za-z0-9._-]', '_', '%s-%s-r%s-%s-%s%s' % (args.target, args.kind, s['revision'], stamp, uuid.uuid4().hex[:4],
                                                               '-lead' if as_lead else ''))
    directory = cards_root(store) / card_id
    directory.mkdir(parents=True)
    card = dict(format=FORMAT, id=card_id, dir=str(directory), store=str(store), revision=s['revision'], kind=args.kind,
                goal_context_hash=flow.digest(flow.goal_context(s, payload)), decision=payload.get('decision'),
                target=args.target, mode=mode, role=role, tool=tool, model=model, effort=effort,
                actor='architect' if as_lead else 'orchestrator', lead=as_lead, launch=(spec or {}).get('launch_id'),
                subjects=flow.current_subjects(s, args.target) if args.target in s['plan']['tasks'] else None,
                access='write' if role in WRITE_ROLES else 'read', resume=resume, created_at=now(),
                ledger_start='start' if args.target in s['plan']['tasks'] and args.kind in TASK_KINDS and mode == 'dispatch'
                else 'decision_start' if args.kind == 'DECIDE' and mode == 'dispatch' else None,
                retry_of=payload.get('retry_of'), continuation_of=payload.get('continuation_of'), replacement_reason=replacement)
    payload_path = directory / 'payload.json'
    write_new(payload_path, json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    card['payload_file'] = str(payload_path)
    prompt_path = directory / 'prompt.md'
    write_new(prompt_path, render_prompt(card, payload, s, store, task_base, workspace))
    card['prompt_file'] = str(prompt_path); card['prompt_sha256'] = sha(prompt_path)
    pointer = ('你是 batch-flow 角色会话。先用 Read 完整读取派发卡 %s（sha256 %s），按其中身份、范围、角色入口与交付形态工作；'
               '最终回复只给卡中规定的交付结构。' % (prompt_path, card['prompt_sha256']))
    if host == 'agy' and tool in {'claude', 'claude-code'}:
        tool = 'claude-cli'  # outside Claude Code a bare claude role is a nested `claude -p`
    if host != 'agy' and tool in NATIVE_TOOLS:
        tool = 'agy'  # outside AGY a native AGY role runs as the AGY CLI
    need(not (host == 'agy' and is_agent_tool(tool)), 'AGY 编排者不能派发 Claude 子代理（%s）；角色改配 agy-native、claude/claude-cli、agy 或 codex' % tool)
    if native:
        # Role is the card id, so the launcher's decision log and the child transcript name this card.
        call = dict(TypeName=AGY_TYPES[role], Role=card_id, Prompt=pointer)
        if str(model).lower() not in {'inherit', 'default', str((spec or {}).get('model', '')).lower()}:
            call['Model'] = model  # the user's configured model; same as the orchestrator (or inherit) sends none
        card['route'] = 'agy_native'
        card['host_call'] = dict(tool='invoke_subagent', input=call)
        card['define_call'] = dict(tool='define_subagent', input=agy_definition(call['TypeName']))
    elif is_agent_tool(tool):
        alias = agent_model(model)
        need(alias in AGENT_MODELS, 'Agent 子代理模型须为 %s 或其完整 ID（如 claude-opus-5-5），当前 %r' % (sorted(AGENT_MODELS), model))
        need(effort == 'default' or effort in AGENT_EFFORTS, 'Agent effort 须为 default 或 %s，当前 %r' % (sorted(AGENT_EFFORTS), effort))
        subtype = tool.split(':', 1)[1] if ':' in tool else AGENT_TYPES[role]
        card['agent_model'] = alias
        if resume:
            card['route'] = 'send_message'
            # The resumed agent appends to its old transcript: only lines after this offset belong to the card.
            path = agent_transcript(spec, resume)
            card['resume_offset'] = sum(1 for _ in path.open(encoding='utf-8')) if path is not None and path.is_file() else 0
            card['host_call'] = dict(tool='SendMessage', input=dict(to=resume, message=pointer))
        else:
            card['route'] = 'agent'
            # Only the lead's nested dispatch is foreground (it must collect its executor). Orchestrator cards stay
            # background with a timer: the haiku orchestrator cannot emit a JSON boolean false, and a background
            # lead whose own child finishes is woken by the completion notice (seen on 2.1.318).
            foreground = as_lead
            card['foreground'] = foreground
            # The description is the card id: the agent's .meta.json (written at launch) then names its card.
            call = dict(description=card_id, subagent_type=subtype, model=alias,
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
        if host == 'agy':
            card['host_call'] = dict(tool='run_command', input=dict(CommandLine=shlex.join(argv)))
        else:
            card['host_call'] = dict(tool='Bash', input=dict(command=shlex.join(argv), run_in_background=not as_lead,
                                                              description='%s %s via %s' % (args.kind, args.target, backend)))
    else:
        raise CardError('调用工具 %r 不可派发（支持 claude / claude-agent[:类型]（Claude 宿主子代理）、agy、codex、claude-cli（外部进程））' % tool)
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
    out.append('2. 原样调用 %s（前台，等待结束），参数逐字使用（run_in_background 必须是 JSON 布尔 false，字符串会转成后台）：\n%s'
               % (call['tool'], json.dumps(call['input'], ensure_ascii=False, indent=2)))
    if card['route'] in {'agent', 'send_message'}:
        out.append('3. 把第 2 步工具返回原文（含 agentId）用 Write 新建为 %s/receipt.txt；把其中子代理的最终报告原文 Write 为 %s/result.md。'
                   '若返回的是 "Async agent launched" 而不是报告，说明宿主改成了后台：等它的完成通知再写 result.md，不要提前结束。' % (d, d))
    else:
        out.append('3. 命令结束后无需另存，finish 会经 agent_cli collect 回收。')
    if card['ledger_start'] == 'start':
        out.append('4. 回收登记：' + shlex.join(base + ['finish', '--store', store, '--card', card['id'], '--revision', '<当前revision>']
                                              + (['--conversation', '<agent ID>'] if card['route'] == 'agent' else [])) +
                   '\n   交付里的 subject 版本与台账当前版本 %s 不同时，先以执行角色或 architect 运行 flow.py change_subject（--evidence 为 result.md），再 finish。'
                   % json.dumps(card.get('subjects') or {}, ensure_ascii=False))
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
    if card.get('lead') or card.get('foreground'):
        return lead_steps(card, [sys.executable, str(SCRIPTS / 'dispatch_card.py')], card['store'])
    return ['编排者卡由 dispatch_card.py next 驱动：照抄 next 给出的调用，之后再运行 next（回收、登记都由 next 完成）。']


def show(card):
    return dict(card=card['id'], route=card['route'], role=card['role'], kind=card['kind'], target=card['target'],
                prompt_file=card['prompt_file'], state=state(card['dir']), steps=steps(card))


def current_card_goals(card, s):
    # Pre-feature cards are stale once structured boundaries are enabled.
    if 'goal_context_hash' not in card:
        return 'goal_contract' not in s['plan']
    item = dict(target=card['target'])
    if card.get('decision'): item['decision'] = card['decision']
    return card['goal_context_hash'] == flow.digest(flow.goal_context(s, item))


def start(args):
    directory = card_dir(args.store, args.card); card = load_json(directory / 'card.json'); st = state(directory)
    need(not st.get('attempt') and not st.get('started'), '本卡已登记开始；不要重复 start')
    need(current_card_goals(card, load_json(args.store)), '任务目标或计划版本已变化；重新生成派发卡，不发送旧提示词')
    need(card['ledger_start'], '本动作没有 start 登记')
    call = dict(tool=card['tool'], model=card['model'], effort=card['effort'], conversation_id=card.get('resume') or 'pending:' + card['id'])
    if card['ledger_start'] == 'start':
        data = {k: card[k] for k in ['retry_of', 'continuation_of', 'replacement_reason'] if card.get(k)}
        argv = ['start', '--revision', str(args.revision), '--actor', card.get('actor', 'orchestrator'), '--task', card['target'],
                '--tool', call['tool'], '--model', call['model'], '--effort', call['effort'], '--conversation', call['conversation_id']]
        result = run_flow(args.store, *argv, data=data or None)
        s = load_json(args.store)
        mine = [a for a in s['state']['attempts'] if a['task'] == card['target']]
        need(mine and mine[-1].get('result') is None, 'start 已写入但找不到运行中的 attempt；读取 status 核对')
        st.update(started=now(), attempt=mine[-1]['id'], revision=result['revision'], actor=card.get('actor', 'orchestrator'))
    else:
        ledger = load_json(args.store)
        handling = ledger['decisions'][card['target']].get('handling')
        previous = (handling or {}).get('call', {})
        if previous.get('conversation_id', '').startswith('pending:'):
            need(ledger['revision'] == args.revision, 'revision conflict: reread status before adopting pending handling')
            need(all(previous.get(k) == call[k] for k in ['tool','model','effort']),
                 'pending decision handling uses different call configuration')
            for other_path in cards_root(args.store).glob('*/card.json'):
                if other_path.parent == directory: continue
                other = load_json(other_path); other_state = state(other_path.parent)
                need(other['target'] != card['target'] or not other_state.get('started') or other_state.get('finished'),
                     'decision already has an active dispatch card; collect it instead of dispatching twice')
            # The lead already registered its start, but has not bound an actual session yet.
            # Reuse that registration; do not overwrite its time or issue a second decision_start.
            result = dict(revision=ledger['revision'])
            st.update(started=handling['started_at'], revision=result['revision'], reused_pending_handling=True)
        else:
            result = run_flow(args.store, 'decision_start', '--revision', str(args.revision), '--actor', 'architect',
                              data=dict(id=card['target'], call=call))
            st.update(started=now(), revision=result['revision'])
    save_state(directory, st)
    return dict(ok=True, card=card['id'], attempt=st.get('attempt'), revision=st['revision'], next=steps(card)[1:])


def parse_result(text):
    match = re.search(r'^\s*[*#>\s]*结果\s*[:：]\s*\**\s*([A-Z_]+)', text, re.M)
    return match.group(1) if match else None


def finish(args):
    return finish_card(args.store, card_dir(args.store, args.card), args.revision, args.conversation)


def register_confirmation_wait(store, card, result_path, rev):
    ledger = load_json(store)
    hashes = flow.evidence([str(result_path)], Path(store).resolve().parent)
    for wait in ledger['state'].get('confirmation_waits', []):
        if wait['decision'] != card['decision'] or wait['requirement'] != card['target']:
            continue
        original = wait['evidence_hashes']
        need(all(original[p] == h for p, h in hashes.items() if p in original),
             '原始等待证据已改变；保留原报告，不以覆盖文件绕过等待登记')
        if original == hashes:
            # The ledger write may have succeeded before saving card state. Reuse
            # that exact registration without another mutation or history entry.
            return dict(ok=True, revision=ledger['revision'])
    return run_flow(store, 'decision_progress', '--revision', str(rev), '--actor', 'orchestrator',
                    data=dict(id=card['decision'], requirement=card['target'], status='waiting', evidence=[str(result_path)]))


def migrate_confirmation_waits(store):
    # Old finished cards carried no formal wait. Import only original raw delivery,
    # preserving requirement ownership; unrelated progress never makes it retryable.
    s = load_json(store)
    for row in flow.legacy_confirmation_waits(s, Path(store).parent):
        card = load_json(Path(row['path']).parent / 'card.json')
        card['decision'] = row['decision']
        register_confirmation_wait(store, card, row['path'], revision(store))


def finish_card(store, directory, revision, conversation_arg=None):
    directory = Path(directory); card = load_json(directory / 'card.json'); st = state(directory)
    need(not st.get('finished'), '本卡已回收')
    result_path = directory / 'result.md'
    conversation = None
    evidence = []
    if card['route'] == 'cli':
        import agent_cli
        report = agent_cli.collect(directory / 'run')
        need(report.get('event') != 'CLI_INCOMPLETE', '后台运行尚未结束（无 exit.json）；等待完成通知，不要重派')
        need(report.get('usable'), 'CLI 调用不可用：%s；保留 %s 原始记录，按恢复流程分类，不自动重试' % (report.get('problems'), directory / 'run'))
        receipt = directory / 'run' / 'receipt.json'
        need(receipt.is_file(), 'CLI 缺少原始启动回执 run/receipt.json；核查真实调用，不生成替代回执或重派')
        if not result_path.exists():
            write_new(result_path, report.get('response') or '')
        conversation = report['conversation_id']
        evidence += [str(directory / 'run' / name) for name in ['request.json', 'stdout', 'stderr', 'exit.json'] if (directory / 'run' / name).exists()]
    else:
        need(result_path.is_file() and result_path.read_text().strip(), '先把子代理最终报告原文 Write 为 %s' % result_path)
        conversation = card.get('resume') or conversation_arg
        need(conversation, 'Agent 派发需 --conversation <agent ID>（来自 receipt）')
        receipt = directory / 'receipt.txt'
        need(receipt.is_file() and conversation in receipt.read_text(), 'agent ID 必须出现在本卡 receipt.txt 中')
    for timer in sorted(directory.glob('timer-*.ticket')):
        subprocess.run([sys.executable, str(SCRIPTS / 'progress_timer.py'), 'cancel', '--ticket', str(timer)], capture_output=True, text=True)
    text = result_path.read_text(encoding='utf-8')
    value = parse_result(text)
    out = dict(ok=True, card=card['id'], result=value, result_file=str(result_path), conversation_id=conversation)
    if card['ledger_start'] != 'start':
        if card['kind'] == 'CONFIRM_CONDITION' and value == 'WAIT_EVIDENCE':
            res = register_confirmation_wait(store, card, result_path, revision)
            out['revision'] = res['revision']
        st.update(finished=now(), conversation_id=conversation, result=value,
                  progress_marker=flow.progress_marker(load_json(store))); save_state(directory, st)
        out['note'] = '确认等待已登记时由 status 路由诊断；其余操作按 result.md“需登记的操作”原文代交（actor 为原责任角色）。'
        return out
    need(st.get('attempt'), '本卡未登记 start')
    need(value in flow.RESULTS, '交付首行缺少合法“结果：<值>”（%s）；退回原角色补齐，不代填' % sorted(flow.RESULTS))
    s = load_json(store)
    attempt = next(a for a in s['state']['attempts'] if a['id'] == st['attempt'])
    actor = attempt.get('performer_role') or card['role']
    data = dict(attempt=st['attempt'], result=value, evidence=[str(result_path), str(receipt)] + evidence,
                call=dict(tool=card['tool'], model=card['model'], effort=card['effort'], conversation_id=conversation))
    res = run_flow(store, 'finish', '--revision', str(revision), '--actor', actor, data=data)
    st.update(finished=now(), conversation_id=conversation, result=value, revision=res['revision']); save_state(directory, st)
    out['revision'] = res['revision']
    return out


# ---- next: the orchestrator's only command ----------------------------------------------------------
# Restarting the orchestrator is the normal way to recover (no session resume): the launcher holds the ledger
# lock, so nothing dispatched by an earlier launch can still be running inside Claude Code. next therefore
# collects what finished, records the rest as INVALID (host interruption) and lets status offer the retry.

def launch_started(spec, runtime):
    if spec.get('started_at'):
        return spec['started_at']
    path = Path(runtime) / 'spec.json'
    return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()


def card_live(card, spec, started):
    if card.get('launch'):
        return card['launch'] == spec.get('launch_id')
    return card.get('created_at', '') >= started


def project_dir(spec):
    return Path.home() / '.claude' / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', spec['workspace'])


def read_meta(path):
    meta = path.with_name(path.name[:-len('.jsonl')] + '.meta.json')
    try:
        return load_json(meta)
    except (OSError, ValueError):
        return {}


def agent_id(path):
    return path.name[len('agent-'):-len('.jsonl')]


def find_agent(card, dirs, handle=None):
    """The orchestrator's own (depth 1) subagent of this card: its description is the card id (older cards:
    its first message points at the card's prompt)."""
    for directory in dirs:
        if handle:
            path = directory / ('agent-%s.jsonl' % handle)
            if path.is_file():
                return path
            continue
        for meta_path in sorted(directory.glob('agent-*.meta.json')):
            path = meta_path.with_name(meta_path.name[:-len('.meta.json')] + '.jsonl')
            meta = read_meta(path)
            if meta.get('parentAgentId'):
                continue
            if meta.get('description') == card['id']:
                return path
            if not path.is_file():
                continue
            try:
                with path.open(encoding='utf-8') as stream:
                    first = stream.readline()
            except OSError:
                continue
            if card['prompt_file'] in first:
                return path
    return None


def line_count(path):
    if not path.is_file():
        return 0
    with path.open(encoding='utf-8') as stream:
        return sum(1 for _ in stream)


def agent_report(path, offset=0):
    """Final report: the last SubagentHandback message or assistant text after offset."""
    text = None
    with path.open(encoding='utf-8') as stream:
        for i, line in enumerate(stream):
            if i < offset:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get('type') != 'assistant':
                continue
            for block in (entry.get('message') or {}).get('content') or []:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use' and block.get('name') == 'SubagentHandback':
                    text = (block.get('input') or {}).get('message') or text
                elif block.get('type') == 'text' and block.get('text', '').strip():
                    text = block['text']
    return text


def revision(store):
    return load_json(store)['revision']


def ledger_attempt(store, attempt_id):
    return next((a for a in load_json(store)['state']['attempts'] if a['id'] == attempt_id), None)


def register_receipt(store, directory, card, st, handle, evidence, spec):
    """register_dispatch with the host completion notice as the (native) wakeup; no script timer."""
    if card['ledger_start'] != 'start' or not st.get('attempt'):
        return
    a = ledger_attempt(store, st['attempt'])
    if not a or a.get('result') or a.get('dispatch'):
        return
    due = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).isoformat()
    data = dict(attempt=st['attempt'], handle=handle, evidence=[str(evidence)], timer_kind='native',
                timer_id='host-notification:' + handle, due_at=due,
                owner_conversation_id=spec.get('session_id') or spec.get('owner_conversation') or 'launch:' + spec['launch_id'])
    res = run_flow(store, 'register_dispatch', '--revision', str(revision(store)), '--actor', st.get('actor', 'orchestrator'), data=data)
    st['revision'] = res['revision']; save_state(directory, st)


def collect_agent(store, directory, card, spec, path):
    """Returns the finish result, or None while the agent is still working."""
    st = state(directory); handle = agent_id(path); meta = read_meta(path)
    if st.get('handle') != handle:
        st['handle'] = handle; save_state(directory, st)
    receipt = directory / 'receipt.txt'
    if not receipt.exists():
        write_new(receipt, json.dumps(dict(agentId=handle, transcript=str(path), meta=meta), ensure_ascii=False, indent=2))
    register_receipt(store, directory, card, st, handle, receipt, spec)
    report = agent_report(path, card.get('resume_offset', 0)) if path.is_file() else None
    if not meta.get('finished') or not report:
        return None
    if not (directory / 'result.md').exists():
        write_new(directory / 'result.md', report)
    return finish_card(store, directory, revision(store), handle)


def cli_pid(directory):
    try:
        return int(load_json(directory / 'run' / 'receipt.json')['pid'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def interrupt(store, directory, card, reason, info=None):
    """Work that died with an earlier orchestrator launch: INVALID in the ledger, then status offers retry_of."""
    st = state(directory)
    lost = directory / 'lost.json'
    if not lost.exists():
        write_new(lost, json.dumps(dict(reason=reason, at=now(), **(info or {})), ensure_ascii=False, indent=2))
    for timer in sorted(directory.glob('timer-*.ticket')):
        subprocess.run([sys.executable, str(SCRIPTS / 'progress_timer.py'), 'cancel', '--ticket', str(timer)], capture_output=True, text=True)
    result = 'INTERRUPTED'
    a = ledger_attempt(store, st['attempt']) if st.get('attempt') else None
    if a and a.get('result'):
        result = a['result']
    elif a:
        evidence = [str(lost)] + [str(directory / n) for n in ['receipt.txt'] if (directory / n).exists()]
        data = dict(attempt=a['id'], result='INVALID', evidence=evidence,
                    call=dict(tool=card['tool'], model=card['model'], effort=card['effort'],
                              conversation_id=st.get('handle') or a['call']['conversation_id']))
        res = run_flow(store, 'finish', '--revision', str(revision(store)), '--actor', a.get('performer_role') or card['role'], data=data)
        st['revision'] = res['revision']; result = 'INVALID'
    st.update(finished=now(), result=result, interrupted=str(lost)); save_state(directory, st)
    return result


def reconcile(store, spec, started):
    out = dict(collected=[], interrupted=[], running=[], pending=[], problems=[])
    claude = spec.get('host') == 'claude' and spec.get('session_id')
    current = [project_dir(spec) / spec['session_id'] / 'subagents'] if claude else []
    every = sorted(project_dir(spec).glob('*/subagents')) if claude else []
    for path in sorted(cards_root(store).glob('*/card.json'), key=lambda p: load_json(p).get('created_at', '')):
        directory = path.parent; card = load_json(path); st = state(directory)
        if st.get('finished'):
            continue
        live = card_live(card, spec, started)
        try:
            if card.get('lead') or card.get('foreground'):
                if live:
                    out['running'].append(dict(card=card['id'], by='technical lead'))
                else:
                    out['interrupted'].append(dict(card=card['id'], result=interrupt(store, directory, card, '编排者重启，技术负责人与其子代理随之终止')))
                continue
            if card['route'] == 'cli':
                pid = cli_pid(directory)
                if (directory / 'run' / 'exit.json').exists() and not (directory / 'run' / 'request.json').exists():
                    # agent_cli writes request.json before launching; an exit.json without it is not a real CLI run record.
                    out['interrupted'].append(dict(card=card['id'], result=interrupt(
                        store, directory, card, '运行目录有 exit.json 但缺 agent_cli 启动前写入的 request.json：不是真实 CLI 运行记录，不采信',
                        dict(run=str(directory / 'run'))), reason='no_cli_request'))
                elif (directory / 'run' / 'exit.json').exists():
                    import agent_cli
                    report = agent_cli.collect(directory / 'run')
                    if report.get('usable'):
                        out['collected'].append(finish_card(store, directory, revision(store)))
                    else:  # a tool failure, not a technical result: INVALID keeps the raw run and lets status retry
                        out['interrupted'].append(dict(card=card['id'], result=interrupt(
                            store, directory, card, 'CLI 运行不可用：%s' % report.get('problems'), dict(run=str(directory / 'run'))), reason='cli_failed'))
                elif pid and alive(pid):
                    if live:
                        register_receipt(store, directory, card, st, 'pid:%d' % pid, directory / 'run' / 'receipt.json', spec)
                    out['running'].append(dict(card=card['id'], pid=pid))
                elif live and pid is None:
                    out['pending'].append(card)
                else:
                    out['interrupted'].append(dict(card=card['id'], result=interrupt(store, directory, card, 'CLI 运行随原编排会话终止（无 exit.json，进程已不在）', dict(pid=pid))))
                continue
            if card['route'] == 'agy_native':
                outcome = reconcile_native(store, directory, card, spec, live)
                out[outcome[0]].append(outcome[1])
                continue
            handle = st.get('handle') or (card.get('resume') if card['route'] == 'send_message' else None)
            agent = find_agent(card, current if live else every, handle)
            if agent is not None and card['route'] == 'send_message' and line_count(agent) <= card.get('resume_offset', 0):
                agent = None  # the message was never sent
            if live and agent is None:
                out['pending'].append(card)
                continue
            if agent is not None:
                done = collect_agent(store, directory, card, spec, agent)
                if done:
                    out['collected'].append(done)
                    continue
                if live:
                    out['running'].append(dict(card=card['id'], agent=agent_id(agent)))
                    continue
            info = dict(handle=st.get('handle'), transcript=str(agent) if agent else None, current_session=spec.get('session_id'))
            out['interrupted'].append(dict(card=card['id'], result=interrupt(store, directory, card, '编排者重启，子代理随原会话终止且未交付', info)))
        except (CardError, flow.FlowError, OSError, KeyError, ValueError) as error:
            out['problems'].append(dict(card=card['id'], error=str(error)))
    return out


def mechanical_registrations(store):
    done = []
    for _ in range(20):
        action = next((a for a in flow.status(load_json(store))['actions']
                       if a['kind'] == 'CLOSE_DECISION' or (a['kind'] == 'REGISTER_DECISION' and a.get('issue'))), None)
        if action is None:
            break
        if action['kind'] == 'CLOSE_DECISION':
            run_flow(store, 'close', '--actor', 'orchestrator', '--revision', str(revision(store)), '--id', action['target'],
                     '--note', '恢复条件已由 status 判定齐全，机械闭环')
        else:
            run_flow(store, 'signal', '--actor', 'orchestrator', '--revision', str(revision(store)), '--from-issue', action['issue']['key'])
        done.append('%s %s' % (action['kind'], action['target']))
    return done


def agy_logged(spec, tool, label):
    """The AGY launcher logs every allowed main-session call with its Role/Name label."""
    try:
        lines = (Path(spec['runtime']) / 'decisions.jsonl').read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get('tool') == tool and entry.get('label') == label and entry.get('decision') in {'allow', 'auto_approve'}:
            return entry
    return None


def agy_child(spec, card):
    """Actual child conversation of a native card: the child whose transcript names the card."""
    try:
        lines = (Path(spec['runtime']) / 'children.jsonl').read_text().splitlines()
    except OSError:
        return None
    seen = set()
    for line in lines:
        try:
            child = json.loads(line)
        except ValueError:
            continue
        cid, path = child.get('conversationId'), child.get('transcriptPath')
        if not cid or cid in seen or not path:
            continue
        seen.add(cid)
        try:
            if card['id'] in Path(path).read_text(encoding='utf-8', errors='replace'):
                return cid
        except OSError:
            continue
    return None


def reconcile_native(store, directory, card, spec, live):
    st = state(directory); result = directory / 'result.md'
    invoked = agy_logged(spec, 'invoke_subagent', card['id']) if live else None
    if live and not invoked and not result.exists():
        return 'pending', card
    receipt = directory / 'receipt.txt'
    handle = st.get('handle') or agy_child(spec, card) or 'agy-invoke:' + card['id']
    if invoked and not receipt.exists():
        write_new(receipt, json.dumps(dict(card=card['id'], handle=handle, decision=invoked), ensure_ascii=False, indent=2))
    if st.get('handle') != handle:
        st['handle'] = handle; save_state(directory, st)
    if receipt.exists():
        register_receipt(store, directory, card, st, handle, receipt, spec)
    if result.exists() and result.read_text().strip():
        if not receipt.exists():
            write_new(receipt, json.dumps(dict(card=card['id'], handle=handle, note='result.md written before restart'), ensure_ascii=False))
        return 'collected', finish_card(store, directory, revision(store), handle)
    if live:
        return 'running', dict(card=card['id'], agent=handle,
                               todo='子代理交付到达后，用 write_to_file（Overwrite=false）把最终报告原文写到 %s，再运行 next' % result)
    return 'interrupted', dict(card=card['id'], result=interrupt(store, directory, card, '编排者重启，AGY 原生子代理随原会话终止且未交付'))


def emit(store, card, runtime):
    st = state(card['dir'])
    define = card.get('define_call')
    if define and not agy_logged(runtime_spec(store, runtime), 'define_subagent', define['input']['Name']):
        return dict(do='call', card=card['id'], tool=define['tool'], input=define['input'],
                    say='先逐字照抄这个角色定义调用（任何字段都不改）；返回后立即再运行 next。')
    if card['ledger_start'] and not st.get('started'):
        start(argparse.Namespace(store=store, card=card['id'], revision=revision(store)))
    call = card['host_call']
    return dict(do='call', card=card['id'], tool=call['tool'], input=call['input'],
                say='逐字照抄 tool 与 input 发起这一个调用（任何字段都不改）；调用返回后立即再运行 next。')


def next_step(args):
    store = str(Path(args.store).resolve())
    spec = runtime_spec(store, args.runtime)
    need(spec is not None, 'next 只在编排启动器（Claude Code / AGY）会话中运行')
    runtime = spec['runtime']
    migrate_confirmation_waits(store)
    report = reconcile(store, spec, launch_started(spec, runtime))
    try:
        report['registered'] = mechanical_registrations(store)
    except (CardError, flow.FlowError) as error:
        report['registered'] = []; report['problems'].append(dict(registration=str(error)))
    base = {k: v for k, v in report.items() if v and k != 'pending'}
    s = load_json(store)
    supersede = []
    for card in report['pending']:
        if state(card['dir']).get('started'):
            if not current_card_goals(card, s):
                return dict(base, do='attention', card=card['id'],
                            todo='已登记但尚未调用的派发卡目标已变化；由责任人保留原记录并处理交接，不发送旧提示词或重置预算')
            return dict(base, **emit(store, card, runtime))
        try:
            find_action(s, card['kind'], card['target'])
            need(current_card_goals(card, s), '旧派发卡目标已变化')
            return dict(base, **emit(store, card, runtime))
        except CardError:
            supersede.append(card)  # made but never dispatched, and the ledger moved on
    for card in supersede:
        st = state(card['dir']); st.update(finished=now(), result='SUPERSEDED'); save_state(card['dir'], st)
    busy = {r['card'] for r in report['running']}
    busy_targets = {load_json(cards_root(store) / c / 'card.json')['target'] for c in busy}
    gate = flow.pre_yield(s, 'orchestrator', None, Path(store).parent)
    work = [a for a in gate['actions'] if a.get('orchestrator_action', {}).get('mode') in {'dispatch', 'delegate_to_lead'}
            and a['target'] not in busy_targets]
    work.sort(key=lambda a: a['kind'] == 'CONFIRM_CONDITION')  # formal tasks before confirmations
    for action in work:
        try:
            card = make(argparse.Namespace(store=store, kind=action['kind'], target=action['target'], workspace=None,
                                           runtime=str(runtime), as_lead=False, recheck=False))
            return dict(base, **emit(store, card, runtime))
        except (CardError, flow.FlowError) as error:
            base.setdefault('problems', []).append(dict(action='%s %s' % (action['kind'], action['target']), error=str(error)))
    other = [dict(kind=a['kind'], target=a['target'], todo=a.get('orchestrator_todo') or a.get('instruction'))
             for a in gate['actions'] if a.get('orchestrator_action', {}).get('mode') not in {'dispatch', 'delegate_to_lead'}]
    users = [dict(kind=a['kind'], target=a['target'], todo=a.get('orchestrator_todo') or a.get('instruction'))
             for a in gate['user_actions']]
    if base.get('running') or gate['running']:
        return dict(base, do='wait', user_items=users, say='有工作在运行：running 中带 todo 的照做；否则结束本轮，等完成通知（AGY 用 command_status/wait/manage_subagents 等其结束）；之后运行 next。'
                    + ('另有待用户处理的事项，一并原样告诉用户。' if users else ''))
    if other or users:
        return dict(base, do='ask_user', items=other + users,
                    say='把这些事项原样告诉用户；用户给出原话后按 todo 登记（flow.py），再运行 next。')
    if base.get('problems'):
        return dict(base, do='report', say='把 problems 原样告诉用户，不自行修复或绕过。')
    return dict(base, do='done' if gate['disposition'] == 'COMPLETE' else 'report', disposition=gate['disposition'],
                say='没有可派发的工作：把 disposition 告诉用户。')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('next', help='orchestrator: reconcile cards, then one call to copy (or wait / ask user / report)')
    p.add_argument('--store', required=True); p.add_argument('--runtime')
    p = sub.add_parser('make'); p.add_argument('--store', required=True); p.add_argument('--kind', required=True)
    p.add_argument('--target', required=True); p.add_argument('--workspace'); p.add_argument('--runtime')
    p.add_argument('--as-lead', action='store_true', help='technical lead subagent dispatching the payload role of a delegated action')
    p.add_argument('--recheck', action='store_true', help='compatibility flag; never bypasses unresolved evidence diagnosis')
    p = sub.add_parser('show'); p.add_argument('--store', required=True); p.add_argument('--card')
    p = sub.add_parser('start'); p.add_argument('--store', required=True); p.add_argument('--card', required=True)
    p.add_argument('--revision', type=int, required=True)
    p = sub.add_parser('finish'); p.add_argument('--store', required=True); p.add_argument('--card', required=True)
    p.add_argument('--revision', type=int, required=True); p.add_argument('--conversation')
    args = parser.parse_args()
    try:
        if args.command == 'next':
            out = next_step(args)
            if out.get('problems'):
                out['say'] = out['say'] + ' problems 原样告诉用户。'
        elif args.command == 'make':
            out = show(make(args))
        elif args.command == 'show':
            if args.card:
                out = show(load_json(card_dir(args.store, args.card) / 'card.json'))
            else:
                out = [dict(card=c.parent.name, state=state(c.parent)) for c in sorted(cards_root(args.store).glob('*/card.json'))]
        else:
            out = dict(start=start, finish=finish)[args.command](args)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    except (CardError, flow.FlowError, OSError, KeyError, ValueError) as error:
        print(json.dumps(dict(ok=False, error=str(error)), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
