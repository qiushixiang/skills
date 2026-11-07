"""Evidence component (design 23.1): `bf record` / `bf snapshot` write the only files the gate accepts as evidence.
Semantics ported from v1 `flow.py record/snapshot`. They never take the store lock or change orders.json.
Also `doctor`: references from a task directory to skill scripts that no longer exist."""
import base64
import json
import os
import re
import subprocess
from pathlib import Path

from .store import BfError, _atomic_write, need, now

RECORD_KEYS = {'command': list, 'cwd': str, 'started_at': str, 'ended_at': str, 'exit_code': int, 'stdout': str, 'stderr': str}
SNAPSHOT_KEYS = {'repository': str, 'base_commit': str, 'paths': list, 'diff': str, 'untracked_base64': dict}
SKILL = Path(__file__).resolve().parents[2]


def is_evidence(path):
    """A cited file counts only if it is valid JSON in the record or the snapshot schema."""
    try:
        data = json.loads(Path(str(path)).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and any(
        all(isinstance(data.get(k), t) and not (t is int and isinstance(data.get(k), bool)) for k, t in schema.items())
        for schema in (RECORD_KEYS, SNAPSHOT_KEYS))


def _save(base, value):
    path = Path(str(base)) / 'records' / (os.urandom(12).hex() + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=1) + '\n').encode('utf-8'))
    return str(path)


def record(base, argv, cwd=None):
    """Run argv as a direct child, write the record, return (result dict, exit code to pass through)."""
    need(argv, 'record requires -- COMMAND [ARG ...]')
    cwd = Path(cwd or os.getcwd()).resolve()
    started = now()
    run = subprocess.run(list(argv), cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True, errors='replace')
    path = _save(base, {'command': list(argv), 'cwd': str(cwd), 'started_at': started, 'ended_at': now(),
                        'exit_code': run.returncode, 'stdout': run.stdout, 'stderr': run.stderr})
    return {'ok': run.returncode == 0, 'evidence': path, 'exit_code': run.returncode}, (run.returncode if 0 <= run.returncode <= 125 else 1)


def snapshot(base, repo, paths):
    need(repo is not None and paths, 'snapshot requires --repo and explicit --paths')
    repo = Path(str(repo)).resolve()
    try:
        selected = [str((repo / n).resolve().relative_to(repo)) for n in paths]
    except ValueError:
        raise BfError('snapshot path outside the repository')

    def git(*parts):
        run = subprocess.run(['git', '--literal-pathspecs', '-C', str(repo)] + list(parts), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        need(run.returncode == 0, run.stderr.decode(errors='replace'))
        return run.stdout
    head = git('rev-parse', '--verify', 'HEAD').decode().strip()
    diff = git('diff', '--binary', 'HEAD', '--', *selected).decode(errors='replace')
    untracked = {}
    for raw in git('ls-files', '-z', '--others', '--exclude-standard', '--', *selected).split(b'\0'):
        if raw:
            name = os.fsdecode(raw)
            need(not (repo / name).is_symlink(), 'untracked symlink: use an explicit export tool')
            untracked[name] = base64.b64encode((repo / name).resolve().read_bytes()).decode('ascii')
    return {'ok': True, 'evidence': _save(base, {'repository': str(repo), 'base_commit': head, 'paths': selected,
                                                   'diff': diff, 'untracked_base64': untracked})}


def doctor(base):
    """Scan the task directory (not .batch-flow, logs, .git) for references to skill scripts that do not exist."""
    task = Path(str(base)).resolve().parent
    missing, pattern = [], re.compile(r'batch-flow-orchestration/scripts/([\w./-]+)')
    for root, dirs, files in os.walk(str(task)):
        dirs[:] = [d for d in dirs if d not in ('.batch-flow', 'logs', '.git', 'node_modules')]
        for name in files:
            path = Path(root) / name
            try:
                if path.stat().st_size > 2000000:
                    continue
                lines = path.read_text(encoding='utf-8').splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for n, line in enumerate(lines, 1):
                for ref in pattern.findall(line):
                    ref = ref.rstrip('.')
                    if not (SKILL / 'scripts' / ref).exists():
                        missing.append({'file': str(path), 'line': n, 'ref': 'scripts/' + ref})
    return {'missing': missing}
