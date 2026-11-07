"""Driver loop (design 6.1): select, render, launch, wait, collect, gate, route. State lives in files only (I7)."""
import json
import fcntl
import os
import subprocess
import time

from . import api, cards
from .store import BfError, now


def ensure_running(base, command=None):
    """Front-owned restart using the last explicit run configuration; never resumes a user stop."""
    base = api.load(base).base.resolve()
    state = api.load(base)
    if state.stopped or not (api.runnable(state) or api.orders(state, state_='running')):
        return {'started': False, 'reason': 'stopped or no runnable work'}
    with open(str(base / '.driver.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'started': False, 'reason': 'already running'}
        if command is None:
            try:
                command = json.loads((base / 'driver-command.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise BfError('no previous run configuration; start bf run with the required profiles first')
    with open(str(base / 'driver.log'), 'ab') as log:
        proc = subprocess.Popen(command, cwd=str(base.parent), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return {'started': True, 'pid': proc.pid}


class Runner(object):
    def __init__(self, base, adapter, parallel=1, poll_interval=0.2, run_command=None):
        self.base, self.adapter, self.parallel, self.poll_interval = api.load(base).base, adapter, parallel, poll_interval
        self.inflight = {}  # run_id -> {handle, order, assignment, n}
        self.recovered = False
        self.started = None
        self.exit_now = False
        self.lock = None
        self.run_command = run_command

    def _beat(self, exited=False):
        data = {'pid': os.getpid(), 'started': self.started, 'last_step': now(), 'exited': exited}
        tmp = self.base / 'driver.json.tmp'
        tmp.write_text(json.dumps(data), encoding='utf-8')
        os.replace(str(tmp), str(self.base / 'driver.json'))

    def _begin(self):
        if self.started is None:
            self.lock = open(str(self.base / '.driver.lock'), 'a')
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.lock.close()
                self.lock = None
                raise BfError('driver already running for %s' % self.base)
            if self.run_command:
                from .store import _atomic_write
                _atomic_write(self.base / 'driver-command.json', json.dumps(self.run_command).encode('utf-8'))
            self.started = now()
            api.driver_event(self.base, 'driver_start')

    def close(self):
        if self.started is not None:
            self.started = self.started
            self._beat(exited=True)
            api.driver_event(self.base, 'driver_exit')
            self.started = None
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def _dir(self, run_id):
        path = self.base / 'records' / 'runs' / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _persist(self, run_id):
        info = self.inflight[run_id]
        (self._dir(run_id) / 'inflight.json').write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')

    def _pend(self, run_id, order_id, assignment, text, resume, n):
        """Stopped: keep the continuation for a later driver (resume) instead of launching it."""
        self.inflight[run_id] = {'handle': None, 'order': order_id, 'assignment': assignment, 'n': n,
                                 'pending': {'text': text, 'resume': resume}}
        self._persist(run_id)

    def _launch_pending(self):
        st = api.load(self.base)
        if st.stopped:
            return False
        went = False
        for run_id, info in list(self.inflight.items()):
            if info.get('handle') is None and info.get('pending'):
                p = info['pending']
                self._launch(st, info['order'], run_id, info['assignment'], p['text'], p['resume'], info['n'])
                went = True
        return went

    def _launch(self, st, order_id, run_id, assignment, text, resume, n):
        rd = self._dir(run_id)
        card = rd / ('card-%d.md' % n)
        card.write_text(text, encoding='utf-8')
        handle = self.adapter.launch(st.orders[order_id], assignment, str(card), str(rd), resume=resume)
        self.inflight[run_id] = {'handle': handle, 'order': order_id, 'assignment': assignment, 'n': n}
        self._persist(run_id)

    def _recover(self, st):
        """Open runs without a live handle were lost with the previous driver: adopt them or mark them INVALID."""
        for o in api.orders(st, state_='running'):
            run = next((r for r in o['runs'] if r['ended'] is None), None)
            if run is None or run['run_id'] in self.inflight:
                continue
            saved = self._dir(run['run_id']) / 'inflight.json'
            if saved.is_file():
                self.inflight[run['run_id']] = json.loads(saved.read_text(encoding='utf-8'))
            else:
                api.abandon(self.base, o['id'], run['run_id'], 'driver lost the run before launch')

    def _collect(self):
        progress = False
        for run_id, info in list(self.inflight.items()):
            if info.get('handle') is None:
                continue
            res = self.adapter.poll(info['handle'])
            if res is None:
                continue
            progress = True
            if res.get('invalid'):  # tool failure, not a technical result: free of charge, bounded by invalid_retry
                api.abandon(self.base, info['order'], run_id, 'CLI unusable: %s' % '; '.join(res.get('problems') or ['unknown']))
                del self.inflight[run_id]
                saved = self._dir(run_id) / 'inflight.json'
                if saved.exists():
                    saved.unlink()
                continue
            out = api.deliver(self.base, info['order'], run_id, res.get('text') or '', events=res.get('events') or (),
                              decision=res.get('decision'), conversation_id=res.get('conversation_id'),
                              session_kb=res.get('session_kb'))
            st = api.load(self.base)
            if out['outcome'] == 'continue':
                k = next(r for r in st.orders[info['order']]['runs'] if r['run_id'] == run_id)['continuations']
                text = cards.continue_prompt(info['order'], out['reason'], k, st.plan['policy']['continue_limit'])
                if st.stopped:
                    self._pend(run_id, info['order'], info['assignment'], text, res.get('conversation_id'), info['n'] + 1)
                else:
                    self._launch(st, info['order'], run_id, info['assignment'], text, res.get('conversation_id'), info['n'] + 1)
            else:
                del self.inflight[run_id]
                saved = self._dir(run_id) / 'inflight.json'
                if saved.exists():
                    saved.unlink()
        return progress

    def _dispatch(self):
        st = api.load(self.base)
        if st.stopped:
            return False
        progress = False
        if api.check_design(self.base):
            st = api.load(self.base)
        if api.preflight(self.base):
            st = api.load(self.base)
        for oid in api.runnable(st, self.parallel):
            o = st.orders[oid]
            if o['type'] == 'release':
                api.release(self.base, oid)
                progress = True
                continue
            a = dict(api.assign(st, oid), access='read' if o['type'] == 'review' and not st.plan.get('repos') else 'write')
            resume = a['session']['conversation_id'] if a['session']['mode'] == 'resume' else None
            run_id = api.start(self.base, oid, conversation_id=resume)
            st = api.load(self.base)
            self._launch(st, oid, run_id, a, cards.render(st, oid, a, run_dir=self._dir(run_id)), resume, 1)
            progress = True
        return progress

    def _attention(self):
        from .store import refresh_attention
        refresh_attention(self.base)

    def _stop_now(self):
        """stop --now: kill every in-flight call, register the runs as INVALID (free) and leave."""
        for run_id, info in list(self.inflight.items()):
            if info.get('handle') is not None:
                self.adapter.kill(info['handle'])
            api.abandon(self.base, info['order'], run_id, '用户要求立即停止，调用已终止')
            del self.inflight[run_id]
            saved = self._dir(run_id) / 'inflight.json'
            if saved.exists():
                saved.unlink()
        self.exit_now = True

    def step(self):
        self._begin()
        if not self.recovered:
            self.recovered = True
            self._recover(api.load(self.base))
        if api.load(self.base).kill:
            self._stop_now()
            self._beat()
            return True
        progress = self._collect()
        progress = self._launch_pending() or progress
        progress = self._dispatch() or progress
        self._attention()
        self._beat()
        return progress

    def run_until_idle(self):
        try:
            while True:
                progress = self.step()
                if self.exit_now:
                    return
                if not progress:
                    if not any(i.get('handle') is not None for i in self.inflight.values()):
                        return
                    time.sleep(self.poll_interval)
        finally:
            self.close()
