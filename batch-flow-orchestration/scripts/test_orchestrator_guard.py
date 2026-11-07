#!/usr/bin/env python3
"""Unit tests for the Claude Code orchestrator PreToolUse guard (no host calls)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
GUARD = HERE / 'orchestrator_guard.py'
SID = '11111111-2222-3333-4444-555555555555'


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / 'task'
        self.mgmt = self.base / '.batch-flow'
        self.mgmt.mkdir(parents=True)
        (self.mgmt / 'flow.json').write_text('{}')
        (self.base / 'design.md').write_text('x')
        self.src = Path(self.tmp.name) / 'src'
        self.src.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_guard(self, tool, tool_input, cards=False, **extra):
        event = dict(session_id=SID, cwd=str(self.base), hook_event_name='PreToolUse',
                     tool_name=tool, tool_input=tool_input)
        event.update(extra)
        proc = subprocess.run([sys.executable, str(GUARD), '--host', 'claude', '--conversation', SID,
                               '--management-root', str(self.mgmt), '--read-root', str(self.base),
                               '--agent-model', 'sonnet', '--agent-model', 'opus',
                               '--agent-type', 'bf-executor', '--agent-type', 'general-purpose',
                               '--lead-agent-type', 'bf-architect', '--worker-agent-type', 'bf-executor', '--worker-agent-type', 'bf-reviewer']
                              + (['--card-root', str(self.mgmt / 'records' / 'dispatch')] if cards else [])
                              + [x for p in getattr(self, 'switch', []) for x in ('--account-switch', p)],
                              input=json.dumps(event), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if not proc.stdout.strip():
            return None
        out = json.loads(proc.stdout)['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'deny')
        return out['permissionDecisionReason']

    def assertAllowed(self, *a, **k):
        self.assertIsNone(self.run_guard(*a, **k))

    def assertDenied(self, *a, **k):
        self.assertIn('ROLE_BOUNDARY_BLOCKED', self.run_guard(*a, **k) or '')

    def test_management_scripts_allowed(self):
        self.assertAllowed('Bash', {'command': 'python3 %s --store %s status' % (HERE / 'flow.py', self.mgmt / 'flow.json')})
        self.assertAllowed('Bash', {'command': 'python3 %s wait --ticket /x --goal "S3 (repair)"' % (HERE / 'progress_timer.py'),
                                    'run_in_background': True})

    def test_configured_account_switch(self):
        swap = Path(self.tmp.name) / 'bin' / 'codex-swap'
        command = lambda *a: {'command': ' '.join([str(swap), *a])}
        self.assertDenied('Bash', command('-k'))  # not configured for this launch
        self.switch = [str(swap)]
        for args in [(), ('-k',), ('-n',), ('-k', 'user@example.com'), ('user@example.com', '-k')]:
            self.assertAllowed('Bash', command(*args))
        for args in [('-K',), ('-k', '-k'), ('--foo',), ('a@b.c', 'd@e.f'), ('not-an-account',), ('-k', '../x')]:
            self.assertDenied('Bash', command(*args))
        for raw in ['codex-swap -k', '%s -k; make' % swap, '%s -k | tee x' % swap, 'bash %s -k' % swap,
                    str(swap.parent / 'other') + ' -k']:
            self.assertDenied('Bash', {'command': raw})

    def test_product_and_dynamic_shell_denied(self):
        for command in ['make -j8', 'bash tests/run_board_test.sh s1', 'git commit -m x',
                        'python3 -c "print(1)"', 'python3 %s --store f snapshot' % (HERE / 'flow.py'),
                        'python3 %s status | tee x' % (HERE / 'flow.py'), 'cat $(ls)', 'sudo -n /usr/local/sbin/ec-board-net up',
                        'cat %s' % (self.src / 'main.c')]:
            with self.subTest(command=command):
                self.assertDenied('Bash', {'command': command})

    def test_read_scope(self):
        self.assertAllowed('Read', {'file_path': str(self.base / 'design.md')})
        self.assertAllowed('Read', {'file_path': str(HERE / 'flow.py')})
        self.assertAllowed('Bash', {'command': 'ls ' + str(self.mgmt)})
        self.assertDenied('Read', {'file_path': str(self.src / 'main.c')})
        self.assertDenied('Grep', {'pattern': 'x', 'path': str(self.src)})
        self.assertDenied('Read', {'file_path': str(self.base / '..' / 'src' / 'main.c')})

    def test_writes(self):
        self.assertAllowed('Write', {'file_path': str(self.mgmt / 'receipts' / 'A1.txt'), 'content': 'x'})
        self.assertDenied('Write', {'file_path': str(self.mgmt / 'flow.json'), 'content': '{}'})
        self.assertDenied('Write', {'file_path': str(self.base / 'design.md'), 'content': 'x'})
        self.assertDenied('Write', {'file_path': str(self.src / 'main.c'), 'content': 'x'})
        (self.mgmt / 'old.txt').write_text('x')
        self.assertDenied('Write', {'file_path': str(self.mgmt / 'old.txt'), 'content': 'y'})
        self.assertDenied('Edit', {'file_path': str(self.mgmt / 'old.txt'), 'old_string': 'x', 'new_string': 'y'})

    def test_agent_dispatch(self):
        self.assertAllowed('Agent', {'subagent_type': 'general-purpose', 'model': 'sonnet', 'prompt': 'p', 'description': 'd'})
        self.assertDenied('Agent', {'subagent_type': 'fork', 'prompt': 'p', 'description': 'd'})
        self.assertDenied('Agent', {'subagent_type': 'Explore', 'model': 'sonnet', 'prompt': 'p', 'description': 'd'})
        self.assertDenied('Agent', {'subagent_type': 'general-purpose', 'prompt': 'p', 'description': 'd'})
        self.assertDenied('Agent', {'subagent_type': 'general-purpose', 'model': 'haiku', 'prompt': 'p', 'description': 'd'})

    def test_unknown_tools_denied_known_coordination_allowed(self):
        for tool in ['WebFetch', 'NotebookEdit', 'Workflow', 'Monitor', 'mcp__ide__executeCode']:
            self.assertDenied(tool, {})
        for tool in ['SendMessage', 'TaskStop', 'ToolSearch', 'AskUserQuestion']:
            self.assertAllowed(tool, {})
        self.assertAllowed('Skill', {'skill': 'batch-flow-orchestration'})
        self.assertDenied('Skill', {'skill': 'code-review'})

    def test_card_enforcement(self):
        cards = self.mgmt / 'records' / 'dispatch'
        prompt = 'read card X sha 1'
        cli = 'python3 %s run --profile p --task T' % (HERE / 'agent_cli.py')
        for name, call in [('c1', dict(tool='Agent', input=dict(description='d', subagent_type='bf-executor', model='sonnet', prompt=prompt, run_in_background=True))),
                           ('c2', dict(tool='Bash', input=dict(command=cli))),
                           ('c3', dict(tool='SendMessage', input=dict(to='agent-1', message=prompt)))]:
            (cards / name).mkdir(parents=True)
            (cards / name / 'card.json').write_text(json.dumps(dict(host_call=call)))
        ok = {'subagent_type': 'bf-executor', 'model': 'sonnet', 'prompt': prompt, 'description': 'd', 'run_in_background': True}
        self.assertIsNone(self.run_guard('Agent', ok, cards=True))
        self.assertIn('dispatch card', self.run_guard('Agent', dict(ok, prompt=prompt + ' and also fix X'), cards=True))
        self.assertIn('dispatch card', self.run_guard('Agent', dict(ok, model='opus'), cards=True))
        self.assertIn('dispatch card', self.run_guard('Agent', dict(ok, effort='max'), cards=True))
        for value in ['true', 'false', True]:  # all run in the background on the host, like the card
            self.assertIsNone(self.run_guard('Agent', dict(ok, run_in_background=value), cards=True))
        self.assertIn('dispatch card', self.run_guard('Agent', dict(ok, run_in_background=False), cards=True))
        (cards / 'c4').mkdir()
        (cards / 'c4' / 'card.json').write_text(json.dumps(dict(host_call=dict(tool='Agent', input=dict(ok, effort='medium')))))
        self.assertIsNone(self.run_guard('Agent', dict(ok, effort='medium'), cards=True))
        self.assertIsNone(self.run_guard('Bash', {'command': cli, 'run_in_background': True}, cards=True))
        self.assertIn('dispatch_card.py next', self.run_guard('Bash', {'command': cli + ' --add-dir /'}, cards=True))
        self.assertIsNone(self.run_guard('SendMessage', {'to': 'agent-1', 'message': prompt}, cards=True))
        self.assertIn('dispatch card', self.run_guard('SendMessage', {'to': 'agent-1', 'message': 'just do it'}, cards=True))
        self.assertIsNone(self.run_guard('Bash', {'command': 'python3 %s next --store f' % (HERE / 'dispatch_card.py')}, cards=True))
        self.assertDenied('Bash', {'command': 'python3 %s make --store f --kind EXECUTE --target T' % (HERE / 'dispatch_card.py')}, cards=True)
        self.assertIn('record', self.run_guard('Bash', {'command': 'python3 %s --store f record --cwd /x' % (HERE / 'flow.py')}, cards=True))

    def test_subagents_and_other_sessions_untouched(self):
        self.assertAllowed('Edit', {'file_path': str(self.src / 'main.c')}, agent_id='a1', agent_type='general-purpose')
        self.assertAllowed('Bash', {'command': 'make -j8'}, agent_id='a1', agent_type='general-purpose')
        self.assertAllowed('Bash', {'command': 'make -j8'}, session_id='other')

    def test_lead_subagent_dispatch_rules(self):
        cards = self.mgmt / 'records' / 'dispatch'
        prompt = 'read lead card'
        ok = dict(subagent_type='bf-executor', model='sonnet', prompt=prompt, description='d', run_in_background=False, effort='medium')
        (cards / 'x-lead').mkdir(parents=True)
        (cards / 'x-lead' / 'card.json').write_text(json.dumps(dict(lead=True, host_call=dict(tool='Agent', input=ok))))
        lead = dict(agent_id='a1', agent_type='bf-architect')
        self.assertIsNone(self.run_guard('Agent', ok, cards=True, **lead))
        self.assertIn('as-lead', self.run_guard('Agent', dict(ok, prompt='do more'), cards=True, **lead))
        self.assertIn('boolean', self.run_guard('Agent', dict(ok, run_in_background='false'), cards=True, **lead))
        self.assertIn('may dispatch only', self.run_guard('Agent', dict(ok, subagent_type='general-purpose'), cards=True, **lead))
        self.assertIn('may not dispatch', self.run_guard('Agent', ok, cards=True, agent_id='a2', agent_type='bf-executor'))
        self.assertIn('flow.py', self.run_guard('Write', {'file_path': str(self.mgmt / 'flow.json'), 'content': '{}'}, **lead))
        self.assertIsNone(self.run_guard('Bash', {'command': 'python3 %s --store f start --actor architect' % (HERE / 'flow.py')}, **lead))
        # The orchestrator cannot borrow the lead's card or identity.
        self.assertIn('dispatch card', self.run_guard('Agent', ok, cards=True))
        for command in ['python3 %s make --store f --kind EXECUTE --target T --as-lead' % (HERE / 'dispatch_card.py'),
                        'python3 %s start --store f --card x-lead --revision 3' % (HERE / 'dispatch_card.py'),
                        'python3 %s --store f start --actor architect --revision 3' % (HERE / 'flow.py')]:
            with self.subTest(command=command):
                self.assertDenied('Bash', {'command': command}, cards=True)

    def test_unreadable_input_fails_closed(self):
        proc = subprocess.run([sys.executable, str(GUARD), '--host', 'claude', '--conversation', SID,
                               '--management-root', str(self.mgmt), '--agent-model', 'sonnet'],
                              input='not json', capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


if __name__ == '__main__':
    unittest.main()
