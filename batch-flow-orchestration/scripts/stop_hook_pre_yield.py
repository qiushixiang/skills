#!/usr/bin/env python3
"""Explicitly bound AGY/Codex/Claude Code Stop hook adapter for flow.py pre_yield."""
import argparse
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

def response(host, continue_work=False, reason=''):
    if host in {'codex','claude'}:
        return {'decision':'block','reason':reason} if continue_work else {}
    return {'decision':'continue','reason':reason} if continue_work else {'decision':'stop'}

def explicit_stop(event):
    reason=str(event.get('terminationReason','')).lower()
    if reason in {'user_canceled','user_cancelled','error','max_invocations','max_token_budget_exceeded','max_steps_exceeded','budget_exceeded','interrupted'}:
        return True
    last=str(event.get('lastUserInput',''))
    return any(word in last for word in ('先停','暂停','停止','停下','先别跑','中断','先别动'))

def read_event(timeout=2.0):
    if sys.stdin.isatty():
        return None, 'stdin is a tty'
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, io.UnsupportedOperation):
        try:
            return json.load(sys.stdin), None
        except Exception as exc:
            return None, str(exc)

    if sys.platform != 'win32':
        import os, select, time
        chunks = []
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                rlist, _, _ = select.select([fd], [], [], min(remaining, 0.5))
            except (OSError, select.error) as exc:
                return None, f'select error on stdin: {exc}'
            if not rlist:
                if chunks:
                    break
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError as e:
                return None, f'read error on stdin: {e}'
            if not chunk:
                break
            chunks.append(chunk)
            try:
                return json.loads(b''.join(chunks).decode('utf-8')), None
            except Exception:
                pass
        if not chunks:
            return None, 'no data on stdin'
        try:
            return json.loads(b''.join(chunks).decode('utf-8')), None
        except Exception as exc:
            return None, f'invalid event: {exc}'
    else:
        try:
            return json.load(sys.stdin), None
        except Exception as exc:
            return None, str(exc)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',choices=['agy','codex','claude'],required=True)
    parser.add_argument('--store',type=Path,required=True)
    parser.add_argument('--role',choices=['orchestrator','architect'],required=True)
    parser.add_argument('--conversation',help='bound owner conversation ID; required for a live hook')
    parser.add_argument('--audit-log',type=Path,help='optional bound role audit log; missing/invalid log blocks when enabled')
    parser.add_argument('--audit-management-root',type=Path,action='append',default=[])
    args=parser.parse_args()
    event, err = read_event()
    if not isinstance(event, dict):
        print(json.dumps(response(args.host)))
        if err:
            sys.stderr.write(f'[batch-flow-hook] invalid event: {err}\n')
        return
    actual=event.get('conversationId') if args.host=='agy' else event.get('session_id')
    if not args.conversation or not actual or actual!=args.conversation:
        print(json.dumps(response(args.host)))
        return
    if explicit_stop(event):
        print(json.dumps(response(args.host)))
        return
    store=args.store.resolve()
    if not store.is_file():
        print(json.dumps(response(args.host,bool(args.audit_log and args.role=='orchestrator'),'Role audit enabled but bound flow store unavailable; inspect host configuration.')))
        sys.stderr.write('[batch-flow-hook] bound flow store unavailable\n')
        return
    script=Path(__file__).with_name('flow.py')
    try:
        argv=[sys.executable,str(script),'--store',str(store),'pre_yield','--actor',args.role]
        if args.audit_log and args.role=='orchestrator':
            argv+=['--audit-log',str(args.audit_log),'--conversation',args.conversation]
            for root in args.audit_management_root:argv+=['--audit-management-root',str(root)]
        run=subprocess.run(argv,capture_output=True,text=True,timeout=10)
        report=json.loads(run.stdout)
        if not isinstance(report,dict) or 'can_yield' not in report: raise ValueError('invalid pre_yield result')
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        print(json.dumps(response(args.host,bool(args.audit_log and args.role=='orchestrator'),'Role audit could not complete; inspect hook error and do not claim audit passed.')))
        sys.stderr.write(f'[batch-flow-hook] pre_yield unavailable: {exc}\n')
        return
    if report.get('disposition')=='ROLE_BOUNDARY_BLOCKED':
        audit=report['boundary_audit']
        details='; '.join('line '+str(f['line'])+': '+f['reason'] for f in audit.get('findings',[])) or audit.get('reason','audit unavailable')
        message='Orchestrator role audit '+audit['status']+': '+details+'. Stop direct product work, preserve evidence, and dispatch diagnosis/recovery to the responsible role. Do not automatically revert changes. Explicit user stop still takes precedence.'
        # A repeated stop must not silently waive a detected boundary violation.
        print(json.dumps(response(args.host,True,message),ensure_ascii=False))
        return
    if report['can_yield']:
        print(json.dumps(response(args.host)))
        return
    # Loop guard: the host reports it is already continuing because of this hook.
    # If nothing was registered since the last block, blocking again cannot help.
    guard=store.parent/('.stop_hook_'+hashlib.sha256(args.conversation.encode()).hexdigest()[:16]+'.json')
    mark={'revision':report.get('revision'),'disposition':report.get('disposition')}
    if event.get('stop_hook_active'):
        try: last=json.loads(guard.read_text())
        except (OSError,ValueError): last=None
        if last==mark:
            print(json.dumps(response(args.host)))
            sys.stderr.write(f"[batch-flow-hook] {mark['disposition']} unchanged at revision {mark['revision']} after a previous block; allowing stop to avoid a loop\n")
            return
    try: guard.write_text(json.dumps(mark))
    except OSError as exc: sys.stderr.write(f'[batch-flow-hook] loop guard not saved: {exc}\n')
    actions=', '.join(str(a.get('target')) for a in report.get('actions',[]))
    problems='; '.join(str(p.get('reason')) for p in report.get('dispatch_problems',[]))
    message=f"batch-flow {report.get('disposition','UNKNOWN')}: {actions or problems or 'inspect current responsibility and recovery conditions'}"
    print(json.dumps(response(args.host,True,message),ensure_ascii=False))

if __name__=='__main__':
    main()
