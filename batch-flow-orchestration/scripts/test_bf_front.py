#!/usr/bin/env python3
"""batch-flow v2 chat front as the operations role (design §22): read-everything permissions, operational failures
handled by the front, and a human-readable report. Written before the implementation. Python 3.8 stdlib only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bf import api, front  # noqa: E402
from test_bf_core import make_plan, record_json  # noqa: E402

CLI = str(HERE / 'bf' / 'cli.py')
SWAP = '/home/u/.local/bin/codex-swap'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / 'task' / '.batch-flow'
        (self.base / 'records').mkdir(parents=True)
        self.workspace = self.root / 'src'
        self.workspace.mkdir()
        approval = self.root / 'ok.txt'
        approval.write_text('批准')
        plan = make_plan(workspace=str(self.workspace))
        plan['batches'] = [{'id': 'B1', 'title': '帧隔离与预算'}, {'id': 'B2', 'title': '两层回归汇总'}]
        plan['deliverables']['D1']['title'] = '旧帧不发布'
        plan['deliverables']['D2']['title'] = 'D2'  # untitled (as imported from v1): falls back to the batch title
        api.init(self.base, plan, approval)

    def tearDown(self):
        self.tmp.cleanup()

    def impl(self, target):
        return api.orders(api.load(self.base), type='implement', target=target)[0]['id']

    def ev(self, tool, **inputs):
        return {'tool_name': tool, 'tool_input': inputs, 'cwd': str(self.base)}

    def decide(self, event):
        return front.hook_decision(event, self.base, account_switch=[SWAP])['decision']


class Permissions(Base):  # §22.1
    def test_reads_anything(self):
        for e in [self.ev('Read', file_path=str(self.base / 'plan.json')), self.ev('Read', file_path=str(self.base / 'orders.json')),
                  self.ev('Read', file_path=str(self.workspace / 'main.c')),
                  self.ev('Grep', pattern='x', path=str(self.workspace)), self.ev('Glob', pattern='**/*.md'),
                  self.ev('Skill', skill='batch-flow-orchestration'), self.ev('ScheduleWakeup', delaySeconds=600, prompt='汇报'),
                  self.ev('CronCreate', cron='*/10 * * * *', prompt='汇报')]:
            self.assertEqual(self.decide(e), 'allow', e)

    def test_writes_only_user_words(self):
        self.assertEqual(self.decide(self.ev('Write', file_path=str(self.base / 'answers' / '1.txt'), content='同意')), 'allow')
        for e in [self.ev('Write', file_path=str(self.base / 'orders.json'), content='{}'),
                  self.ev('Write', file_path=str(self.base / 'plan.json'), content='{}'),
                  self.ev('Write', file_path=str(self.workspace / 'main.c'), content='x'),
                  self.ev('Edit', file_path=str(self.base / 'answers' / '1.txt')),
                  self.ev('NotebookEdit', notebook_path=str(self.workspace / 'a.ipynb')),
                  self.ev('Agent', prompt='fix the bug'), self.ev('Task', prompt='x')]:
            self.assertEqual(self.decide(e), 'deny', e)

    def test_shell(self):
        bf = '%s %s --base %s ' % (sys.executable, CLI, self.base)
        ok = [bf + 'report --text', bf + 'retry WO-0001 --note quota', bf + 'escalate WO-0001 --note x', bf + 'status',
              '%s -n' % SWAP, 'ls -la %s' % self.base, 'cat %s' % (self.base / 'plan.json'), 'tail -n 50 %s' % (self.base / 'x.log'),
              'grep -rn timeout %s' % self.workspace, 'git -C %s log --oneline -5' % self.workspace,
              'git -C %s status' % self.workspace, 'git -C %s diff --stat' % self.workspace, 'wc -l %s' % (self.base / 'events.jsonl')]
        for c in ok:
            self.assertEqual(self.decide(self.ev('Bash', command=c)), 'allow', c)
        bad = [bf + 'init --plan p.json --approval %s' % (self.base / 'answers' / '1.txt'),
               bf + 'revise --plan p.json --approval %s' % (self.base / 'answers' / '1.txt'),
               'git -C %s commit -m x' % self.workspace, 'git -C %s checkout .' % self.workspace,
               'rm -f %s' % (self.base / 'orders.json'), 'cat a | sh', 'ls > /tmp/x', 'find . -delete', 'find . -exec rm {} ;',
               'python3 build.py', './build.sh --product-name x', 'sed -i s/a/b/ %s' % (self.workspace / 'main.c'),
               '%s -K' % SWAP]
        for c in bad:
            self.assertEqual(self.decide(self.ev('Bash', command=c)), 'deny', c)


class Operations(Base):  # §22.2
    def fail(self, oid, times=2, reason='CLI unusable: You have hit your usage limit'):
        for _ in range(times):
            api.abandon(self.base, oid, api.start(self.base, oid), reason)

    def test_repeated_tool_failure_waits_for_front_not_lead(self):
        oid = self.impl('D1')
        self.fail(oid)
        st = api.load(self.base)
        self.assertEqual(api.orders(st, type='decide'), [])
        self.assertIn('usage limit', st.orders[oid]['ops']['problem'])
        self.assertNotIn(oid, api.runnable(st, parallel=5))
        self.assertIn(self.impl('D2'), api.runnable(st, parallel=5))  # the rest keeps going
        ops = api.report(self.base)['ops']
        self.assertEqual([(x['order'], x['label']) for x in ops], [(oid, 'D1 旧帧不发布')])
        self.assertIn('usage limit', ops[0]['problem'])

    def test_retry_after_switching_account(self):
        oid = self.impl('D1')
        self.fail(oid)
        api.retry(self.base, oid, '已切换 codex 账号')
        st = api.load(self.base)
        self.assertNotIn('ops', st.orders[oid])
        self.assertIn(oid, api.runnable(st, parallel=5))
        self.assertEqual(api.assign(st, oid)['stage'], 'primary')  # failures cost no budget
        event = json.loads((self.base / 'events.jsonl').read_text().splitlines()[-1])
        self.assertEqual((event['op'], event['actor']), ('retry', 'front'))
        with self.assertRaises(api.BfError):
            api.retry(self.base, oid, 'again')  # nothing pending

    def test_escalate_to_lead(self):
        oid = self.impl('D1')
        self.fail(oid, reason='CLI unusable: model returned malformed tool call')
        api.escalate(self.base, oid, '不是账号问题，像是模型调用格式错误')
        d = api.orders(api.load(self.base), type='decide', target='D1')
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]['input']['trigger'], 'tool_failure')
        self.assertIn('模型调用格式错误', json.dumps(d[0]['input'], ensure_ascii=False))


class Report(Base):  # §22.3
    def test_structured_report_has_people_facing_fields(self):
        r = api.report(self.base)
        self.assertEqual(r['roles']['executor'], {'tool': 'codex', 'model': 'exec-m', 'effort': 'medium'})
        self.assertEqual([(x['label'], x['type'], x['role'], x['model']) for x in r['next']][:2],
                         [('D1 旧帧不发布', 'implement', 'executor', 'exec-m'), ('D2 帧隔离与预算', 'implement', 'executor', 'exec-m')])
        self.assertEqual([(b['id'], b['title'], b['state'], b['done'], b['total']) for b in r['batches']],
                         [('B1', '帧隔离与预算', 'active', 0, 2), ('B2', '两层回归汇总', 'pending', 0, 1)])
        self.assertEqual(sorted(r['budgets']), ['D1', 'D2', 'D3'])  # open deliverables only

    def test_done_deliverables_leave_budgets(self):
        oid = self.impl('D1')
        ev = self.base / 'records' / ('%032x.json' % 7)
        ev.write_text(record_json(7))
        api.deliver(self.base, oid, api.start(self.base, oid), '结果：VERIFIED\n停止原因：完成\n验证证据：%s\n' % ev, conversation_id='c')
        self.assertNotIn('D1', api.report(self.base)['budgets'])

    def test_text_report_reads_like_a_status_note(self):
        oid = self.impl('D1')
        api.start(self.base, oid)
        text = api.report_text(self.base)
        for piece in ['正在做', 'D1 旧帧不发布', 'exec-m', '下一步', 'D2 帧隔离与预算', '待你决定', '角色', '批次']:
            self.assertIn(piece, text)
        self.assertNotIn('WO-', text)  # people read titles, not order ids
        self.assertNotIn('{', text)
        self.assertLess(len(text.splitlines()), 25)


class Prompt(unittest.TestCase):  # §22.4
    def test_prompt_states_duties_style_and_limits(self):
        p = front.prompt('/t/.batch-flow', account_switch=[SWAP])
        for piece in ['运维', 'report --text', 'retry', 'escalate', SWAP, '标题', 'answers', 'set-role', '技术负责人', '不改代码']:
            self.assertIn(piece, p)
        self.assertLess(len(p), 5000)


if __name__ == '__main__':
    unittest.main()
