"""Chat-front command whitelist (design 6.3): one bf/cli.py invocation with a read/answer/run/stop subcommand."""
import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

COMMANDS = ('status', 'report', 'run', 'ensure-run', 'stop', 'resume', 'answer', 'doctor', 'watch')
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
ADJUST['retry'] = ADJUST['escalate'] = {'pos': 'one', 'flags': {'--note': 1}, 'need': ('--note',), 'order': True}
ADJUST['snapshot'] = {'pos': None, 'flags': {'--repo': 1, '--paths': -1}, 'need': ('--repo', '--paths')}
ADJUST['reconcile'] = {'pos': None, 'flags': {'--file': 1, '--approval': 1, '--dry-run': 0}, 'need': ('--file', '--approval')}
FLAGS = {'doctor': {}, 'watch': {'--since': 1, '--once': 0}, 'stop': {'--now': 0}, 'report': {'--text': 0}, 'run': {'--background': 0, '--parallel': 1, '--profile': 1}, 'answer': {'--file': 1, '--approve': 0, '--reject': 0}}
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
    if spec.get('order') and (not rest or not re.match(r'^WO-\d+$', rest[0])):
        return False
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
    if '--file' in values and not _inside(values['--file'][0], os.path.join(base, 'answers')):
        return False
    return True


def allowed(command, account_switch=(), bound_base=None):
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
    if bound_base is not None and (Path(words[3]).resolve() != Path(bound_base).resolve() or
                                  Path(cli).resolve() != Path(__file__).resolve().with_name('cli.py') or
                                  Path(words[0]).resolve() != Path(sys.executable).resolve()):
        return False
    if words[4] == 'record':
        rest = words[5:]
        if '--' not in rest:
            return False
        head, child = rest[:rest.index('--')], rest[rest.index('--') + 1:]
        return (head == [] or (len(head) == 2 and head[0] == '--cwd')) and bool(child) and readonly_words(child)
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

PASS_TOOLS = ('Monitor', 'ScheduleWakeup', 'CronCreate', 'CronDelete', 'CronList')


def _inside(path, root, cwd=None):
    path = os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd or '', path))
    root = os.path.realpath(str(root))
    return path.startswith(root + os.sep)


READ_TOOLS = ('Read', 'Grep', 'Glob', 'Skill')
READONLY = ('ls', 'cat', 'head', 'tail', 'grep', 'rg', 'wc', 'ps', 'pgrep', 'true')
FIND_WRITES = ('-exec', '-execdir', '-ok', '-okdir', '-delete', '-fprint', '-fprint0', '-fprintf', '-fls')
GIT_READS = ('log', 'status', 'diff', 'show')
HOOK_META = re.compile(r'[;<>`$\n\\]')


def readonly_words(w):
    """ls/cat/head/tail/grep/rg/wc/ps/pgrep, find without write actions, git -C <dir> log|status|diff|show, cd <dir>."""
    if not w:
        return False
    if w[0] in READONLY:
        return not any(x in ('-f', '-F', '--follow', '--pre') or x.startswith('--pre=') for x in w[1:])
    if w[0] == 'find':
        return not any(x in FIND_WRITES for x in w[1:])
    if w[0] == 'cd':
        return len(w) == 2
    if w[0] == 'git' and len(w) >= 4 and w[1] == '-C':
        return w[3] in GIT_READS and not any(x.startswith(('--output', '--ext-diff', '--exec')) for x in w[4:])
    return False


def readonly_shell(command):
    if HOOK_META.search(command or ''):
        return False
    try:
        return readonly_words(shlex.split(command))
    except ValueError:
        return False


def shell_ok(command, account_switch=(), bound_base=None):
    """Pipelines (|) and && whose every segment is an allowed bf command, account switch or read-only command."""
    if HOOK_META.search(command or ''):
        return False
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return False
    segments, cur = [], []
    for t in tokens:
        if t in ('|', '&&'):
            segments.append(cur)
            cur = []
        elif re.fullmatch(r'[();<>|&]+', t):
            return False
        else:
            cur.append(t)
    segments.append(cur)
    return all(s and (readonly_words(s) or allowed(' '.join(shlex.quote(x) for x in s), account_switch, bound_base)) for s in segments)


