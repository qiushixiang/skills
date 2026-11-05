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
                        'json.dump(dict(argv=sys.argv[1:],env=os.environ.get("BATCH_FLOW_CLAUDE_LAUNCH"),depth=os.environ.get("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"),cwd=os.getcwd()),open(%r,"w"))\n'
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
        self.assertEqual([n for n, a in agents.items() if 'Agent' in a['tools']], ['bf-architect'])
        self.assertEqual(out['env']['CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH'], '2')
        self.assertIn('--lead-agent-type', hooks['PreToolUse'][0]['hooks'][0]['command'])
        self.assertIn(str(HERE.parent), [argv[i + 1] for i, a in enumerate(argv) if a == '--add-dir'])
        for flag in ['--dangerously-skip-permissions', 'bypassPermissions', '--disallowedTools']:
            self.assertNotIn(flag, argv)

    def test_launch_creates_runtime_and_env_binding(self):
        proc = self.launch(); self.assertEqual(proc.returncode, 0, proc.stderr)
        cap = json.loads(self.capture.read_text())
        runtime = self.task / '.batch-flow' / '.claude-launch' / cap['env']
        spec = json.loads((runtime / 'spec.json').read_text())
        self.assertEqual((cap['cwd'], cap['depth']), (str(self.ws.resolve()), '2'))
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
        self.assertTrue(spec['started_at'])
        stop = json.loads((runtime / 'settings.json').read_text())['hooks']['Stop'][0]['hooks'][0]['command']
        self.assertIn('--next-command', stop)
        proc = self.launch('--resume-launch', spec['launch_id'])  # restart is a new launch; no resume
        self.assertEqual(proc.returncode, 2)

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
