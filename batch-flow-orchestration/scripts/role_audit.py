#!/usr/bin/env python3
"""Record bound host tool events; audit orchestrator boundaries, without rollback."""
import argparse
import datetime as dt
import fcntl
import json
from pathlib import Path
import re
import shlex
import sys
import account_switch

FORMAT='batch-flow-role-audit/v1'
SCRIPTS=Path(__file__).resolve().parent
EDIT_TOOLS={'write','edit','multiedit','write_file','edit_file','write_to_file',
            'replace_file_content','multi_replace_file_content','apply_patch'}
SHELL_TOOLS={'bash','run_command','exec_command','shell_command','shell'}
READ_TOOLS={'read','glob','grep','view_file','list_dir','find_by_name','grep_search',
            'read_file','view_file_outline','view_code_item','read_terminal'}
AGENT_TOOLS={'invoke_subagent','spawn_agent','followup_task','send_message','sendmessage',
             'agent','task','wait_agent','list_agents','interrupt_agent','wait','write_stdin',
             'define_subagent','manage_subagents','manage_task','schedule','ask_question','list_permissions'}
AGENT_TOOLS.update({'command_status','finish','wait_5_seconds'})
# Claude Code coordination tools (2.1.318).
AGENT_TOOLS.update({'taskstop','taskoutput','toolsearch','askuserquestion','listagents','croncreate',
                    'crondelete','cronlist','schedulewakeup','pushnotification','skill'})
MANAGEMENT=['flow.py','agent_cli.py','progress_timer.py','dispatch_card.py','workflow_metrics.py']


def append_event(log, host, conversation, event, begin=False, account_switch=()):
    actual=event.get('conversationId') if host=='agy' else event.get('session_id')
    if not conversation or actual!=conversation:
        return False
    if host=='claude' and event.get('agent_id'):
        return False  # Claude Code subagents share session_id; their calls are not orchestrator calls.
    row=dict(format=FORMAT,session_id=actual,kind='begin' if begin else 'tool',
             time=dt.datetime.now(dt.timezone.utc).isoformat())
    if not begin:
        call=event.get('toolCall',{}) if host=='agy' else {}
        name=call.get('name') if host=='agy' else event.get('tool_name')
        inputs=call.get('args') if host=='agy' else event.get('tool_input')
        if isinstance(inputs,str):
            try:inputs=json.loads(inputs)
            except ValueError:inputs=None
        # Persist unsupported events too; never silently treat them as audited.
        row.update(tool=name,inputs=inputs,cwd=event.get('cwd') or next(iter(event.get('workspacePaths') or []),None))
        if account_switch:row['account_switch']=list(account_switch)
    with Path(log).open('a',encoding='utf-8') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX)
        stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        stream.flush()
    return True


def inside(path, roots, cwd):
    path=Path(path)
    if not path.is_absolute():
        if not cwd:raise ValueError('relative path without execution cwd')
        path=Path(cwd)/path
    path=path.resolve()
    return any(path==root or root in path.parents for root in roots)


def shell_check(command, cwd, roots, switch_paths=()):
    """Conservative literal shell subset, never execute inspected commands."""
    if not isinstance(command,str) or not command.strip():
        return 'unavailable','missing shell command'
    try:literal=shlex.split(command)
    except ValueError:literal=[]
    if len(literal)>1 and command==shlex.join(literal) and re.fullmatch(r'python(3(\.\d+)?)?',Path(literal[0]).name) and Path(literal[1]).resolve() in [SCRIPTS/name for name in MANAGEMENT]:
        if Path(literal[1]).name=='flow.py' and ('--' in literal[2:] or 'snapshot' in literal[2:] or 'record' in literal[2:]):
            return 'violation','orchestrator used a management helper for product command/diff capture'
        return 'clean','canonical literal management command'
    if literal and command==shlex.join(literal) and account_switch.allowed(literal,switch_paths):
        return 'clean','user-configured account switch command'
    # Dynamic shell/code wrappers require a stronger host audit, not guessed intent.
    if any(marker in command for marker in ['$(', '`', '${', '\\']) or re.search(r'<<|\n',command):
        return 'unavailable','dynamic or multiline shell cannot be verified by this auditor'
    try:
        lexer=shlex.shlex(command,posix=True,punctuation_chars=';&|<>')
        lexer.whitespace_split=True
        tokens=list(lexer)
    except ValueError:
        return 'unavailable','invalid shell syntax'
    segments=[[]]
    for token in tokens:
        if token in {';','&&','||','|'}:segments.append([])
        else:segments[-1].append(token)
    for words in segments:
        if not words:continue
        if any(token in {'>','>>','<','2>','&','&>'} for token in words):
            return 'unavailable','shell redirection/background operator requires host verification'
        executable=Path(words[0]).name
        if executable=='git':
            tail=words[1:];index=0
            while index<len(tail) and tail[index].startswith('-'):
                index+=2 if tail[index] in {'-C','-c','--git-dir','--work-tree'} else 1
            operation=tail[index] if index<len(tail) else ''
            if operation in {'add','commit','merge','rebase','reset','checkout','switch','cherry-pick','push','apply','am','clean','restore','stash'}:
                return 'violation','orchestrator called a Git write operation: '+operation
            if operation in {'status','rev-parse','ls-files','version'}:continue
            return 'unavailable','Git operation is outside the mechanical metadata subset'
        if re.fullmatch(r'python(3(\.\d+)?)?',executable) and len(words)>1:
            script=Path(words[1])
            if not script.is_absolute():
                if not cwd:return 'unavailable','script path lacks cwd'
                script=Path(cwd)/script
            if script.resolve() in [SCRIPTS/name for name in MANAGEMENT]:
                if script.name=='flow.py' and ('--' in words[2:] or 'snapshot' in words[2:] or 'record' in words[2:]):
                    return 'violation','orchestrator used a management helper for product command/diff capture'
                continue
        # A script invocation is technical work even if its name is not build.sh.
        if executable in {'make','cmake','ninja','ctest','pytest','meson','gn','gcc','clang','cargo','go','npm','pnpm','yarn','node'} or any(re.search(r'(^|/)(build|test)[^/]*\.(sh|py)$',word) for word in words[:3]):
            return 'violation','orchestrator directly invoked a build/test/product command'
        if executable in {'pwd','true','ls','stat','readlink'}:continue
        return 'unavailable','unrecognized command; audit cannot certify role compliance'
    return 'clean','mechanical command'


