"""Task environment: backend-neutral requirements, the adapter capability table, resolution and
registration smoke receipts (design: authorization-conveyance-and-env-split.md, A3).

The fixed part of every CLI call (output format, resume, prompt transport, default sandbox) lives in
agent_cli.py. A plan only states what its work needs (``env.requirements``); this module maps each need
onto every role backend that will execute it, refuses what a backend cannot provide, and binds the result
to smoke receipts produced at registration time.  The driver only compares hashes before dispatch.
"""
import functools
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import model
from .store import BfError, need, now

ENV_KEYS = ('requirements', 'blocked_output', 'guide')
COMMON = ('id', 'kind', 'reason', 'applies')
KINDS = {
    'host_parent': (),                                  # CLI parent must run on the host (NoNewPrivs=0)
    'extra_dirs': ('dirs',),                            # extra writable directories
    'host_entry': ('entries', 'preflight', 'smoke'),    # controlled entries run outside any CLI sandbox
    'sandbox_escalation': ('mode',),                    # how sandboxed calls may escalate
}
OPTIONAL = {'host_entry': ('preflight',)}
ESCALATION_MODES = ('auto_review',)
ACCESS = ('read', 'write')
BACKENDS = ('codex', 'claude', 'opencode', 'agy')
MAPPED, NATIVE, UNSUPPORTED = 'mapped', 'native', 'unsupported'

# Capability table. Changing a cell needs real acceptance evidence (scripts/test_agent_cli_live.py).
# native: the backend applies no OS isolation, so the need is met without flags; the card says so.
TABLE = {
    'host_parent': {'codex': MAPPED, 'claude': MAPPED, 'opencode': MAPPED, 'agy': MAPPED},
    'extra_dirs': {'codex': MAPPED, 'claude': MAPPED, 'agy': MAPPED, 'opencode': NATIVE},
    'host_entry': {'codex': MAPPED, 'opencode': NATIVE, 'claude': UNSUPPORTED, 'agy': UNSUPPORTED},
    'sandbox_escalation': {'codex': MAPPED, 'opencode': NATIVE, 'claude': UNSUPPORTED, 'agy': UNSUPPORTED},
}
NOTES = {
    ('extra_dirs', 'opencode'): 'OpenCode 无 OS 隔离：额外目录天然可访问，不传参数',
    ('host_entry', 'opencode'): 'OpenCode 无 OS 隔离：受控入口直接在宿主执行',
    ('sandbox_escalation', 'opencode'): 'OpenCode 无 OS 沙箱：无需提权',
    ('host_entry', 'codex'): 'Codex 沙箱内：受控入口须命中宿主 rules 的 allow 规则或经自动审阅提权',
    ('host_entry', 'claude'): 'Claude 无提示权限模式下的宿主入口未经真实验证',
    ('host_entry', 'agy'): 'AGY 宿主入口未经真实验证',
    ('sandbox_escalation', 'claude'): 'Claude 不提供等价的自动审阅提权',
    ('sandbox_escalation', 'agy'): 'AGY 自动审阅提权未经真实验证',
}
# Offline test suites set this to model a task whose registration smoke already ran. The CLI never sets it;
# dedicated environment tests keep it False and exercise real receipts.
ASSUME_SMOKE_FOR_TESTS = False
SANDBOXED = ('codex',)          # these backends need a real model call to prove the environment
SMOKE_TIMEOUT = 900
LEGACY_HINT = ('计划 env 仍是按后端分节的旧格式；先运行 bf migrate-env 转换，补写需求的 smoke，'
               '再 env-resolve --smoke 并经 revise 登记')


# ---- structure -------------------------------------------------------------------------------------

def is_legacy(env):
    return isinstance(env, dict) and any(key in model.CLI_TOOLS or key in BACKENDS for key in env)


