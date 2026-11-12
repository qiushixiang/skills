"""Codex/AGY front contract tests: actual launcher/hook subprocesses, no model invocation."""
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bf import api, front, front_hosts
from test_bf_front import Base
from test_bf_workflow import FAKE_CLI

HERE = Path(__file__).resolve().parent

FAKE_FRONT = r'''
import json, os, re, shlex, subprocess, sys
from pathlib import Path
a = sys.argv[1:]
if BACKEND == 'codex':
    config = next(a[i+1] for i,x in enumerate(a) if x=='-c' and a[i+1].startswith('hooks.PreToolUse='))
    hook = json.loads(re.search(r'command=("(?:\\.|[^"\\])*")',config).group(1))
else:
    hook = json.loads((Path.cwd()/'.agents/hooks.json').read_text())['batch-flow-front']['PreToolUse'][0]['hooks'][0]['command']
hook_argv = shlex.split(hook)
ready_before = (Path(hook_argv[hook_argv.index('--runtime')+1])/'ready.json').exists()
def invoke(name, args):
    event = ({'tool_name': name, 'tool_input': args, 'session_id': 'codex-test', 'cwd': str(Path.cwd())}
             if BACKEND == 'codex' else {'toolCall':{'name':name,'args':args}, 'conversationId':'agy-test','modelName':'Gemini 4 Argon'})
    call = subprocess.run(shlex.split(hook),input=json.dumps(event),capture_output=True,text=True,check=True)
    result = json.loads(call.stdout)
    return result.get('decision') if BACKEND=='agy' else result['hookSpecificOutput']['permissionDecision']
def shell(command):
    return invoke('Bash' if BACKEND=='codex' else 'run_command',
                  {'command':command} if BACKEND=='codex' else {'CommandLine':command})
read = shell(COMMAND)
if read == 'allow':
    report = subprocess.run(shlex.split(COMMAND),check=True,capture_output=True,text=True).stdout
target = str(Path(BASE)/'answers'/'test.txt')
if BACKEND=='codex':
    answer = invoke('apply_patch',{'command':'*** Begin Patch\n*** Add File: '+target+'\n+test answer\n*** End Patch'})
    source = invoke('apply_patch',{'command':'*** Begin Patch\n*** Add File: '+str(Path(SOURCE)/'bad.py')+'\n+bad\n*** End Patch'})
else:
    answer = invoke('write_file',{'Path':target,'Content':'test answer'})
    source = invoke('write_file',{'Path':str(Path(SOURCE)/'bad.py'),'Content':'bad'})
if answer=='allow':
    Path(target).write_text('test answer')
foreign = shell(COMMAND.replace(BASE, str(Path(BASE).parent/'another-task'/'.batch-flow')))
Path(CAPTURE).write_text(json.dumps({'read':read,'answer':answer,'source':source,'foreign':foreign,
                                    'argv':a,'cwd':str(Path.cwd()), 'report':report,'ready_before':ready_before}))
sys.exit(23)
'''


