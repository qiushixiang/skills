#!/usr/bin/env python3
"""Linux CLI adapter. No model defaults, ledger writes, retries or daemonization."""
import argparse
import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


REQUIRED = {
    'agy': ['--mode', '--model', '--effort', '--output-format', '--conversation', '--settings'],
    'codex': ['--sandbox', '--model', '--config', '--json', '--add-dir', '--skip-git-repo-check'],
    'claude': ['--permission-mode', '--permission-prompts', '--disallowedTools', '--model', '--effort', '--resume', '--output-format'],
}
EFFORTS = {
    'agy': {'low', 'medium', 'high', 'xhigh', 'max'},
    'codex': {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'},
    'claude': {'low', 'medium', 'high', 'xhigh', 'max'},
}


def effort_override(backend, effort):
    if effort == 'default':
        return None  # Explicitly inherit CLI/model configuration; never send this sentinel.
    if effort not in EFFORTS[backend]:
        raise ValueError('unsupported '+backend+' effort: '+str(effort))
    return effort


def uuid_session(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def version(binary):
    result = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=15, check=True)
    return result.stdout.strip()


def probe(backend, output, binary=None):
    if sys.platform != 'linux':
        raise ValueError('this adapter is validated for Linux only')
    binary = shutil.which(binary or backend)
    if not binary:
        raise ValueError('CLI executable not found')
    binary = str(Path(binary).resolve())
    cli_version = version(binary)
    prefix = [binary, 'exec'] if backend == 'codex' else [binary]
    help_result = subprocess.run(prefix + ['--help'], capture_output=True, text=True, timeout=15, check=True)
    help_text = help_result.stdout + help_result.stderr
    if any(flag not in help_text for flag in REQUIRED[backend]):
        raise ValueError('unsupported CLI flags; inspect this version before adapting')
    resume_help = ''
    if backend == 'codex':
        result = subprocess.run([binary, 'exec', 'resume', '--help'], capture_output=True, text=True, timeout=15, check=True)
        resume_help = result.stdout + result.stderr
        if '--json' not in resume_help or 'SESSION_ID' not in resume_help:
            raise ValueError('unsupported Codex resume interface')
    profile = dict(format='batch-flow-cli/v1', backend=backend, binary=binary,
                   version=cli_version, platform=sys.platform, checked_at=now(),
                   help=help_text, resume_help=resume_help,
                   validation='local version/help only; model execution not tested')
    save(output, profile)
    return dict(event='CLI_PROBED', profile=str(Path(output).resolve()), backend=backend, version=cli_version)


def build_command(profile, args, prompt):
    binary = profile['binary']; backend = profile['backend']
    effort = effort_override(backend, args.effort)
    if backend == 'agy':
        # Read roles must rerun builds/tests: plan mode only plans writing commands (verified
        # 1.5.0), so read access is accept-edits plus a session deny on every file write/edit.
        argv = [binary, '--mode', 'accept-edits',
                '--model', args.model, '--output-format', 'json']
        if effort is not None:
            argv += ['--effort', effort]
        if args.conversation:
            argv += ['--conversation', args.conversation]
        for directory in args.add_dir:
            argv += ['--add-dir', directory]
        settings = json.loads(settings_json(args.settings)) if args.settings else {}
        if args.access == 'read':
            permissions = settings.setdefault('permissions', {})
            if not isinstance(permissions, dict):
                raise ValueError('settings.permissions must be a JSON object')
            deny = permissions.setdefault('deny', [])
            if 'write_file(*)' not in deny:
                deny.append('write_file(*)')
        if settings:
            argv += ['--settings', json.dumps(settings, ensure_ascii=False)]
        return argv + ['-p', prompt], None
    if backend == 'claude':
        # Read roles must still rerun builds/tests: plan mode refuses writing shell commands
        # (verified 2.1.318), so use default mode and remove only the file-edit tools.
        argv = [binary, '-p', '--model', args.model,
                '--permission-mode', 'default' if args.access == 'read' else 'acceptEdits',
                '--permission-prompts', 'none', '--output-format', 'json']
        if args.access == 'read':
            argv += ['--disallowedTools', 'Edit Write NotebookEdit']
        if effort is not None:
            argv += ['--effort', effort]
        argv += ['--resume', args.conversation] if args.conversation else ['--session-id', str(uuid.uuid4())]
        for directory in args.add_dir:
            argv += ['--add-dir', directory]
        if args.settings:
            argv += ['--settings', settings_json(args.settings)]
        return argv, prompt
    sandbox = 'read-only' if args.access == 'read' else 'workspace-write'
    # Options precede resume: resume itself does not accept -s or -C.
    argv = [binary, 'exec', '-C', args.cwd, '-s', sandbox,
            '-m', args.model, '--json', '--skip-git-repo-check']
    if effort is not None:
        argv += ['-c', 'model_reasoning_effort=' + json.dumps(effort)]
    for directory in args.add_dir:
        argv += ['--add-dir', directory]
    if args.settings:
        raise ValueError('--settings is for AGY/Claude; Codex sandbox and effort are explicit')
    argv += ['resume', args.conversation, '-'] if args.conversation else ['-']
    return argv, prompt


def validate_call(profile, args):
    """Reject invalid contracts before spawning a CLI or creating a run directory."""
    backend = profile['backend']
    if not args.model.strip() or not args.effort.strip():
        raise ValueError('model/effort must be explicit and non-empty')
    effort_override(backend, args.effort)
    if backend == 'codex' and args.model == 'gpt-6.1-sol' and args.effort in {'none', 'minimal'}:
        raise ValueError('gpt-6.1-sol does not support none/minimal effort')
    args.cwd = str(Path(args.cwd).resolve(strict=True))
    if not Path(args.cwd).is_dir():
        raise ValueError('cwd must be a directory')
    args.add_dir = [str(Path(p).resolve(strict=True)) for p in args.add_dir]
    source = getattr(args, 'resume_from', None)
    if source and not args.conversation:
        raise ValueError('--resume-from requires --conversation')
    if args.conversation:
        if not args.conversation.strip() or args.conversation.startswith('pending:'):
            raise ValueError('resume requires an actual conversation ID')
        if not uuid_session(args.conversation):
            raise ValueError(backend+' resume requires a native UUID, not a logical context name; record a fresh context with replacement reason')
        if not source:
            raise ValueError(backend+' resume requires --resume-from <previous successful adapter run directory>')
        if source:
            prior = read(Path(source)/'request.json')
            result = collect(source)
            if not result.get('usable') or result.get('conversation_id') != args.conversation or prior['backend'] != backend:
                raise ValueError('resume evidence does not prove a successful matching backend/session')
            if prior['cwd'] != args.cwd:
                raise ValueError('resume cwd differs from its recorded execution environment')
    return dict(event='CLI_CALL_CHECKED', backend=backend, model=args.model,
                effort=args.effort, effort_override=effort_override(backend, args.effort),
                cwd=args.cwd, conversation_id=args.conversation)


def check_profile(profile_path):
    """Check executable visibility in the caller's actual environment; no model call."""
    profile = read(profile_path)
    if profile.get('format') != 'batch-flow-cli/v1' or profile.get('platform') != sys.platform or sys.platform != 'linux':
        raise ValueError('invalid Linux CLI profile')
    try:
        actual = version(profile['binary'])
    except FileNotFoundError as error:
        raise ValueError('CLI cannot start in this execution environment: ' + profile['binary'] +
                         '; check sandbox mounts and executable interpreter; this does not prove a missing host installation') from error
    if actual != profile['version']:
        raise ValueError('CLI version changed; probe a new profile before dispatch')
    return dict(event='CLI_CHECKED', backend=profile['backend'], binary=profile['binary'],
                version=actual, validation='version only in current execution environment; no authentication/network/model check')


def settings_json(path):
    settings = read(path)
    if not isinstance(settings, dict):
        raise ValueError('settings must be a JSON object')
    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {'dangerouslySkipPermissions', 'allowDangerouslySkipPermissions'} and child:
                    raise ValueError('permission bypass settings are unsupported')
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
        elif isinstance(value, str) and value in {'always-proceed', 'bypassPermissions', 'danger-full-access'}:
            raise ValueError('permission bypass settings are unsupported')
    check(settings)
    return json.dumps(settings, ensure_ascii=False)


def inspect_result(backend, stdout, stderr, returncode, expected=None):
    problems = []; cid = None; response = None
    try:
        if backend == 'codex':
            events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            if not all(isinstance(event, dict) for event in events):
                raise ValueError('expected event objects')
            cid = next((e.get('thread_id') for e in events if e.get('type') == 'thread.started'), None)
            if not uuid_session(cid):
                problems.append('Codex did not return a native session UUID')
            messages = [e['item'].get('text') for e in events if e.get('type') == 'item.completed' and isinstance(e.get('item'), dict) and e['item'].get('type') == 'agent_message']
            response = messages[-1] if messages else None
            if not any(e.get('type') == 'turn.completed' for e in events):
                problems.append('missing turn.completed')
            if any(e.get('type') in {'error', 'turn.failed'} for e in events):
                problems.append('CLI error event')
        else:
            result = json.loads(stdout)
            if not isinstance(result, dict):
                raise ValueError('expected result object')
            cid = result.get('conversation_id' if backend == 'agy' else 'session_id')
            response = result.get('response' if backend == 'agy' else 'result')
            if backend == 'agy' and (result.get('status') != 'SUCCESS' or result.get('error')):
                problems.append('AGY status/error')
            if backend == 'claude' and (result.get('is_error') is not False or result.get('subtype') != 'success'):
                problems.append('Claude subtype/is_error')
            if result.get('denied_actions') or result.get('permission_denials'):
                problems.append('permission denials reported')
    except (ValueError, KeyError, TypeError):
        problems.append('invalid output schema')
    if returncode != 0:
        problems.append('nonzero CLI exit')
    if not isinstance(cid, str) or not cid.strip() or cid.startswith('pending:'):
        problems.append('missing actual conversation ID')
    elif not uuid_session(cid):
        problems.append('CLI did not return a native session UUID')
    if expected and cid != expected:
        problems.append('resume conversation ID mismatch')
    if not isinstance(response, str) or not response.strip():
        problems.append('empty response')
    if any(token in stderr.lower() for token in ['auto-denied', 'permission denied', 'approval required']):
        problems.append('permission failure in stderr')
    return dict(event='CLI_COLLECTED', usable=not problems, conversation_id=cid,
                response=response, problems=problems,
                limit='Transport checks only; task correctness and actual model require role evidence/review.')


def run(profile_path, args):
    profile = read(profile_path)
    validate_call(profile, args)
    check_profile(profile_path)
    if not args.model.strip() or not args.effort.strip() or not args.task.strip():
        raise ValueError('task/model/effort must be explicit and non-empty')
    prompt = Path(args.prompt_file).read_text(encoding='utf-8')
    if not prompt.strip():
        raise ValueError('empty prompt')
    argv, input_text = build_command(profile, args, prompt)
    output = Path(args.run_dir).resolve()
    output.mkdir(mode=0o700)  # Exclusive: never overwrite or rerun a previous call.
    request = dict(task=args.task, backend=profile['backend'], version=profile['version'],
                   cwd=args.cwd, model=args.model, effort=args.effort, access=args.access,
                   conversation=args.conversation, argv=argv, stdin=input_text, started_at=now())
    request.update(effort_override=effort_override(profile['backend'], args.effort),
                   resume_from=getattr(args, 'resume_from', None))
    save(output / 'request.json', request)
    with (output / 'stdout').open('x') as stdout, (output / 'stderr').open('x') as stderr:
        proc = subprocess.Popen(argv, cwd=args.cwd, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, text=True)
        receipt = dict(event='CLI_STARTED', backend=profile['backend'], task=args.task,
                       pid=proc.pid, handle='pid:' + str(proc.pid), started_at=now(), run_dir=str(output))
        save(output / 'receipt.json', receipt)
        print(json.dumps(receipt, ensure_ascii=False), flush=True)
        # Keep the supervisor in a host-managed background job, never detach it.
        try:
            proc.communicate(input=input_text)
        except KeyboardInterrupt:
            # Do not retry or kill possibly-writing descendants automatically.
            print(json.dumps(dict(event='CLI_INTERRUPTED', pid=proc.pid, run_dir=str(output),
                                  instruction='Inspect original process and outputs; do not redispatch.')), flush=True)
            return 130
    save(output / 'exit.json', dict(returncode=proc.returncode, ended_at=now()))
    report = collect(output)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['usable'] else 2


def collect(output):
    output = Path(output)
    if not (output / 'exit.json').exists():
        return dict(event='CLI_INCOMPLETE', run_dir=str(output), usable=False,
                    instruction='Inspect host handle and raw output; absence of exit.json does not prove process liveness. Do not redispatch.')
    request = read(output / 'request.json'); end = read(output / 'exit.json')
    result = inspect_result(request['backend'], (output / 'stdout').read_text(),
                            (output / 'stderr').read_text(), end['returncode'], request['conversation'])
    result.update(task=request['task'], run_dir=str(output), returncode=end['returncode'],
                  started_at=request['started_at'], ended_at=end['ended_at'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('probe')
    check.add_argument('--backend', required=True, choices=REQUIRED)
    check.add_argument('--binary')
    check.add_argument('--profile', required=True)
    verify = commands.add_parser('check', help='version check in the actual dispatch environment; never calls a model')
    verify.add_argument('--profile', required=True)
    for flag in ['model', 'effort', 'cwd', 'conversation', 'resume-from']:
        verify.add_argument('--'+flag)
    verify.add_argument('--access', choices=['read', 'write'], default='read')
    verify.add_argument('--add-dir', action='append', default=[])
    verify.set_defaults(settings=None)
    launch = commands.add_parser('run')
    for flag in ['profile', 'task', 'cwd', 'model', 'effort', 'prompt-file', 'run-dir']:
        launch.add_argument('--' + flag, required=True)
    launch.add_argument('--access', choices=['read', 'write'], required=True)
    launch.add_argument('--conversation')
    launch.add_argument('--resume-from', help='previous successful adapter run proving the actual session ID; required for every backend resume')
    launch.add_argument('--add-dir', action='append', default=[])
    launch.add_argument('--settings', help='AGY/Claude session settings JSON file; authorized policy only')
    finish = commands.add_parser('collect')
    finish.add_argument('--run-dir', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'run':
            return run(args.profile, args)
        if args.command == 'check' and any(getattr(args, key) is not None for key in ['model','effort','cwd','conversation','resume_from']):
            if not all(getattr(args, key) for key in ['model','effort','cwd']):
                raise ValueError('call preflight requires --model, --effort and --cwd together')
            result = validate_call(read(args.profile), args)
            check_profile(args.profile)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0
        result = (probe(args.backend, args.profile, args.binary) if args.command == 'probe'
                  else check_profile(args.profile) if args.command == 'check' else collect(args.run_dir))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if args.command in {'probe', 'check'} or result.get('usable') else 2
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps(dict(event='CLI_ADAPTER_ERROR', error=str(error)), ensure_ascii=False), flush=True)
        return 2


if __name__ == '__main__':
    sys.exit(main())
