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
    for name in ('status', 'resume', 'doctor', 'ensure-run'):
        sub.add_parser(name)
    sub.add_parser('stop').add_argument('--now', action='store_true')
    rec = sub.add_parser('record')
    rec.add_argument('--cwd')
    snap = sub.add_parser('snapshot')
    snap.add_argument('--repo', required=True)
    snap.add_argument('--paths', nargs='+', required=True)
    wt = sub.add_parser('watch')
    wt.add_argument('--since', type=int, default=0)
    wt.add_argument('--once', action='store_true')
    sub.add_parser('report').add_argument('--text', action='store_true')
    rc = sub.add_parser('reconcile')
    rc.add_argument('--file', required=True)
    rc.add_argument('--approval', required=True)
    rc.add_argument('--dry-run', action='store_true')
    rr = sub.add_parser('restore-review')
    rr.add_argument('order')
    rr.add_argument('--run', required=True)
    rr.add_argument('--approval', required=True)
    for name in ('retry', 'escalate'):
        p = sub.add_parser(name)
        p.add_argument('order')
        p.add_argument('--note', required=True)
    run = sub.add_parser('run')
    run.add_argument('--background', action='store_true', help='Start a detached single driver and return immediately')
    run.add_argument('--parallel', type=int, default=1)
    run.add_argument('--profile', action='append', default=[], help='backend=profile.json (repeatable)')
    run.add_argument('--agent-cli')
    ans = sub.add_parser('answer')
    ans.add_argument('order')
    ans.add_argument('--file', required=True)
    group = ans.add_mutually_exclusive_group(required=True)
    group.add_argument('--approve', action='store_true')
    group.add_argument('--reject', action='store_true')
    sub.add_parser('models').add_argument('backend')
    sr = sub.add_parser('set-role')
    sr.add_argument('role')
    sr.add_argument('--backend', required=True)
    sr.add_argument('--name', required=True)
    sr.add_argument('--effort', default='default')
    en = sub.add_parser('envelope')
    en.add_argument('--add', nargs='+', default=[])
    en.add_argument('--remove', nargs='+', default=[])
    gr = sub.add_parser('grant')
    gr.add_argument('target')
    for k in ('implement', 'backup', 'lead', 'repair', 'review'):
        gr.add_argument('--' + k, type=int)
    for name in ('hold', 'unhold'):
        sub.add_parser(name).add_argument('targets', nargs='+')
    sp = sub.add_parser('supersede')
    sp.add_argument('order')
    sp.add_argument('--reason', required=True)
    for name in ('set-role', 'envelope', 'grant', 'hold', 'unhold', 'supersede'):
        sub.choices[name].add_argument('--approval', required=True)
    for name in ('init', 'revise'):
        p = sub.add_parser(name)
        p.add_argument('--plan', required=True)
        p.add_argument('--approval', required=True)
    sub.choices['revise'].add_argument('--revalidate', nargs='*', default=[])
    argv = list(sys.argv[1:] if argv is None else argv)
    child = []
    if '--' in argv:  # record's child command is never parsed by argparse
        split = argv.index('--')
        argv, child = argv[:split], argv[split + 1:]
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
        elif args.cmd == 'report' and args.text:
            sys.stdout.write(api.report_text(base))
            return 0
        elif args.cmd == 'report':
            out = api.report(base)
        elif args.cmd == 'reconcile':
            from bf import reconcile
            out = reconcile.apply(base, args.file, args.approval, dry_run=args.dry_run)
        elif args.cmd == 'restore-review':
            out = api.restore_review(base, args.order, args.run, args.approval)
        elif args.cmd in ('retry', 'escalate'):
            out = {args.cmd: getattr(api, args.cmd)(base, args.order, args.note)}
        elif args.cmd == 'record':
            from bf import evidence
            out, code = evidence.record(base, child, args.cwd)
            print(json.dumps(out, ensure_ascii=False))
            return code
        elif args.cmd == 'snapshot':
            from bf import evidence
            out = evidence.snapshot(base, args.repo, args.paths)
        elif args.cmd == 'doctor':
            from bf import evidence
            out = evidence.doctor(base)
        elif args.cmd == 'watch':
            since = args.since
            while True:
                lines, since = api.watch(base, since)
                for line in lines:
                    print(line, flush=True)
                if args.once:
                    return 0
                import time
                time.sleep(2)
        elif args.cmd == 'stop':
            out = {'stop': api.stop(base, now=args.now)}
        elif args.cmd == 'resume':
            out = {'resume': api.resume(base)}
        elif args.cmd == 'ensure-run':
            from bf import runner
            out = runner.ensure_running(base)
        elif args.cmd == 'answer':
            text = Path(args.file).read_text(encoding='utf-8')
            out = {'answered': api.answer(base, args.order, text, approve=args.approve)}
        elif args.cmd in ('models', 'set-role', 'envelope', 'grant', 'hold', 'unhold', 'supersede'):
            from bf import adjust
            if args.cmd == 'models':
                out = adjust.models(base, args.backend)
            elif args.cmd == 'set-role':
                out = adjust.set_role(base, args.role, args.backend, args.name, args.approval, effort=args.effort)
            elif args.cmd == 'envelope':
                out = {'batches': adjust.envelope(base, args.approval, add=args.add, remove=args.remove)}
            elif args.cmd == 'grant':
                out = {'overrides': adjust.grant(base, args.target, args.approval, **dict(
                    (k, getattr(args, k)) for k in ('implement', 'backup', 'lead', 'repair', 'review')))}
            elif args.cmd in ('hold', 'unhold'):
                out = {'holds': getattr(adjust, args.cmd)(base, args.targets, args.approval)}
            else:
                out = {'superseded': adjust.supersede(base, args.order, args.approval, args.reason)}
        elif args.cmd in ('init', 'revise'):
            plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
            out = {args.cmd: (api.init(base, plan, args.approval) if args.cmd == 'init' else
                              api.revise_plan(base, plan, args.approval, revalidate=args.revalidate))}
        else:
            from bf import adapter, runner
            runner.check_driver_environment(api.load(base))
            profiles = adapter.ensure_profiles(base, args.agent_cli, dict(p.split('=', 1) for p in args.profile))
            ad = adapter.AgentCliAdapter(base, profiles, agent_cli=args.agent_cli)
            command = [sys.executable, str(Path(__file__).resolve()), '--base', str(base.resolve()),
                       'run', '--parallel', str(args.parallel)]
            for backend, profile in profiles.items():
                command += ['--profile', '%s=%s' % (backend, Path(profile).resolve())]
            if args.agent_cli:
                command += ['--agent-cli', str(Path(args.agent_cli).resolve())]
            if args.background:
                out = runner.ensure_running(base, command)
            else:
                runner.Runner(base, ad, parallel=args.parallel, run_command=command).run_until_idle()
                out = api.report(base)
    except api.BfError as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
