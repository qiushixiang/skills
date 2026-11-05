#!/usr/bin/env python3
"""Claude Code PreToolUse guard: block orchestrator tool calls outside its role before they run.

The explicitly bound main session gets the full allowlist. Subagent calls (hook input carries
agent_id/agent_type, verified on 2.1.318) keep their own tools, with two narrow rules: no subagent
writes flow.json directly, and only a technical-lead type (--lead-agent-type) may call Agent, only
for worker types (--worker-agent-type) and, with --card-root, only verbatim from a dispatch card.
Other sessions pass through untouched. Session-wide --disallowedTools cannot do this:
it is inherited by every subagent. This is an allowlist; unknown tools are denied.
"""
import argparse
import json
from pathlib import Path
import re
import shlex
import sys

SCRIPTS = Path(__file__).resolve().parent
MANAGEMENT_SCRIPTS = {'flow.py', 'agent_cli.py', 'progress_timer.py', 'workflow_metrics.py', 'dispatch_card.py'}
# Same set role_audit certifies, so an allowed call never makes the Stop audit unavailable.
READ_COMMANDS = {'ls', 'pwd', 'stat', 'readlink'}
PASS_TOOLS = {'TaskStop', 'TaskOutput', 'ToolSearch', 'AskUserQuestion', 'ListAgents',
              'CronCreate', 'CronDelete', 'CronList', 'ScheduleWakeup', 'PushNotification'}
READ_TOOLS = {'Read', 'Grep', 'Glob'}
SHELL_META = re.compile(r'[;&|<>`$\\\n]')


def inside(path, roots, cwd):
    path = Path(path)
    if not path.is_absolute():
        if not cwd:
            return False
        path = Path(cwd) / path
    path = path.resolve()
    return any(path == root or root in path.parents for root in roots)


def card_calls(card_root, lead=False):
    """Host calls of orchestrator cards (lead=False) or of technical-lead --as-lead cards (lead=True)."""
    calls = []
    if card_root is None:
        return calls
    for path in sorted(Path(card_root).glob('*/card.json')):
        try:
            card = json.loads(path.read_text(encoding='utf-8'))
            if bool(card.get('lead')) == lead:
                calls.append(card['host_call'])
        except (OSError, ValueError, KeyError):
            continue
    return calls


def same(key, a, b):
    if key == 'run_in_background':
        # Compare what the host will do: Claude Code 2.1.318 runs an Agent in the foreground only for the JSON
        # boolean false; anything else (true, the string "false", omitted) is background. Haiku sends strings.
        return (a is not False) == (b is not False)
    return a == b


def matches_card(tool, inputs, card_root, keys, lead=False):
    for call in card_calls(card_root, lead):
        if call.get('tool') == tool and all(same(k, inputs.get(k), call['input'].get(k)) for k in keys):
            return True
    return False


def check_bash(inputs, cwd, read_roots, card_root=None):
    command = inputs.get('command')
    if not isinstance(command, str) or not command.strip():
        return 'empty shell command'
    if SHELL_META.search(command):
        return 'orchestrator shell is limited to one literal command without pipes, redirection or substitution'
    try:
        words = shlex.split(command)
    except ValueError:
        return 'unparsable shell command'
    exe = Path(words[0]).name
    if re.fullmatch(r'python(3(\.\d+)?)?', exe):
        if len(words) < 2 or words[1].startswith('-'):
            return 'python is allowed only to run the skill management scripts'
        script = Path(words[1]) if Path(words[1]).is_absolute() else Path(cwd or '/') / words[1]
        if script.resolve() not in {SCRIPTS / name for name in MANAGEMENT_SCRIPTS}:
            return 'python is allowed only to run the skill management scripts'
        if script.name == 'flow.py' and ('--' in words[2:] or 'snapshot' in words[2:] or 'record' in words[2:]):
            return 'flow.py record/snapshot capture product commands and diffs; that is executor work'
        if script.name == 'dispatch_card.py' and (words[2:3] not in (['next'], ['show']) or '--as-lead' in words[2:]):
            return 'the orchestrator runs only dispatch_card.py next (it makes, starts, collects and registers cards itself)'
        if script.name == 'flow.py' and any(w == '--actor' for w in words[2:]) and 'architect' in words[2:] and \
                any(op in words[2:] for op in ['start', 'register_dispatch']):
            return 'start/register as architect is the technical lead dispatch; delegate the card instead'
        if script.name == 'agent_cli.py' and 'run' in words[2:3] and card_root is not None and \
                not matches_card('Bash', dict(command=command.strip()), card_root, ['command']):
            return 'agent_cli.py run must be copied verbatim from dispatch_card.py next'
        return None
    if exe in READ_COMMANDS:
        paths = [word for word in words[1:] if not word.startswith('-')]
        if not all(inside(p, read_roots, cwd) for p in paths) or (not paths and not inside(cwd or '/', read_roots, None)):
            return 'read command outside the orchestrator read roots (business source is dispatched, not explored)'
        return None
    return 'command is not a management script or allowed read command: ' + exe


