#!/usr/bin/env python3
"""Linux AGY orchestrator launcher and conversation-scoped PreToolUse gate."""
import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import account_switch

SCRIPTS=Path(__file__).resolve().parent
SKILL=SCRIPTS.parent
AGENT='batch_flow_orchestrator'
ENV_KEY='BATCH_FLOW_AGY_LAUNCH'
# Hook payloads, decision values and the native tool schema are undocumented runtime behaviour (see
# hosts-agy-codex.md); they were verified live on 1.4.12-1.5.1.
# Lowest version whose hooks/native-tool interfaces were verified live. Newer releases are assumed
# backward compatible and accepted; the SessionStart binding watchdog still ends an unbound session.
MIN_VERSION='1.4.12'
def version_key(text):
    parts=re.findall(r'\d+',text.strip().split('-')[0])
    if not parts:raise ValueError('cannot parse AGY version: %r'%text)
    return tuple(int(x) for x in parts)
BIND_SECONDS=120
NATIVE=['define_subagent','invoke_subagent','send_message','manage_subagents',
        'manage_task','command_status','schedule','ask_question','finish','wait']
TOOLS=NATIVE+['view_file','write_to_file','run_command']
ROLES={'batch_flow_executor','batch_flow_technical_lead','batch_flow_reviewer','batch_flow_third_party'}
FLOW_COMMANDS={'status','capabilities','observe','pre_yield','apply','validate','render','metrics','start','finish',
               'signal','resolve','repin_requirement','classify_attempt','resume_attempt','set_models',
               'set_user_mode','register_dispatch','close','authorize','change_subject','feedback',
               'decision_progress','decision_start','init','plan_update','design_refresh'}
FLAGS={
    'flow.py':{'--store','--actor','--revision','--request','--data','--attempt','--task','--id','--signal',
               '--reason','--summary','--note','--tool','--model','--effort','--conversation','--result',
               '--evidence','--blocks','--clarification','--context-mode','--audit-log','--audit-management-root'},
    'progress_timer.py':{'--ticket','--task','--goal','--handle','--seconds'},
    'dispatch_card.py':{'--store','--card'},
    'agent_cli.py':{'--profile','--task','--cwd','--model','--effort','--prompt-file','--run-dir',
                    '--access','--conversation','--resume-from','--add-dir','--output','--catalog','--name'},
}