class Hosts(Base):
    def test_agy_file_tool_aliases_share_answer_policy(self):
        runtime = self.root/'agy-files'
        runtime.mkdir()
        answers = self.base/'answers'
        answers.mkdir()
        existing = answers/'existing.txt'
        existing.write_text('original')
        (answers/'escape').symlink_to(self.workspace, target_is_directory=True)
        for tool, key in (('write_file', 'Path'), ('write_to_file', 'TargetFile')):
            cases = [(str(answers/'new.txt'), 'allow'),
                     (str(existing), 'deny'),
                     (str(self.workspace/'bad.py'), 'deny'),
                     (str(answers/'..'/'plan.json'), 'deny'),
                     (str(answers/'escape'/'bad.py'), 'deny'),
                     ('', 'deny')]
            for path, expected in cases:
                for serialized in (False, True):
                    with self.subTest(tool=tool, path=path, serialized=serialized):
                        args = {key: path}
                        event = {'toolCall': {'name': tool, 'args': json.dumps(args) if serialized else args}}
                        self.assertEqual(front_hosts.hook(event, self.base, 'agy', runtime)['decision'], expected)
        for tool in ('read_file', 'view_file'):
            event = {'toolCall': {'name': tool, 'args': {'Path': str(existing)}}}
            self.assertEqual(front_hosts.hook(event, self.base, 'agy', runtime)['decision'], 'allow')
        event = {'toolCall': {'name': 'unknown_file_tool', 'args': {'Path': str(answers/'new.txt')}}}
        self.assertEqual(front_hosts.hook(event, self.base, 'agy', runtime)['decision'], 'deny')
        self.assertEqual(existing.read_text(), 'original')

    def test_argon_short_name_is_replaced_in_the_actual_launch_command(self):
        for name in ('argon', 'Gemini 4 Argon', 'gemini-4-argon'):
            call = subprocess.run([str(HERE/'start-batch-flow.sh'),str(self.base.parent),'--backend','agy',
                                   '--model',name,'--effort','high','--binary','/missing/should-not-run','--dry-run'],
                                  capture_output=True,text=True,check=True)
            spec = json.loads(call.stdout)
            self.assertEqual(spec['requested_model'], name)
            self.assertEqual(spec['model'], 'gemini-4-argon')
            self.assertEqual(spec['argv'][spec['argv'].index('--model')+1], 'gemini-4-argon')
            self.assertEqual(spec['argv'][spec['argv'].index('--effort')+1], 'high')

    def test_other_short_names_resolve_uniquely_from_metadata(self):
        import model_catalog
        data = 'claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n'
        with patch('agent_cli.version', return_value='test'), patch.object(model_catalog.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, data, '')):
            self.assertEqual(front_hosts.agy_model('opus', '/fake/agy'), 'claude-opus-4-6-thinking')

    def test_ambiguous_short_names_and_unknown_names_do_not_choose_a_model(self):
        import model_catalog
        data = 'gemini-3.8-flash-high\tGemini 3.8 Flash (High)\ngemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n'
        with patch('agent_cli.version', return_value='test'), patch.object(model_catalog.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, data, '')):
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                front_hosts.agy_model('flash', '/fake/agy')
            with self.assertRaisesRegex(ValueError, 'not found'):
                front_hosts.agy_model('typo', '/fake/agy')

    def test_hook_preserves_native_reported_model(self):
        runtime = self.root/'reported-model'
        runtime.mkdir()
        event = {'toolCall': {'name': 'view_file', 'args': {}}, 'conversationId': 'test', 'modelName': 'Gemini 4 Argon'}
        result = front_hosts.hook(event, self.base, 'agy', runtime)
        self.assertEqual(result['decision'], 'allow')
        self.assertEqual(json.loads((runtime/'ready.json').read_text())['model'], 'Gemini 4 Argon')

    def test_both_native_fronts_load_guard_and_operate_only_on_bound_task(self):
        shortcut = self.root/'start-batch-flow.sh'
        shortcut.symlink_to(HERE/'start-batch-flow.sh')
        for backend in ('codex', 'agy'):
            with self.subTest(backend=backend):
                answer = self.base / 'answers' / 'test.txt'
                if answer.exists():
                    answer.unlink()
                capture = self.root / ('%s.json' % backend)
                binary = self.root / ('fake ' + backend)
                command = __import__('shlex').join([sys.executable, str(HERE/'bf/cli.py'), '--base', str(self.base), 'report', '--text'])
                binary.write_text('#!%s\nBACKEND=%r\nCOMMAND=%r\nBASE=%r\nSOURCE=%r\nCAPTURE=%r\n' % (
                    sys.executable, backend, command, str(self.base), str(self.workspace), str(capture)) + FAKE_FRONT)
                binary.chmod(0o755)
                runtime = self.base/'front'/backend
                runtime.mkdir(parents=True)
                (runtime/'ready.json').write_text('{"session":"stale"}')
                before_revision = api.load(self.base).revision
                chosen = 'argon' if backend=='agy' else 'chosen-model'
                call = subprocess.run([str(shortcut),str(self.base.parent),'--backend',backend,'--binary',str(binary),
                                       '--model',chosen,'--effort','high'],cwd=self.root,
                                      capture_output=True,text=True,timeout=10)
                self.assertEqual(call.returncode,23,call.stderr)
                result = json.loads(capture.read_text())
                self.assertFalse(result['ready_before'])
                self.assertEqual(api.load(self.base).revision,before_revision)
                self.assertEqual([result[k] for k in ('read', 'answer', 'source', 'foreign')], ['allow', 'allow', 'deny', 'deny'])
                self.assertIn('正在做', result['report'])
                self.assertEqual(answer.read_text(), 'test answer')
                self.assertFalse((self.workspace/'bad.py').exists())
                argv = result['argv']
                self.assertEqual(argv[argv.index('--model')+1], 'gemini-4-argon' if backend=='agy' else 'chosen-model')
                self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', argv)
                self.assertNotIn('--dangerously-skip-permissions', argv)
                if backend=='agy':
                    self.assertIn('--prompt-interactive', argv)
                    self.assertNotIn('-p', argv)
                    self.assertIn('run --background', (Path(result['cwd'])/'AGENTS.md').read_text())
                else:
                    self.assertNotIn('--settings', argv)
                    self.assertIn('--no-alt-screen', argv)

    def test_prompt_uses_launcher_notifications_instead_of_claude_tools(self):
        for backend in ('codex', 'agy'):
            text = front.prompt(self.base, backend=backend, runtime=self.root)
            self.assertIn('notifications.log', text)
            self.assertIn('不会自动唤醒', text)
            self.assertIn('run --background', text)
            self.assertNotIn('用 Monitor 工具', text)
            if backend == 'agy':
                for field in ('schedule', 'DurationSeconds', 'CronExpression', 'IsDaemon', '创建成功'):
                    self.assertIn(field, text)

    def test_dry_run_previews_both_fronts_without_executing_binary(self):
        for backend in ('codex','agy'):
            call = subprocess.run([str(HERE/'start-batch-flow.sh'),str(self.base.parent),'--backend',backend,
                                   '--binary','/missing/should-not-run','--dry-run'],
                                  capture_output=True,text=True,check=True)
            spec = json.loads(call.stdout)
            self.assertEqual(spec['backend'],backend)
            self.assertEqual(spec['argv'][0],'/missing/should-not-run')
            self.assertFalse((Path(spec['runtime'])/'ready.json').exists())

    def test_codex_patch_cannot_edit_answers_or_escape_through_symlink(self):
        runtime = self.root/'runtime'
        runtime.mkdir()
        answers = self.base/'answers'
        answers.mkdir()
        (answers/'escape').symlink_to(self.workspace, target_is_directory=True)
        for command in ('*** Begin Patch\n*** Update File: %s\n@@\n-old\n+new\n*** End Patch' % (answers/'a.txt'),
                        '*** Begin Patch\n*** Add File: %s\n+bad\n*** End Patch' % (answers/'escape'/'bad.py')):
            result = front_hosts.hook({'tool_name':'apply_patch','tool_input':{'command':command}}, self.base,'codex',runtime)
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_unbound_native_hook_is_not_reported_as_ready(self):
        runtime = self.root/'unbound'
        runtime.mkdir()
        spec = {'argv':[sys.executable, '-c', 'import time; time.sleep(30)'], 'cwd':str(self.root), 'runtime':str(runtime)}
        started = time.monotonic()
        self.assertEqual(front_hosts.run(spec, self.base, bind_timeout=0.01), 2)
        self.assertLess(time.monotonic()-started, 5)

    def test_native_hook_errors_return_parseable_denial(self):
        for backend in ('codex','agy'):
            event = ({'tool_name':'Bash','tool_input':{'command':'cat /tmp/x'},'session_id':'test'} if backend=='codex' else
                     {'toolCall':{'name':'run_command','args':{'CommandLine':'cat /tmp/x'}},'conversationId':'test'})
            # The missing runtime causes a real I/O error while registering readiness.
            call = subprocess.run([sys.executable,str(HERE/'bf/front.py'),'hook','--backend',backend,'--base',str(self.base),
                                   '--runtime',str(self.root/'missing')],input=json.dumps(event),capture_output=True,text=True,check=True)
            result = json.loads(call.stdout)
            decision = result['decision'] if backend=='agy' else result['hookSpecificOutput']['permissionDecision']
            self.assertEqual(decision,'deny')

    def test_terminal_report_and_events_do_not_require_model_turns(self):
        runtime = self.root/'notifications'
        runtime.mkdir()
        stream = io.StringIO()
        monitor = front_hosts.Notifications(self.base, runtime, stream, interval=600)
        api.stop(self.base)
        monitor.tick(at=monitor.next_report-1)
        self.assertIn('已停止', stream.getvalue())
        self.assertNotIn('角色：', stream.getvalue())
        monitor.tick(at=monitor.next_report)
        self.assertIn('角色：', stream.getvalue())
        self.assertEqual((runtime/'notifications.log').read_text(), stream.getvalue())
        before = stream.getvalue()
        monitor.tick(at=monitor.next_report-1)
        self.assertEqual(stream.getvalue(), before)

    def test_active_front_keeps_events_and_model_reports_out_of_tui_streams(self):
        runtime = self.root/'quiet-front'
        runtime.mkdir()
        child_code = '''
import json, subprocess, sys, time
from pathlib import Path
Path(sys.argv[1], 'ready.json').write_text(json.dumps({'session':'test','model':'native-model'}))
subprocess.run([sys.executable, sys.argv[2], '--base', sys.argv[3], 'stop'],
               check=True, capture_output=True)
time.sleep(1.5)
print('TUI ONLY')
'''
        spec = {'runtime':str(runtime), 'cwd':str(self.root),
                'argv':[sys.executable, '-c', child_code, str(runtime), str(HERE/'bf/cli.py'), str(self.base)]}
        launcher = 'import json,sys; from pathlib import Path; from bf.front_hosts import run; sys.exit(run(json.loads(sys.argv[1]),Path(sys.argv[2])))'
        call = subprocess.run([sys.executable, '-c', launcher, json.dumps(spec), str(self.base)],
                              cwd=HERE, capture_output=True, text=True, timeout=10)
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertEqual(call.stdout, 'TUI ONLY\n')
        self.assertEqual(call.stderr, '')
        log = (runtime/'notifications.log').read_text()
        self.assertIn('已停止', log)
        self.assertIn('宿主报告模型：native-model', log)

    def test_default_notifications_and_errors_only_write_log(self):
        runtime = self.root/'quiet-notifications'
        runtime.mkdir()
        monitor = front_hosts.Notifications(self.base, runtime)
        with patch('sys.stdout', new_callable=io.StringIO) as stdout, patch('sys.stderr', new_callable=io.StringIO) as stderr:
            api.stop(self.base)
            monitor.tick(at=monitor.next_report)
            with patch.object(monitor.stop, 'wait', side_effect=[False, True]), patch.object(monitor, 'tick', side_effect=ValueError('broken event')):
                monitor.run()
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual(stderr.getvalue(), '')
        log = (runtime/'notifications.log').read_text()
        self.assertIn('已停止', log)
        self.assertIn('角色：', log)
        self.assertIn('notification error: broken event', log)

    def test_background_driver_returns_then_runs_with_fake_backend(self):
        backend = self.root/'fake_agent_cli.py'
        backend.write_text('SCRIPTS=%r\n' % str(HERE) + FAKE_CLI)
        call = subprocess.run([sys.executable,str(HERE/'bf/cli.py'),'--base',str(self.base),
                               'run','--background','--agent-cli',str(backend)],capture_output=True,text=True,check=True,timeout=10)
        self.assertTrue(json.loads(call.stdout)['started'])
        deadline = time.monotonic()+20
        report = None
        while time.monotonic()<deadline:
            try:
                report = json.loads((self.base/'driver.log').read_text().splitlines()[-1])
            except (OSError, ValueError, IndexError):
                pass
            if report and report.get('driver',{}).get('exited'):
                break
            time.sleep(0.05)
        self.assertTrue(report and report.get('driver',{}).get('exited'), (self.base/'driver.log').read_text())
        self.assertEqual(len(report['ask_user']), 1)


if __name__ == '__main__':
    unittest.main()
