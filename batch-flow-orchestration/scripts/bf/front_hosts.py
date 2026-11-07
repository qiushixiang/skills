"""Interactive CLI front adapters. Reuse the front policy; never schedule work orders."""
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time

from . import api, front


def agy_model(name, binary, query_catalog=True):
    """Explicit aliases win; resolve other short names uniquely, without choosing a variant."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import model_catalog
    key = model_catalog.normalized(name)
    if not key:
        raise ValueError('AGY model name is empty')
    path = Path(__file__).resolve().parents[2] / 'references' / 'model-aliases.json'
    aliases = json.loads(path.read_text(encoding='utf-8')).get('agy', {})
    matches = {value for alias, value in aliases.items() if model_catalog.normalized(alias) == key}
    if matches:
        if len(matches) != 1 or not all(isinstance(x, str) and x.strip() for x in matches):
            raise ValueError('conflicting or invalid AGY aliases for %s' % name)
        return matches.pop()
    # Explicit IDs may describe models not enumerated for this account; never substitute another ID.
    if re.fullmatch(r'[a-zA-Z0-9]+(?:[-_.][a-zA-Z0-9]+)+', name):
        return name
    if not query_catalog:
        raise ValueError('AGY short name %r needs a model catalogue; use a full ID or add an explicit model-aliases.json mapping for dry-run' % name)
    from agent_cli import version
    try:
        profile = {'backend': 'agy', 'binary': binary, 'version': version(binary)}
        catalog = model_catalog.query(profile)
    except subprocess.SubprocessError as error:
        raise ValueError('cannot query AGY model metadata; use a full ID or an explicit alias') from error
    result = model_catalog.resolve(profile, catalog, name)
    if result['status'] == 'resolved':
        return result['config']['model']
    candidates = result['candidates'] or [m for m in catalog['models'] if any(
        key in model_catalog.normalized(m.get(field, '')) for field in ('id', 'name', 'family'))]
    if len(candidates) == 1:
        return candidates[0]['id']
    if candidates:
        raise ValueError('AGY short name %r is ambiguous; choose an exact ID: %s' % (name, ', '.join(m['id'] for m in candidates)))
    raise ValueError('AGY model %r was not found; available IDs: %s' % (name, ', '.join(m['id'] for m in catalog['models'])))


def hook_command(base, backend, runtime, account_switch):
    args = [sys.executable, str(Path(front.__file__).resolve()), 'hook', '--base', str(base),
            '--backend', backend, '--runtime', str(runtime)]
    for path in account_switch:
        args += ['--account-switch', path]
    return ' '.join(shlex.quote(x) for x in args)


def prepare(base, backend, binary, runtime, instructions, model=None, effort=None, account_switch=(), query_catalog=True):
    """Create only per-launch files. Never alter user/project configuration or credentials."""
    requested_model = model
    if backend == 'opencode':
        from . import front_opencode
        return front_opencode.prepare(base, binary, runtime, instructions, model, account_switch)
    if backend == 'agy' and model is not None:
        model = agy_model(model, binary, query_catalog)
    command = hook_command(base, backend, runtime, account_switch)
    handler = {'type': 'command', 'command': command, 'timeout': 20}
    groups = [{'matcher': '*', 'hooks': [handler]}]
    initial = '先运行 status 并展示状态，等用户指示。'
    if backend == 'codex':
        # -c takes TOML, not a JSON object. Keep the small inline hook table explicit.
        hooks = '[{matcher="*",hooks=[{type="command",command=%s,timeout=20}]}]' % json.dumps(command, ensure_ascii=False)
        argv = [binary, '--cd', str(base.parent), '--sandbox', 'workspace-write', '--ask-for-approval', 'on-request',
                '--no-alt-screen', '--enable', 'hooks', '-c', 'hooks.PreToolUse=' + hooks,
                '-c', 'developer_instructions=' + json.dumps(instructions, ensure_ascii=False)]
        if effort:
            argv += ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
        cwd = base.parent
    else:
        # AGY ignores settings.hooks; discovery loads this isolated workspace's .agents/hooks.json.
        custom = runtime / '.agents'
        custom.mkdir(exist_ok=True)
        (custom / 'hooks.json').write_text(json.dumps({'batch-flow-front': {'PreToolUse': groups}}, ensure_ascii=False), encoding='utf-8')
        (runtime / 'AGENTS.md').write_text(instructions + '\n', encoding='utf-8')
        argv = [binary, '--add-dir', str(base.parent), '--mode', 'accept-edits',
                '--settings', json.dumps({'altScreenMode': 'never', 'toolPermission': 'request-review'})]
        if effort:
            argv += ['--effort', effort]
        cwd = runtime
    if model:
        argv += ['--model', model]
    argv += ['--prompt-interactive', initial] if backend == 'agy' else [initial]
    return {'argv': argv, 'cwd': str(cwd), 'runtime': str(runtime), 'backend': backend,
            'requested_model': requested_model, 'model': model}


def _patch_allowed(text, base, cwd):
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
        return False
    paths, i = set(), 1
    while i < len(lines) - 1:
        if not lines[i].startswith('*** Add File: '):
            return False
        path = lines[i][14:]
        if path in paths or front.hook_decision({'tool_name': 'Write', 'tool_input': {'file_path': path}, 'cwd': cwd}, base)['decision'] != 'allow':
            return False
        paths.add(path)
        i += 1
        while i < len(lines) - 1 and lines[i].startswith('+'):
            i += 1
    return bool(paths)


def hook(event, base, backend, runtime, account_switch=()):
    """Normalize native tool events, then apply the same front policy. Unknown tools stay denied."""
    event = event if isinstance(event, dict) else {}
    if backend == 'opencode':
        from . import front_opencode
        return front_opencode.hook(event, base, Path(runtime), account_switch)
    if backend == 'agy':
        call = event.get('toolCall') or {}
        name, args = call.get('name'), call.get('args') or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        args = args if isinstance(args, dict) else {}
        cwd = args.get('Cwd') or str(runtime)
        mapping = {'run_command': ('Bash', {'command': args.get('CommandLine', '')}),
                   'view_file': ('Read', {}), 'list_dir': ('Glob', {}), 'grep_search': ('Grep', {}),
                   'find_by_name': ('Glob', {}),
                   'write_to_file': ('Write', {'file_path': args.get('TargetFile', '')})}
        tool, inp = mapping.get(name, ('unknown', {}))
        ev = {'tool_name': tool, 'tool_input': inp, 'cwd': cwd}
        allowed = front.hook_decision(ev, base, account_switch)['decision'] == 'allow'
        if name == 'run_command' and (args.get('RequestedTerminalID') or args.get('RunPersistent')):
            allowed = False
        if name in ('command_status', 'wait', 'ask_question', 'finish'):
            allowed = not args.get('Input') and not args.get('SendInput')
        session = event.get('conversationId')
    else:
        name, args = event.get('tool_name'), event.get('tool_input') or {}
        args = args if isinstance(args, dict) else {}
        cwd = event.get('cwd') or str(base.parent)
        allowed = front.hook_decision(event, base, account_switch)['decision'] == 'allow'
        if name == 'apply_patch':
            allowed = _patch_allowed(args.get('command') or args.get('patch') or '', base, cwd)
        if name in ('update_plan', 'view_image'):
            allowed = True
        session = event.get('session_id')
    # A fresh per-launch marker proves at least one native PreToolUse invocation reached this guard.
    if session:
        reported = event.get('modelName') if backend == 'agy' else event.get('model')
        (Path(runtime) / 'ready.json').write_text(json.dumps({'session': session, 'model': reported}), encoding='utf-8')
    reason = '' if allowed else 'batch-flow front: only bound bf commands, read-only tools and new answers/ files are allowed'
    if backend == 'agy':
        return {'decision': 'allow' if allowed else 'deny', 'reason': reason}
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow' if allowed else 'deny',
                                    'permissionDecisionReason': reason}}


class Notifications:
    """Log events/reports without writing into a child TUI's terminal."""
    def __init__(self, base, runtime, stream=None, interval=600):
        self.base, self.runtime = base, runtime
        self.stream = stream
        self.since = api.load(base).revision
        self.interval = interval
        self.next_report = time.monotonic() + interval
        self.stop = threading.Event()

    def emit(self, text):
        with (self.runtime / 'notifications.log').open('a', encoding='utf-8') as out:
            out.write(text)
        if self.stream is not None:
            self.stream.write(text)
            self.stream.flush()

    def tick(self, at=None):
        at = time.monotonic() if at is None else at
        lines, self.since = api.watch(self.base, self.since)
        if at >= self.next_report:
            lines.append(api.report_text(self.base))
            self.next_report = at + self.interval
        if lines:
            text = '\n[batch-flow]\n' + '\n'.join(lines) + '\n'
            self.emit(text)

    def run(self):
        while not self.stop.wait(1):
            try:
                self.tick()
            except (OSError, ValueError, api.BfError) as error:
                try:
                    self.emit('[batch-flow] notification error: %s\n' % error)
                except OSError:
                    pass  # A log failure must not corrupt the active TUI either.