def normalize(plan):
    """Validate plan['env'] in place: requirements only, no backend call parameters."""
    env = plan.get('env')
    need(isinstance(env, dict), 'plan.env is required: {"requirements": [...]}; write [] when the task needs nothing')
    need(not is_legacy(env), LEGACY_HINT)
    unknown = sorted(set(env) - set(ENV_KEYS))
    need(not unknown, 'plan.env accepts only %s; backend call parameters belong to the adapter: %s'
         % ('/'.join(ENV_KEYS), ', '.join(unknown)))
    reqs = env.get('requirements')
    need(isinstance(reqs, list), 'plan.env.requirements is required (a list; [] when the task needs nothing)')
    for key in ('blocked_output', 'guide'):
        need(env.get(key) is None or isinstance(env[key], str), 'plan.env.%s must be a string' % key)
    seen = set()
    deliverables = set(plan.get('deliverables', {}))
    for req in reqs:
        need(isinstance(req, dict), 'env requirement must be an object')
        rid = req.get('id')
        need(isinstance(rid, str) and rid.strip() and rid not in seen, 'env requirement id missing or duplicated: %r' % rid)
        seen.add(rid)
        kind = req.get('kind')
        need(kind in KINDS, 'env requirement %s: unknown kind %r (one of %s)' % (rid, kind, ', '.join(KINDS)))
        extra = sorted(set(req) - set(COMMON) - set(KINDS[kind]))
        need(not extra, 'env requirement %s: unsupported fields %s' % (rid, ', '.join(extra)))
        need(isinstance(req.get('reason'), str) and req['reason'].strip(), 'env requirement %s: reason required' % rid)
        applies = req.get('applies')
        need(isinstance(applies, dict) and set(applies) == {'deliverables', 'access'},
             'env requirement %s: applies needs deliverables and access' % rid)
        dels = applies['deliverables']
        need(dels == '*' or (isinstance(dels, list) and dels and all(d in deliverables for d in dels)),
             'env requirement %s: applies.deliverables must be "*" or known deliverables' % rid)
        acc = applies['access']
        need(isinstance(acc, list) and acc and all(a in ACCESS for a in acc) and len(set(acc)) == len(acc),
             'env requirement %s: applies.access must list read and/or write' % rid)
        for field in KINDS[kind]:
            if field in OPTIONAL.get(kind, ()) and field not in req:
                continue
            need(field in req, 'env requirement %s: %s required' % (rid, field))
        if kind == 'extra_dirs':
            need(_abs_list(req['dirs']), 'env requirement %s: dirs must be absolute paths' % rid)
        elif kind == 'host_entry':
            need(_abs_list(req['entries']), 'env requirement %s: entries must be absolute paths' % rid)
            need(req.get('preflight') is None or isinstance(req['preflight'], str),
                 'env requirement %s: preflight must be a command string' % rid)
            smoke = req['smoke']
            need(isinstance(smoke, list) and smoke and all(isinstance(x, str) and x for x in smoke)
                 and smoke[0] in req['entries'],
                 'env requirement %s: smoke must be an argv list starting with one of its entries '
                 '(a side-effect-free self check that takes no shared lock and touches no device)' % rid)
        elif kind == 'sandbox_escalation':
            need(req['mode'] in ESCALATION_MODES, 'env requirement %s: mode must be one of %s' % (rid, ', '.join(ESCALATION_MODES)))
    return env


def _abs_list(value):
    return isinstance(value, list) and value and all(isinstance(x, str) and os.path.isabs(x) and x != '/' for x in value)


def requirements(plan):
    env = plan.get('env') or {}
    return [] if is_legacy(env) else list(env.get('requirements') or [])


def entries(plan):
    """Controlled entries of the plan (legacy per-backend env included, read-only use)."""
    env = plan.get('env') or {}
    if is_legacy(env):
        return [e for section in env.values() if isinstance(section, dict) for e in section.get('entry', [])]
    return [e for req in requirements(plan) if req['kind'] == 'host_entry' for e in req['entries']]


def text(plan, key):
    env = plan.get('env') or {}
    if is_legacy(env):
        values = [section.get(key) for section in env.values() if isinstance(section, dict) and section.get(key)]
        return values[0] if values else None
    return env.get(key)


# ---- resolution -------------------------------------------------------------------------------------

def applicable(plan, access, target):
    """Requirements that apply to one call: its access and the deliverables behind its target."""
    dels = set(model.deliverables_of(plan, target))
    out = []
    for req in requirements(plan):
        applies = req['applies']
        if access not in applies['access']:
            continue
        if applies['deliverables'] == '*' or dels & set(applies['deliverables']):
            out.append(req)
    return out


def access_for(plan, order_type):
    """The one access rule shared by the driver, cards, registration and dispatch checks: reviews read
    unless the plan declares repos (reviewers then rerun builds and verification in a writable sandbox)."""
    return 'read' if order_type == 'review' and not plan.get('repos') else 'write'


