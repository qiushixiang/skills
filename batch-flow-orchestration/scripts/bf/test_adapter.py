#!/usr/bin/env python3
"""Unit test of bf.adapter against a fake agent_cli-shaped script (no model or real CLI)."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bf import adapter, api  # noqa: E402
from test_bf_core import make_plan  # noqa: E402

FAKE = '''
import json, sys
from pathlib import Path
a = sys.argv[1:]
def opt(n):
    return a[a.index(n) + 1] if n in a else None
if a[0] == 'run':
    d = Path(opt('--run-dir')); d.mkdir()
    ev = [{'type': 'item.completed', 'item': {'type': 'file_change'}}, {'type': 'turn.completed'}]
    (d / 'stdout').write_text('\\n'.join(json.dumps(e) for e in ev))
    (d / 'argv.json').write_text(json.dumps(a))
    (d / 'exit.json').write_text('{"returncode": 0}')
else:
    d = Path(opt('--run-dir'))
    print(json.dumps({'usable': True, 'conversation_id': 'sess-' + d.name, 'response': '结果：FAILED'}))
'''


class AdapterSpec(unittest.TestCase):
    def test_launch_poll_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / '.batch-flow'
            (base / 'records').mkdir(parents=True)
            approval = Path(tmp) / 'a.txt'
            approval.write_text('ok')
            api.init(base, make_plan(workspace=tmp), approval)
            fake = Path(tmp) / 'fake_agent_cli.py'
            fake.write_text(FAKE)
            ad = adapter.AgentCliAdapter(base, {'codex': 'p.json'}, agent_cli=fake)
            order = api.orders(api.load(base), type='implement', target='D1')[0]
            assignment = api.assign(api.load(base), order['id'])
            card = Path(tmp) / 'card.md'
            card.write_text('x')
            run_dir = base / 'records' / 'runs' / 'r1'
            run_dir.mkdir(parents=True)
            handle = ad.launch(order, assignment, card, run_dir)
            res = None
            for _ in range(100):
                res = ad.poll(handle)
                if res:
                    break
                time.sleep(0.05)
            self.assertEqual(res['conversation_id'], 'sess-cli-1')
            self.assertEqual(res['text'], '结果：FAILED')
            self.assertEqual(res['events'], [{'type': 'file_change'}])
            (run_dir / 'decision.json').write_text('{"diagnosis": "d", "actions": []}')
            handle = ad.launch(order, assignment, card, run_dir, resume='sess-cli-1')
            argv = json.loads((Path(handle['dir']) / 'argv.json').read_text()) if (Path(handle['dir']) / 'argv.json').exists() else None
            for _ in range(100):
                res = ad.poll(handle)
                if res:
                    break
                time.sleep(0.05)
            argv = json.loads((Path(handle['dir']) / 'argv.json').read_text())
            self.assertIn('--resume-from', argv)
            self.assertEqual(argv[argv.index('--conversation') + 1], 'sess-cli-1')
            self.assertEqual(res['decision']['diagnosis'], 'd')

    def test_profiles_probed_once_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / '.batch-flow'
            (base / 'records').mkdir(parents=True)
            approval = Path(tmp) / 'a.txt'
            approval.write_text('ok')
            api.init(base, make_plan(workspace=tmp), approval)
            log = Path(tmp) / 'probes.log'
            fake = Path(tmp) / 'fake_probe.py'
            fake.write_text("import sys\nfrom pathlib import Path\na = sys.argv[1:]\n"
                            "open(%r, 'a').write(a[a.index('--backend') + 1] + '\\n')\n"
                            "Path(a[a.index('--profile') + 1]).write_text('{}')\n" % str(log))
            first = adapter.ensure_profiles(base, fake)
            self.assertEqual(sorted(first), ['claude', 'codex', 'opencode'])
            self.assertEqual(sorted(log.read_text().split()), ['claude', 'codex', 'opencode'])
            self.assertEqual(adapter.ensure_profiles(base, fake), first)
            self.assertEqual(len(log.read_text().split()), 3)  # reused, no second probe
            override = adapter.ensure_profiles(base, fake, {'codex': '/x/c.json'})
            self.assertEqual(override['codex'], '/x/c.json')


if __name__ == '__main__':
    unittest.main()