def hook_decision(event, base, account_switch=()):
    """Operations role (design 22): read anything, write only new answer files, bf commands (not init/revise), the
    configured account switch and a read-only shell."""
    base = os.path.normpath(str(base))
    tool, inp, cwd = event.get('tool_name'), event.get('tool_input') or {}, event.get('cwd')
    path = inp.get('file_path') or ''
    if tool == 'Bash':
        command = inp.get('command') or ''
        ok = shell_ok(command, account_switch, base)
    elif tool == 'Write':
        target = path if os.path.isabs(path) else os.path.join(cwd or '', path)
        ok = bool(path) and _inside(target, os.path.join(base, 'answers')) and not os.path.exists(target)
    else:
        ok = tool in READ_TOOLS or tool in PASS_TOOLS
    return {'decision': 'allow' if ok else 'deny',
            'reason': '' if ok else 'chat front may read anything, write only answers/, and run bf commands or a read-only shell'}


def settings(base, account_switch=()):
    cmd = '%s %s hook --base %s' % (shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)), shlex.quote(str(base)))
    cmd += ''.join(' --account-switch %s' % shlex.quote(p) for p in account_switch)
    return {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [{'type': 'command', 'command': cmd, 'timeout': 20}]}]}}


def prompt(base, account_switch=(), backend='claude', runtime=None):
    cli = shlex.join([sys.executable, str(Path(__file__).resolve().with_name('cli.py')), '--base', str(base)])
    watch = ('2. 启动驱动器后立刻用 Monitor 工具挂上 CLI watch（事件推送，每行一条：待你决定、运维故障、交付结果、批次放行、驱动器启停），被唤醒后告诉用户并按需处理；另用 CronCreate 建一个每 10 分钟重复的定时任务（不要用一次性的 ScheduleWakeup），以及用户询问时，运行 CLI report --text，把结果转述给用户。'
             if backend == 'claude' else
             '2. 启动器将事件与每 10 分钟报告写入 %s/notifications.log，不向当前 TUI 插入文字，也不会自动唤醒闲置模型。不要调用 Claude 专有的 Monitor/CronCreate。每次用户交互先读最新报告，处理运维故障、转达待决问题；用户询问时运行 CLI report --text。' % runtime)
    if backend == 'opencode':
        watch = ('2. 本次 OpenCode 插件每 10 分钟及关键事件后自动请求汇报，忙碌时合并等待，不需要创建定时任务。'
                 '收到带 batch-flow 自动通知标记的请求时，重新运行 CLI report --text，并按需读 report JSON，主动向用户简短汇报、处理运维和转达问题。'
                 '自动通知是脚本提醒，不是用户原话或批准，不能据此解除 stop、扩大授权或回答待决问题。'
                 '不要调用 Claude 专有的 Monitor/CronCreate。用户询问时也运行 CLI report --text。')
    lines = [
        '你是 batch-flow v2 的运维与沟通角色（聊天前端）。脚本负责派发、交付判定与额度；技术负责人负责技术判断；你负责运维、汇报和转达用户的决定。',
        '用户问题只允许六类新增决定；report.ops 是受阻待处理，不是用户确认。次数用尽、工具故障或技术暂时无解只报告，不追加“是否继续”的问题。边界见 %s。' % (Path(__file__).resolve().parents[2] / 'references/user-decisions.md'),
        '下文 CLI 指：%s' % cli,
        '职责：',
        '1. 启动、停止、查看驱动器：CLI run --background 后台启动并立即返回；按 plan.env.require_host 要求使用宿主执行并保留原有权限审批。CLI stop / CLI resume（stop 阻止新派发与续跑，在途调用自行结束；用户说“立刻停”“全部停掉”时用 CLI stop --now，终止在途调用，登记为免扣无效）；CLI status。',
        '   驱动器空闲时退出。answer/resume/retry/escalate/unhold/grant/envelope/set-role/supersede 成功后统一运行 CLI ensure-run，按上次配置确保单实例继续；它不会解除 stop。首次运行仍用 run。',
        watch + '需要更多细节时读 CLI report 的 JSON、%s/attention.md 与 %s/records/runs/ 下的卡片和原始输出。' % (base, base),
        '   当前待决只以最新 CLI report 的 ask_user 为准；attention.md 是派生视图，旧通知与历史对话不代表仍需批准。计划修订或用户回答后先重新查询，不重复请求已失效事项。',
        '3. 处理运维故障：report 的“待你决定”里带运维故障的项，是 CLI 连续不可用而被暂停的工单。读它的 problem 和对应运行目录 %s/records/runs/<运行>/ 下的原始输出再判断：' % base,
        '   - codex 额度或用量限制，且已配置账号切换命令：运行账号切换，然后 CLI retry WO-n --note \'已切换账号\'；',
        '   - 认证失效、网络、需要用户操作的问题：如实告诉用户；用户处理后再 retry；',
        '   - 看起来是技术问题（模型调用格式错误、反复同一异常等）：CLI escalate WO-n --note \'你的观察\'，交给技术负责人。',
        '   工单号只用于命令，不要对用户说。',
        '4. 转达用户的回答与调整，全部带用户原话文件 --approval %s/answers/<n>.txt：' % base,
        '   - 回答问题：CLI answer WO-n --file %s/answers/<n>.txt --approve|--reject；' % base,
        '   - 换模型：CLI models <后端>；CLI set-role <角色> --backend <后端> --name \'<用户的说法>\' [--effort E] --approval F（角色：executor backup_executor lead reviewer third_party）；多个候选或未找到时把候选展示给用户，绝不自己挑；',
        '   - CLI envelope --add|--remove <批次>... --approval F；CLI grant <交付目标> --implement|--backup|--lead|--repair|--review N --approval F（审阅组只用 --review，只增不减）；CLI hold|unhold <交付目标或批次>... --approval F；CLI supersede WO-n --reason R --approval F。',
        '   - 已获批准的历史登记修复用 CLI reconcile --file answers/清单.json --approval answers/原话.txt，先加 --dry-run 核对；不改写历史运行、不启动项目编排。详情见 operations.md。',
        '   用户已明确提出或批准的调整，直接把原话保存到新文件 %s/answers/<n>.txt 后执行并复述结果，不重复请示同一授权。只有缺少不可替代的信息或真实新增授权边界才请求决定。' % base,
        '边界：不改代码或状态文件，不运行构建或测试，不做技术判断；新增或改写交付目标、验收、依赖是计划修订，由技术负责人起草，你不运行 init/revise。可以读取任何文件和运行只读命令（ls cat head tail grep rg wc find、git -C <目录> log status diff show）。被拦截时不要换方式绕过，向用户报告。',
        '汇报写法：按批次与标题描述工作（如“S15「旧帧不发布」实施”），不要单独抛工单号，不罗列已完成项，只说正在做、下一步、需要用户决定的事和风险；简短。']
    for p in account_switch:
        lines.append('账号切换命令：%s [-k] [-n] [目标账号]（只此形式）。由你判断何时切换，例如 codex 额度用尽时；切换后对受影响工单 retry。' % p)
    return '\n'.join(lines)