STAGE_OF = {'executor': 'primary', 'backup_executor': 'backup', 'lead': 'lead'}
COORDINATION = ('decide', 'triage')


def _implements(plan, role, deliverable):
    """Executors may receive any implementation or repair (ladders, grants and retries move work between
    them), so they are always in scope. The lead implements only where it has rounds; granting lead rounds
    re-registers the environment (adjust.grant and the lead's own grant action)."""
    if role != 'lead':
        return True
    from . import policy
    return (policy.rounds(plan, deliverable, 'lead') or 0) > 0


def _scopes(plan):
    """(role, access, target, work) for every call a role can receive. work=False marks lead coordination
    (decide/triage), which gets only the needs its backend supports; implementation and review get all."""
    roles = plan['roles']
    out = []
    for role in ('executor', 'backup_executor', 'lead'):
        if role in roles:
            out += [(role, 'write', d, True) for d in plan['deliverables'] if _implements(plan, role, d)]
    if 'lead' in roles:
        targets = list(plan['deliverables']) + list(plan['review_groups']) + [None]   # decide on a review: group
        out += [('lead', 'write', t, False) for t in targets]
    for group, spec in plan['review_groups'].items():
        role = spec.get('reviewer', 'reviewer')
        if role in roles:
            out.append((role, access_for(plan, 'review'), group, True))
    return out


def _call_requirements(plan, backend, access, target, work):
    reqs = applicable(plan, access, target)
    if not work:
        reqs = [r for r in reqs if TABLE[r['kind']].get(backend, UNSUPPORTED) != UNSUPPORTED]
    return reqs


def profile_version(base, backend):
    try:
        return json.loads((Path(str(base)) / 'profiles' / (backend + '.json')).read_text(encoding='utf-8')).get('version')
    except (OSError, ValueError):
        return None


def rules_digest():
    """SHA256 over the Codex rules the host actually loads ($CODEX_HOME/rules/*.rules)."""
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'rules'
    h = hashlib.sha256()
    try:
        files = sorted(home.glob('*.rules'))
    except OSError:
        files = []
    for path in files:
        try:
            h.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
        except OSError:
            h.update(path.name.encode() + b'\0<unreadable>\0')
    return h.hexdigest()


def rule_files():
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'rules'
    try:
        return [str(p) for p in sorted(home.glob('*.rules'))]
    except OSError:
        return []


def _launch(backend, reqs):
    launch = {'require_host': False, 'add_dirs': [], 'approval_policy': None}
    for req in reqs:
        if TABLE[req['kind']][backend] != MAPPED:
            continue
        if req['kind'] == 'host_parent':
            launch['require_host'] = True
        elif req['kind'] == 'extra_dirs':
            launch['add_dirs'] += [d for d in req['dirs'] if d not in launch['add_dirs']]
        elif req['kind'] == 'sandbox_escalation' and backend == 'codex':
            launch['approval_policy'] = 'on-request'
    return launch


def combo_of(plan, base, backend, access, reqs):
    """Identity of one smoke-able call environment; changes when the need, the CLI or the rules change."""
    spec = {'backend': backend, 'access': access,
            'requirements': sorted((json.loads(json.dumps(r)) for r in reqs), key=lambda r: r['id']),
            'launch': _launch(backend, reqs), 'profile_version': profile_version(base, backend)}
    if backend == 'codex' and any(r['kind'] == 'host_entry' for r in reqs):
        spec['rules_sha256'] = rules_digest()
    key = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    return key, spec


