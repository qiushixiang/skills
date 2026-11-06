#!/usr/bin/env python3
"""Maintenance-only registration of a newly available CLI in an explicitly bound launcher.

Run outside the restricted orchestrator, under user-authorized host maintenance.
Never writes a task ledger, changes a role/model, launches a model, or replaces a profile.
"""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
import subprocess
import uuid

import agent_cli
from flow import write_atomic


def register(store, runtime, backend, binary=None):
    store = Path(store).resolve(strict=True)
    runtime = Path(runtime).resolve(strict=True)
    if (runtime.parent.name not in {'.agy-launch', '.claude-launch'} or
            runtime.parent.parent != store.parent or
            re.fullmatch(r'[0-9a-f]{32}', runtime.name) is None):
        raise ValueError('runtime must be the explicit launcher directory belonging to this store')
    if backend not in agent_cli.REQUIRED:
        raise ValueError('unsupported backend: ' + backend)
    spec_path = runtime / 'spec.json'
    if spec_path.is_symlink():
        raise ValueError('launcher spec must not be a symlink')
    with (runtime / '.profiles.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = spec_path.read_bytes()
        spec = json.loads(before)
        if (spec.get('launch_id') != runtime.name or Path(spec['store']).resolve() != store or
                Path(spec.get('runtime', runtime)).resolve() != runtime):
            raise ValueError('launcher/store/launch identity mismatch')
        profiles = spec.get('profiles')
        if not isinstance(profiles, list):
            raise ValueError('launcher profiles must be an array')
        existing = []
        for name in profiles:
            path = Path(name)
            if path.is_symlink() or path.resolve().parent != runtime:
                raise ValueError('profile is outside the bound launcher or is a symlink')
            profile = agent_cli.read(path)
            if profile.get('backend') == backend:
                existing.append(path)
        if len(existing) > 1:
            raise ValueError('ambiguous registered backend; preserve evidence and diagnose')
        if existing:
            agent_cli.check_profile(existing[0])
            return dict(event='LAUNCHER_PROFILE_REGISTERED', changed=False, backend=backend,
                        profile=str(existing[0]), runtime=str(runtime))
        # Version/help probe only. A new immutable filename avoids adopting an
        # unregistered or hand-written profile already present in this runtime.
        profile_path = runtime / (backend + '-profile-' + uuid.uuid4().hex + '.json')
        agent_cli.probe(backend, profile_path, binary)
        agent_cli.check_profile(profile_path)
        if spec_path.read_bytes() != before:
            raise ValueError('launcher spec changed during probe; reread binding before retrying')
        spec['profiles'] = profiles + [str(profile_path)]
        registration = dict(event='LAUNCHER_PROFILE_REGISTERED', changed=True, backend=backend,
                            profile=str(profile_path), runtime=str(runtime), at=agent_cli.now(),
                            previous_spec_sha256=hashlib.sha256(before).hexdigest(),
                            validation='real version/help probe only; no model call or ledger change')
        spec.setdefault('profile_registrations', []).append(registration)
        write_atomic(spec_path, spec)
        return registration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--backend', choices=agent_cli.REQUIRED, required=True)
    parser.add_argument('--binary')
    args = parser.parse_args()
    # This script is intentionally absent from orchestrator tool allowlists.
    try:
        print(json.dumps(register(args.store, args.runtime, args.backend, args.binary), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps(dict(event='LAUNCHER_PROFILE_ERROR', error=str(error)), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
