"""AGY native wakeups and bound hash-pinned observers; no models or timers run."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from bf import front, front_hosts


class ObserverGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.task = Path(self.tmp.name) / 'task'
        self.base = self.task / '.batch-flow'
        self.base.mkdir(parents=True)
        self.runtime = self.base / 'front' / 'agy'
        self.runtime.mkdir(parents=True)
        self.script = self.task / 'observer.py'
        self.script.write_text('print("read-only observer")\n')
        self.argv = [sys.executable, '-I', str(self.script), 'status']
        self.row = {'argv': self.argv, 'sha256': hashlib.sha256(self.script.read_bytes()).hexdigest()}
        self.save([self.row])

    def save(self, rows):
        (self.base / 'plan.json').write_text(json.dumps({'front': {'readonly_commands': rows}}))

    def hook(self, name, args):
        return front_hosts.hook({'toolCall': {'name': name, 'args': args}},
                                self.base, 'agy', self.runtime)['decision']

    def test_native_agy_schedule_and_serialized_event(self):
        for args in ({'DurationSeconds': 60, 'Prompt': 'check bound task', 'TimerCondition': 'never'},
                     {'CronExpression': '*/5 * * * *', 'Prompt': 'check bound task', 'IsDaemon': False}):
            self.assertEqual(self.hook('schedule', args), 'allow')
            self.assertEqual(self.hook('schedule', json.dumps(args)), 'allow')
        event = {'toolCall': {'name': 'schedule', 'args': {'DurationSeconds': 30, 'Prompt': 'check'}}}
        command = [sys.executable, str(Path(front.__file__)), 'hook', '--base', str(self.base),
                   '--backend', 'agy', '--runtime', str(self.runtime)]
        result = subprocess.run(command, input=json.dumps(event), text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)['decision'], 'allow')

    def test_schedule_rejects_execution_payload_and_daemon(self):
        bad = [{}, {'Prompt': 'x'}, {'DurationSeconds': True, 'Prompt': 'x'},
               {'DurationSeconds': -1, 'Prompt': 'x'}, {'DurationSeconds': float('inf'), 'Prompt': 'x'},
               {'DurationSeconds': 1, 'CronExpression': '* * * * *', 'Prompt': 'x'},
               {'CronExpression': 'invalid', 'Prompt': 'x'},
               {'CronExpression': '* * * * *', 'Prompt': 'x', 'IsDaemon': True},
               {'DurationSeconds': 1, 'Prompt': 'x', 'CommandLine': 'rm -rf /'},
               {'DurationSeconds': 1, 'Prompt': 'x', 'ConversationId': 'other'},
               {'DurationSeconds': 1, 'Prompt': ''}]
        for args in bad:
            self.assertEqual(self.hook('schedule', args), 'deny', args)
        self.assertEqual(self.hook('unknown_schedule_tool', {'Prompt': 'x'}), 'deny')

    def test_only_exact_approved_observer_arguments(self):
        self.assertEqual(self.hook('run_command', {'CommandLine': shlex.join(self.argv)}), 'allow')
        for args in (self.argv + ['--background'], self.argv[:-1] + ['grant'],
                     [sys.executable, str(self.script), 'status'],
                     [sys.executable, '-c', 'print(1)']):
            self.assertEqual(self.hook('run_command', {'CommandLine': shlex.join(args)}), 'deny', args)
        self.assertEqual(self.hook('run_command', {'CommandLine': shlex.join(self.argv) + '; rm x'}), 'deny')

    def test_changed_script_foreign_task_and_symlink_denied(self):
        self.script.write_text('print("changed")\n')
        self.assertFalse(front.allowed(shlex.join(self.argv), bound_base=self.base))
        self.row['sha256'] = hashlib.sha256(self.script.read_bytes()).hexdigest()
        other = Path(self.tmp.name) / 'foreign.py'
        other.write_text(self.script.read_text())
        argv = self.argv[:2] + [str(other), 'status']
        self.save([dict(self.row, argv=argv)])
        self.assertFalse(front.allowed(shlex.join(argv), bound_base=self.base))
        self.script.unlink()
        self.script.symlink_to(other)
        self.save([self.row])
        self.assertFalse(front.allowed(shlex.join(self.argv), bound_base=self.base))

    def test_missing_or_malformed_approval_denied(self):
        for data in ({}, {'front': []}, {'front': {'readonly_commands': [None]}},
                     {'front': {'readonly_commands': {'argv': self.argv}}}):
            (self.base / 'plan.json').write_text(json.dumps(data))
            self.assertFalse(front.allowed(shlex.join(self.argv), bound_base=self.base))


if __name__ == '__main__':
    unittest.main()