def resolve(plan, base):
    """Full resolution table: per role call scope, the combos, notes and refusals. Pure apart from reading
    profiles and the rules digest."""
    combos, rows, failures, notes = {}, [], [], []
    for role, access, target, work in _scopes(plan):
        cfg = plan['roles'][role]
        backend = model.backend(cfg['tool'])
        reqs = _call_requirements(plan, backend, access, target, work)
        if not reqs:
            continue
        for req in reqs:
            status = TABLE[req['kind']].get(backend, UNSUPPORTED)
            if status == UNSUPPORTED:
                failures.append('%s（%s）在 %s/%s 上执行 %s：需求 %s（%s）不受支持：%s' % (
                    role, backend, access, target or '协调', target or '-', req['id'], req['kind'],
                    NOTES.get((req['kind'], backend), '能力表未登记')))
            elif status == NATIVE:
                note = '%s（%s）：%s' % (role, backend, NOTES.get((req['kind'], backend), '后端无隔离'))
                if note not in notes:
                    notes.append(note)
        if backend == 'codex' and access == 'write' and any(r['kind'] == 'host_entry' for r in reqs):
            if not any(r['kind'] == 'sandbox_escalation' for r in reqs) and not _rules_allow_all(reqs):
                failures.append('%s（codex）执行 %s：受控入口既未命中宿主 rules 的 allow 规则，也没有 sandbox_escalation 需求'
                                % (role, target or '协调'))
        key, spec = combo_of(plan, base, backend, access, reqs)
        combos.setdefault(key, dict(spec, roles=[]))
        if role not in combos[key]['roles']:
            combos[key]['roles'].append(role)
        rows.append({'role': role, 'backend': backend, 'access': access, 'target': target, 'combo': key})
    digest = hashlib.sha256(json.dumps(sorted(combos), ensure_ascii=False).encode('utf-8')).hexdigest()
    return {'hash': digest, 'combos': combos, 'rows': rows, 'failures': list(dict.fromkeys(failures)), 'notes': notes}


def _rules_allow_all(reqs):
    files = rule_files()
    if not files:
        return False
    for req in reqs:
        if req['kind'] != 'host_entry':
            continue
        if not _policy_allows(tuple(files), tuple(req['smoke']), rules_digest()):
            return False
    return True


@functools.lru_cache(maxsize=64)
def _policy_allows(files, argv, digest):
    """codex execpolicy check: does the host's rule set allow this argv outside the sandbox?"""
    cmd = ['codex', 'execpolicy', 'check']
    for f in files:
        cmd += ['--rules', f]
    try:
        done = subprocess.run(cmd + list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=30)
        result = json.loads(done.stdout or '{}')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    return any(rule.get('decision') == 'allow' or (rule.get('prefixRuleMatch') or {}).get('decision') == 'allow'
               for rule in result.get('matchedRules') or [] if isinstance(rule, dict)) or result.get('decision') == 'allow'


# ---- smoke ------------------------------------------------------------------------------------------

def receipt_path(base, key):
    return Path(str(base)) / 'records' / 'env-smoke' / (key + '.json')


def read_receipt(base, key):
    try:
        return json.loads(receipt_path(base, key).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _host_checks(spec, run_host):
    checks = []
    for req in spec['requirements']:
        if req['kind'] == 'host_parent':
            ok, detail = _check_host_parent()
            checks.append({'requirement': req['id'], 'check': 'host_parent', 'ok': ok, 'detail': detail})
        elif req['kind'] == 'extra_dirs':
            for d in req['dirs']:
                ok, detail = _check_dir(d)
                checks.append({'requirement': req['id'], 'check': 'dir_writable', 'path': d, 'ok': ok, 'detail': detail})
        elif req['kind'] == 'host_entry' and spec['backend'] not in SANDBOXED:
            code, output = run_host(req['smoke'])
            checks.append({'requirement': req['id'], 'check': 'host_smoke', 'argv': req['smoke'],
                           'ok': code == 0, 'exit_code': code, 'detail': output[-4000:]})
    return checks


def _check_host_parent():
    scripts = str(Path(__file__).resolve().parents[1])
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from agent_cli import check_execution
    try:
        check_execution({'require_host': True})
        return True, 'NoNewPrivs:0'
    except ValueError as error:
        return False, str(error)


def _check_dir(path):
    try:
        need(Path(path).is_dir(), 'not a directory')
        with tempfile.NamedTemporaryFile(dir=path, prefix='.bf-env-smoke-'):
            pass
        return True, 'writable'
    except (OSError, BfError) as error:
        return False, str(error)


def run_host_command(argv):
    try:
        done = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, errors='replace', timeout=SMOKE_TIMEOUT)
        return done.returncode, done.stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)


def smoke_card(spec, token):
    commands = model_commands(spec, token)
    lines = ['这是一次环境冒烟调用，不是任务工作。不要阅读或修改任何项目文件，不要解释。',
             '下面共 %d 条命令，必须按顺序逐条直接执行，一条都不能省略：每条单独执行一次，'
             '不组合命令、不另套 shell、不加 timeout 或重定向。' % len(commands),
             '受控入口自检只读、不取锁、不碰设备；需要提权或审批时按正常流程申请，不要跳过。',
             '某条失败也继续执行后面的命令。%d 条全部执行完后只回复一行：SMOKE_DONE %s' % (len(commands), token), '']
    for i, argv in enumerate(commands, 1):
        lines.append('%d. %s' % (i, shlex.join(argv)))
    return '\n'.join(lines) + '\n'


