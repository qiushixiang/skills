"""Driver loop (design 6.1): select, render, launch, wait, collect, gate, route. State lives in files only (I7)."""
import json
import time

from . import api, cards
from .store import BfError, now


class Runner(object):
    def __init__(self, base, adapter, parallel=1, poll_interval=0.2):
        self.base, self.adapter, self.parallel, self.poll_interval = api.load(base).base, adapter, parallel, poll_interval
        self.inflight = {}  # run_id -> {handle, order, assignment, n}
        self.recovered = False

    def _dir(self, run_id):
        path = self.base / 'records' / 'runs' / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _persist(self, run_id):
        info = self.inflight[run_id]
        (self._dir(run_id) / 'inflight.json').write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')

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
        for oid in api.runnable(st, self.parallel):
            o = st.orders[oid]
            if o['type'] == 'release':
                api.release(self.base, oid)
                progress = True
                continue
            a = api.assign(st, oid)
            resume = a['session']['conversation_id'] if a['session']['mode'] == 'resume' else None
            run_id = api.start(self.base, oid, conversation_id=resume)
            st = api.load(self.base)
            self._launch(st, oid, run_id, a, cards.render(st, oid, a, run_dir=self._dir(run_id)), resume, 1)
            progress = True
        return progress

    def _attention(self):
        st = api.load(self.base)
        asks = api.orders(st, type='ask_user', state_='open')
        path = self.base / 'attention.md'
        text = ''.join('- %s（%s）：%s\n' % (o['id'], o['target'] or '-', o['input'].get('question', '')) for o in asks)
        text = ('# 需要用户回答\n\n' + text) if asks else ''
        if (path.read_text(encoding='utf-8') if path.exists() else '') != text:
            path.write_text(text, encoding='utf-8')

    def step(self):
        if not self.recovered:
            self.recovered = True
            self._recover(api.load(self.base))
        progress = self._collect()
        progress = self._dispatch() or progress
        self._attention()
        return progress

    def run_until_idle(self):
        while True:
            if not self.step():
                if not self.inflight:
                    return
                time.sleep(self.poll_interval)
