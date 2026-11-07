#!/usr/bin/env python3
"""bf command line (design 6.1, 18): python3 bf/cli.py --base <dir> <command>. Prints JSON."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bf import api  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(prog='bf')
    ap.add_argument('--base', required=True)
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('status', 'report', 'stop', 'resume'):
        sub.add_parser(name)
    run = sub.add_parser('run')
    run.add_argument('--parallel', type=int, default=1)
    run.add_argument('--profile', action='append', default=[], help='backend=profile.json (repeatable)')
    run.add_argument('--agent-cli')
    ans = sub.add_parser('answer')
    ans.add_argument('order')
    ans.add_argument('--file', required=True)
    group = ans.add_mutually_exclusive_group(required=True)
    group.add_argument('--approve', action='store_true')
    group.add_argument('--reject', action='store_true')
    imp = sub.add_parser('import')
    imp.add_argument('--v1', required=True)
    imp.add_argument('--approval', required=True)
    imp.add_argument('--workspace')
    for name in ('init', 'revise'):
        p = sub.add_parser(name)
        p.add_argument('--plan', required=True)
        p.add_argument('--approval', required=True)
    args = ap.parse_args(argv)
    base = Path(args.base)
    try:
        if args.cmd == 'status':
            r = api.report(base)
            counts = {}
            for o in api.load(base).orders.values():
                counts[o['state']] = counts.get(o['state'], 0) + 1
            out = {'revision': r['revision'], 'stopped': r['stopped'], 'orders': counts, 'running': len(r['running']),
                   'ask_user': r['ask_user'], 'runnable': r['runnable']}
        elif args.cmd == 'report':
            out = api.report(base)
        elif args.cmd in ('stop', 'resume'):
            out = {args.cmd: getattr(api, args.cmd)(base)}
        elif args.cmd == 'answer':
            text = Path(args.file).read_text(encoding='utf-8')
            out = {'answered': api.answer(base, args.order, text, approve=args.approve)}
        elif args.cmd in ('init', 'revise'):
            plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
            out = {args.cmd: (api.init if args.cmd == 'init' else api.revise_plan)(base, plan, args.approval)}
        elif args.cmd == 'import':
            from bf import importer
            out = importer.import_v1(args.v1, base, args.approval, workspace=args.workspace)
        else:
            from bf import adapter, runner
            profiles = adapter.ensure_profiles(base, args.agent_cli, dict(p.split('=', 1) for p in args.profile))
            ad = adapter.AgentCliAdapter(base, profiles, agent_cli=args.agent_cli)
            runner.Runner(base, ad, parallel=args.parallel).run_until_idle()
            out = api.report(base)
    except api.BfError as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