def model_commands(spec, token):
    """Entry self checks first (they are what a skipped tail would lose), then directory writes."""
    out = [list(req['smoke']) for req in spec['requirements'] if req['kind'] == 'host_entry']
    for req in spec['requirements']:
        if req['kind'] == 'extra_dirs' and spec['access'] == 'write':
            for d in req['dirs']:
                probe = os.path.join(d, '.bf-env-smoke-' + token)
                out += [['touch', probe], ['rm', '-f', probe]]
    if not out and spec['backend'] in SANDBOXED and spec['launch'] != _launch(spec['backend'], []):
        out = [['true']]   # the launch itself (approval policy, writable roots) must be proven by a real call
    return out


def _seen(events, argv):
    """Last completed execution of argv (started events carry no exit code and never count)."""
    want = shlex.join(argv)
    plain = ' '.join(argv)
    found = None
    for item in events:
        command = item.get('command') or ''
        if item.get('exit_code') is not None and (want in command or plain in command):
            found = item
    return found


def run_model_smoke(base, plan, spec, token, agent_cli=None):
    """One real CLI call with the resolved launch parameters; returns (events, problems, run_dir)."""
    role = spec['roles'][0]
    cfg = plan['roles'][role]
    profile = Path(str(base)) / 'profiles' / (spec['backend'] + '.json')
    if not profile.is_file():
        return [], ['profile missing: %s (run bf run once or agent_cli.py probe)' % profile], None
    work = Path(str(base)) / 'records' / 'env-smoke' / 'runs'
    work.mkdir(parents=True, exist_ok=True)
    card = work / ('%s-%s.md' % (token, spec['access']))
    card.write_text(smoke_card(spec, token), encoding='utf-8')
    run_dir = work / ('%s-%s' % (token, spec['access']))
    cli = str(agent_cli or Path(__file__).resolve().parents[1] / 'agent_cli.py')
    argv = [sys.executable, cli, 'run', '--profile', str(profile), '--task', 'env-smoke-' + token,
            '--cwd', plan.get('workspace') or str(Path(str(base)).parent), '--model', cfg['model'],
            '--effort', cfg.get('effort') or 'default', '--prompt-file', str(card), '--run-dir', str(run_dir),
            '--access', spec['access']]
    launch = spec['launch']
    for d in launch['add_dirs']:
        argv += ['--add-dir', d]
    if launch['require_host']:
        argv.append('--require-host')
    if launch['approval_policy']:
        argv += ['--approval-policy', launch['approval_policy']]
    try:
        done = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                              errors='replace', timeout=SMOKE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [], ['smoke call timed out after %ss' % SMOKE_TIMEOUT], str(run_dir)
    problems = [] if done.returncode == 0 else ['agent_cli run exit %s: %s' % (done.returncode, done.stdout[-1500:])]
    events = []
    try:
        for line in (run_dir / 'stdout').read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                item = (json.loads(line) or {}).get('item')
            except (ValueError, AttributeError):
                continue
            if isinstance(item, dict) and item.get('type') == 'command_execution':
                events.append(item)
    except OSError as error:
        problems.append('no CLI output: %s' % error)
    return events, problems, str(run_dir)