def atomic(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.agy-launch-')
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def load(path):return json.loads(Path(path).read_text())


def within(path,root):
    path=Path(path).resolve();root=Path(root).resolve()
    return path==root or root in path.parents


def next_command(spec):
    return shlex.join([spec['python'],str(SCRIPTS/'dispatch_card.py'),'next','--store',spec['store']])


def loop_text(spec):
    status=shlex.join([spec['python'],str(SCRIPTS/'flow.py'),'--store',spec['store'],'status'])
    switch=spec.get('account_switch') or []
    return (('\n账号切换命令（用户配置，何时使用听用户指示）：'+' / '.join(switch)+'；只能用该绝对路径，参数仅限 -k、-n 和至多一个目标账号。' if switch else '')+
            '\n启动时先运行 '+status+'，展示状态后结束本轮，等用户明确要求继续；不要自动运行 next。'
            '\n用户口语指定角色模型时，先按 linux-cli.md 使用 agent_cli.py models 查询指定后端，再用 resolve-model 校验映射；唯一匹配直接 set_models，真实多候选才确认变体。未指定 effort 沿用 default，不重复询问。'
            '\n用户要求继续后，运行 '+next_command(spec)+'，按输出的 do 字段办：'
            'do=call 时逐字照抄 tool 与 input 发起这一个调用（字段一个不改），返回后立即再运行 next；'
            'do=wait 时用 command_status/wait 等在跑的命令结束，再运行 next；'
            'do=ask_user 时把 items 原样告诉用户，拿到原话后按 todo 用 flow.py 登记，再运行 next；'
            'do=report/done 时把 say、problems、disposition 原样告诉用户。回收、登记、重启后的清理都由 next 完成。'
            '中断后直接重新启动编排者即可。')


def capabilities():
    return ('AGY batch-flow 编排者能力\n'
            '允许：台账管理、定义/派发/回收代理、进度与一次性计时、用户汇报。\n'
            '文件：只读技能 Markdown 和管理记录；只新建 records 内的 json/txt/md 记录。\n'
            '命令：仅固定 dispatch_card.py next、flow.py、progress_timer.py 和已核查 profile 的 agent_cli.py。\n'
            '禁止：直接改业务代码、git add/commit/merge、产品构建/测试、任意 shell、MCP。\n'
            '执行者：独立工具配置；主会话门禁不施加到子代理，原有权限审批仍有效。\n'
            '终端：本编排会话关闭 AGY 终端沙箱；固定脚本白名单和执行前门禁仍有效。\n'
            'AGY 1.4.12 的启动回执仍可能列出完整工具集；这里按会话在执行前拒绝，不宣称工具已隐藏。\n'
            '这是宿主执行前门禁，不是 OS 隔离；需真实宿主加载 hooks 才能拦截。')


def agent_text():
    tools='\n'.join('  - '+tool for tool in TOOLS)
    return ('---\nname: '+AGENT+'\ndescription: Restricted batch-flow coordinator\n'
            'mainAgent: true\nsubagent: false\ninheritMcp: false\nmcpServers: []\n'
            'tools:\n'+tools+'\n---\n\n'+capabilities()+
            '\n\n只通过 start_agy_orchestrator.py 启动。先读取当前技能的 SKILL.md、编排者角色文档和 AGY 宿主指南。'
            '只执行 orchestrator_todo，将 subagent_payload 原样派给责任角色；不代做技术工作。'
            'run_command 仅使用 SessionStart 提供的固定脚本路径、显式 --store 和 shell 规范引用；'
            '没有启动器注入的能力及路径说明时停止并报告配置缺失。'
            '主会话 CLI 模型配置不证明子代理模型已匹配；原生工具不能表达指定模型时用固定 CLI 入口。\n')


def safe_command(command,spec):
    """Only canonical literal argv, never evaluate a shell to inspect a command."""
    if not isinstance(command,str):return False
    try:argv=shlex.split(command)
    except ValueError:return False
    # Canonical shell quoting ensures substitutions/operators cannot be smuggled
    # through double quotes or command separators. Quoted JSON stays literal.
    if not argv or command!=shlex.join(argv):return False
    if account_switch.allowed(argv,spec.get('account_switch',[])):return True
    if len(argv)<3 or argv[0]!=spec['python']:return False
    script=Path(argv[1])
    if not script.is_absolute():return False
    script=script.resolve()
    if script not in {SCRIPTS/'flow.py',SCRIPTS/'progress_timer.py',SCRIPTS/'agent_cli.py',SCRIPTS/'dispatch_card.py'}:return False
    args=argv[2:]
    if '--' in args:return False
    def option(flag):
        if args.count(flag)!=1:return None
        i=args.index(flag)
        return args[i+1] if i+1<len(args) else None
    # Reject parser aliases and = spellings to keep the reviewed form singular.
    if any(token.startswith('-') and token not in FLAGS[script.name] for token in args):return False
    if script.name=='flow.py':
        if option('--store')!=spec['store']:return False
        if any(token in args for token in ['--cwd','--repo','--paths','--git-ref']):return False
        return any(token in FLOW_COMMANDS for token in args) and not any(token in args for token in ['snapshot','record'])
    if script.name=='dispatch_card.py':
        # The orchestrator's single command; it makes, starts, collects and registers cards itself.
        return args[0] in {'next','show'} and option('--store')==spec['store']
    if script.name=='progress_timer.py':
        ticket=option('--ticket')
        return args[0] in {'wait','cancel'} and bool(ticket) and within(ticket,spec['records'])
    profile=option('--profile')
    if args[0] in {'models','resolve-model'}:
        from model_catalog import safe_metadata_command
        return safe_metadata_command(args,spec.get('profiles',[]),spec['records'])
    if args[0]=='collect':
        output=option('--run-dir')
        return bool(output) and within(output,spec['records'])
    if args[0]=='check':
        if profile not in spec.get('profiles',[]):return False
        allowed={'--profile','--model','--effort','--cwd','--conversation','--resume-from','--access','--add-dir'}
        if any(token.startswith('-') and token not in allowed for token in args):return False
        cwd=option('--cwd');source=option('--resume-from')
        return (not cwd or any(within(cwd,root) for root in [spec['workspace'],spec.get('task_base',spec['workspace'])])) and (not source or within(source,spec['records'])) and all(i+1<len(args) and any(within(args[i+1],root) for root in [spec['workspace'],spec.get('task_base',spec['workspace'])]) for i,token in enumerate(args) if token=='--add-dir')
    if args[0]!='run' or profile not in spec.get('profiles',[]):return False
    if '--settings' in args:return False
    source=option('--resume-from')
    if source and not within(source,spec['records']):return False
    cwd=option('--cwd');output=option('--run-dir');prompt=option('--prompt-file')
    if not cwd or not any(within(cwd,root) for root in [spec['workspace'],spec.get('task_base',spec['workspace'])]):return False
    if not output or not within(output,spec['records']) or not prompt or not within(prompt,spec['records']):return False
    for i,token in enumerate(args):
        if token=='--add-dir' and (i+1==len(args) or not any(within(args[i+1],root) for root in [spec['workspace'],spec.get('task_base',spec['workspace'])])):return False
    return True


def gate(name,inputs,spec):
    if name not in TOOLS or not isinstance(inputs,dict):return False,'tool is not in the orchestrator list'
    if name=='run_command':
        if inputs.get('RequestedTerminalID') or inputs.get('RunPersistent'):
            return False,'reusing/persisting a terminal is not allowed'
        if inputs.get('BypassSandbox'):
            return False,'sandbox overrides are not needed or permitted; use the launcher execution policy'
        return (True,'fixed management command') if safe_command(inputs.get('CommandLine'),spec) else (False,'only canonical fixed management commands are allowed')
    if name=='view_file':
        path=inputs.get('AbsolutePath')
        ok=isinstance(path,str) and Path(path).is_absolute() and (
            (within(path,SKILL) and Path(path).suffix=='.md') or
            within(path,spec['records']) or Path(path).resolve()==Path(spec['store']) or
            Path(path).resolve()==Path(spec['runtime'])/'children.jsonl')
        return bool(ok),'read only skill Markdown, the bound ledger or management records'
    if name=='write_to_file':
        path=inputs.get('TargetFile')
        ok=isinstance(path,str) and Path(path).is_absolute() and within(path,spec['records']) and Path(path).suffix in {'.json','.txt','.md'} and not Path(path).exists() and inputs.get('Overwrite') is False
        return bool(ok),'only create immutable management records; no business/config/ledger edits'
    if name=='define_subagent':
        # The custom-main-agent registry exposes Cortex PascalCase fields;
        # the generic CLI docs describe a separate snake_case interface.
        aliases={'Name':'name','Description':'description','SystemPrompt':'system_prompt',
                 'EnableWriteTools':'enable_write_tools','EnableSubagentTools':'enable_subagent_tools','EnableMCPTools':'enable_mcp_tools'}
        if any(key in inputs for key in aliases):
            if not set(inputs)<=set(aliases):return False,'do not mix agent definition schemas'
            inputs={aliases[key]:value for key,value in inputs.items()}
        allowed={'name','description','system_prompt','enable_write_tools','enable_subagent_tools','enable_mcp_tools'}
        ok=set(inputs)<=allowed and inputs.get('name') in ROLES and inputs.get('enable_mcp_tools') is False and isinstance(inputs.get('enable_write_tools'),bool) and isinstance(inputs.get('enable_subagent_tools'),bool)
        ok=ok and all(isinstance(inputs.get(key),str) and inputs[key].strip() for key in ['description','system_prompt'])
        if inputs.get('name')!='batch_flow_technical_lead' and inputs.get('enable_subagent_tools') is not False:ok=False
        if inputs.get('name') in {'batch_flow_reviewer','batch_flow_third_party'} and inputs.get('enable_write_tools') is not False:ok=False
        return bool(ok),'use the verified agent definition; executor cannot spawn agents or enable MCP'
    if name=='invoke_subagent':
        agents=inputs.get('Subagents') if set(inputs)=={'Subagents'} else [inputs]
        ok=isinstance(agents,list) and bool(agents) and all(isinstance(a,dict) and set(a)<={'TypeName','Role','Prompt','Workspace','Model'} and a.get('TypeName') in ROLES and isinstance(a.get('Prompt'),str) and bool(a['Prompt'].strip()) and ('Model' not in a or isinstance(a['Model'],str)) for a in agents)
        return bool(ok),'use a dedicated role and the runtime-exposed single/array schema; any Model value must follow the approved configuration'
    if name=='manage_task':return inputs.get('Action') in {'list','status'},'task input injection/termination is not a coordinator shortcut'
    if name=='schedule':
        seconds=inputs.get('DurationSeconds')
        return isinstance(seconds,(int,float)) and not isinstance(seconds,bool) and 0<seconds<=600 and not inputs.get('CronExpression') and inputs.get('MaxIterations',1)==1,'only one-shot progress timers up to 600 seconds'
    return True,'native coordination tool'


def hook(event_name,runtime,event,environ=None):
    spec=load(Path(runtime)/'spec.json');environ=os.environ if environ is None else environ
    passthrough={'decision':'allow'} if event_name=='PreToolUse' else {'decision':'stop'} if event_name=='Stop' else {}
    if environ.get(ENV_KEY)!=spec['launch_id']:return passthrough
    actual=event.get('conversationId')
    if not isinstance(actual,str) or not actual:
        return {'decision':'deny','reason':'missing conversation identity'} if event_name=='PreToolUse' else {}
    owner_path=Path(runtime)/'owner.json'
    if event_name=='SessionStart' and not owner_path.exists():
        with owner_path.open('x') as stream:json.dump({'conversation_id':actual,'model_name':event.get('modelName')},stream)
        import role_audit
        role_audit.append_event(Path(runtime)/'audit.jsonl','agy',actual,event,begin=True)
        example=shlex.join([spec['python'],str(SCRIPTS/'flow.py'),'--store',spec['store'],'status'])
        paths='\n技能目录：'+str(SKILL)+'\n当前任务 base：'+spec.get('task_base',str(Path(spec['store']).parent.parent))+'\n台账：'+spec['store']+'\n记录目录：'+spec['records']+'\n子会话实际宿主元数据：'+str(Path(runtime)/'children.jsonl')+'\n核查过的 CLI profiles：'+json.dumps(spec['profiles'])+'\n管理命令示例：'+example+'\n仅协调本任务，不自动搜索或切换其他任务。命令须使用这里的绝对 Python 路径及 shlex.join 等价规范引用，显式指定 --store；禁止 record 和 snapshot。'
        paths+='\n能力可随时重新查询：'+shlex.join([spec['python'],str(SCRIPTS/'flow.py'),'capabilities','--store',spec['store']])+'。status/pre_yield 的 host_capabilities 和派发动作 cli_adapter.profile 也会重新提供路径；上下文压缩后先查询，不猜测 profile，不把遗忘路径当成无权限或要求用户换模型。'
        if spec.get('terminal_sandbox') is False:
            paths+='\n本编排会话的终端沙箱已关闭，命令仍必须通过固定脚本门禁，不传 BypassSandbox。首次 start/decision_start 前对拟用 profile 运行 agent_cli.py check --profile <路径> --model <配置模型> --effort <配置值> --cwd <工作目录>，续接另给 --conversation <实际原生会话ID> --resume-from <上轮成功run-dir>，保存真实输出；失败先处理环境，不登记技术尝试。effort=default 表示不覆盖CLI配置，不发送字符串default、不改成medium。逻辑会话名不可用于Codex续接，旧handling登记修复按contexts.md。check 不调用模型，也不证明认证或网络已就绪。不得直接运行 agy/codex/claude/opencode 或读取其认证配置。代理进程沿各自原有权限和沙箱执行。'
        return {'injectSteps':[{'systemMessage':{'systemMessage':capabilities()+loop_text(spec)+paths}}]}
    if not owner_path.exists():
        return {'decision':'deny','reason':'launcher SessionStart binding is missing'} if event_name=='PreToolUse' else {}
    owner=load(owner_path)['conversation_id']
    if actual!=owner:
        if event_name in {'SessionStart','PreToolUse'}:
            # Capture host metadata rather than asking the model to guess its identity.
            with (Path(runtime)/'children.jsonl').open('a') as stream:
                fcntl.flock(stream,fcntl.LOCK_EX)
                stream.write(json.dumps({key:event.get(key) for key in ['conversationId','modelName','workspacePaths','transcriptPath']})+'\n')
        return passthrough  # AGY requires an explicit allow; {} can deny a child.
    if event_name=='PreToolUse':
        call=event.get('toolCall',{});inputs=call.get('args')
        if isinstance(inputs,str):
            try:inputs=json.loads(inputs)
            except ValueError:inputs=None
        allowed,reason=gate(call.get('name'),inputs,spec)
        if not allowed and call.get('name')=='run_command':
            reason+='; Recover current launcher capabilities with '+shlex.join([spec['python'],str(SCRIPTS/'flow.py'),'capabilities','--store',spec['store']])+'; registered profiles: '+json.dumps(spec.get('profiles',[]))+'; a rejected guessed path does not prove missing CLI permission.'
        decision='auto_approve' if allowed and call.get('name')=='run_command' else 'allow' if allowed else 'deny'
        with (Path(runtime)/'decisions.jsonl').open('a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            # label: invoke_subagent Role / define_subagent Name, so dispatch_card.py next can see what was sent.
            label=(inputs.get('Role') or inputs.get('Name') or inputs.get('name')) if isinstance(inputs,dict) else None
            stream.write(json.dumps(dict(conversation_id=actual,tool=call.get('name'),decision=decision,label=label,
                reason=reason,step=event.get('stepIdx'),args_sha256=hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()))+'\n')
        return {'decision':decision,'reason':reason}
    if event_name=='PostToolUse':
        import role_audit
        role_audit.append_event(Path(runtime)/'audit.jsonl','agy',owner,event,account_switch=spec.get('account_switch',[]))
    if event_name=='Stop':
        if not Path(spec['store']).is_file():
            from stop_hook_pre_yield import explicit_stop
            if explicit_stop(event):return {'decision':'stop'}
            import role_audit
            audit=role_audit.audit(Path(runtime)/'audit.jsonl',owner,[spec['records']])
            if audit['status']!='clean':
                return {'decision':'continue','reason':'orchestrator role audit '+audit['status']+'; preserve evidence and resolve host/audit issues without product work'}
            return {'decision':'stop','reason':'No initialized ledger yet; report missing approved plan/role configuration to the user. This is not project completion.'}
        command=[spec['python'],str(SCRIPTS/'stop_hook_pre_yield.py'),'--host','agy','--store',spec['store'],
                 '--role','orchestrator','--conversation',owner,'--audit-log',str(Path(runtime)/'audit.jsonl'),
                 '--inspect-until-next','--next-command',next_command(spec)]
        result=subprocess.run(command,input=json.dumps(event),capture_output=True,text=True,timeout=15,check=True)
        return json.loads(result.stdout)
    return {}


def install(workspace,runtime,spec):
    agents=workspace/'.agents/agents';agents.mkdir(parents=True,exist_ok=True)
    agent=agents/(AGENT+'.md');text=agent_text()
    created=not agent.exists()
    if not created and agent.read_text()!=text:raise ValueError('existing orchestrator agent differs; refusing to overwrite')
    hooks=workspace/'.agents/hooks.json'
    original=hooks.read_text() if hooks.exists() else None
    config=json.loads(original) if original is not None else {}
    if not isinstance(config,dict):raise ValueError('existing hooks.json is not an object')
    key='batch-flow-orchestrator-'+spec['launch_id']
    def command(event):
        return shlex.join([spec['python'],str(Path(__file__).resolve()),'hook','--runtime',str(runtime),'--event',event])
    definition={'SessionStart':[{'type':'command','command':command('SessionStart'),'timeout':10}]}
    for event in ['PreToolUse','PostToolUse']:
        definition[event]=[{'matcher':'.*','hooks':[{'type':'command','command':command(event),'timeout':10}]}]
    definition['Stop']=[{'type':'command','command':command('Stop'),'timeout':20}]
    config[key]=definition
    if created:
        with agent.open('x') as stream:stream.write(text)
    try:atomic(hooks,config)
    except OSError:
        if created and agent.read_text()==text:agent.unlink()
        raise
    return agent,created,hooks,original,key,definition


def cleanup(installed):
    agent,created,hooks,original,key,definition=installed
    config=load(hooks)
    if config.get(key)!=definition:raise ValueError('launcher hook changed during execution; preserve it for inspection')
    del config[key]
    prior=json.loads(original) if original is not None else {}
    if config==prior:
        if original is None:hooks.unlink()
        else:hooks.write_text(original)
    else:atomic(hooks,config)
    if created and agent.is_file() and agent.read_text()==agent_text():agent.unlink()


def launch(args):
    if sys.platform!='linux':raise ValueError('Linux only')
    workspace=Path(args.workspace).resolve(strict=True)
    if not workspace.is_dir():raise ValueError('workspace must be a directory')
    selected=getattr(args,'task_base',None)
    if not selected:
        if not sys.stdin.isatty():raise ValueError('specify the current task directory with --task-base; no task is selected automatically')
        selected=input('请输入本会话的任务 base 目录：').strip()
        if not selected:raise ValueError('task base must not be empty')
    task_base=Path(selected).expanduser()
    if not task_base.is_absolute():task_base=workspace/task_base
    task_base=task_base.resolve(strict=True)
    if not task_base.is_dir():raise ValueError('task base must be an existing directory')
    store=Path(args.store).expanduser() if args.store else task_base/'.batch-flow/flow.json'
    if not store.is_absolute():store=task_base/store
    store=store.resolve()
    if not within(store,task_base):raise ValueError('--store must belong to the selected task base')
    if store.name!='flow.json' or store.parent in {workspace,task_base}:raise ValueError('store must be flow.json inside a dedicated management directory')
    binary=shutil.which(args.binary)
    if not binary:raise ValueError('AGY executable not found')
    result=subprocess.run([binary,'--version'],capture_output=True,text=True,check=True,timeout=15)
    version=result.stdout.strip().split()[0] if result.stdout.strip() else ''
    if version_key(version)<version_key(MIN_VERSION):raise ValueError('AGY %s is older than the minimum supported %s (hook/tool interfaces unverified)'%(version,MIN_VERSION))
    if not args.model or not args.effort:raise ValueError('model and effort must not be empty')
    spec=dict(launch_id=uuid.uuid4().hex,python=sys.executable,workspace=str(workspace),task_base=str(task_base),store=str(store),records=str(store.parent/'records'),profiles=[],model=args.model,effort=args.effort,terminal_sandbox=False,agy_version=version,
              account_switch=[account_switch.configured(p) for p in (getattr(args,'account_switch',None) or [])])
    runtime=store.parent/'.agy-launch'/spec['launch_id']
    spec['runtime']=str(runtime)
    argv=[binary,'--agent',AGENT,'--model',args.model,'--effort',args.effort,'--mode','accept-edits',
          '--settings',json.dumps({'toolPermission':'request-review','enableTerminalSandbox':False}),
          '--prompt-interactive','你是 batch-flow 编排者。'+capabilities()+'\n当前任务 base：'+str(task_base)+'\n本任务台账：'+str(store)+'\n只协调这个任务，不搜索或切换其他任务。'+loop_text(spec)+'\n现在只运行 status 并展示状态，等用户明确要求继续后再运行 next。']
    if not within(task_base,workspace):argv[1:1]=['--add-dir',str(task_base)]
    print(capabilities(),flush=True)
    print('主模型：'+args.model+' / '+args.effort+'；台账：'+str(store),flush=True)
    print('当前任务 base：'+str(task_base),flush=True)
    if args.dry_run:
        print(json.dumps(dict(event='AGY_LAUNCH_DRY_RUN',agy_version=version,task_base=str(task_base),store=str(store),argv=argv,agent=agent_text(),runtime=str(runtime)),ensure_ascii=False,indent=2))
        return 0
    store.parent.mkdir(parents=True,exist_ok=True)
    (workspace/'.agents').mkdir(parents=True,exist_ok=True)
    with (workspace/'.agents/.batch-flow-orchestrator.lock').open('a') as workspace_lock, (store.parent/'.agy-orchestrator.lock').open('a') as ledger_lock, \
            (store.parent/'.orchestrator.lock').open('a') as any_host_lock:
        # .orchestrator.lock is shared with the Claude Code launcher: one orchestrator per ledger on any host,
        # so cards of an earlier launch can be treated as finished or interrupted by dispatch_card.py next.
        for lock in [workspace_lock,ledger_lock,any_host_lock]:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('an orchestrator launcher already owns this workspace or ledger')
        runtime.mkdir(parents=True,mode=0o700)
        Path(spec['records']).mkdir(exist_ok=True)
        # Profiles are launcher-owned; the main session cannot edit them.
        import agent_cli
        for backend in agent_cli.REQUIRED:
            executable=binary if backend=='agy' else shutil.which(backend)
            if executable:
                profile=runtime/(backend+'-profile.json')
                try:
                    agent_cli.probe(backend,profile,executable)
                    spec['profiles'].append(str(profile))
                except (OSError,ValueError,subprocess.SubprocessError) as error:
                    if backend=='agy':raise
                    print(backend+' CLI profile unavailable: '+str(error),file=sys.stderr)
        spec['started_at']=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        atomic(runtime/'spec.json',spec)
        installed=install(workspace,runtime,spec)
        env=os.environ.copy();env[ENV_KEY]=spec['launch_id']
        try:
            child=subprocess.Popen(argv,cwd=workspace,env=env)
            # 1.5.1 live run: when the workspace is trusted for the first time inside the session, hooks.json is
            # loaded after SessionStart and the session runs with no gate at all. Never leave such a session running.
            deadline=time.monotonic()+BIND_SECONDS
            while child.poll() is None and not (runtime/'owner.json').exists() and time.monotonic()<deadline:
                time.sleep(1)
            if child.poll() is None and not (runtime/'owner.json').exists():
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait()
                print('AGY 在 %d 秒内未加载本次 hooks（首次信任工作区时常见），门禁未生效，已结束会话；直接重新启动即可。'%BIND_SECONDS,file=sys.stderr)
                return 2
            code=child.wait()
            if not (runtime/'owner.json').exists():
                print('AGY 未回传 SessionStart 绑定；不能报告本次门禁已生效。',file=sys.stderr)
                return code or 2
            return code
        finally:cleanup(installed)


def main():
    if len(sys.argv)>1 and sys.argv[1]=='hook':
        parser=argparse.ArgumentParser();parser.add_argument('--runtime',type=Path,required=True)
        parser.add_argument('--event',choices=['SessionStart','PreToolUse','PostToolUse','Stop'],required=True)
        args=parser.parse_args(sys.argv[2:])
        try:result=hook(args.event,args.runtime,json.load(sys.stdin))
        except Exception as error:
            result={'decision':'deny' if args.event=='PreToolUse' else 'continue','reason':'orchestrator guard unavailable: '+str(error)} if args.event in {'PreToolUse','Stop'} else {}
            print(str(error),file=sys.stderr)
        print(json.dumps(result,ensure_ascii=False));return 0
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',default=str(Path.cwd()))
    parser.add_argument('--task-base',help='current task directory; relative paths use --workspace; prompts on a terminal if omitted')
    parser.add_argument('--store',help='flow.json inside the selected task; relative paths use --task-base')
    parser.add_argument('--model',default='gemini-3.8-flash-medium',help='main model (default: gemini-3.8-flash-medium)')
    parser.add_argument('--effort',default='medium',choices=['low','medium','high','xhigh','max'],help='reasoning effort (default: medium)')
    parser.add_argument('--binary',default='agy');parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--account-switch',action='append',help='absolute path of a user account switch command the orchestrator may run (repeatable)')
    args=parser.parse_args()
    try:return launch(args)
    except KeyboardInterrupt:
        print('AGY launcher cancelled',file=sys.stderr);return 130
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        print('AGY launcher: '+str(error),file=sys.stderr);return 2


if __name__=='__main__':sys.exit(main())
