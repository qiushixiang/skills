"""Read-only CLI model metadata and deterministic name matching. Never starts a turn."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import selectors
import subprocess
import time


def fingerprint(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


def normalized(value):
    value = re.sub(r'\bv(?=\d)', '', value.casefold())
    return ''.join(c for c in value if c.isalnum())


def safe_metadata_command(args, profiles, records):
    """Shared host gate: bound profile, literal options, records-only catalogue paths."""
    allowed = ({'--profile', '--output'} if args[0] == 'models' else
               {'--profile', '--catalog', '--name', '--effort'})
    if len(args) % 2 != 1:
        return False
    options = {}
    for flag, value in zip(args[1::2], args[2::2]):
        if flag not in allowed or flag in options:
            return False
        options[flag] = value
    if options.get('--profile') not in profiles:
        return False
    if args[0] == 'resolve-model' and not all(options.get(k) for k in ['--catalog', '--name']):
        return False
    return all(Path(records).resolve() in Path(options[k]).resolve().parents
               for k in ['--catalog', '--output'] if k in options)


def entry(model, name, **extra):
    if not isinstance(model, str) or not model or not isinstance(name, str):
        raise ValueError('invalid model metadata')
    return dict(id=model, name=name, **extra)


def codex_models(binary):
    # JSON-RPC metadata only: no thread/start or turn/start, no prompt or authentication writes.
    proc = subprocess.Popen([binary, 'app-server'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    buffer = b''
    deadline = time.monotonic() + 30
    sequence = 0

    def send(value):
        proc.stdin.write((json.dumps(value) + '\n').encode()); proc.stdin.flush()

    def request(method, params):
        nonlocal sequence, buffer
        sequence += 1
        send(dict(id=sequence, method=method, params=params))
        while True:
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                message = json.loads(line)
                if message.get('id') == sequence:
                    if 'error' in message:
                        raise ValueError('model metadata RPC failed')
                    return message['result']
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ValueError('model metadata RPC timed out')
            import os
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                raise ValueError('model metadata RPC closed before response')
            buffer += chunk
            if len(buffer) > 8_000_000:
                raise ValueError('model metadata response too large')

    try:
        request('initialize', dict(clientInfo=dict(name='batch_flow_models', version='1.0')))
        send(dict(method='initialized', params={}))
        models, cursor = [], None
        for _ in range(100):
            page = request('model/list', dict(limit=100, cursor=cursor, includeHidden=False))
            models.extend(page['data'])
            cursor = page.get('nextCursor')
            if not cursor:
                return models
        raise ValueError('model catalogue pagination exceeded limit')
    finally:
        selector.close()
        proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
        proc.stdout.close()


def query(profile):
    backend, binary = profile['backend'], profile['binary']
    source, complete = '', True
    models = []

    def command(args):
        result = subprocess.run([binary] + args, capture_output=True, text=True, timeout=30)
        if result.returncode:
            # Do not echo arbitrary stderr/configuration/credentials into the catalogue.
            raise ValueError('model metadata command failed; backend=' + backend + ', exit=' + str(result.returncode))
        if len(result.stdout) > 8_000_000:
            raise ValueError('model metadata response too large')
        return result.stdout

    if backend == 'agy':
        source = 'models'
        for line in command(['models']).splitlines():
            parts = line.strip().split('\t', 1)
            if len(parts) != 2:
                continue
            model, name = parts
            variant = re.search(r'\s+\((Low|Medium|High|Xhigh|Max)\)$', name, re.I)
            models.append(entry(model, name, family=name[:variant.start()] if variant else name,
                                variant=variant.group(1).lower() if variant else None))
    elif backend == 'opencode':
        source = 'api model.list --standalone'
        data = json.loads(command(['api', 'model.list', '--standalone']))

        def visit(value):
            if isinstance(value, dict):
                if all(isinstance(value.get(k), str) for k in ['providerID', 'id', 'name']):
                    model = value.get('modelID', value['id'])
                    models.append(entry(value['providerID'] + '/' + model, value['name']))
                else:
                    for child in value.values():
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(data)
    elif backend == 'codex':
        source = 'app-server initialize; model/list'
        for value in codex_models(binary):
            models.append(entry(value['model'], value['displayName'],
                                efforts=[v['reasoningEffort'] for v in value.get('supportedReasoningEfforts', [])],
                                default_effort=value.get('defaultReasoningEffort')))
    elif backend == 'claude':
        source, complete = '--help model aliases (not a remote model catalogue)', False
        help_text = command(['--help'])
        block = re.search(r'--model\b(.*?)(?=\n\s+--|\Z)', help_text, re.S)
        if block:
            for alias in re.findall(r"['\"]([a-z][a-z0-9-]*)['\"]", block.group(1)):
                models.append(entry(alias, alias))
    else:
        raise ValueError('unsupported model catalogue backend')
    if not models:
        raise ValueError('no model metadata recognized; CLI contract may have changed')
    unique = {value['id']: value for value in models}
    return dict(format='batch-flow-models/v1', backend=backend, binary=binary,
                version=profile['version'], profile_sha256=fingerprint(profile),
                checked_at=dt.datetime.now(dt.timezone.utc).isoformat(), source=source,
                complete=complete, default_model=None, models=list(unique.values()),
                limitation='Metadata does not prove authentication, entitlement or successful inference; configured model default is unknown.')


def resolve(profile, catalog, name, effort='default'):
    from agent_cli import effort_override
    effort_override(profile['backend'], effort)
    if (catalog.get('format') != 'batch-flow-models/v1' or
            catalog.get('profile_sha256') != fingerprint(profile) or
            catalog.get('backend') != profile['backend'] or catalog.get('version') != profile['version']):
        raise ValueError('catalogue does not match this checked profile; query models again')
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(catalog['checked_at'])).total_seconds()
    if not 0 <= age <= 3600:
        raise ValueError('catalogue expired or timestamp is in the future; query models again')
    key = normalized(name)
    if not key:
        raise ValueError('model name is empty')
    # Exact IDs win over display names shared by providers. Family matching preserves variants.
    exact = [v for v in catalog['models'] if v['id'].casefold() == name.casefold()]
    candidates = exact or [v for v in catalog['models'] if key in {
        normalized(v['id']), normalized(v['name']), normalized(v.get('family', ''))}]
    result = dict(backend=profile['backend'], requested=name, effort=effort, candidates=candidates)
    if len(candidates) == 1:
        value = candidates[0]
        if effort != 'default' and 'efforts' in value and effort not in value['efforts']:
            raise ValueError('selected model does not advertise requested effort')
        result.update(status='resolved', config=dict(tool=profile['backend'], model=value['id'], effort=effort))
    else:
        result['status'] = 'needs_clarification' if candidates else 'not_found' if catalog['complete'] else 'unverified'
    return result