def smoke(base, plan, run_host=None, run_model=None, only=None, force=False):
    """Run registration smoke for every combo lacking a passing receipt; write one receipt per combo.
    A passing receipt is reused: its key already binds the needs, the CLI profile and the host rules."""
    base = Path(str(base))
    resolution = resolve(plan, base)
    need(not resolution['failures'], 'environment resolution failed:\n- ' + '\n- '.join(resolution['failures']))
    run_host = run_host or run_host_command
    run_model = run_model or run_model_smoke
    results = {}
    passed = {}
    # Largest need sets first: a passing call with the same backend, access and launch proves its subsets.
    ordered = sorted(resolution['combos'].items(), key=lambda kv: -len(kv[1]['requirements']))
    for key, spec in ordered:
        if only and key not in only:
            continue
        cover = next((k for k, s in passed.items() if _covers(s, spec)), None)
        if cover and not force:
            _write_receipt(base, {'key': key, 'combo': spec, 'ok': True, 'at': now(), 'method': 'covered',
                                  'checks': [{'check': 'covered_by', 'key': cover, 'ok': True}]})
            results[key] = {'ok': True, 'covered_by': cover, 'roles': spec['roles'], 'backend': spec['backend'],
                            'access': spec['access'], 'receipt': str(receipt_path(base, key)), 'failed': []}
            continue
        previous = read_receipt(base, key)
        if not force and previous and previous.get('ok') and previous.get('key') == key:
            results[key] = {'ok': True, 'reused': True, 'roles': spec['roles'], 'backend': spec['backend'],
                            'access': spec['access'], 'receipt': str(receipt_path(base, key)), 'failed': []}
            passed[key] = spec
            continue
        token = '%s-%d' % (key[:12], int(time.time() * 1000) % 100000)
        checks = _host_checks(spec, run_host)
        run_dir = None
        commands = model_commands(spec, token) if spec['backend'] in SANDBOXED else []
        if commands and all(c['ok'] for c in checks):
            events, problems, run_dir = run_model(base, plan, spec, token)
            for problem in problems:
                checks.append({'check': 'model_call', 'ok': False, 'detail': problem})
            for argv in commands:
                item = _seen(events, argv)
                checks.append({'check': 'model_command', 'argv': argv, 'ok': bool(item) and item.get('exit_code') == 0,
                               'exit_code': item.get('exit_code') if item else None,
                               'detail': (item.get('aggregated_output') or '')[-2000:] if item else 'not executed'})
        elif commands:
            checks.append({'check': 'model_call', 'ok': False, 'detail': 'skipped: host checks failed first'})
        if not checks:
            checks.append({'check': 'nothing_to_prove', 'ok': True,
                           'detail': '全部需求在该后端为天然满足或只需默认启动参数'})
        receipt = {'key': key, 'combo': spec, 'checks': checks, 'ok': all(c['ok'] for c in checks),
                   'at': now(), 'run_dir': run_dir,
                   'method': 'model' if commands else 'host'}
        path = _write_receipt(base, receipt)
        if receipt['ok']:
            passed[key] = spec
        results[key] = {'ok': receipt['ok'], 'roles': spec['roles'], 'backend': spec['backend'],
                        'access': spec['access'], 'receipt': str(path),
                        'failed': [c for c in checks if not c['ok']]}
    return {'hash': resolution['hash'], 'notes': resolution['notes'], 'smoke': results}


def _covers(big, small):
    if (big['backend'], big['access'], big['profile_version'], big.get('rules_sha256')) != \
            (small['backend'], small['access'], small['profile_version'], small.get('rules_sha256', big.get('rules_sha256'))):
        return False
    if big['launch'] != small['launch']:
        return False
    have = {r['id']: r for r in big['requirements']}
    return all(have.get(r['id']) == r for r in small['requirements'])


