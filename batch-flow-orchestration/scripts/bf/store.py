"""Storage (design 4, I1): exclusive lock, CAS, idempotent request ids, atomic writes, events.jsonl."""
import fcntl
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class BfError(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise BfError(msg)


def now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def sha256_file(path):
    """Hex digest of a file, or None when it cannot be read."""
    try:
        h = hashlib.sha256()
        with open(str(path), 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


class State(object):
    def __init__(self, base, plan, orders, revision=0, stopped=False, seq=0, kill=False):
        self.base = Path(base)
        self.plan = plan
        self.orders = orders
        self.revision = revision
        self.stopped = stopped
        self.kill = kill
        self.seq = seq
        self.plan_dirty = False
        self.opened = []  # order ids opened by the current transaction


@contextmanager
def _locked(base, shared=False):
    with open(str(Path(base) / '.lock'), 'a') as f:
        fcntl.flock(f, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read_json(path):
    with open(str(path), 'r', encoding='utf-8') as f:
        return json.load(f)


def _read(base):
    base = Path(base)
    need((base / 'plan.json').is_file() and (base / 'orders.json').is_file(), 'not initialised: %s' % base)
    meta = _read_json(base / 'orders.json')
    return State(base, _read_json(base / 'plan.json'), meta['orders'], meta['revision'],
                 meta.get('stopped', False), meta.get('seq', 0), meta.get('kill', False))


def load(base):
    base = Path(base)
    need(base.is_dir(), 'no such directory: %s' % base)
    with _locked(base, shared=True):
        return _read(base)


def _dump(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=1) + '\n').encode('utf-8')


def _atomic_write(path, data):
    path = str(path)
    tmp = '%s.tmp.%d' % (path, os.getpid())
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _find_request(base, request_id):
    path = Path(base) / 'events.jsonl'
    if not path.is_file():
        return None
    with open(str(path), 'r', encoding='utf-8') as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('request_id') == request_id:
                return event
    return None


def _write_attention(state):
    """Derived view of current open questions; callers hold the store lock."""
    asks = [o for o in state.orders.values() if o['type'] == 'ask_user' and o['state'] == 'open']
    text = ('# 需要用户回答\n\n' + ''.join('- %s（%s）：%s\n' %
            (o['id'], o['target'] or '-', o['input'].get('question', '').replace('\n', '\n  ')) for o in asks)) if asks else ''
    path = state.base / 'attention.md'
    if (path.read_text(encoding='utf-8') if path.exists() else '') != text:
        _atomic_write(path, text.encode('utf-8'))


def refresh_attention(base):
    """Refresh a derived report without changing state, revisions, budgets or driver status."""
    with _locked(base):
        _write_attention(_read(base))


def transact(base, op, actor, fn, expected_revision=None, request_id=None, init=False):
    """Run fn(state) under the exclusive lock and commit it as exactly one revision and one event line."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    with _locked(base):
        if request_id:
            prior = _find_request(base, request_id)
            if prior is not None:
                _write_attention(_read(base))
                return prior.get('result')
        if init:
            need(not (base / 'plan.json').exists(), 'already initialised: %s' % base)
            state = State(base, None, {}, 0)
        else:
            state = _read(base)
        if expected_revision is not None:
            need(expected_revision == state.revision, 'revision conflict: expected %s, current %s'
                 % (expected_revision, state.revision))
        result = fn(state)
        state.revision += 1
        if state.plan_dirty:
            _atomic_write(base / 'plan.json', _dump(state.plan))
            (base / 'plan.history').mkdir(exist_ok=True)
            _atomic_write(base / 'plan.history' / ('plan-%04d.json' % state.plan['revision']), _dump(state.plan))
        orders_bytes = _dump({'revision': state.revision, 'stopped': state.stopped, 'kill': state.kill, 'seq': state.seq,
                              'orders': state.orders})
        _atomic_write(base / 'orders.json', orders_bytes)
        event = {'revision': state.revision, 'request_id': request_id or uuid.uuid4().hex, 'actor': actor,
                 'op': op, 'at': now(), 'result': result, 'orders_sha256': hashlib.sha256(orders_bytes).hexdigest()}
        with open(str(base / 'events.jsonl'), 'ab') as f:
            f.write((json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        _write_attention(state)
        return result
