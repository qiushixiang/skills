#!/usr/bin/env python3
"""batch-flow v2 user adjustments (design §21): structured commands the chat front runs on the user's words.

Written before the implementation. Python 3.8, standard library only. No model or real CLI is called.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bf import adjust, api, front  # noqa: E402
from test_bf_core import make_plan, INFEASIBLE  # noqa: E402

CATALOG = {'complete': True, 'models': [
    {'id': 'deepseek/deepseek-flash', 'name': 'DeepSeek V4.1 Flash 1017'},
    {'id': 'gemini-3.8-flash-high', 'name': 'Gemini 3.8 Flash (High)'},
    {'id': 'gemini-3.8-flash-medium', 'name': 'Gemini 3.8 Flash (Medium)'}]}


def fake_resolver(calls):
    """Stands in for model_catalog: query(profile) -> catalogue, resolve(profile, catalogue, name, effort) -> result."""
    def query(profile):
        calls.append(('query', profile['backend']))
        return CATALOG

    def resolve(profile, catalog, name, effort='default'):
        calls.append(('resolve', name))
        key = name.lower().replace(' ', '')
        hits = [m for m in catalog['models'] if key in (m['id'] + m['name']).lower().replace(' ', '')]
        if len(hits) == 1:
            return {'status': 'resolved', 'config': {'tool': profile['backend'], 'model': hits[0]['id'], 'effort': effort}}
        if hits:
            return {'status': 'needs_clarification', 'candidates': [m['id'] for m in hits]}
        return {'status': 'not_found'}
    return query, resolve


class Adjust(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / '.batch-flow'
        (self.base / 'records').mkdir(parents=True)
        (self.base / 'profiles').mkdir()
        for backend in ('opencode', 'agy', 'codex', 'claude'):
            (self.base / 'profiles' / (backend + '.json')).write_text(json.dumps({'backend': backend, 'binary': '/bin/' + backend, 'version': '1'}))
        approval = self.root / 'plan-ok.txt'
        approval.write_text('批准')
        api.init(self.base, make_plan(), approval)
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def words(self, text):
        self.n += 1
        path = self.base / 'answers' / ('%d.txt' % self.n)
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
        return path

    def st(self):
        return api.load(self.base)

    def impl(self, target):
        return api.orders(self.st(), type='implement', target=target)[0]['id']

    def test_set_role_uses_catalogue_resolution(self):
        calls = []
        query, resolve = fake_resolver(calls)
        out = adjust.set_role(self.base, 'executor', 'opencode', 'deepseek v4.1 flash', self.words('执行者换成 deepseek flash'),
                              query=query, resolve=resolve)
        self.assertEqual(out['status'], 'resolved')
        self.assertEqual(self.st().plan['roles']['executor'], {'tool': 'opencode', 'model': 'deepseek/deepseek-flash', 'effort': 'default'})
        self.assertEqual(calls[0], ('query', 'opencode'))
        self.assertTrue(list((self.base / 'catalogs').glob('opencode-*.json')))  # catalogue kept as evidence
        event = json.loads((self.base / 'events.jsonl').read_text().splitlines()[-1])
        self.assertEqual(event['op'], 'set_role')

    def test_ambiguous_or_unknown_model_changes_nothing(self):
        before = self.st().plan['roles']['executor']
        rev = self.st().revision
        query, resolve = fake_resolver([])
        out = adjust.set_role(self.base, 'executor', 'agy', 'gemini 3.8 flash', self.words('换成 gemini'), query=query, resolve=resolve)
        self.assertEqual(out['status'], 'needs_clarification')
        self.assertEqual(sorted(out['candidates']), ['gemini-3.8-flash-high', 'gemini-3.8-flash-medium'])
        out = adjust.set_role(self.base, 'executor', 'agy', 'gpt-9', self.words('换成 gpt-9'), query=query, resolve=resolve)
        self.assertEqual(out['status'], 'not_found')
        self.assertEqual((self.st().plan['roles']['executor'], self.st().revision), (before, rev))

    def test_unknown_role_and_missing_words_rejected(self):
        query, resolve = fake_resolver([])
        with self.assertRaises(api.BfError):
            adjust.set_role(self.base, 'orchestrator', 'codex', 'x', self.words('x'), query=query, resolve=resolve)
        with self.assertRaises(api.BfError):
            adjust.set_role(self.base, 'executor', 'codex', 'x', self.root / 'missing.txt', query=query, resolve=resolve)

    def test_tool_change_forces_fresh_session(self):  # §9 fix
        oid = self.impl('D1')
        run = api.start(self.base, oid)
        api.deliver(self.base, oid, run, INFEASIBLE, conversation_id='s1')
        self.assertEqual(api.assign(self.st(), oid)['session']['mode'], 'resume')
        query, resolve = fake_resolver([])
        adjust.set_role(self.base, 'executor', 'opencode', 'deepseek v4.1 flash', self.words('换模型'), query=query, resolve=resolve)
        a = api.assign(self.st(), oid)
        self.assertEqual((a['tool'], a['session']['mode']), ('opencode', 'fresh'))

    def test_envelope_add_remove(self):
        adjust.envelope(self.base, self.words('B2 先别做'), remove=['B2'])
        self.assertEqual(self.st().plan['envelope']['batches'], ['B1'])
        adjust.envelope(self.base, self.words('B2 继续'), add=['B2'])
        self.assertEqual(self.st().plan['envelope']['batches'], ['B1', 'B2'])
        with self.assertRaises(api.BfError):
            adjust.envelope(self.base, self.words('x'), add=['B9'])

    def test_grant_adds_rounds_for_one_target(self):
        oid = self.impl('D1')
        for _ in range(3):
            api.deliver(self.base, oid, api.start(self.base, oid), INFEASIBLE, conversation_id='c')
        adjust.grant(self.base, 'D1', self.words('D1 再给两轮'), implement=2)
        self.assertEqual(self.st().plan['policy']['overrides']['D1']['implement'], 5)
        with self.assertRaises(api.BfError):
            adjust.grant(self.base, 'D1', self.words('x'), implement=-1)  # budgets are never taken back here

    def test_hold_pauses_only_its_part(self):
        adjust.hold(self.base, ['D1'], self.words('D1 先停'))
        runnable = api.runnable(self.st(), parallel=5)
        self.assertNotIn(self.impl('D1'), runnable)
        self.assertIn(self.impl('D2'), runnable)
        adjust.hold(self.base, ['B1'], self.words('B1 整批先停'))
        self.assertEqual(api.runnable(self.st(), parallel=5), [])
        adjust.unhold(self.base, ['D1', 'B1'], self.words('恢复'))
        self.assertIn(self.impl('D1'), api.runnable(self.st(), parallel=5))

    def test_plan_written_before_holds_still_loads(self):  # upgrade safety for live tasks
        path = self.base / 'plan.json'
        plan = json.loads(path.read_text())
        plan.pop('holds', None)
        path.write_text(json.dumps(plan))
        self.assertIn(self.impl('D1'), api.runnable(self.st(), parallel=5))
        adjust.hold(self.base, ['D1'], self.words('D1 先停'))
        self.assertNotIn(self.impl('D1'), api.runnable(self.st(), parallel=5))

    def test_user_supersede(self):
        adjust.supersede(self.base, self.impl('D2'), self.words('D2 不做了'), reason='用户取消')
        o = api.orders(self.st(), type='implement', target='D2')[0]
        self.assertEqual((o['state'], o['output']['reason']), ('superseded', '用户取消'))
        with self.assertRaises(api.BfError):
            adjust.supersede(self.base, self.impl('D2'), self.words('again'), reason='x')  # not open any more


class FrontAdjust(unittest.TestCase):
    def setUp(self):
        self.cli = str(HERE / 'bf' / 'cli.py')
        self.base = '/t/.batch-flow'

    def cmd(self, rest):
        return '%s %s --base %s %s' % (sys.executable, self.cli, self.base, rest)

    def test_whitelist_covers_adjustments_with_user_words(self):
        words = '--approval /t/.batch-flow/answers/3.txt'
        ok = ['models opencode', "set-role executor --backend opencode --name 'deepseek v4.1 flash' %s" % words,
              'set-role reviewer --backend claude --name opus --effort medium %s' % words,
              'envelope --add S21 %s' % words, 'envelope --remove S19 %s' % words,
              'grant S15.E --implement 1 %s' % words, 'hold S19 S20 %s' % words, 'unhold S19 %s' % words,
              "supersede WO-0031 --reason '用户取消' %s" % words]
        for rest in ok:
            self.assertTrue(front.allowed(self.cmd(rest)), rest)
        bad = ['set-role executor --backend opencode --name x',                      # no user words
               'set-role executor --backend opencode --name x --approval /tmp/x.txt',  # words outside answers/
               'revise --plan /t/p.json --approval /t/.batch-flow/answers/1.txt',      # plan edits stay with the lead
               'init --plan /t/p.json --approval /t/.batch-flow/answers/1.txt']
        for rest in bad:
            self.assertFalse(front.allowed(self.cmd(rest)), rest)

    def test_account_switch_command(self):
        swap = '/home/u/.local/bin/codex-swap'
        self.assertTrue(front.allowed('%s -k' % swap, account_switch=[swap]))
        self.assertTrue(front.allowed('%s user@example.com' % swap, account_switch=[swap]))
        self.assertFalse(front.allowed('%s -K' % swap, account_switch=[swap]))
        self.assertFalse(front.allowed('%s -k; rm x' % swap, account_switch=[swap]))
        self.assertFalse(front.allowed('%s -k' % swap))  # not configured at launch
        settings = json.dumps(front.settings('/t/.batch-flow', account_switch=[swap]))
        self.assertIn(swap, settings)

    def test_prompt_explains_adjustment_flow(self):
        prompt = front.prompt('/t/.batch-flow')
        for text in ['set-role', 'models', 'hold', 'envelope', 'grant', 'supersede', '复述', 'answers']:
            self.assertIn(text, prompt)
        self.assertLess(len(prompt), 4000)


if __name__ == '__main__':
    unittest.main()
