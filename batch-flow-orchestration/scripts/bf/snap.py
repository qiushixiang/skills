"""Per-run change tracking (design 24.1, 24.2): dirty-file baseline at start, changed set at registration."""
import fnmatch
import hashlib
import os
import subprocess


def _git(repo, *args):
    run = subprocess.run(['git', '-C', repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return run.stdout if run.returncode == 0 else None


def _hash(path):
    try:
        if os.path.islink(path):
            return 'link:' + os.readlink(path)
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None  # deleted


def dirty(repo):
    """{'head', 'files': {relative path: sha256 or None when deleted}} for every dirty file, untracked included."""
    head = _git(repo, 'rev-parse', 'HEAD')
    out = _git(repo, 'status', '--porcelain', '-z', '--untracked-files=all')
    files, tokens, i = {}, (out or b'').decode('utf-8', 'replace').split('\0'), 0
    while i < len(tokens):
        t = tokens[i]
        i += 1
        if len(t) < 4:
            continue
        files[t[3:]] = _hash(os.path.join(repo, t[3:]))
        if t[0] in 'RC' or t[1] in 'RC':  # rename/copy entries carry the old path as the next token
            if i < len(tokens) and tokens[i]:
                files[tokens[i]] = _hash(os.path.join(repo, tokens[i]))
            i += 1
    return {'head': head.decode().strip() if head else None, 'files': files}


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
        if before['head'] and now['head'] and before['head'] != now['head']:
            names = _git(repo, 'diff', '--name-only', '-z', before['head'], now['head'])
            paths |= set(n for n in (names or b'').decode('utf-8', 'replace').split('\0') if n)
        if paths:
            out[repo] = sorted(paths)
    return out


def protected_hits(plan, order, changes):
    allow = list(order['input'].get('allow_protected') or [])
    hits = []
    for repo, rels in changes.items():
        for rel in rels:
            if any(fnmatch.fnmatch(rel, g) for g in plan.get('protected') or []) and not any(fnmatch.fnmatch(rel, g) for g in allow):
                hits.append(rel)
    return hits
