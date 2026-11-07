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
# Adjustment commands: positionals ('one', 'many' or None), flags with value counts (-1 = variadic), required flags.
ADJUST = {
    'models': {'pos': 'one', 'flags': {}, 'need': ()},
    'set-role': {'pos': 'one', 'flags': {'--backend': 1, '--name': 1, '--effort': 1, '--approval': 1}, 'need': ('--backend', '--name', '--approval')},
    'envelope': {'pos': None, 'flags': {'--add': -1, '--remove': -1, '--approval': 1}, 'need': ('--approval',), 'any': ('--add', '--remove')},
    'grant': {'pos': 'one', 'flags': dict([('--' + k, 1) for k in ('implement', 'backup', 'lead', 'repair', 'review')] + [('--approval', 1)]),
              'need': ('--approval',), 'any': ('--implement', '--backup', '--lead', '--repair', '--review')},
    'hold': {'pos': 'many', 'flags': {'--approval': 1}, 'need': ('--approval',)},
    'unhold': {'pos': 'many', 'flags': {'--approval': 1}, 'need': ('--approval',)},
    'supersede': {'pos': 'one', 'flags': {'--reason': 1, '--approval': 1}, 'need': ('--reason', '--approval')},
}
FLAGS = {'run': {'--parallel': 1, '--profile': 1}, 'answer': {'--file': 1, '--approve': 0, '--reject': 0}}
SHELL_META = r'[;&|<>`$()\n\\]'
SWITCH_FLAGS = ('-k', '-n')
SWITCH_TARGET = re.compile(r'[^@\s/]+@[^@\s/]+')


def configured_switch(path):
    """Validate a launcher --account-switch value; return its resolved absolute path."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError('--account-switch needs an absolute path: %s' % path)
    p = p.resolve()
    if not p.is_file() or not os.access(str(p), os.X_OK):
        raise ValueError('--account-switch is not an executable file: %s' % p)
    return str(p)


def _switch_allowed(words, paths):
    """<configured absolute path> [-k] [-n] [one target account] (rules of the v1 account_switch command)."""
    head = Path(words[0])
    if not head.is_absolute() or head.resolve() not in set(Path(p).resolve() for p in paths):
        return False
    flags = [a for a in words[1:] if a.startswith('-')]
    targets = [a for a in words[1:] if not a.startswith('-')]
    return (all(x in SWITCH_FLAGS for x in flags) and len(flags) == len(set(flags))
            and len(targets) <= 1 and all(SWITCH_TARGET.fullmatch(t) for t in targets))


def _adjust_allowed(spec, base, rest):
    values, pos, i = {}, [], 0
    while i < len(rest):
        tok = rest[i]
        if tok in spec['flags']:
            n = spec['flags'][tok]
            if tok in values:
                return False
            vals, i = [], i + 1
            while i < len(rest) and (len(vals) < n if n > 0 else not rest[i].startswith('--')):
                vals.append(rest[i])
                i += 1
            if len(vals) != n and (n > 0 or not vals):
                return False
            values[tok] = vals
        elif tok.startswith('--'):
            return False
        else:
            pos.append(tok)
            i += 1
    if len(pos) != {'one': 1, 'many': len(pos), None: 0}[spec['pos']] or (spec['pos'] == 'many' and not pos):
        return False
    if not all(f in values for f in spec['need']) or (spec.get('any') and not any(f in values for f in spec['any'])):
        return False
    if '--approval' in values and not _inside(values['--approval'][0], os.path.join(base, 'answers')):
        return False
    return True


def allowed(command, account_switch=()):
    if re.search(SHELL_META, command or ''):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if words and account_switch and _switch_allowed(words, account_switch):
        return True
    if len(words) < 5 or not os.path.basename(words[0]).startswith('python'):
        return False
    cli = os.path.normpath(words[1])
    if os.path.basename(cli) != 'cli.py' or os.path.basename(os.path.dirname(cli)) != 'bf':
        return False
    if words[2] != '--base':
        return False
    if words[4] in ADJUST:
        return _adjust_allowed(ADJUST[words[4]], os.path.normpath(words[3]), words[5:])
    if words[4] not in COMMANDS:
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


def hook_decision(event, base, account_switch=()):
    """Allow only the front's duties: bf commands, reading attention and run cards, writing new answer files."""
    base = os.path.normpath(str(base))
    tool, inp, cwd = event.get('tool_name'), event.get('tool_input') or {}, event.get('cwd')
    path = inp.get('file_path') or ''
    if tool == 'Bash':
        ok = allowed(inp.get('command') or '', account_switch)
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