def decide_subagent(event, lead_types, worker_types, card_root):
    tool = event.get('tool_name'); kind = event.get('agent_type')
    inputs = event.get('tool_input') if isinstance(event.get('tool_input'), dict) else {}
    if tool in {'Write', 'Edit', 'NotebookEdit'}:
        target = inputs.get('file_path') or inputs.get('notebook_path') or ''
        if Path(target).name == 'flow.json':
            return 'flow.json is changed only through flow.py'
    if tool == 'Agent':
        if kind not in lead_types:
            return 'subagent type %r may not dispatch agents' % kind
        sub = inputs.get('subagent_type') or 'general-purpose'
        if sub not in worker_types:
            return 'technical lead may dispatch only %s, got %r' % (sorted(worker_types), sub)
        if card_root is not None and not matches_card('Agent', inputs, card_root, ['subagent_type', 'model', 'prompt', 'effort', 'run_in_background'], lead=True):
            return ('lead dispatch must be copied verbatim from a dispatch_card.py --as-lead card; '
                    'run_in_background must be the JSON boolean false (a string runs in the background)')
    return None


def decide(event, conversation, management_roots, read_roots, agent_types, agent_models, skills, card_root=None,
           lead_types=frozenset(), worker_types=frozenset()):
    """Return a deny reason, or None to fall through to normal Claude Code permissions."""
    if event.get('session_id') != conversation:
        return None
    if event.get('agent_id'):
        return decide_subagent(event, lead_types, worker_types, card_root)
    tool = event.get('tool_name')
    inputs = event.get('tool_input') if isinstance(event.get('tool_input'), dict) else {}
    cwd = event.get('cwd')
    if tool in PASS_TOOLS:
        return None
    if tool == 'Bash':
        return check_bash(inputs, cwd, read_roots, card_root)
    if tool == 'SendMessage':
        if card_root is not None and not matches_card('SendMessage', inputs, card_root, ['to', 'message']):
            return 'SendMessage continuation must be copied verbatim from a dispatch card'
        return None
    if tool in READ_TOOLS:
        target = inputs.get('file_path') or inputs.get('path') or cwd
        if not isinstance(target, str) or not inside(target, read_roots, cwd):
            return tool + ' outside the orchestrator read roots (business source is dispatched, not explored)'
        return None
    if tool == 'Write':
        target = inputs.get('file_path')
        if not isinstance(target, str) or not inside(target, management_roots, cwd):
            return 'orchestrator may only write new management records under the management root'
        if Path(target).name == 'flow.json':
            return 'flow.json is changed only through flow.py'
        resolved = Path(target) if Path(target).is_absolute() else Path(cwd) / target
        if resolved.exists():
            return 'receipts/records are immutable; write a new file instead of overwriting'
        return None
    if tool == 'Agent':
        kind = inputs.get('subagent_type') or 'general-purpose'
        if kind == 'fork' or kind not in agent_types:
            return 'subagent_type %r is not a registered role agent type' % kind
        if card_root is not None:
            if not matches_card('Agent', inputs, card_root, ['description', 'subagent_type', 'model', 'prompt', 'effort', 'run_in_background']):
                return 'Agent dispatch must be copied verbatim from dispatch_card.py next (a dispatch card)'
            return None
        if inputs.get('model') not in agent_models:
            return 'Agent dispatch must pass an explicit registered model, got %r' % inputs.get('model')
        return None
    if tool == 'Skill':
        if inputs.get('skill') in skills:
            return None
        return 'skill %r is outside the orchestrator role' % inputs.get('skill')
    return 'tool %r is outside the orchestrator allowlist' % tool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=['claude'], required=True)
    parser.add_argument('--conversation', required=True, help='bound orchestrator session ID (claude --session-id)')
    parser.add_argument('--management-root', action='append', required=True, help='e.g. <task-base>/.batch-flow')
    parser.add_argument('--read-root', action='append', default=[], help='task base / design docs; skill dir is always readable')
    parser.add_argument('--agent-type', action='append', default=None, help='allowed subagent_type (default general-purpose)')
    parser.add_argument('--agent-model', action='append', default=[], help='allowed Agent models when no --card-root is used')
    parser.add_argument('--card-root', help='records/dispatch: Agent/SendMessage/agent_cli run must equal a dispatch card call')
    parser.add_argument('--skill', action='append', default=['batch-flow-orchestration'])
    parser.add_argument('--lead-agent-type', action='append', default=[], help='subagent type that may dispatch workers')
    parser.add_argument('--worker-agent-type', action='append', default=[], help='types a lead subagent may dispatch')
    args = parser.parse_args()
    management = [Path(p).resolve() for p in args.management_root]
    reads = [Path(p).resolve() for p in args.read_root] + management + [SCRIPTS.parent]
    try:
        event = json.load(sys.stdin)
    except ValueError as exc:
        print('orchestrator_guard: unreadable hook input, failing closed: %s' % exc, file=sys.stderr)
        return 2
    reason = decide(event, args.conversation, management, reads,
                    set(args.agent_type or ['general-purpose']), set(args.agent_model), set(args.skill),
                    Path(args.card_root).resolve() if args.card_root else None,
                    set(args.lead_agent_type), set(args.worker_agent_type))
    if reason:
        print(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
            'permissionDecisionReason': 'ROLE_BOUNDARY_BLOCKED (%s): ' % (event.get('agent_type') or 'orchestrator') + reason +
            '. Dispatch this work to the registered role instead.'}}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
