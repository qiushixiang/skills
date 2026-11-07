#!/usr/bin/env python3
"""batch-flow v2 evidence, notification and stop specification (design §23). Written before the implementation.

Covers: the evidence component (bf record / snapshot) as the skill's public contract, the gate accepting only
records, bf doctor, the event stream for the chat front (bf watch + driver events), true stop semantics, and the
front's relaxed read-only shell. Python 3.8, standard library only.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bf import api, front, runner  # noqa: E402
from test_bf_core import make_plan, UNFINISHED, EDIT  # noqa: E402
from test_bf_runner import FakeAdapter, done, release  # noqa: E402

CLI = [sys.executable, str(HERE / 'bf' / 'cli.py')]
SCHEMA = {'command': list, 'cwd': str, 'started_at': str, 'ended_at': str, 'exit_code': int, 'stdout': str, 'stderr': str}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / 'task' / '.batch-flow'
        (self.base / 'records').mkdir(parents=True)
        approval = self.root / 'ok.txt'
        approval.write_text('批准')
        api.init(self.base, make_plan(workspace=str(self.root)), approval)

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, check=False):
        return subprocess.run(CLI + ['--base', str(self.base)] + list(args), capture_output=True, text=True, check=check)


class Record(Base):  # §23.1 — the public contract used by roles and project test entries
    def test_record_runs_command_and_writes_schema(self):
        work = self.root / 'work'
        work.mkdir()
        proc = self.cli('record', '--cwd', str(work), '--', sys.executable, '-c', 'import os,sys; print(os.getcwd()); print("e", file=sys.stderr); sys.exit(3)')
        self.assertEqual(proc.returncode, 3)  # exit code passes through
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual((out['ok'], out['exit_code']), (False, 3))
        path = Path(out['evidence'])
        self.assertEqual(path.parent, self.base / 'records')
        data = json.loads(path.read_text())
        for key, kind in SCHEMA.items():
            self.assertIsInstance(data[key], kind, key)
        self.assertEqual(data['cwd'], str(work))
        self.assertIn(str(work), data['stdout'])
        self.assertIn('e', data['stderr'])
        second = json.loads(self.cli('record', '--', 'true').stdout.strip().splitlines()[-1])
        self.assertNotEqual(second['evidence'], out['evidence'])  # never overwrites
        self.assertEqual(second['exit_code'], 0)

    def test_record_does_not_change_orders(self):
        before = (self.base / 'orders.json').read_bytes()
        self.cli('record', '--', 'true', check=True)
        self.assertEqual((self.base / 'orders.json').read_bytes(), before)

    def test_snapshot_captures_selected_paths(self):
        repo = self.root / 'repo'
        repo.mkdir()
        git = lambda *a: subprocess.run(['git', '-C', str(repo)] + list(a), check=True, capture_output=True)
        git('init', '-q')
        git('config', 'user.email', 't@t')
        git('config', 'user.name', 't')
        (repo / 'a.c').write_text('int a;\n')
        git('add', 'a.c')
        git('commit', '-qm', 'init')
        (repo / 'a.c').write_text('int a = 1;\n')
        (repo / 'new.h').write_text('#define X 1\n')
        out = json.loads(self.cli('snapshot', '--repo', str(repo), '--paths', 'a.c', 'new.h', check=True).stdout.strip().splitlines()[-1])
        data = json.loads(Path(out['evidence']).read_text())
        self.assertEqual(sorted(data), ['base_commit', 'diff', 'paths', 'repository', 'untracked_base64'])
        self.assertIn('int a = 1;', data['diff'])
        self.assertIn('new.h', data['untracked_base64'])

    def test_gate_accepts_only_records_as_evidence(self):
        oid = api.orders(api.load(self.base), type='implement', target='D1')[0]['id']
        hand = self.base / 'records' / 'handwritten.json'
        hand.write_text(json.dumps({'note': '我跑过了，通过'}))
        run = api.start(self.base, oid)
        out = api.deliver(self.base, oid, run, '结果：VERIFIED\n停止原因：完成\n验证证据：%s\n' % hand, events=[EDIT], conversation_id='c')
        self.assertFalse(out['legal'])
        real = json.loads(self.cli('record', '--', 'true', check=True).stdout.strip().splitlines()[-1])['evidence']
        out = api.deliver(self.base, oid, run, '结果：VERIFIED\n停止原因：完成\n验证证据：%s\n' % real, events=[EDIT], conversation_id='c')
        self.assertTrue(out['legal'])

    def test_cards_tell_roles_how_to_record(self):
        fake = FakeAdapter(self.base, {('implement', 'D1'): [done()]})
        r = runner.Runner(self.base, fake)
        r.step()
        card = fake.launches[0]['card']
        self.assertIn('record --cwd', card)
        self.assertIn(str(self.base), card)
        r.close()


class Doctor(Base):  # §23.2
    def test_doctor_finds_references_to_missing_skill_scripts(self):
        task = self.base.parent
        (task / 'tests').mkdir()
        (task / 'tests' / 'run.sh').write_text('FLOW_PY=".../.agents/skills/batch-flow-orchestration/scripts/flow.py"\n'
                                               'BF=".../.agents/skills/batch-flow-orchestration/scripts/bf/cli.py"\n')
        out = json.loads(self.cli('doctor', check=True).stdout)
        refs = [(Path(x['file']).name, x['line'], x['ref']) for x in out['missing']]
        self.assertEqual(refs, [('run.sh', 1, 'scripts/flow.py')])


class Watch(Base):  # §23.3 — push channel for the chat front
    def test_notable_events_and_driver_lifecycle(self):
        oid = api.orders(api.load(self.base), type='implement', target='D1')[0]['id']
        since = api.load(self.base).revision
        fake = FakeAdapter(self.base, {('implement', 'D1'): [dict(text='结果：ARCH_DECISION_REQUIRED\n停止原因：待裁决\n上升问题：冲突\n', events=[EDIT])],
                                       ('decide', 'D1'): [dict(text='结果：VERIFIED\n停止原因：完成\n',
                                                               decision={'diagnosis': 'd', 'actions': [{'grant': {'D1': {'implement': 1}}}]})],
                                       ('implement', 'D2'): [done()]})
        runner.Runner(self.base, fake).run_until_idle()
        lines = api.watch_lines(self.base, since)
        text = '\n'.join(lines)
        for piece in ['驱动器启动', 'D1', '待你决定', '驱动器退出']:
            self.assertIn(piece, text)
        self.assertTrue(all('\n' not in line for line in lines))
        out = self.cli('watch', '--since', str(since), '--once', check=True).stdout.strip().splitlines()
        self.assertEqual(out, lines)
        report = api.report(self.base)
        self.assertFalse(report['driver']['alive'])  # it exited; heartbeat file says so

    def test_ops_pause_is_notable(self):
        oid = api.orders(api.load(self.base), type='implement', target='D1')[0]['id']
        since = api.load(self.base).revision
        for _ in range(2):
            api.abandon(self.base, oid, api.start(self.base, oid), 'CLI unusable: usage limit')
        self.assertTrue(any('运维' in line for line in api.watch_lines(self.base, since)))


class Stop(Base):  # §23.4 — user stop has priority (I6)
    def test_stop_blocks_continuations_and_resume_continues(self):
        fake = FakeAdapter(self.base, {('implement', 'D1'): [dict(text=UNFINISHED, events=[EDIT]), done()],
                                       ('implement', 'D2'): [done()], ('review', 'R12'): [release()],
                                       ('implement', 'D3'): [done()], ('review', 'R3'): [release()]})
        r = runner.Runner(self.base, fake)
        r.step()                       # launches D1
        api.stop(self.base)
        r.run_until_idle()             # collects D1's illegal delivery but must not relaunch it
        self.assertEqual(len(fake.launches), 1)
        api.resume(self.base)
        runner.Runner(self.base, fake).run_until_idle()
        self.assertEqual(fake.launches[1]['target'], 'D1')
        self.assertIn('尚未合法结束', fake.launches[1]['card'])  # the paused continuation resumes
        run = api.orders(api.load(self.base), type='implement', target='D1')[0]['runs'][0]
        self.assertEqual((run['result'], run['segments']), ('VERIFIED', 2))

    def test_stop_now_kills_in_flight_calls(self):
        fake = FakeAdapter(self.base, {('implement', 'D1'): [dict(hang=True)]})
        r = runner.Runner(self.base, fake)
        r.step()
        api.stop(self.base, now=True)
        r.run_until_idle()             # returns instead of waiting on the hanging call
        self.assertEqual(fake.killed, ['h1'])
        run = api.orders(api.load(self.base), type='implement', target='D1')[0]['runs'][0]
        self.assertEqual((run['result'], run['exempt']), ('INVALID', True))
        self.assertIn('用户', run['stop_reason'])
        self.assertTrue(api.load(self.base).stopped)

    def test_cli_stop_now(self):
        self.cli('stop', '--now', check=True)
        st = api.load(self.base)
        self.assertTrue(st.stopped)


class FrontShell(Base):  # §23.5
    def decide(self, command):
        return front.hook_decision({'tool_name': 'Bash', 'tool_input': {'command': command}, 'cwd': str(self.base)}, self.base)['decision']

    def test_read_only_pipelines_allowed(self):
        bf = '%s %s --base %s' % (sys.executable, HERE / 'bf' / 'cli.py', self.base)
        for c in ['ls -lt %s/records/runs | head -5' % self.base, 'cd %s && ls' % self.base, 'ps -ef', 'pgrep -af cli.py',
                  'grep -n x %s | wc -l' % (self.base / 'events.jsonl'), bf + ' stop --now', bf + ' watch --since 3',
                  bf + ' doctor', bf + ' record -- true']:
            self.assertEqual(self.decide(c), 'allow', c)
        for c in ['cat a | sh', 'ls | xargs rm', 'C="x"; ls', 'ls > /tmp/x', 'cd /tmp && rm -rf x', 'ls $(pwd)', 'kill 123']:
            self.assertEqual(self.decide(c), 'deny', c)

    def test_monitor_and_cron_for_push_and_reports(self):
        for tool in ['Monitor', 'CronCreate', 'CronList', 'CronDelete']:
            d = front.hook_decision({'tool_name': tool, 'tool_input': {}, 'cwd': str(self.base)}, self.base)['decision']
            self.assertEqual(d, 'allow', tool)
        prompt = front.prompt(str(self.base))
        for piece in ['Monitor', 'watch', 'CronCreate', 'stop --now']:
            self.assertIn(piece, prompt)


if __name__ == '__main__':
    unittest.main()