def settings(base, account_switch=()):
    cmd = '%s %s hook --base %s' % (shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)), shlex.quote(str(base)))
    cmd += ''.join(' --account-switch %s' % shlex.quote(p) for p in account_switch)
    return {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': cmd, 'timeout': 20}]}]}}


def prompt(base, account_switch=()):
    cli = '%s %s --base %s' % (sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cli.py'), base)
    lines = [
        '你是 batch-flow v2 的聊天前端：启动/停止/查看驱动器，定时汇报，转交用户回答，执行用户的结构化调整。你不选择、不派发、不回收工单，不读写状态文件。',
        '下文 CLI 指：%s' % cli,
        '只能运行这些命令（单条，不加管道或重定向）：',
        '- 查看：CLI status | CLI report',
        '- 启动驱动器：CLI run，必须用 Bash 工具的 run_in_background 选项启动，这样它在你与用户交谈时持续运行；不要前台运行。',
        '- 停止/恢复：CLI stop | CLI resume；stop 只阻止新派发，在途调用会自行结束并被回收。',
        '- 登记用户回答：CLI answer WO-n --file %s/answers/<n>.txt --approve|--reject' % base,
        '汇报：用户要求启动后，用 ScheduleWakeup 或 CronCreate 每 10 分钟运行一次 report，用自然语言转述：在途工单与耗时、新完成或新开立的工单、额度余量、需要用户回答的问题。',
        '需要用户的事项见 %s/attention.md；把问题原样告诉用户。' % base,
        '回答流程：用户口述决定后，把用户的原话写入新文件 %s/answers/<n>.txt（不要改写、不要补充），再运行 answer 命令，approve 对应用户同意。' % base,
        '调整（每条都带 --approval %s/answers/<n>.txt，即用户原话文件）：' % base,
        '- 换模型：CLI models <后端>（只读查目录）；CLI set-role <角色> --backend <后端> --name \'<用户的说法>\' [--effort E] --approval F。角色：executor backup_executor lead reviewer third_party。',
        '- 范围：CLI envelope --add|--remove <批次>... --approval F；额度：CLI grant <交付目标> --implement|--backup|--lead|--repair|--review N --approval F（只增不减）。',
        '- 暂停：CLI hold <交付目标或批次>... --approval F；恢复：CLI unhold ...；作废开立中的工单：CLI supersede WO-n --reason R --approval F。',
        '流程：用户用自然语言提出调整时，先把它复述成上面的确切命令并请用户确认；确认后把用户的话原样写入 answers/<n>.txt，再运行命令。',
        'set-role 返回 needs_clarification 或 not_found 时，把候选逐项展示给用户让其选择，绝不自己挑；用户选定后用确切的模型 id 重新执行。',
        '新增或改写交付目标、验收、依赖属于计划修订，由技术负责人起草，你不运行 init/revise，告诉用户转交技术负责人。',
        '被拦截时不要换方式绕过，向用户报告。可读文件：attention.md 与 records/runs/ 下的卡片和交付。']
    for p in account_switch:
        lines.append('账号切换：用户要求时可运行 %s [-k] [-n] [目标账号]，仅此形式。' % p)
    return '\n'.join(lines)


def launch(base, model=None, binary='claude', account_switch=()):
    account_switch = [configured_switch(p) for p in account_switch]
    base = Path(str(base)).resolve()
    (base / 'answers').mkdir(exist_ok=True)
    runtime = base / 'front'
    runtime.mkdir(exist_ok=True)
    (runtime / 'settings.json').write_text(json.dumps(settings(base, account_switch), ensure_ascii=False), encoding='utf-8')
    argv = [binary, '--settings', str(runtime / 'settings.json'), '--append-system-prompt', prompt(base, account_switch),
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
    hook.add_argument('--account-switch', action='append', default=[])
    run = sub.add_parser('launch')
    run.add_argument('--base', required=True)
    run.add_argument('--model')
    run.add_argument('--binary', default='claude')
    run.add_argument('--account-switch', action='append', default=[])
    args = ap.parse_args(argv)
    if args.cmd == 'launch':
        return launch(args.base, args.model, args.binary, args.account_switch)
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    d = hook_decision(event, args.base, args.account_switch)
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': d['decision'],
                                             'permissionDecisionReason': d['reason']}}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
