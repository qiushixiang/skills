#!/usr/bin/env python3
"""Claude Code orchestrator launcher: one bound session, role agents, pre-tool guard, audit and stop hook.

Nothing is written to .claude/ or global settings: hooks and role agents are passed per launch
(--settings / --agents) and the runtime directory under the ledger keeps them as evidence.
Subcommand `context` is the SessionStart hook that re-injects capabilities after compaction.
Recovery is a restart: every launch is a new session, and `dispatch_card.py next` reconciles what the
previous launch left (collects finished work, records interrupted work as INVALID so status retries it).
"""
import argparse
import datetime
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


def next_command(spec):
    return shlex.join([spec['python'], str(SCRIPTS / 'dispatch_card.py'), 'next', '--store', spec['store']])


def capabilities(spec):
    status = shlex.join([spec['python'], str(SCRIPTS / 'flow.py'), '--store', spec['store'], 'status'])
    return '\n'.join([
        'Claude Code batch-flow 编排者（受限启动，会话 %s）' % spec['session_id'],
        '任务目录：' + spec['task_base'], '台账：' + spec['store'], '源码工作目录：' + spec['workspace'], '技能目录：' + str(SKILL),
        '',
        '你只做一件事：运行 ' + next_command(spec) + '，按输出的 do 字段办：',
        '- do=call：逐字照抄 tool 与 input 发起这一个调用（字段一个不改），返回后立即再运行 next。',
        '- do=wait：结束本轮，等完成通知；任何完成通知到达后运行 next。',
        '- do=ask_user：把 items 原样告诉用户；拿到用户原话后按 todo 用 flow.py 登记（如 set_models/authorize），再运行 next。',
        '- do=report / done：把 say、problems、disposition 原样告诉用户。',
        '回收、登记、重启后的清理都由 next 完成，不需要你判断。用户问进度时可运行 ' + status + '。',
        '用户说停：停止发起新调用并结束本轮；要恢复时重启编排者即可（next 会处理上次留下的工作）。',
        '禁止：读业务源码、改代码或设计书、Git 写、产品构建/测试、任意 shell 组合。被 ROLE_BOUNDARY_BLOCKED 拦截时不要换方式绕过，向用户报告。',
        '技术内容（实现、审阅、裁决、方案）一律交给角色，你只协调。',
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
                                '--conversation', sid, '--audit-log', log, '--next-command', next_command(spec))]}],
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
    return spec, runtime


def argv_for(spec, binary, runtime):
    argv = [binary, '--model', spec['model'], '--session-id', spec['session_id']]
    argv += ['--settings', str(Path(runtime) / 'settings.json'), '--agents', json.dumps(agents(), ensure_ascii=False),
             '--append-system-prompt', capabilities(spec)]
    if spec.get('effort'):
        argv += ['--effort', spec['effort']]
    # Role agents must read the card (task base) and the role docs (skill dir); live e2e showed
    # reads outside the workspace are denied otherwise.
    for extra in [spec['task_base'], str(SKILL)]:
        if not within(extra, spec['workspace']):
            argv += ['--add-dir', extra]
    argv.append('你是本任务的 batch-flow 编排者。现在运行 next。')
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
    spec, runtime = build(args)
    argv = argv_for(spec, binary, runtime)
    if args.dry_run:
        print(json.dumps(dict(event='CLAUDE_LAUNCH_DRY_RUN', spec=spec, settings=settings(spec), argv=argv,
                                    env={ENV_KEY: spec['launch_id'], DEPTH_KEY: SPAWN_DEPTH}), ensure_ascii=False, indent=2))
        return 0
    store = Path(spec['store'])
    store.parent.mkdir(parents=True, exist_ok=True)
    # .orchestrator.lock is shared with the AGY launcher: one orchestrator per ledger on any host.
    with (store.parent / '.claude-orchestrator.lock').open('a') as lock, (store.parent / '.orchestrator.lock').open('a') as any_host:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(any_host, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another orchestrator launcher (Claude Code or AGY) owns this ledger')
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
        # Taken under the ledger lock: cards of earlier launches cannot still be running in Claude Code.
        spec['started_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        atomic(runtime / 'spec.json', spec)
        atomic(runtime / 'settings.json', settings(spec))
        print(capabilities(spec), flush=True)
        print('launch_id=%s session_id=%s（中断后直接重新启动即可）' % (spec['launch_id'], spec['session_id']), flush=True)
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
