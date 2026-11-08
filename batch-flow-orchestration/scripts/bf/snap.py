"""Per-run change tracking (design 24.1, 24.2): dirty-file baseline at start, changed set at registration."""
import hashlib
import os
import subprocess
from .store import need


def _git(repo, *args):
    run = subprocess.run(['git', '-C', repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    need(run.returncode == 0, 'Git observation failed in %s: %s' % (repo, run.stderr.decode(errors='replace')))
    return run.stdout


def _hash(path):
    try:
        if os.path.islink(path):
            return hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        if os.path.isdir(path):
            if os.path.exists(os.path.join(path, '.git')):
                return 'gitlink:' + _git(path, 'rev-parse', 'HEAD').decode().strip()
            return 'directory'
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None  # deleted; other I/O failures must be reported as observation failures


def dirty(repo):
    """{'head', 'files': {relative path: sha256 or None when deleted}} for every dirty file, untracked included."""
    head = _git(repo, 'rev-parse', 'HEAD')
    out = _git(repo, 'status', '--porcelain', '-z', '--untracked-files=all')
    files, modes, tokens, i = {}, {}, (out or b'').decode('utf-8', 'replace').split('\0'), 0
    while i < len(tokens):
        t = tokens[i]
        i += 1
        if len(t) < 4:
            continue
        files[t[3:]] = _hash(os.path.join(repo, t[3:]))
        modes[t[3:]] = file_state(os.path.join(repo, t[3:]))['mode']
        if t[0] in 'RC' or t[1] in 'RC':  # rename/copy entries carry the old path as the next token
            if i < len(tokens) and tokens[i]:
                files[tokens[i]] = _hash(os.path.join(repo, tokens[i]))
                modes[tokens[i]] = file_state(os.path.join(repo, tokens[i]))['mode']
            i += 1
    return {'head': head.decode().strip() if head else None, 'files': files, 'modes': modes}


def baseline(plan):
    return dict((repo, dirty(repo)) for repo in plan.get('repos') or [])


def changed(plan, base):
    """{repo: sorted relative paths} changed since the baseline: dirty now with another hash or newly dirty, plus files
    that were dirty at the baseline and are clean now, plus files of commits made since."""
    out = {}
    for repo, before in (base or {}).items():
        now = dirty(repo)
        paths = set(p for p, h in now['files'].items() if p not in before['files'] or before['files'][p] != h)
        paths |= set(p for p in before['files'] if p not in now['files'])
        paths |= set(p for p, mode in now['modes'].items() if p in before.get('modes', {}) and before['modes'][p] != mode)
        if before['head'] and now['head'] and before['head'] != now['head']:
            names = _git(repo, 'diff', '--name-only', '-z', before['head'], now['head'])
            paths |= set(n for n in (names or b'').decode('utf-8', 'replace').split('\0') if n)
        if paths:
            out[repo] = sorted(paths)
    return out


def file_state(path):
    """Content identity, Git-relevant mode and type; missing paths are explicit observations."""
    try:
        st = os.lstat(path)
        if os.path.isdir(path) and not os.path.islink(path):
            identity = _hash(path)
            return {'type': 'gitlink' if identity.startswith('gitlink:') else 'directory',
                    'mode': '160000' if identity.startswith('gitlink:') else '040000',
                    'sha256': None, 'git_head': identity[8:] if identity.startswith('gitlink:') else None}
        kind = 'symlink' if os.path.islink(path) else 'file' if os.path.isfile(path) else 'other'
        mode = '120000' if kind == 'symlink' else '100755' if st.st_mode & 0o111 else '100644'
        return {'type': kind, 'mode': mode, 'sha256': _hash(path)}
    except FileNotFoundError:
        return {'type': 'missing', 'mode': None, 'sha256': None}
