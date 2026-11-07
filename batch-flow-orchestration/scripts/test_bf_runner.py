#!/usr/bin/env python3
"""batch-flow v2 phase B specification tests: cards, runner, report, CLI and chat-front whitelist.

Written before the implementation (design/workorder-redesign.md §6, §10, §18). Uses a fake role adapter;
no model or real CLI is invoked. Python 3.8, standard library only.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bf import api, cards, runner, front  # noqa: E402
from test_bf_core import make_plan, record_json, EDIT, NEEDS_DECISION, UNFINISHED  # noqa: E402


class FakeAdapter:
    """Scripted role responses keyed by order type and target; records every launch."""

    def __init__(self, base, script):
        self.base = base
        self.script = script          # {(type, target): [response, ...]} consumed in order
        self.launches = []            # (order_id, assignment, card_text, resume_conversation)
        self.pending = {}
        self.killed = []
        self.n = 0

    def launch(self, order, assignment, card_path, run_dir, resume=None):
        self.n += 1
        handle = 'h%d' % self.n
        self.launches.append(dict(order=order['id'], type=order['type'], target=order['target'], assignment=assignment,
                                  card=Path(card_path).read_text(encoding='utf-8'), resume=resume, run_dir=str(run_dir)))
        responses = self.script.get((order['type'], order['target'])) or self.script.get((order['type'], None))
        response = dict(responses.pop(0))
        response.setdefault('conversation_id', resume or 'conv-%d' % self.n)
        if response.get('evidence'):
            path = Path(self.base) / 'records' / ('%032x.json' % (1000 + self.n))
            path.write_text(record_json(self.n))
            response['text'] = response['text'].replace('{EV}', str(path))
        if not response.get('hang'):  # a hanging call never finishes on its own
            self.pending[handle] = response
        return handle

    def poll(self, handle):
        return self.pending.pop(handle, None)

    def kill(self, handle):
        self.killed.append(handle)
        self.pending.pop(handle, None)


def done(extra=''):
    return dict(text='结果：VERIFIED\n停止原因：完成\n验证证据：{EV}\n上升问题：无\n' + extra, evidence=True, events=[EDIT])


def release():
    return dict(text='结果：RELEASE\n复跑与证据：{EV}\n', evidence=True)


class RunnerSpec(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / '.batch-flow'
        (self.base / 'records').mkdir(parents=True)
        self.approval = Path(self.tmp.name) / 'approval.txt'
        self.approval.write_text('用户：批准')

    def tearDown(self):
        self.tmp.cleanup()

    def init(self, **over):
        api.init(self.base, make_plan(**over), self.approval)

    def test_runs_a_batch_to_release_without_intervention(self):  # §1, §6.1
        self.init(envelope={'batches': ['B1']})
        fake = FakeAdapter(self.base, {('implement', 'D1'): [done()], ('implement', 'D2'): [done()],
                                       ('review', 'R12'): [release()]})
        r = runner.Runner(self.base, fake)
        r.run_until_idle()
        st = api.load(self.base)
        self.assertEqual(api.orders(st, type='release', target='B1')[0]['state'], 'done')
        self.assertEqual([x['type'] for x in fake.launches], ['implement', 'implement', 'review'])
        self.assertEqual(api.runnable(st), [])  # D3 outside envelope

    def test_continuation_resumes_same_session_with_short_prompt(self):  # §10.3
        self.init()
        fake = FakeAdapter(self.base, {('implement', 'D1'): [dict(text=UNFINISHED, events=[EDIT]), done()],
                                       ('implement', 'D2'): [done()], ('review', 'R12'): [release()],
                                       ('implement', 'D3'): [done()], ('review', 'R3'): [release()]})
        runner.Runner(self.base, fake).run_until_idle()
        first, second = fake.launches[0], fake.launches[1]
        self.assertEqual((first['target'], second['target']), ('D1', 'D1'))
        self.assertEqual(second['resume'], 'conv-1')
        self.assertIn('尚未合法结束', second['card'])
        self.assertLess(len(second['card']), 800)
        run = api.orders(api.load(self.base), type='implement', target='D1')[0]['runs']
        self.assertEqual((len(run), run[0]['segments']), (1, 2))

    def test_cards_carry_order_specific_inputs(self):  # §6, §10.1, problems 3/6
        self.init()
        fake = FakeAdapter(self.base, {
            ('implement', 'D1'): [done('候选新项：\n- CAN 自测超时\n')], ('implement', 'D2'): [done()],
            ('review', 'R12'): [dict(text='结果：FIX_REQUIRED\n复跑与证据：{EV}\n阻断：B1 | 返修:D1 | 方案级:否 | 关闭条件:改正 2.3 节\n', evidence=True),
                                release()],
            ('repair', 'D1'): [done('阻断关闭：B1=已关闭 已改\n')],
            ('triage', 'B1'): [dict(text='结果：VERIFIED\n停止原因：完成\n', decision={'diagnosis': 't', 'items': {'C1': {'disposition': 'defer', 'reason': 'later'}}})],
            ('implement', 'D3'): [done()], ('review', 'R3'): [release()]})
        runner.Runner(self.base, fake).run_until_idle()
        by_type = {}
        for launch in fake.launches:
            by_type.setdefault(launch['type'], []).append(launch)
        card = by_type['implement'][0]['card']
        for heading in ['## 目标', '## 环境', '## 本单', '## 交付']:
            self.assertIn(heading, card)
        self.assertLess(card.index('## 环境'), card.index('## 本单'))
        self.assertIn('停止原因', card)
        repair = by_type['repair'][0]['card']
        self.assertIn('B1', repair)
        self.assertIn('改正 2.3 节', repair)
        self.assertIn('已审阅通过的部分不得改动', repair)
        self.assertIn('阻断关闭', repair)
        recheck = by_type['review'][1]['card']
        self.assertIn('B1', recheck)
        triage = by_type['triage'][0]['card']
        self.assertIn('CAN 自测超时', triage)
        self.assertIn('decision.json', triage)

    def test_ask_user_pauses_only_dependents_and_answer_resumes(self):  # §5.4, §11
        self.init()
        fake = FakeAdapter(self.base, {
            ('implement', 'D1'): [dict(text=NEEDS_DECISION, events=[EDIT]), done()],
            ('decide', 'D1'): [dict(text='结果：VERIFIED\n停止原因：完成\n', decision={'diagnosis': 'd', 'actions': [{'grant': {'D1': {'implement': 1}}}]})],
            ('implement', 'D2'): [done()], ('review', 'R12'): [release()],
            ('implement', 'D3'): [done()], ('review', 'R3'): [release()]})
        r = runner.Runner(self.base, fake)
        r.run_until_idle()
        st = api.load(self.base)
        ask = api.orders(st, type='ask_user', target='D1')[0]
        self.assertEqual(ask['state'], 'open')
        self.assertEqual(api.orders(st, type='implement', target='D2')[0]['state'], 'done')  # others kept going
        self.assertIn(ask['id'], (self.base / 'attention.md').read_text(encoding='utf-8'))
        api.answer(self.base, ask['id'], '同意', approve=True)
        r.run_until_idle()
        self.assertEqual(api.orders(api.load(self.base), type='implement', target='D1')[0]['state'], 'done')

    def test_stop_is_checked_between_launches(self):  # I6
        self.init()
        fake = FakeAdapter(self.base, {('implement', 'D1'): [done()], ('implement', 'D2'): [done()]})
        r = runner.Runner(self.base, fake)
        r.step()
        api.stop(self.base)
        r.run_until_idle()
        self.assertEqual(len(fake.launches), 1)

    def test_restart_recovers_from_files(self):  # I7
        self.init()
        fake = FakeAdapter(self.base, {('implement', 'D1'): [done()], ('implement', 'D2'): [done()],
                                       ('review', 'R12'): [release()], ('implement', 'D3'): [done()], ('review', 'R3'): [release()]})
        previous = runner.Runner(self.base, fake)
        previous.step()
        previous.close()  # process exit releases the lifetime driver lock
        runner.Runner(self.base, fake).run_until_idle()  # a new runner continues from orders.json
        self.assertTrue(all(o['state'] == 'done' for o in api.orders(api.load(self.base), type='release')))

    def test_lost_run_is_marked_invalid_and_retried_without_budget(self):  # I7, I2
        self.init()
        oid = api.orders(api.load(self.base), type='implement', target='D1')[0]['id']
        api.start(self.base, oid)  # a previous runner died after starting this run
        fake = FakeAdapter(self.base, {('implement', 'D1'): [done()], ('implement', 'D2'): [done()],
                                       ('review', 'R12'): [release()], ('implement', 'D3'): [done()], ('review', 'R3'): [release()]})
        runner.Runner(self.base, fake).run_until_idle()
        runs = api.orders(api.load(self.base), type='implement', target='D1')[0]['runs']
        self.assertEqual([x['result'] for x in runs], ['INVALID', 'VERIFIED'])
        self.assertTrue(runs[0]['exempt'])

    def test_report_summarises_progress(self):  # §6.3
        self.init()
        fake = FakeAdapter(self.base, {('implement', 'D1'): [done()], ('implement', 'D2'): [done()]})
        r = runner.Runner(self.base, fake)
        r.step()
        r.step()
        report = api.report(self.base)
        for key in ['running', 'recent', 'ask_user', 'budgets', 'runnable']:
            self.assertIn(key, report)
        self.assertTrue(any(item['target'] == 'D1' for item in report['recent']))
        self.assertIn('D2', report['budgets'])  # open deliverables only (§22.3)
        r.close()

    def test_cli_commands(self):  # §6.1
        self.init()
        cli = [sys.executable, str(HERE / 'bf' / 'cli.py'), '--base', str(self.base)]
        out = json.loads(subprocess.run(cli + ['status'], capture_output=True, text=True, check=True).stdout)
        self.assertIn('runnable', out)
        subprocess.run(cli + ['stop'], check=True, capture_output=True)
        self.assertTrue(api.load(self.base).stopped)
        subprocess.run(cli + ['resume'], check=True, capture_output=True)
        self.assertFalse(api.load(self.base).stopped)


class FrontSpec(unittest.TestCase):  # §6.3
    def test_whitelist(self):
        cli = str(HERE / 'bf' / 'cli.py')
        ok = ['%s %s --base /t/.batch-flow %s' % (sys.executable, cli, c) for c in ['status', 'report', 'stop', 'resume']]
        ok.append('%s %s --base /t/.batch-flow answer WO-0007 --file /t/.batch-flow/answers/a1.txt --approve' % (sys.executable, cli))
        for command in ok:
            self.assertTrue(front.allowed(command), command)
        bad = ['%s %s --base /t/.batch-flow status; rm -rf /' % (sys.executable, cli),
               '%s %s --base /t/.batch-flow status | tee x' % (sys.executable, cli),
               'python3 /tmp/other.py status', 'cat /t/.batch-flow/orders.json > /tmp/x',
               '%s %s --base /t/.batch-flow deliver WO-1' % (sys.executable, cli)]
        for command in bad:
            self.assertFalse(front.allowed(command), command)


if __name__ == '__main__':
    unittest.main()
