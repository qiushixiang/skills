#!/usr/bin/env python3
"""Opt-in real CLI fresh/resume acceptance. Two model calls in a disposable workspace."""
import argparse
import json
from pathlib import Path
import tempfile
import uuid

import agent_cli


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=agent_cli.REQUIRED, default='codex')
    parser.add_argument('--model', required=True)
    parser.add_argument('--effort', required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='batch-'+args.backend+'-acceptance-'))
    profile = root/(args.backend+'-profile.json')
    agent_cli.probe(args.backend, profile)
    nonce = 'RESUME_'+uuid.uuid4().hex
    prompt = root/'first.txt'
    prompt.write_text('Isolated CLI acceptance check. Do not use tools, read files, or edit anything. '
                      'Remember this marker for the next turn and reply with only it: '+nonce)
    call = argparse.Namespace(task='cli-acceptance', model=args.model, effort=args.effort,
                              cwd=str(root), access='read', add_dir=[], settings=None,
                              conversation=None, resume_from=None, prompt_file=str(prompt),
                              run_dir=str(root/'first'))
    print(json.dumps(dict(event='LIVE_ACCEPTANCE_STARTED', backend=args.backend, root=str(root), model=args.model,
                          effort=args.effort)), flush=True)
    if agent_cli.run(profile, call) != 0:
        raise ValueError('fresh call failed; preserve raw evidence at '+str(root))
    first = agent_cli.collect(call.run_dir)
    if not agent_cli.uuid_session(first['conversation_id']) or nonce not in first['response']:
        raise ValueError('fresh call did not return a native ID and expected marker')
    call.conversation = first['conversation_id']
    call.resume_from = call.run_dir
    call.run_dir = str(root/'resumed')
    prompt = root/'second.txt'
    prompt.write_text('Do not use tools. Reply with only the marker I asked you to remember in the previous turn.')
    call.prompt_file = str(prompt)
    if agent_cli.run(profile, call) != 0:
        raise ValueError('resume failed; preserve raw evidence at '+str(root))
    resumed = agent_cli.collect(call.run_dir)
    if resumed['conversation_id'] != first['conversation_id'] or nonce not in resumed['response']:
        raise ValueError('resume did not preserve both native session ID and prior-turn memory')
    report = dict(event='LIVE_ACCEPTANCE_PASSED', backend=args.backend, root=str(root), model=args.model,
                  effort=args.effort, conversation_id=first['conversation_id'],
                  same_session=True, retained_previous_marker=True,
                  limit='Two real CLI calls with read access requested; not a product-task acceptance. AGY accept-edits with write_file deny / Claude default mode without edit tools is not OS read-only isolation.')
    agent_cli.save(root/'report.json', report)
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
