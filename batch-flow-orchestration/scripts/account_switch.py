#!/usr/bin/env python3
"""User-configured account switch command the orchestrator may run (for example a CLI quota account swap).

The launcher receives the command path from the user (--account-switch); the orchestrator guard, the AGY
gate and the role audit all accept exactly that absolute path with a narrow argument set. When to run it
is a user instruction for the session, not a skill rule.
"""
import os
from pathlib import Path
import re

ALLOWED_FLAGS = {'-k', '-n'}
TARGET = re.compile(r'[^@\s/]+@[^@\s/]+')


def configured(path):
    """Validate a launcher --account-switch value; return its resolved absolute path."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError('--account-switch needs an absolute path: %s' % path)
    p = p.resolve()
    if not p.is_file() or not os.access(p, os.X_OK):
        raise ValueError('--account-switch is not an executable file: %s' % p)
    return str(p)


def allowed(argv, paths):
    """True when argv is <configured absolute path> [-k] [-n] [one target account]."""
    if not argv or not paths:
        return False
    head = Path(argv[0])
    if not head.is_absolute() or head.resolve() not in {Path(p).resolve() for p in paths}:
        return False
    args = argv[1:]
    targets = [a for a in args if not a.startswith('-')]
    flags = [a for a in args if a.startswith('-')]
    return (all(f in ALLOWED_FLAGS for f in flags) and len(flags) == len(set(flags))
            and len(targets) <= 1 and all(TARGET.fullmatch(t) for t in targets))
