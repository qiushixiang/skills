#!/usr/bin/env python3
"""No-model tests for the Linux launcher and real hook wire format."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import start_agy_orchestrator as launcher


def result_text(result):
    return result['injectSteps'][0]['systemMessage']['systemMessage']


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.runtime=self.root/'.batch-flow/.agy-launch/test';self.runtime.mkdir(parents=True)
        self.records=self.root/'.batch-flow/records';self.records.mkdir()
        self.spec=dict(launch_id='test',python=sys.executable,workspace=str(self.root),
                      store=str(self.root/'.batch-flow/flow.json'),records=str(self.records),
                      runtime=str(self.runtime),profiles=[str(self.runtime/'agy-profile.json')])
        launcher.atomic(self.runtime/'spec.json',self.spec)
        self.env={launcher.ENV_KEY:'test'}

    def tearDown(self):self.tmp.cleanup()

    def event(self,name,inputs,owner='main'):
        return dict(conversationId=owner,toolCall=dict(name=name,args=inputs))

    def hook(self,event_name,event):return launcher.hook(event_name,self.runtime,event,self.env)

    def start(self):return self.hook('SessionStart',dict(conversationId='main',modelName='chosen'))

    def command(self,*parts):return shlex.join([sys.executable,str(launcher.SCRIPTS/'flow.py'),'--store',self.spec['store'],*parts])

    def test_parent_is_bound_and_capabilities_are_injected(self):
        result=self.start()
        self.assertIn('编排者能力',result['injectSteps'][0]['systemMessage']['systemMessage'])
        self.assertEqual(launcher.load(self.runtime/'owner.json')['conversation_id'],'main')
        self.assertEqual(self.hook('SessionStart',dict(conversationId='child',modelName='child-model')), {})
        child=json.loads((self.runtime/'children.jsonl').read_text())
        self.assertEqual(child['modelName'],'child-model')
        self.assertEqual(launcher.load(self.runtime/'owner.json')['conversation_id'],'main')

    def test_parent_commands_are_denied_before_execution_child_is_not_blocked(self):
        self.start()
        for command in ['git commit -m bad','git add a','git merge branch','bash build.sh','python3 tests/run.py']:
            event=self.event('run_command',dict(CommandLine=command))
            self.assertEqual(self.hook('PreToolUse',event)['decision'],'deny')
            event['conversationId']='child'
            self.assertEqual(self.hook('PreToolUse',event),{'decision':'allow'})

    def test_management_command_is_precise_and_cannot_escape(self):
        self.start_text=self.start()
        command=self.command('status')
        self.assertEqual(self.hook('PreToolUse',self.event('run_command',dict(CommandLine=command)))['decision'],'auto_approve')
        query=self.command('capabilities')
        self.assertEqual(self.hook('PreToolUse',self.event('run_command',dict(CommandLine=query)))['decision'],'auto_approve')
        denied=self.hook('PreToolUse',self.event('run_command',dict(CommandLine='codex --version')))
        self.assertIn('capabilities',denied['reason'])
        self.assertIn(self.spec['profiles'][0],denied['reason'])
        for invalid in [command+'; git commit',command+' && true',command+' --stor /tmp/other',
                        command.replace('status','snapshot'),self.command('record','--','git','commit'),
                        command.replace('--store','--store='+self.spec['store']),
                        command+' --note "$(touch /tmp/should-not-run)"',
                        command.replace(str(launcher.SCRIPTS/'flow.py'),'/tmp/flow.py')]:
            self.assertFalse(launcher.safe_command(invalid,self.spec),invalid)
        literal=self.command('feedback','--note','literal $(not-executed)')
        self.assertTrue(launcher.safe_command(literal,self.spec))
        card=shlex.join([self.spec['python'],str(launcher.SCRIPTS/'dispatch_card.py'),'next','--store',self.spec['store']])
        self.assertEqual(self.hook('PreToolUse',self.event('run_command',dict(CommandLine=card)))['decision'],'auto_approve')
        self.assertIn('dispatch_card.py',result_text(self.start_text))
        invoke=dict(TypeName='batch_flow_executor',Role='card-1',Prompt='read card')
        self.assertEqual(self.hook('PreToolUse',self.event('invoke_subagent',invoke))['decision'],'allow')
        logged=[json.loads(l) for l in (self.runtime/'decisions.jsonl').read_text().splitlines()]
        self.assertEqual((logged[-1]['tool'],logged[-1]['label']),('invoke_subagent','card-1'))  # next reads this label
        for invalid in [card.replace(' next ',' make '),card.replace(self.spec['store'],'/tmp/flow.json'),card+' --as-lead']:
            self.assertFalse(launcher.safe_command(invalid,self.spec),invalid)

    def test_file_tool_scope_including_symlinks_and_immutable_records(self):
        good=dict(TargetFile=str(self.records/'receipt.txt'),Overwrite=False,CodeContent='raw')
        self.assertTrue(launcher.gate('write_to_file',good,self.spec)[0])
        for path in [self.root/'kernel/a.c',Path(self.spec['store']),self.runtime/'spec.json']:
            self.assertFalse(launcher.gate('write_to_file',dict(good,TargetFile=str(path)),self.spec)[0])
        (self.records/'receipt.txt').write_text('existing')
        self.assertFalse(launcher.gate('write_to_file',good,self.spec)[0])
        (self.records/'escape').symlink_to(self.root/'kernel')
        self.assertFalse(launcher.gate('write_to_file',dict(good,TargetFile=str(self.records/'escape/a.txt')),self.spec)[0])
        self.assertFalse(launcher.gate('replace_file_content',dict(TargetFile=str(self.records/'receipt.txt')),self.spec)[0])
        self.assertTrue(launcher.gate('view_file',dict(AbsolutePath=str(launcher.SKILL/'SKILL.md')),self.spec)[0])
        self.assertFalse(launcher.gate('view_file',dict(AbsolutePath=str(self.root/'kernel/a.c')),self.spec)[0])

    def test_native_definition_and_invocation_use_actual_schema(self):
        define=dict(name='batch_flow_executor',description='implementation',system_prompt='execute within scope',
                    enable_write_tools=True,enable_subagent_tools=False,enable_mcp_tools=False)
        self.assertTrue(launcher.gate('define_subagent',define,self.spec)[0])
        pascal={key:value for key,value in zip(['Name','Description','SystemPrompt','EnableWriteTools','EnableSubagentTools','EnableMCPTools'],define.values())}
        self.assertTrue(launcher.gate('define_subagent',pascal,self.spec)[0])
        for changes in [dict(enable_subagent_tools=True),dict(enable_mcp_tools=True),dict(name=launcher.AGENT)]:
            self.assertFalse(launcher.gate('define_subagent',dict(define,**changes),self.spec)[0])
        invoke=dict(Subagents=[dict(TypeName='batch_flow_executor',Role='Executor T1',Prompt='payload')])
        self.assertTrue(launcher.gate('invoke_subagent',invoke,self.spec)[0])
        invoke['Subagents'][0]['Model']='flash'
        self.assertTrue(launcher.gate('invoke_subagent',invoke,self.spec)[0])
        self.assertTrue(launcher.gate('invoke_subagent',invoke['Subagents'][0],self.spec)[0])
        invoke['Subagents'][0]['TypeName']='self'
        self.assertFalse(launcher.gate('invoke_subagent',invoke,self.spec)[0])

    def test_mcp_task_input_and_repeated_timers_are_not_escape_hatches(self):
        self.assertFalse(launcher.gate('mcp__shell__execute',{},self.spec)[0])
        self.assertFalse(launcher.gate('manage_task',dict(Action='send_input',Input='git commit'),self.spec)[0])
        self.assertTrue(launcher.gate('schedule',dict(DurationSeconds=600,Prompt='progress'),self.spec)[0])
        self.assertFalse(launcher.gate('schedule',dict(DurationSeconds=600,CronExpression='* * * * *'),self.spec)[0])

    def test_missing_binding_fails_closed_and_unrelated_launch_is_ignored(self):
        event=self.event('run_command',dict(CommandLine='git commit'))
        self.assertEqual(self.hook('PreToolUse',event)['decision'],'deny')
        self.assertEqual(launcher.hook('PreToolUse',self.runtime,event,{}),{'decision':'allow'})

    def test_fresh_workspace_can_wait_for_approved_plan_without_stop_loop(self):
        self.start()
        result=self.hook('Stop',dict(conversationId='main'))
        self.assertEqual(result['decision'],'stop')
        self.assertIn('not project completion',result['reason'])
        self.hook('PostToolUse',self.event('write_to_file',dict(TargetFile=str(self.root/'business/file.c'))))
        self.assertEqual(self.hook('Stop',dict(conversationId='main'))['decision'],'continue')
        self.assertEqual(self.hook('Stop',dict(conversationId='main',terminationReason='user_canceled'))['decision'],'stop')

    def test_install_restore_preserves_other_hooks_and_user_agent(self):
        hooks=self.root/'.agents/hooks.json';hooks.parent.mkdir()
        original='{"existing": {"enabled": true}}\n'
        hooks.write_text(original)
        installed=launcher.install(self.root,self.runtime,self.spec)
        self.assertIn('existing',launcher.load(hooks))
        self.assertIn('PreToolUse',launcher.load(hooks)[installed[4]])
        launcher.cleanup(installed)
        self.assertEqual(hooks.read_text(),original)
        self.assertFalse(installed[0].exists())
        agent=self.root/'.agents/agents'/ (launcher.AGENT+'.md');agent.write_text('user definition')
        with self.assertRaises(ValueError):launcher.install(self.root,self.runtime,self.spec)
        self.assertEqual(agent.read_text(),'user definition')

    def test_cleanup_preserves_hooks_added_while_session_runs(self):
        installed=launcher.install(self.root,self.runtime,self.spec)
        hooks=installed[2];data=launcher.load(hooks);data['new-user-hook']={};launcher.atomic(hooks,data)
        launcher.cleanup(installed)
        self.assertEqual(launcher.load(hooks),{'new-user-hook':{}})

    def test_second_launcher_cannot_take_the_same_workspace(self):
        binary=self.root/'agy';binary.write_text('#!'+sys.executable+'\nprint("1.4.12")\n');binary.chmod(0o700)
        agents=self.root/'.agents';agents.mkdir()
        args=argparse.Namespace(workspace=str(self.root),task_base=str(self.root),store=None,model='user-model',effort='medium',binary=str(binary),dry_run=False)
        with (agents/'.batch-flow-orchestrator.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch('builtins.print'),self.assertRaisesRegex(ValueError,'already owns'):
                launcher.launch(args)
        self.assertFalse((agents/'hooks.json').exists())

    def test_profile_and_terminal_bypasses_are_denied(self):
        argv=[sys.executable,str(launcher.SCRIPTS/'agent_cli.py'),'run','--profile',self.spec['profiles'][0],
              '--cwd',str(self.root),'--run-dir',str(self.records/'run1'),'--prompt-file',str(self.records/'prompt.txt')]
        self.assertTrue(launcher.safe_command(shlex.join(argv),self.spec))
        for extra in [['--prof','/tmp/forged.json'],['--settings','/tmp/policy.json']]:
            self.assertFalse(launcher.safe_command(shlex.join(argv+extra),self.spec))
        self.assertFalse(launcher.gate('run_command',dict(CommandLine=self.command('status'),RequestedTerminalID='other'),self.spec)[0])

    def test_dry_run_does_not_install_hooks_or_call_a_model(self):
        binary=self.root/'agy';binary.write_text('#!'+sys.executable+'\nimport sys\nassert sys.argv[1:]==["--version"]\nprint("1.4.12")\n');binary.chmod(0o700)
        args=argparse.Namespace(workspace=str(self.root),task_base=str(self.root),store=None,model='user-model',effort='medium',binary=str(binary),dry_run=True)
        with patch('builtins.print'):self.assertEqual(launcher.launch(args),0)
        self.assertFalse((self.root/'.agents').exists())

    def test_fake_host_full_launch_loads_hooks_and_restores_configuration(self):
        binary=self.root/'agy'
        binary.write_text('#!'+sys.executable+'\n'+'''
import json,os,pathlib,shlex,subprocess,sys
if '--version' in sys.argv:print('1.4.12');sys.exit(0)
if '--help' in sys.argv:
 print('--mode --model --effort --output-format --conversation --settings');sys.exit(0)
assert sys.argv[sys.argv.index('--agent')+1]=='batch_flow_orchestrator'
settings=json.loads(sys.argv[sys.argv.index('--settings')+1])
assert settings['enableTerminalSandbox'] is False
assert '--sandbox' not in sys.argv
assert 'permissions' not in settings
hooks=json.loads(pathlib.Path('.agents/hooks.json').read_text())
definition=next(v for k,v in hooks.items() if k.startswith('batch-flow-orchestrator-'))
def call(stage,event):
 item=definition[stage][0]
 command=item['hooks'][0]['command'] if 'hooks' in item else item['command']
 p=subprocess.run(shlex.split(command),input=json.dumps(event),text=True,capture_output=True,check=True)
 return json.loads(p.stdout)
start=call('SessionStart',{'conversationId':'main','modelName':'explicit-main'})
assert 'injectSteps' in start
event={'conversationId':'main','toolCall':{'name':'run_command','args':{'CommandLine':'git commit -m unauthorized'}}}
assert call('PreToolUse',event)['decision']=='deny'
event['conversationId']='child';assert call('PreToolUse',event)=={'decision':'allow'}
assert call('SessionStart',{'conversationId':'child','modelName':'explicit-child'})=={}
assert call('Stop',{'conversationId':'main','terminationReason':'user_canceled'})=={'decision':'stop'}
print('FAKE_HOST_VERIFIED')
''')
        binary.chmod(0o700)
        args=argparse.Namespace(workspace=str(self.root),task_base=str(self.root),store=None,model='explicit-main',effort='medium',binary=str(binary),dry_run=False)
        with patch('builtins.print'):self.assertEqual(launcher.launch(args),0)
        self.assertFalse((self.root/'.agents/hooks.json').exists())
        self.assertFalse((self.root/'.agents/agents'/ (launcher.AGENT+'.md')).exists())
        self.assertEqual(len(list((self.root/'.batch-flow/.agy-launch').glob('*/owner.json'))),1)

    def test_task_base_binds_the_selected_task_and_keeps_source_workspace(self):
        task=self.root/'tasks/selected';task.mkdir(parents=True)
        other=self.root/'tasks/other';other.mkdir()
        binary=self.root/'agy';binary.write_text('#!'+sys.executable+'\nprint("1.4.12")\n');binary.chmod(0o700)
        command=[sys.executable,str(Path(launcher.__file__)),'--workspace',str(self.root),
                 '--task-base','tasks/selected','--binary',str(binary),'--dry-run']
        result=subprocess.run(command,capture_output=True,text=True,check=True)
        data=json.loads(result.stdout[result.stdout.index('{'):])
        self.assertEqual(data['task_base'],str(task))
        self.assertEqual(data['store'],str(task/'.batch-flow/flow.json'))
        self.assertIn(str(task),data['argv'][-1])
        self.assertNotIn(str(other),data['argv'][-1])
        self.assertFalse((task/'.batch-flow').exists())
        result=subprocess.run(command+['--store',str(other/'.batch-flow/flow.json')],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('selected task base',result.stderr)

    def test_missing_task_base_requires_user_choice_without_searching(self):
        args=argparse.Namespace(workspace=str(self.root),task_base=None,store=None,
             model='user-model',effort='medium',binary='missing-should-not-run',dry_run=True)
        with patch('sys.stdin.isatty',return_value=False),self.assertRaisesRegex(ValueError,'--task-base'):
            launcher.launch(args)
        self.assertFalse((self.root/'.agents').exists())

    def test_host_commands_require_checked_adapter_without_sandbox_override(self):
        self.spec['terminal_sandbox']=False
        check=shlex.join([sys.executable,str(launcher.SCRIPTS/'agent_cli.py'),'check','--profile',self.spec['profiles'][0]])
        self.assertFalse(launcher.gate('run_command',dict(CommandLine=check,BypassSandbox=True),self.spec)[0])
        self.assertTrue(launcher.gate('run_command',dict(CommandLine=check),self.spec)[0])
        for command in [self.command('status'),'codex --version',check+'; git commit',check.replace(self.spec['profiles'][0],'/tmp/forged.json')]:
            self.assertFalse(launcher.gate('run_command',dict(CommandLine=command,BypassSandbox=True),self.spec)[0])
        self.assertTrue(launcher.gate('run_command',dict(CommandLine=self.command('status')),self.spec)[0])
        # Build canonical argv, not a shell fragment.
        full_check=shlex.join(shlex.split(check)+['--model','gpt-6.1-sol','--effort','default','--cwd',str(self.root),
                                                   '--conversation','01a1a540-b6eb-7ab0-aa1c-92f0cb428e69','--resume-from',str(self.records/'first')])
        self.assertTrue(launcher.gate('run_command',dict(CommandLine=full_check),self.spec)[0])
        self.assertFalse(launcher.safe_command(full_check.replace(str(self.records/'first'),'/tmp/other-run'),self.spec))

    def test_terminal_asks_for_task_base_and_injects_that_choice(self):
        task=self.root/'chosen';task.mkdir()
        binary=self.root/'agy';binary.write_text('#!'+sys.executable+'\nprint("1.4.12")\n');binary.chmod(0o700)
        args=argparse.Namespace(workspace=str(self.root),task_base=None,store=None,
             model='user-model',effort='medium',binary=str(binary),dry_run=True)
        with patch('sys.stdin.isatty',return_value=True),patch('builtins.input',return_value='chosen') as ask,patch('builtins.print') as output:
            self.assertEqual(launcher.launch(args),0)
        ask.assert_called_once()
        report=json.loads(output.call_args.args[0])
        self.assertEqual(report['task_base'],str(task))
        self.assertEqual(report['store'],str(task/'.batch-flow/flow.json'))


if __name__=='__main__':unittest.main(verbosity=2)