def tool_check(row, roots):
    name=row.get('tool');inputs=row.get('inputs')
    if not isinstance(name,str) or not isinstance(inputs,dict):
        return 'unavailable','unsupported tool event schema'
    name=name.rsplit('.',1)[-1].lower()
    cwd=inputs.get('workdir') or inputs.get('cwd') or inputs.get('Cwd') or row.get('cwd')
    if name in SHELL_TOOLS:
        switch=row.get('account_switch') if isinstance(row.get('account_switch'),list) else []
        return shell_check(inputs.get('command') or inputs.get('cmd') or inputs.get('CommandLine'),cwd,roots,switch)
    if name in EDIT_TOOLS:
        paths=[]
        for key in ['file_path','path','TargetFile','AbsolutePath']:
            if isinstance(inputs.get(key),str) and inputs[key].strip():paths.append(inputs[key])
        if name=='apply_patch':
            patch=inputs.get('patch') or inputs.get('input') or inputs.get('command') or ''
            paths+=re.findall(r'^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$',patch,re.M)
        if not paths:return 'unavailable','edit tool has no recognized target paths'
        if any(Path(path).name=='flow.json' for path in paths):
            return 'violation','formal flow.json must be changed through flow.py, not a file edit tool'
        try:
            if not all(inside(path,roots,cwd) for path in paths):
                return 'violation','orchestrator edited a file outside configured management directories'
        except (ValueError,OSError):
            return 'unavailable','edit path cannot be resolved'
        return 'clean','management record write'
    if name in READ_TOOLS or name in AGENT_TOOLS:
        return 'clean','read/delegation/progress tool; payload is not executed by the orchestrator'
    return 'unavailable','unknown tool; extend the host adapter rather than assume compliance'


def audit(log, conversation, management_roots):
    result=dict(status='unavailable',findings=[],captured_calls=0,log=str(Path(log).resolve()),
                scope='since the latest explicit begin for the bound conversation')
    if not conversation:
        result['reason']='audit requires a bound conversation ID';return result
    try:
        rows=[json.loads(line) for line in Path(log).read_text().splitlines() if line.strip()]
        if not all(isinstance(row,dict) and row.get('format')==FORMAT for row in rows):
            raise ValueError('invalid audit record')
        starts=[i for i,row in enumerate(rows) if row.get('session_id')==conversation and row.get('kind')=='begin']
        if not starts:raise ValueError('no bound audit begin marker')
        roots=[Path(root).resolve() for root in management_roots]
        for index,row in enumerate(rows[starts[-1]+1:],starts[-1]+2):
            if row.get('session_id')!=conversation:continue
            if row.get('kind')!='tool':raise ValueError('unexpected record kind')
            result['captured_calls']+=1
            status,reason=tool_check(row,roots)
            if status!='clean':result['findings'].append(dict(line=index,tool=row.get('tool'),status=status,reason=reason))
        result['status']='violation' if any(f['status']=='violation' for f in result['findings']) else 'unavailable' if result['findings'] else 'clean'
        result['limit']='Only captured tool calls are audited; this does not prove hook coverage, actual file changes, or enforce OS isolation.'
    except (OSError,ValueError,TypeError) as error:
        result['reason']=str(error)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['begin','record','check'])
    parser.add_argument('--host',choices=['agy','codex','claude'])
    parser.add_argument('--conversation',required=True)
    parser.add_argument('--log',type=Path,required=True)
    parser.add_argument('--management-root',action='append',default=[])
    parser.add_argument('--account-switch',action='append',default=[],help='user-configured account switch command recorded with each tool row')
    args=parser.parse_args()
    try:
        if args.command=='check':
            report=audit(args.log,args.conversation,args.management_root)
            print(json.dumps(report,ensure_ascii=False));return 0 if report['status']=='clean' else 1
        if not args.host:raise ValueError('begin/record require --host')
        event=json.load(sys.stdin)
        if not isinstance(event,dict):raise ValueError('invalid hook input')
        append_event(args.log,args.host,args.conversation,event,begin=args.command=='begin',account_switch=args.account_switch)
        print('{}');return 0
    except (OSError,ValueError) as error:
        print('{}');print('role audit unavailable: '+str(error),file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