def _write_receipt(base, receipt):
    path = receipt_path(base, receipt['key'])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(receipt, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(str(tmp), str(path))
    return path


# ---- registration and dispatch ----------------------------------------------------------------------

def register(base, plan):
    """Called by init / revise / set-role: refuse unsupported needs and missing or failed receipts,
    then pin the resolution into plan['env_resolved']."""
    resolution = resolve(plan, base)
    need(not resolution['failures'], '执行环境解析失败（先修改需求或角色分工）：\n- ' + '\n- '.join(resolution['failures']))
    missing = []
    combos = {}
    for key, spec in resolution['combos'].items():
        receipt = read_receipt(base, key)
        if receipt is None and ASSUME_SMOKE_FOR_TESTS:
            receipt = {'ok': True, 'key': key, 'assumed': True}
        if not receipt or not receipt.get('ok') or receipt.get('key') != key:
            state = '缺冒烟回执' if not receipt else '冒烟未通过'
            missing.append('%s：%s/%s 角色 %s' % (state, spec['backend'], spec['access'], '、'.join(spec['roles'])))
            continue
        path = receipt_path(base, key)
        combos[key] = {'backend': spec['backend'], 'access': spec['access'], 'roles': spec['roles'],
                       'requirements': [r['id'] for r in spec['requirements']], 'launch': spec['launch'],
                       'receipt': None if receipt.get('assumed') else str(path),
                       'receipt_sha256': None if receipt.get('assumed') else hashlib.sha256(path.read_bytes()).hexdigest()}
    need(not missing, '执行环境须在登记前完成冒烟（bf env-resolve --plan <草案> --smoke）：\n- ' + '\n- '.join(missing))
    plan['env_resolved'] = {'hash': resolution['hash'], 'combos': combos, 'notes': resolution['notes'], 'at': now()}
    return plan['env_resolved']


def call(plan, base, tool, access, target, order_type):
    """Launch parameters for one call, or a drift problem string. Never runs a smoke."""
    backend = model.backend(tool)
    reqs = _call_requirements(plan, backend, access, target, order_type not in COORDINATION)
    if not reqs:
        return {'require_host': False, 'add_dirs': [], 'approval_policy': None}, None
    key, spec = combo_of(plan, base, backend, access, reqs)
    pinned = (plan.get('env_resolved') or {}).get('combos', {})
    if key not in pinned:
        return None, ('执行环境与登记不一致（%s/%s：CLI profile、宿主 rules 或需求在登记后变化）；'
                      '负责人重跑 env-resolve --smoke 并经 revise 更新登记后恢复' % (backend, access))
    return dict(pinned[key]['launch']), None


def requires_host(plan):
    return any(c['launch'].get('require_host') for c in (plan.get('env_resolved') or {}).get('combos', {}).values())


def describe(plan, backend, access, target, order_type='implement'):  # noqa: E302
    """Human lines for the card's environment section."""
    out = []
    for req in _call_requirements(plan, backend, access, target, order_type not in COORDINATION):
        status = TABLE[req['kind']].get(backend, UNSUPPORTED)
        label = {'host_parent': 'CLI 父进程在宿主运行', 'extra_dirs': '额外可写目录 %s' % '、'.join(req.get('dirs', [])),
                 'host_entry': '受控入口在沙箱外执行', 'sandbox_escalation': '沙箱提权：%s' % req.get('mode')}[req['kind']]
        note = NOTES.get((req['kind'], backend))
        out.append('%s（%s%s）' % (label, '天然满足' if status == NATIVE else '已映射', '；' + note if note else ''))
    return out


# ---- migration ----------------------------------------------------------------------------------------

def migrate(plan):
    """Convert a legacy per-backend env; returns (new_env, report lines). Never writes state."""
    env = plan.get('env') or {}
    need(is_legacy(env), 'plan.env is not in the legacy per-backend format')
    sections = {k: v for k, v in env.items() if isinstance(v, dict)}
    report, reqs = [], []

    def same(key):
        values = {json.dumps(v.get(key), sort_keys=True, ensure_ascii=False) for v in sections.values() if key in v}
        if len(values) > 1:
            report.append('差异：各后端的 %s 不一致，已取第一节，须负责人取舍：%s' % (key, ' | '.join(sorted(values))))
        for v in sections.values():
            if key in v:
                return v[key]
        return None

    scope = {'deliverables': '*', 'access': ['read', 'write']}
    if same('require_host'):
        reqs.append({'id': 'host-parent', 'kind': 'host_parent', 'reason': '迁移自 require_host', 'applies': dict(scope)})
    dirs = same('add_dirs')
    if dirs:
        reqs.append({'id': 'extra-dirs', 'kind': 'extra_dirs', 'dirs': list(dirs), 'reason': '迁移自 add_dirs',
                     'applies': {'deliverables': '*', 'access': ['write']}})
    entry = same('entry')
    if entry:
        req = {'id': 'host-entry', 'kind': 'host_entry', 'entries': list(entry), 'smoke': None,
               'reason': '迁移自 entry', 'applies': dict(scope)}
        if same('preflight'):
            req['preflight'] = same('preflight')
        reqs.append(req)
        report.append('缺项：host_entry 的 smoke 须由负责人补写（无副作用、不取共享锁、不碰设备的自检 argv）')
    policy = same('approval_policy')
    if policy:
        if policy == 'on-request':
            reqs.append({'id': 'escalation', 'kind': 'sandbox_escalation', 'mode': 'auto_review',
                         'reason': '迁移自 approval_policy=on-request', 'applies': dict(scope)})
        else:
            report.append('无法转换：approval_policy=%s 没有对应需求，须负责人决定' % policy)
    new = {'requirements': reqs}
    for key in ('blocked_output', 'guide'):
        value = same(key)
        if value:
            new[key] = value
    report.append('提示：所有需求的 applies 默认为全部目标，请按实际需要收窄')
    return new, report