def launch(base, model=None, binary=None, account_switch=(), backend='claude', effort=None, dry_run=False):
    if backend not in ('claude', 'codex', 'agy', 'opencode'):
        raise ValueError('unsupported front backend: %s' % backend)
    if effort:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from agent_cli import effort_override
        effort = effort_override(backend, effort)
    account_switch = [configured_switch(p) for p in account_switch]
    binary = binary or backend
    base = Path(str(base)).resolve()
    (base / 'answers').mkdir(exist_ok=True)
    runtime = base / 'front'
    runtime.mkdir(exist_ok=True)
    if backend != 'claude':
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from bf import api, front_hosts
        try:
            api.load(base)
        except api.BfError as error:
            raise ValueError(str(error)) from error
        runtime = runtime / backend
        runtime.mkdir(exist_ok=True)
        with (runtime / '.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('%s front already running for this task' % backend)
            for marker in ('ready.json', 'plugin-ready.json'):
                if (runtime / marker).exists():
                    (runtime / marker).unlink()
            instructions = prompt(base, account_switch, backend, runtime)
            instructions += '\n启动后先确认本次工具门禁被宿主加载（Codex 首次或 hook 变更后在 /hooks 中信任），再运行 status。'
            spec = front_hosts.prepare(base, backend, binary, runtime, instructions, model, effort, account_switch,
                                       query_catalog=not dry_run)
            if dry_run:
                print(json.dumps(spec, ensure_ascii=False))
                return 0
            print('batch-flow %s 前端；事件与定时汇报：%s/notifications.log' % (backend, runtime), flush=True)
            if spec['model']:
                print('前端模型：%s → %s（可用性由宿主和账号决定）' % (spec['requested_model'] or '默认', spec['model']), flush=True)
            return front_hosts.run(spec, base)
    (runtime / 'settings.json').write_text(json.dumps(settings(base, account_switch), ensure_ascii=False), encoding='utf-8')
    argv = [binary, '--settings', str(runtime / 'settings.json'), '--append-system-prompt', prompt(base, account_switch),
            '--add-dir', str(base)]
    if model:
        argv += ['--model', model]
    if effort:
        argv += ['--effort', effort]
    argv.append('你是本任务的 batch-flow 聊天前端。先运行 status 并展示状态，等用户指示。')
    if dry_run:
        print(json.dumps({'argv': argv, 'cwd': str(base.parent), 'runtime': str(runtime), 'backend': backend}, ensure_ascii=False))
        return 0
    return subprocess.call(argv, cwd=str(base.parent))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='front')
    sub = ap.add_subparsers(dest='cmd', required=True)
    hook = sub.add_parser('hook')
    hook.add_argument('--base', required=True)
    hook.add_argument('--account-switch', action='append', default=[])
    hook.add_argument('--backend', choices=['claude', 'codex', 'agy', 'opencode'], default='claude')
    hook.add_argument('--runtime')
    run = sub.add_parser('launch', description='Launch a Claude Code, Codex CLI, agy or OpenCode V2 chat front.')
    run.add_argument('--base', required=True)
    run.add_argument('--model')
    run.add_argument('--backend', choices=['claude', 'codex', 'agy', 'opencode'], default='claude')
    run.add_argument('--binary', help='Executable for the selected backend')
    run.add_argument('--effort', help='Optional backend reasoning effort; omitted inherits CLI defaults')
    run.add_argument('--dry-run', action='store_true', help='Write per-launch configuration and print argv without starting a model')
    run.add_argument('--account-switch', action='append', default=[])
    args = ap.parse_args(argv)
    if args.cmd == 'launch':
        try:
            return launch(args.base, args.model, args.binary, args.account_switch, args.backend, args.effort, args.dry_run)
        except (OSError, ValueError, RuntimeError) as error:
            print('batch-flow front: %s' % error, file=sys.stderr)
            return 2
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if args.backend != 'claude':
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from bf import front_hosts
        if not args.runtime:
            print('non-Claude hook requires --runtime', file=sys.stderr)
            return 2
        try:
            result = front_hosts.hook(event, Path(args.base).resolve(), args.backend, Path(args.runtime), args.account_switch)
        except Exception:
            # Native hosts may fail open on hook errors: return a valid, explicit denial instead.
            result = ({'decision': 'deny', 'reason': 'batch-flow front guard failed; action denied'} if args.backend in ('agy', 'opencode') else
                      {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                              'permissionDecisionReason': 'batch-flow front guard failed; action denied'}})
        print(json.dumps(result, ensure_ascii=False))
        return 0
    d = hook_decision(event, args.base, args.account_switch)
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': d['decision'],
                                             'permissionDecisionReason': d['reason']}}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