def run(spec, base, bind_timeout=120):
    runtime = Path(spec['runtime'])
    monitor = Notifications(base, runtime)
    kwargs = {}
    if spec.get('backend') == 'opencode':
        from . import front_opencode
        kwargs['env'] = front_opencode.environment(spec)
    child = subprocess.Popen(spec['argv'], cwd=spec['cwd'], **kwargs)
    thread = threading.Thread(target=monitor.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + bind_timeout
    announced = False
    try:
        while child.poll() is None:
            if not announced and (runtime / 'ready.json').is_file():
                try:
                    reported = json.loads((runtime / 'ready.json').read_text(encoding='utf-8')).get('model')
                except (OSError, ValueError):
                    reported = None
                if reported:
                    monitor.emit('宿主报告模型：%s\n' % reported)
                announced = bool(reported)
            if not (runtime / 'ready.json').is_file() and time.monotonic() >= deadline:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                print('前端工具门禁未生效，已结束会话。Codex 检查 /hooks 信任；agy 检查工作区信任；OpenCode 检查 V2 插件加载。', file=sys.stderr)
                return 2
            monitor.stop.wait(0.2)
        if not (runtime / 'ready.json').is_file() and child.returncode == 0:
            print('前端未回传 hook 就绪标记，不能确认权限约束已加载。', file=sys.stderr)
            return 2
        return child.returncode
    except KeyboardInterrupt:
        child.terminate()
        child.wait()
        return 130
    finally:
        monitor.stop.set()
        thread.join(timeout=3)
