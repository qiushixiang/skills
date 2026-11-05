#!/usr/bin/env python3
"""Claude Code launcher with a fake `claude` binary (no model calls)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / 'start_claude_orchestrator.py'


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.ws = root / 'ws'; self.task = self.ws / 'task'; self.task.mkdir(parents=True)
        self.bin = root / 'bin'; self.bin.mkdir()
        self.capture = root / 'capture.json'
        fake = self.bin / 'claude'
        fake.write_text('#!%s\nimport json,os,sys\nif "--version" in sys.argv:print("2.1.318 (Claude Code)");sys.exit(0)\n'
                        'json.dump(dict(argv=sys.argv[1:],env=os.environ.get("BATCH_FLOW_CLAUDE_LAUNCH"),cwd=os.getcwd()),open(%r,"w"))\n'
                        % (sys.executable, str(self.capture)))
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + ':/usr/bin:/bin')

    def tearDown(self):
        self.tmp.cleanup()

    def launch(self, *extra):
        return subprocess.run([sys.executable, str(LAUNCHER), '--workspace', str(self.ws), '--task-base', 'task', *extra],
                              capture_output=True, text=True, env=self.env)

    def test_dry_run_writes_nothing_and_binds_session(self):
        proc = self.launch('--dry-run'); self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout); sid = out['spec']['session_id']
        self.assertFalse((self.task / '.batch-flow').exists())
        hooks = out['settings']['hooks']
        self.assertEqual(set(hooks), {'SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop'})
        for event in hooks.values():
            for h in event[0]['hooks']:
                if 'context' not in h['command']:
                    self.assertIn(sid, h['command'])
        self.assertIn('--card-root', hooks['PreToolUse'][0]['hooks'][0]['command'])
        argv = out['argv']
        self.assertEqual(argv[argv.index('--session-id') + 1], sid)
        self.assertEqual(argv[argv.index('--model') + 1], 'haiku')
        agents = json.loads(argv[argv.index('--agents') + 1])
        self.assertNotIn('Write', agents['bf-reviewer']['tools']); self.assertIn('Edit', agents['bf-executor']['tools'])
        self.assertTrue(all('Agent' not in a['tools'] for a in agents.values()))
        self.assertIn(str(HERE.parent), [argv[i + 1] for i, a in enumerate(argv) if a == '--add-dir'])
        for flag in ['--dangerously-skip-permissions', 'bypassPermissions', '--disallowedTools']:
            self.assertNotIn(flag, argv)

    def test_launch_creates_runtime_and_env_binding(self):
        proc = self.launch(); self.assertEqual(proc.returncode, 0, proc.stderr)
        cap = json.loads(self.capture.read_text())
        runtime = self.task / '.batch-flow' / '.claude-launch' / cap['env']
        spec = json.loads((runtime / 'spec.json').read_text())
        self.assertEqual(cap['cwd'], str(self.ws.resolve()))
        self.assertTrue((runtime / 'settings.json').is_file())
        self.assertTrue((self.task / '.batch-flow' / 'records' / 'dispatch').is_dir())
        bound = subprocess.run([sys.executable, str(HERE / 'flow.py'), 'capabilities', '--store', spec['store']],
                               capture_output=True, text=True, env=dict(self.env, BATCH_FLOW_CLAUDE_LAUNCH=cap['env']))
        info = json.loads(bound.stdout)
        self.assertEqual((info['status'], info['host'], info['conversation_id']), ('bound', 'claude', spec['session_id']))
        for sid, expect in [(spec['session_id'], True), ('other', False)]:
            ctx = subprocess.run([sys.executable, str(LAUNCHER), 'context', '--runtime', str(runtime)],
                                 input=json.dumps(dict(session_id=sid, source='compact')), capture_output=True, text=True)
            text = json.loads(ctx.stdout).get('hookSpecificOutput', {}).get('additionalContext', '')
            self.assertEqual('dispatch_card.py' in text, expect)
        proc = subprocess.run([sys.executable, str(LAUNCHER), '--workspace', str(self.ws), '--store', spec['store'],
                               '--resume-launch', spec['launch_id']], capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = json.loads(self.capture.read_text())['argv']
        self.assertEqual(argv[argv.index('--resume') + 1], spec['session_id']); self.assertNotIn('--session-id', argv)

    def test_shell_wrapper_symlink_uses_link_dir_as_workspace(self):
        link = self.ws / 'start-claude-orchestrator.sh'
        link.symlink_to(HERE / 'start-claude-orchestrator.sh')
        proc = subprocess.run([str(link), '--task-base', 'task', '--dry-run'], capture_output=True, text=True,
                              env=self.env, cwd=str(self.task))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['spec']['workspace'], str(self.ws.resolve()))

    def test_requires_task_and_verified_version(self):
        proc = subprocess.run([sys.executable, str(LAUNCHER), '--workspace', str(self.ws), '--dry-run'],
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(proc.returncode, 2); self.assertIn('--task-base', proc.stderr)
        (self.bin / 'claude').write_text('#!/bin/sh\necho "9.9.9 (Claude Code)"\n')
        proc = self.launch('--dry-run'); self.assertEqual(proc.returncode, 2); self.assertIn('verified', proc.stderr)


if __name__ == '__main__':
    unittest.main()
