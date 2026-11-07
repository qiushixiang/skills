"""Real role adapter: wraps scripts/agent_cli.py run/collect (one detached process and run dir per launch)."""
import json
import os
import subprocess
import sys
from pathlib import Path

from . import api, model

DEFAULT_AGENT_CLI = Path(__file__).resolve().parents[1] / 'agent_cli.py'


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class AgentCliAdapter(object):
    def __init__(self, base, profiles, agent_cli=None, cwd=None):
        self.base, self.profiles = Path(base), dict(profiles)
        self.agent_cli = str(agent_cli or DEFAULT_AGENT_CLI)
        self.cwd = str(cwd or self.base.parent)
        self.procs = {}
        self.sessions_file = self.base / 'records' / 'runs' / 'sessions.json'

    def _sessions(self):
        try:
            return json.loads(self.sessions_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def launch(self, order, assignment, card_path, run_dir, resume=None):
        run_dir = Path(run_dir)
        backend = model.backend(assignment['tool'])
        profile = self.profiles.get(backend)
        if not profile:
            raise api.BfError('no profile configured for backend %s' % backend)
        cli_dir = run_dir / ('cli-%d' % (len(list(run_dir.glob('cli-*'))) + 1))
        argv = [sys.executable, self.agent_cli, 'run', '--profile', profile, '--task', order['id'], '--cwd', self.cwd,
                '--model', assignment['model'], '--effort', assignment['effort'], '--prompt-file', str(card_path),
                '--run-dir', str(cli_dir), '--access', 'write']
        env = model.env_for(api.load(self.base).plan, assignment['tool'])
        for d in env.get('add_dirs') or []:
            argv += ['--add-dir', d]
        if env.get('require_host'):
            argv.append('--require-host')
        if env.get('approval_policy'):
            argv += ['--approval-policy', env['approval_policy']]
        if resume:
            previous = self._sessions().get(resume)
            if not previous:
                raise api.BfError('no recorded run dir for conversation %s' % resume)
            argv += ['--conversation', resume, '--resume-from', previous]
        log = open(str(run_dir / ('launch-%s.log' % cli_dir.name)), 'wb')
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self.procs[proc.pid] = proc
        return {'dir': str(cli_dir), 'pid': proc.pid, 'run_dir': str(run_dir)}

    def poll(self, handle):
        cli_dir = Path(handle['dir'])
        proc = self.procs.get(handle['pid'])
        finished = (cli_dir / 'exit.json').exists()
        if not finished:
            if proc is not None and proc.poll() is None or proc is None and _alive(handle['pid']):
                return None
        report = {}
        if finished:
            done = subprocess.run([sys.executable, self.agent_cli, 'collect', '--run-dir', str(cli_dir)],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
            try:
                report = json.loads(done.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                report = {}
        cid = report.get('conversation_id') if report.get('usable') else None
        if cid:
            sessions = self._sessions()
            sessions[cid] = str(cli_dir)
            self.sessions_file.parent.mkdir(parents=True, exist_ok=True)
            self.sessions_file.write_text(json.dumps(sessions), encoding='utf-8')
        events, size = [], 0
        try:
            raw = (cli_dir / 'stdout').read_text(encoding='utf-8', errors='replace')
            size = len(raw.encode('utf-8'))
            for line in raw.splitlines():
                try:
                    item = (json.loads(line) or {}).get('item')
                except (ValueError, AttributeError):
                    continue
                if isinstance(item, dict) and item.get('type') in ('command_execution', 'file_change'):
                    events.append(item)
        except OSError:
            pass
        decision = None
        try:
            decision = json.loads((Path(handle['run_dir']) / 'decision.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        return {'conversation_id': cid, 'text': (report.get('response') or '') if report.get('usable') else '',
                'events': events, 'session_kb': size // 1024, 'decision': decision}
