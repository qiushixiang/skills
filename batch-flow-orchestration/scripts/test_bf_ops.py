#!/usr/bin/env python3
"""batch-flow v2 phase C operational specification: tool failures, adapter launch arguments, chat-front launcher.

Design §6.3, §19.3. Written before the implementation. Python 3.8, standard library only.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bf import adapter, api, front, runner  # noqa: E402
from test_bf_core import make_plan  # noqa: E402
from test_bf_runner import FakeAdapter, done, release  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / 'task' / '.batch-flow'
        (self.base / 'records').mkdir(parents=True)
        self.workspace = self.root / 'src'
        self.workspace.mkdir()
        self.approval = self.root / 'ok.txt'
        self.approval.write_text('批准')
        api.init(self.base, make_plan(workspace=str(self.workspace)), self.approval)

    def tearDown(self):
        self.tmp.cleanup()

    def impl(self, target):
        return api.orders(api.load(self.base), type='implement', target=target)[0]['id']


class ToolFailures(Base):  # §19.3
    def test_tool_failure_is_invalid_free_and_limited(self):
        oid = self.impl('D1')
        for _ in range(2):
            run = api.start(self.base, oid)
            api.abandon(self.base, oid, run, 'CLI unusable: auth error')
        runs = api.orders(api.load(self.base), type='implement', target='D1')[0]['runs']
        self.assertEqual([(r['result'], r['exempt']) for r in runs], [('INVALID', True)] * 2)
        st = api.load(self.base)
        self.assertEqual(api.orders(st, type='decide', target='D1'), [])  # operational, not a technical decision (§22)
        self.assertEqual(st.orders[oid]['ops']['problem'], 'CLI unusable: auth error')
        self.assertNotIn(oid, api.runnable(st, parallel=5))  # waits for the front
        self.assertEqual(api.assign(st, oid)['stage'], 'primary')  # no budget consumed

    def test_runner_routes_unusable_cli_result_to_abandon(self):
        fake = FakeAdapter(self.base, {('implement', 'D1'): [dict(text='', invalid=True, problems=['no exit code']), done()],
                                       ('implement', 'D2'): [done()], ('review', 'R12'): [release()],
                                       ('implement', 'D3'): [done()], ('review', 'R3'): [release()]})
        runner.Runner(self.base, fake).run_until_idle()
        runs = api.orders(api.load(self.base), type='implement', target='D1')[0]['runs']
        self.assertEqual([r['result'] for r in runs], ['INVALID', 'VERIFIED'])
        self.assertIsNone(fake.launches[1]['resume'])  # a failed call has no session to resume


class AdapterArguments(Base):  # §19.3
    def argv(self, order_type, target, resume=None):
        st = api.load(self.base)
        o = api.orders(st, type=order_type, target=target)[0]
        a = api.assign(st, o['id'])
        ad = adapter.AgentCliAdapter(self.base, {'codex': '/p/codex.json', 'claude': '/p/claude.json'})
        return ad.argv(o, a, '/x/card.md', self.root / 'cli-1', resume=None)

    def test_roles_get_access_and_workspace(self):
        argv = self.argv('implement', 'D1')
        self.assertEqual(argv[argv.index('--access') + 1], 'write')
        self.assertEqual(argv[argv.index('--cwd') + 1], str(self.workspace))
        self.assertEqual(argv[argv.index('--profile') + 1], '/p/codex.json')
        review = self.argv('review', 'R12')
        self.assertEqual(review[review.index('--access') + 1], 'read')
        self.assertEqual(review[review.index('--profile') + 1], '/p/claude.json')

    def test_missing_workspace_is_an_error(self):
        other = self.root / 'other' / '.batch-flow'
        (other / 'records').mkdir(parents=True)
        api.init(other, make_plan(), self.approval)
        st = api.load(other)
        o = api.orders(st, type='implement', target='D1')[0]
        ad = adapter.AgentCliAdapter(other, {'codex': '/p/codex.json'})
        with self.assertRaises(api.BfError):
            ad.argv(o, api.assign(st, o['id']), '/x/card.md', self.root / 'cli-1')


class ChatFront(Base):  # §6.3
    def event(self, tool, **inputs):
        return {'tool_name': tool, 'tool_input': inputs, 'cwd': str(self.base)}

    def test_hook_allows_only_front_duties(self):
        cli = str(HERE / 'bf' / 'cli.py')
        ok = [self.event('Bash', command='%s %s --base %s report' % (sys.executable, cli, self.base)),
              self.event('Read', file_path=str(self.base / 'attention.md')),
              self.event('Read', file_path=str(self.base / 'records' / 'runs' / 'WO-0001-r1' / 'card-1.md')),
              self.event('Write', file_path=str(self.base / 'answers' / 'a1.txt'), content='同意'),
              self.event('ScheduleWakeup', delaySeconds=600, prompt='汇报'), self.event('CronCreate', cron='*/10 * * * *', prompt='汇报')]
        for e in ok:
            self.assertEqual(front.hook_decision(e, self.base)['decision'], 'allow', e)
        bad = [self.event('Bash', command='git status'),
               self.event('Write', file_path=str(self.base / 'orders.json'), content='{}'),
               self.event('Edit', file_path=str(self.base / 'answers' / 'a1.txt')),
               self.event('Agent', prompt='fix it')]  # reading anything is allowed since §22
        for e in bad:
            self.assertEqual(front.hook_decision(e, self.base)['decision'], 'deny', e)

    def test_launcher_settings_and_prompt(self):
        settings = front.settings(self.base)
        hooks = json.dumps(settings['hooks'])
        self.assertIn('PreToolUse', settings['hooks'])
        self.assertIn('front.py', hooks)
        self.assertIn(str(self.base), hooks)
        prompt = front.prompt(self.base)
        for text in ['bf/cli.py', 'report', 'answer', '10', 'attention.md', 'answers']:
            self.assertIn(text, prompt)
        self.assertLess(len(prompt), 3000)


if __name__ == '__main__':
    unittest.main()
