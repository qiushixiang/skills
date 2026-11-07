"""Chat-front command whitelist (design 6.3): one bf/cli.py invocation with a read/answer/run/stop subcommand."""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

COMMANDS = ('status', 'report', 'run', 'stop', 'resume', 'answer')
FLAGS = {'run': {'--parallel': 1, '--profile': 1}, 'answer': {'--file': 1, '--approve': 0, '--reject': 0}}


def allowed(command):
    if re.search(r'[;&|<>`$()\n\\]', command or ''):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) < 5 or not os.path.basename(words[0]).startswith('python'):
        return False
    cli = os.path.normpath(words[1])
    if os.path.basename(cli) != 'cli.py' or os.path.basename(os.path.dirname(cli)) != 'bf':
        return False
    if words[2] != '--base' or words[4] not in COMMANDS:
        return False
    rest, flags, i = words[5:], FLAGS.get(words[4], {}), 0
    if words[4] == 'answer':
        if not rest or not re.match(r'^WO-\d+$', rest[0]):
            return False
        rest = rest[1:]
    while i < len(rest):
        if rest[i] not in flags:
            return False
        i += 1 + flags[rest[i]]
    return i == len(rest)


# ---- launcher: PreToolUse guard, settings and prompt for the chat-front Claude Code session ---------

PASS_TOOLS = ('ScheduleWakeup', 'CronCreate', 'CronDelete', 'CronList')


def _inside(path, root, cwd=None):
    path = os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd or '', path))
    root = os.path.normpath(str(root))
    return path.startswith(root + os.sep)


def hook_decision(event, base):
    """Allow only the front's duties: bf commands, reading attention and run cards, writing new answer files."""
    base = os.path.normpath(str(base))
    tool, inp, cwd = event.get('tool_name'), event.get('tool_input') or {}, event.get('cwd')
    path = inp.get('file_path') or ''
    if tool == 'Bash':
        ok = allowed(inp.get('command') or '')
    elif tool == 'Read':
        ok = bool(path) and (os.path.normpath(path) == os.path.join(base, 'attention.md')
                             or _inside(path, os.path.join(base, 'records', 'runs'), cwd))
    elif tool == 'Write':
        target = path if os.path.isabs(path) else os.path.join(cwd or '', path)
        ok = bool(path) and _inside(target, os.path.join(base, 'answers')) and not os.path.exists(target)
    else:
        ok = tool in PASS_TOOLS
    return {'decision': 'allow' if ok else 'deny',
            'reason': '' if ok else 'chat front may only run bf commands, read attention/run cards and write answers/'}


def settings(base):
    cmd = '%s %s hook --base %s' % (shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)), shlex.quote(str(base)))
    return {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': cmd, 'timeout': 20}]}]}}


def prompt(base):
    cli = '%s %s --base %s' % (sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cli.py'), base)
    return '\n'.join([
        '你是 batch-flow v2 的聊天前端，只做三件事：启动/停止/查看驱动器，定时汇报，转交用户回答。你不选择、不派发、不回收工单，不读写状态文件。',
        '只能运行这些命令（单条、不加管道或重定向）：',
        '- 查看：%s status | %s report' % (cli, cli),
        '- 启动驱动器（后台运行）：%s run；停止/恢复：%s stop | %s resume' % (cli, cli, cli),
        '- 登记用户回答：%s answer WO-n --file %s/answers/<n>.txt --approve|--reject' % (cli, base),
        '汇报：用户要求启动后，用 ScheduleWakeup 或 CronCreate 每 10 分钟运行一次 report，用自然语言转述：在途工单与耗时、新完成或新开立的工单、额度余量、需要用户回答的问题。',
        '需要用户的事项见 %s/attention.md；把问题原样告诉用户。' % base,
        '回答流程：用户口述决定后，把用户的原话写入新文件 %s/answers/<n>.txt（不要改写、不要补充），再运行 answer 命令，approve 对应用户同意。' % base,
        '被拦截时不要换方式绕过，向用户报告。可读文件：attention.md 与 records/runs/ 下的卡片和交付。',
    ])


def launch(base, model=None, binary='claude'):
    base = Path(str(base)).resolve()
    (base / 'answers').mkdir(exist_ok=True)
    runtime = base / 'front'
    runtime.mkdir(exist_ok=True)
    (runtime / 'settings.json').write_text(json.dumps(settings(base), ensure_ascii=False), encoding='utf-8')
    argv = [binary, '--settings', str(runtime / 'settings.json'), '--append-system-prompt', prompt(base),
            '--add-dir', str(base)]
    if model:
        argv += ['--model', model]
    argv.append('你是本任务的 batch-flow 聊天前端。先运行 status 并展示状态，等用户指示。')
    return subprocess.call(argv, cwd=str(base.parent))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='front')
    sub = ap.add_subparsers(dest='cmd', required=True)
    hook = sub.add_parser('hook')
    hook.add_argument('--base', required=True)
    run = sub.add_parser('launch')
    run.add_argument('--base', required=True)
    run.add_argument('--model')
    run.add_argument('--binary', default='claude')
    args = ap.parse_args(argv)
    if args.cmd == 'launch':
        return launch(args.base, args.model, args.binary)
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    d = hook_decision(event, args.base)
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': d['decision'],
                                             'permissionDecisionReason': d['reason']}}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
