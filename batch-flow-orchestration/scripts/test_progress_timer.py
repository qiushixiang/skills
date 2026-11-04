#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('progress_timer.py')


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ticket = Path(self.tmp.name)/'round-1.json'
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=5)
        self.tmp.cleanup()

    def start(self, seconds='30'):
        child = subprocess.Popen([sys.executable, str(SCRIPT), 'wait', '--ticket', str(self.ticket),
                                  '--task', 'B01.E', '--goal', '核验SDK并编译必要部件', '--handle', 'agent-1',
                                  '--seconds', seconds], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        first = json.loads(child.stdout.readline())
        return child, first

    def cancel(self):
        return subprocess.run([sys.executable, str(SCRIPT), 'cancel', '--ticket', str(self.ticket)],
                              capture_output=True, text=True, timeout=5)

    def test_expiry_exits_and_reports_goal_and_handle(self):
        child, first = self.start('0.05')
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, err)
        last = json.loads(out)
        self.assertEqual(first['event'], 'TIMER_STARTED')
        self.assertEqual(last['event'], 'PROGRESS_DUE')
        self.assertEqual((last['task'], last['goal'], last['handle']), ('B01.E', '核验SDK并编译必要部件', 'agent-1'))
        self.assertGreaterEqual(last['elapsed_seconds'], 0.05)
        self.assertEqual(json.loads(self.ticket.read_text())['status'], 'due')
        self.assertEqual(json.loads(self.cancel().stdout)['event'], 'TIMER_ALREADY_DUE')

    def test_task_completion_cancels_timer_without_due_report(self):
        child, _ = self.start()
        result = self.cancel()
        self.assertEqual(result.returncode, 0, result.stderr)
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(json.loads(out)['event'], 'TIMER_CANCELLED')
        self.assertNotIn('PROGRESS_DUE', out)
        self.assertEqual(json.loads(self.cancel().stdout)['event'], 'TIMER_CANCELLED')

    def test_duplicate_ticket_rejected_without_cancelling_original(self):
        first, _ = self.start()
        second, event = self.start('0.01')
        second.communicate(timeout=5)
        self.assertEqual(second.returncode, 1)
        self.assertEqual(event['event'], 'TIMER_ERROR')
        self.assertIsNone(first.poll())
        self.cancel(); first.communicate(timeout=5)

    def test_parallel_timers_cancel_independently(self):
        first, _ = self.start()
        first_ticket = self.ticket
        self.ticket = first_ticket.with_name('round-2.json')
        second, _ = self.start()
        self.cancel(); second.communicate(timeout=5)
        self.assertIsNone(first.poll())
        self.ticket = first_ticket
        self.cancel(); first.communicate(timeout=5)

    def test_invalid_interval_and_unrelated_ticket_are_not_modified(self):
        for value in ['0', '-1', 'nan', 'inf']:
            child, event = self.start(value)
            child.communicate(timeout=5)
            self.assertEqual(child.returncode, 1)
            self.assertEqual(event['event'], 'TIMER_ERROR')
            self.assertFalse(self.ticket.exists())
        self.ticket.write_text('{"unrelated":"data"}')
        before = self.ticket.read_bytes()
        self.assertNotEqual(self.cancel().returncode, 0)
        self.assertEqual(self.ticket.read_bytes(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
