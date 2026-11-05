#!/usr/bin/env python3
"""Claude Code orchestrator launcher: one bound session, role agents, pre-tool guard, audit and stop hook.

Nothing is written to .claude/ or global settings: hooks and role agents are passed per launch
(--settings / --agents) and the runtime directory under the ledger keeps them as evidence.
Subcommand `context` is the SessionStart hook that re-injects capabilities after compaction/resume.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
ENV_KEY = 'BATCH_FLOW_CLAUDE_LAUNCH'
# orchestrator (depth 0) -> technical lead subagent (1) -> executor/reviewer (2); depth 2 cannot spawn again.
# Verified in the 2.1.318 binary: the Agent tool is withheld once a caller's depth reaches this cap.
DEPTH_KEY, SPAWN_DEPTH = 'CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH', '2'
LEAD_TYPES, WORKER_TYPES = ['bf-architect'], ['bf-executor', 'bf-reviewer']
VERIFIED_VERSIONS = {'2.1.318'}
ROLE_TOOLS = {
    'bf-executor': (['Read', 'Write', 'Edit', 'Bash', 'Grep', 'Glob'], 'executor'),
    'bf-architect': (['Read', 'Write', 'Edit', 'Bash', 'Grep', 'Glob', 'Agent'], 'architect（技术负责人）'),
    'bf-reviewer': (['Read', 'Grep', 'Glob', 'Bash'], 'reviewer / third_party（只读审阅，无编辑工具）'),
}


def within(path, root):
    path = Path(path).resolve(); root = Path(root).resolve()
    return path == root or root in path.parents


def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.claude-launch-')
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def agents():
    def prompt(name, label):
        head = '你是 batch-flow ' + label + ' 角色会话。只按派发给你的派发卡工作：先读卡，再读卡中列出的角色入口；'
        if name in LEAD_TYPES:
            return head + ('卡中动作由你作为派发责任人负责：执行/审阅类工作按卡内 dispatch_card.py --as-lead 的步骤拉起 '
                           + '/'.join(WORKER_TYPES) + '，不自己代做；台账只经 flow.py 以卡中命令登记，不直接写 flow.json；'
                           '不修改技能文件；按卡中交付结构给最终回复。')
        return head + '不登记台账、不修改 flow.json、不修改技能文件；按卡中交付结构给最终回复。'
    return {name: dict(description='batch-flow ' + label + ' 角色，只接受派发卡', prompt=prompt(name, label), tools=tools)
            for name, (tools, label) in ROLE_TOOLS.items()}


def capabilities(spec):
    py = spec['python']; store = spec['store']
    status = shlex.join([py, str(SCRIPTS / 'flow.py'), '--store', store, 'status'])
    show = shlex.join([py, str(SCRIPTS / 'dispatch_card.py'), 'show', '--store', store])
    yield_cmd = shlex.join([py, str(SCRIPTS / 'flow.py'), '--store', store, 'pre_yield', '--conversation', spec['session_id'],
                            '--audit-log', str(Path(spec['runtime']) / 'audit.jsonl')])
    return '\n'.join([
        'Claude Code batch-flow 编排者（受限启动，会话 %s）' % spec['session_id'],
        '任务目录：' + spec['task_base'], '台账：' + store, '记录目录：' + spec['records'],
        '源码工作目录：' + spec['workspace'], '技能目录：' + str(SKILL),
        '',
        '允许：flow.py（不含 record/snapshot）、dispatch_card.py、progress_timer.py、agent_cli.py check/collect；'
        '读任务目录和技能目录；在台账目录内新建记录文件（不覆盖）；Agent、SendMessage 和 agent_cli.py run 只能逐字照抄派发卡。',
        '禁止：读业务源码、改代码或设计书、Git 写、产品构建/测试、任意 shell 组合。被 ROLE_BOUNDARY_BLOCKED 拦截时不要换方式绕过，向用户报告。',
        '',
        '工作循环：',
        '1. 运行 ' + status + '，看 actions。',
        '2. mode=request_user：把 orchestrator_todo 中的待决项原样问用户；拿到原话后按协议登记（set_models/authorize 等）。',
        '   mode=register / collect_progress：按 orchestrator_todo 机械登记或收集进度，不作技术判断。',
        '3. mode=dispatch / delegate_to_lead：运行该动作的 dispatch_card_command，然后严格按输出 steps 逐步执行，命令和工具参数逐字照抄，'
        '<当前revision> 用刚读到的 revision，其余尖括号值用上一步工具返回的真实值。',
        '4. 后台子代理/进程完成会有通知，不轮询、不猜测结果；计时到期按卡第 6 步汇报并重启计时。',
        '5. 上下文压缩或忘记进度时运行 ' + show + ' 查看各卡状态，再继续未完成的步骤；不要重复派发已 start 的卡。',
        '6. 结束本轮前运行 ' + yield_cmd + '；未通过时按输出处理。',
        '技术内容（实现、审阅、裁决、方案）一律交给角色，你只协调和登记。',
    ])


def settings(spec):
    py = spec['python']; sid = spec['session_id']; runtime = Path(spec['runtime']); store = spec['store']
    log = str(runtime / 'audit.jsonl')

    def cmd(*argv):
        return dict(type='command', command=shlex.join([py, *argv]), timeout=20)
    guard = cmd(str(SCRIPTS / 'orchestrator_guard.py'), '--host', 'claude', '--conversation', sid,
                '--management-root', str(Path(store).parent), '--read-root', spec['task_base'],
                '--agent-type', 'bf-executor', '--agent-type', 'bf-architect', '--agent-type', 'bf-reviewer',
                '--card-root', str(Path(spec['records']) / 'dispatch'),
                *[x for a in LEAD_TYPES for x in ('--lead-agent-type', a)],
                *[x for a in WORKER_TYPES for x in ('--worker-agent-type', a)])
    return {'hooks': {
        'SessionStart': [{'hooks': [cmd(str(Path(__file__).resolve()), 'context', '--runtime', str(runtime))]}],
        'UserPromptSubmit': [{'hooks': [cmd(str(SCRIPTS / 'role_audit.py'), 'begin', '--host', 'claude', '--conversation', sid, '--log', log)]}],
        'PreToolUse': [{'matcher': '*', 'hooks': [guard]}],
        'PostToolUse': [{'matcher': '*', 'hooks': [cmd(str(SCRIPTS / 'role_audit.py'), 'record', '--host', 'claude', '--conversation', sid, '--log', log)]}],
        'Stop': [{'hooks': [cmd(str(SCRIPTS / 'stop_hook_pre_yield.py'), '--host', 'claude', '--store', store, '--role', 'orchestrator',
                                '--conversation', sid, '--audit-log', log)]}],
    }}


def context_hook(runtime):
    spec = json.loads((Path(runtime) / 'spec.json').read_text())
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if event.get('session_id') != spec['session_id']:
        print('{}'); return 0
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': capabilities(spec)}}, ensure_ascii=False))
    return 0


def build(args):
    workspace = Path(args.workspace).resolve(strict=True)
    if args.resume_launch:
        store = Path(args.store).resolve() if args.store else None
        if store is None:
            raise ValueError('--resume-launch requires --store of the bound ledger')
        runtime = store.parent / '.claude-launch' / args.resume_launch
        spec = json.loads((runtime / 'spec.json').read_text())
        if Path(spec['store']).resolve() != store:
            raise ValueError('launch belongs to another ledger')
        return spec, runtime, True
    if not args.task_base:
        if not sys.stdin.isatty():
            raise ValueError('specify the current task directory with --task-base; no task is selected automatically')
        args.task_base = input('请输入本会话的任务 base 目录：').strip()
        if not args.task_base:
            raise ValueError('task base must not be empty')
    task_base = Path(args.task_base).expanduser()
    task_base = (task_base if task_base.is_absolute() else workspace / task_base).resolve(strict=True)
    store = Path(args.store).expanduser() if args.store else task_base / '.batch-flow/flow.json'
    store = (store if store.is_absolute() else task_base / store).resolve()
    if not within(store, task_base) or store.name != 'flow.json' or store.parent in {workspace, task_base}:
        raise ValueError('store must be flow.json inside a dedicated management directory of the task base')
    launch_id = uuid.uuid4().hex
    runtime = store.parent / '.claude-launch' / launch_id
    spec = dict(launch_id=launch_id, session_id=str(uuid.uuid4()), python=sys.executable, workspace=str(workspace),
                task_base=str(task_base), store=str(store), records=str(store.parent / 'records'), runtime=str(runtime),
                profiles=[], model=args.model, effort=args.effort)
    return spec, runtime, False


def argv_for(spec, binary, runtime, resumed):
    argv = [binary, '--model', spec['model']]
    argv += ['--resume', spec['session_id']] if resumed else ['--session-id', spec['session_id']]
    argv += ['--settings', str(Path(runtime) / 'settings.json'), '--agents', json.dumps(agents(), ensure_ascii=False),
             '--append-system-prompt', capabilities(spec)]
    if spec.get('effort'):
        argv += ['--effort', spec['effort']]
    # Role agents must read the card (task base) and the role docs (skill dir); live e2e showed
    # reads outside the workspace are denied otherwise.
    for extra in [spec['task_base'], str(SKILL)]:
        if not within(extra, spec['workspace']):
            argv += ['--add-dir', extra]
    if not resumed:
        argv.append('你是本任务的 batch-flow 编排者。按系统提示中的工作循环，从第 1 步开始。')
    return argv


def launch(args):
    if sys.platform != 'linux':
        raise ValueError('Linux only')
    binary = shutil.which(args.binary)
    if not binary:
        raise ValueError('claude executable not found')
    version = subprocess.run([binary, '--version'], capture_output=True, text=True, check=True, timeout=15).stdout.split()[0]
    if version not in VERIFIED_VERSIONS:
        raise ValueError('launcher verified for Claude Code %s; review hook/agent interfaces before upgrading (found %s)' % (sorted(VERIFIED_VERSIONS), version))
    spec, runtime, resumed = build(args)
    argv = argv_for(spec, binary, runtime, resumed)
    if args.dry_run:
        print(json.dumps(dict(event='CLAUDE_LAUNCH_DRY_RUN', spec=spec, settings=settings(spec), argv=argv,
                                    env={ENV_KEY: spec['launch_id'], DEPTH_KEY: SPAWN_DEPTH}), ensure_ascii=False, indent=2))
        return 0
    store = Path(spec['store'])
    store.parent.mkdir(parents=True, exist_ok=True)
    with (store.parent / '.claude-orchestrator.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another Claude Code orchestrator launcher owns this ledger')
        if not resumed:
            runtime.mkdir(parents=True, mode=0o700)
            (Path(spec['records']) / 'dispatch').mkdir(parents=True, exist_ok=True)
            import agent_cli
            for backend in ['agy', 'codex', 'claude']:
                executable = shutil.which(backend)
                if not executable:
                    continue
                profile = runtime / (backend + '-profile.json')
                try:
                    agent_cli.probe(backend, profile, executable)
                    spec['profiles'].append(str(profile))
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    print(backend + ' CLI profile unavailable: ' + str(error), file=sys.stderr)
            atomic(runtime / 'spec.json', spec)
        # Rewritten on resume too, so a resumed session gets the current hook rules (role agents come from argv).
        atomic(runtime / 'settings.json', settings(spec))
        print(capabilities(spec), flush=True)
        print('launch_id=%s session_id=%s（续接：--resume-launch %s --store %s）' % (spec['launch_id'], spec['session_id'], spec['launch_id'], spec['store']), flush=True)
        env = os.environ.copy(); env[ENV_KEY] = spec['launch_id']; env[DEPTH_KEY] = SPAWN_DEPTH
        return subprocess.call(argv, cwd=spec['workspace'], env=env)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'context':
        parser = argparse.ArgumentParser(); parser.add_argument('--runtime', required=True)
        return context_hook(parser.parse_args(sys.argv[2:]).runtime)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workspace', default=str(Path.cwd()), help='source root; the session cwd')
    parser.add_argument('--task-base', help='current task directory (relative to --workspace allowed); prompts on a terminal if omitted')
    parser.add_argument('--store', help='flow.json inside the task; default <task-base>/.batch-flow/flow.json')
    parser.add_argument('--model', default='haiku', help='orchestrator model (default haiku)')
    parser.add_argument('--effort', choices=['low', 'medium', 'high', 'xhigh', 'max'], help='omit to inherit CLI config')
    parser.add_argument('--resume-launch', help='launch_id to resume the same orchestrator session')
    parser.add_argument('--binary', default='claude')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        return launch(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('Claude launcher: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
