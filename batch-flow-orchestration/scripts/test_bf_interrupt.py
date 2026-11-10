"""Real subprocess cancellation tests; no model calls."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from bf.adapter import AgentCliAdapter


class InterruptTests(unittest.TestCase):
    def run_case(self, ignore=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'cli.py').write_text('''import os, signal, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
if sys.argv[2] == 'ignore':
    signal.signal(signal.SIGINT, signal.SIG_IGN)
else:
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    (root / 'child').write_text(str(child.pid))
    def cancel(sig, frame):
        (root / 'interrupted').write_text(str(sig))
        child.terminate()
        child.wait()
        sys.exit(1)
    signal.signal(signal.SIGINT, cancel)
(root / 'ready').write_text('ready')
time.sleep(60)
''')
            (root / 'supervisor.py').write_text('''import json, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
p = subprocess.Popen([sys.executable, str(root / 'cli.py'), str(root), sys.argv[2]])
(root / 'receipt.json').write_text(json.dumps({'event': 'CLI_STARTED', 'pid': p.pid}))
code = p.wait()
(root / 'collected').write_text(str(code))
''')
            proc = subprocess.Popen([sys.executable, str(root / 'supervisor.py'), tmp,
                                     'ignore' if ignore else 'cleanup'], start_new_session=True)
            adapter = AgentCliAdapter(tmp, {})
            adapter.procs[proc.pid] = proc
            try:
                deadline = time.monotonic() + 5
                while not (root / 'ready').exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((root / 'ready').exists())
                adapter.kill({'pid': proc.pid, 'dir': tmp}, grace=.3 if ignore else 2)
                self.assertIsNotNone(proc.poll())
                if not ignore:
                    self.assertEqual((root / 'interrupted').read_text(), str(int(signal.SIGINT)))
                    self.assertEqual((root / 'collected').read_text(), '1')
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int((root / 'child').read_text()), 0)
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                if (root / 'child').exists():
                    try:
                        os.kill(int((root / 'child').read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_interrupt_cleans_detached_tool_and_preserves_supervisor_collection(self):
        self.run_case()

    def test_unresponsive_cli_uses_forced_fallback(self):
        self.run_case(ignore=True)


if __name__ == '__main__':
    unittest.main()
