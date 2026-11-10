#!/usr/bin/env python3
"""CLI contracts tested with local fake executables; no models called."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import agent_cli as cli


class AdapterTests(unittest.TestCase):
    def test_codex_launch_policy_is_explicit_on_new_and_resumed_calls(self):
        for conversation in (None, 'actual-session'):
            self.args.conversation = conversation
            self.args.approval_policy = 'on-request'
            argv, _ = cli.build_command(dict(backend='codex', binary='/bin/codex'), self.args, 'test')
            self.assertIn('approval_policy="on-request"', argv)
            self.assertEqual(argv[argv.index('-s') + 1], 'read-only')
            if conversation:
                self.assertLess(argv.index('approval_policy="on-request"'), argv.index('resume'))

    def test_restricted_parent_is_rejected_without_starting_cli(self):
        with patch.object(Path, 'read_text', return_value='NoNewPrivs:\t1\n'), patch.object(cli.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'HOST_ENVIRONMENT_BLOCKED'):
                cli.check_execution(dict(require_host=True))
            launch.assert_not_called()
        self.assertFalse(Path(self.args.run_dir).exists())

    def test_host_check_does_not_override_sandbox_or_directory_scope(self):
        with patch.object(Path, 'read_text', return_value='NoNewPrivs:\t0\n'):
            result = cli.check_execution(dict(require_host=True, add_dirs=[str(self.root)]))
            self.assertTrue(result['require_host'])
            with self.assertRaisesRegex(ValueError, 'add-dir'):
                cli.check_execution(dict(add_dirs=['/']))
        # No opt-in requirement keeps normal sandboxed tasks compatible.
        self.assertFalse(cli.check_execution({})['require_host'])

    def test_unconfigured_backends_keep_relative_add_dir_behavior(self):
        for backend in ('agy', 'claude', 'opencode', 'codex'):
            self.args.add_dir = ['.']
            self.args.effort = 'default'
            self.args.model = 'test/model' if backend == 'opencode' else 'explicit-model'
            cli.validate_call(dict(backend=backend), self.args)
            self.assertEqual(self.args.add_dir, [str(Path.cwd())])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.prompt=self.root/'prompt.txt'
        self.prompt.write_text('literal $(touch should-not-exist) `echo nope`\n中文')
        self.args=argparse.Namespace(cwd=str(self.root),task='T1',model='explicit-model',
            effort='high',access='read',conversation=None,add_dir=[],settings=None,
            prompt_file=str(self.prompt),run_dir=str(self.root/'run'))

    def tearDown(self):self.tmp.cleanup()

    def test_commands_and_resume_permissions(self):
        for backend in cli.REQUIRED:
            self.args.model='test/explicit-model' if backend=='opencode' else 'explicit-model'
            self.args.effort='default' if backend=='opencode' else 'high'
            for access in ['read','write']:
                for resume in [None,'actual-session']:
                    with self.subTest(backend=backend,access=access,resume=resume):
                        self.args.access=access;self.args.conversation=resume
                        argv,stdin=cli.build_command(dict(backend=backend,binary='/bin/'+backend),self.args,self.prompt.read_text())
                        self.assertIn(self.args.model,argv)
                        for flag in ['--continue','--last','--dangerously-skip-permissions']:
                            self.assertNotIn(flag,argv)
                        if backend=='codex':
                            self.assertEqual(stdin,self.prompt.read_text())
                            self.assertIn('read-only' if access=='read' else 'workspace-write',argv)
                            if resume:
                                self.assertLess(argv.index('-s'),argv.index('resume'))
                                self.assertEqual(argv[-3:],['resume',resume,'-'])
                        elif backend=='agy':
                            self.assertIn(str(self.prompt),argv[-1])
                            self.assertNotIn(self.prompt.read_text(),argv)
                            self.assertIn('accept-edits',argv)
                            self.assertNotIn('plan',argv)
                            if access=='read':
                                deny=json.loads(argv[argv.index('--settings')+1])['permissions']['deny']
                                self.assertIn('write_file(*)',deny)
                            else:
                                self.assertNotIn('--settings',argv)
                        elif backend=='claude':
                            self.assertEqual(stdin,self.prompt.read_text())
                            self.assertIn('default' if access=='read' else 'acceptEdits',argv)
                            self.assertNotIn('plan',argv)
                            if access=='read':
                                self.assertEqual(argv[argv.index('--disallowedTools')+1],'Edit Write NotebookEdit')
                            else:
                                self.assertNotIn('--disallowedTools',argv)
                        else:
                            self.assertIsNone(stdin)
                            self.assertEqual(argv[argv.index('--file')+1],str(self.prompt))
                            self.assertNotIn(self.prompt.read_text(),argv)
                            self.assertIn('--standalone',argv)
                            self.assertNotIn('--auto',argv)
                            self.assertEqual('--session' in argv,bool(resume))
                            policy=json.loads(cli.opencode_environment(self.args)['OPENCODE_CONFIG_CONTENT'])
                            edits=policy['agents']['build']['permissions']
                            self.assertEqual(dict(action='edit',resource='*',effect='deny') in edits,access=='read')

    def test_failed_and_empty_success_envelopes(self):
        for backend,good in [('agy',dict(conversation_id='01a1a540-b6eb-7ab0-aa1c-92f0cb428e69',status='SUCCESS',response='done')),
                             ('claude',dict(session_id='01a1a540-b6eb-7ab0-aa1c-92f0cb428e69',subtype='success',is_error=False,result='done'))]:
            self.assertTrue(cli.inspect_result(backend,json.dumps(good),'',0)['usable'])
            field='response' if backend=='agy' else 'result'
            for changes in [{field:''},{'permission_denials':['denied']},{'denied_actions':['denied']},
                            {'error':'failure'} if backend=='agy' else {'is_error':True}]:
                self.assertFalse(cli.inspect_result(backend,json.dumps(dict(good,**changes)),'',0)['usable'])
            self.assertFalse(cli.inspect_result(backend,json.dumps(good),'',1)['usable'])
            self.assertFalse(cli.inspect_result(backend,json.dumps(good),'permission denied',0)['usable'])
            self.assertFalse(cli.inspect_result(backend,json.dumps(good),'',0,expected='different')['usable'])
        for raw in ['not json','null','[]','{}']:
            self.assertFalse(cli.inspect_result('agy',raw,'',0)['usable'])

    def test_codex_requires_completion_and_actual_id(self):
        events=[dict(type='thread.started',thread_id='01a1a540-b6eb-7ab0-aa1c-92f0cb428e69'),
                dict(type='item.completed',item=dict(type='agent_message',text='done')),dict(type='turn.completed')]
        raw=lambda items:'\n'.join(json.dumps(e) for e in items)
        self.assertTrue(cli.inspect_result('codex',raw(events),'',0)['usable'])
        for invalid in [events[:-1],events[1:],events+[dict(type='turn.failed')],
                        [dict(type='thread.started',thread_id='logical-context')]+events[1:]]:
            self.assertFalse(cli.inspect_result('codex',raw(invalid),'',0)['usable'])

    def test_settings_reject_bypass(self):
        path=self.root/'settings.json'
        for value in [dict(toolPermission='always-proceed'),dict(permissions=dict(defaultMode='bypassPermissions'))]:
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):cli.settings_json(path)
        path.write_text(json.dumps(dict(permissions=dict(deny=['write_file(/source)']))))
        self.assertIn('write_file(/source)',cli.settings_json(path))

    def test_fake_cli_lifecycle_raw_evidence_no_rerun_and_version_drift(self):
        self.prompt.write_text('中文-$()`\n' * 150000 + 'END-完整输入')
        for backend in cli.REQUIRED:
            with self.subTest(backend=backend):
                self.args.model='test/explicit-model' if backend=='opencode' else 'explicit-model'
                self.args.effort='default' if backend=='opencode' else 'high'
                binary=self.root/backend
                binary.write_text('#!'+sys.executable+'\n'+'''
import json,sys
name=__file__.split('/')[-1]
if '--version' in sys.argv:
 print('opencode v2.fake' if name=='opencode' else 'fake-1');sys.exit(0)
if '--help' in sys.argv:
 print('--mode --model --effort --output-format --conversation --settings --sandbox --config --json --add-dir --skip-git-repo-check --permission-mode --permission-prompts --disallowedTools --resume SESSION_ID --format --session --agent --standalone --file');sys.exit(0)
name=__file__.split('/')[-1]
if name=='codex':
 for e in [{'type':'thread.started','thread_id':'01a1a540-b6eb-7ab0-aa1c-92f0cb428e69'},{'type':'item.completed','item':{'type':'agent_message','text':sys.stdin.read()}},{'type':'turn.completed'}]:print(json.dumps(e))
elif name=='claude':
 flag='--resume' if '--resume' in sys.argv else '--session-id'
 cid=sys.argv[sys.argv.index(flag)+1]
 print(json.dumps({'session_id':cid,'subtype':'success','is_error':False,'result':sys.stdin.read()}))
elif name=='opencode':
 cid=sys.argv[sys.argv.index('--session')+1] if '--session' in sys.argv else 'ses_TestNative0123456789'
 for e in [{'type':'text','sessionID':cid,'part':{'messageID':'msg_done','text':open(sys.argv[sys.argv.index('--file')+1]).read()}},
           {'type':'step_finish','sessionID':cid,'part':{'messageID':'msg_done','reason':'stop'}}]:print(json.dumps(e))
else:
 print(json.dumps({'conversation_id':'01a1a540-b6eb-7ab0-aa1c-92f0cb428e69','status':'SUCCESS','response':open(sys.argv[-1].split('工单文件 ')[1].split(' 及其指定')[0]).read()}))
''')
                binary.chmod(0o700);profile=self.root/(backend+'.json')
                cli.probe(backend,profile,str(binary))
                self.args.conversation=None
                self.args.resume_from=None
                self.args.run_dir=str(self.root/(backend+'-run'))
                env_override={'OPENCODE_CONFIG_CONTENT':'{"providers":{"private":{"options":{"apiKey":"DO_NOT_RECORD"}}}}'} if backend=='opencode' else {}
                with patch.dict(os.environ,env_override),patch('builtins.print'):self.assertEqual(cli.run(profile,self.args),0)
                report=cli.collect(self.args.run_dir)
                self.assertEqual(report['response'],self.prompt.read_text())
                self.assertTrue((Path(self.args.run_dir)/'receipt.json').exists())
                self.assertNotIn('DO_NOT_RECORD',(Path(self.args.run_dir)/'request.json').read_text())
                before=(Path(self.args.run_dir)/'stdout').read_bytes()
                with self.assertRaises(FileExistsError):cli.run(profile,self.args)
                self.assertEqual(before,(Path(self.args.run_dir)/'stdout').read_bytes())
                self.assertFalse((self.root/'should-not-exist').exists())
                self.args.conversation=report['conversation_id']
                self.args.resume_from=self.args.run_dir
                self.args.run_dir=str(self.root/(backend+'-resume'))
                with patch('builtins.print'):self.assertEqual(cli.run(profile,self.args),0)
                self.assertEqual(cli.collect(self.args.run_dir)['conversation_id'],report['conversation_id'])
                binary.write_text(binary.read_text().replace('fake-1', 'fake-2').replace('v2.fake', 'v2.upgraded'))
                self.args.run_dir=str(self.root/(backend+'-upgraded'))
                with patch('builtins.print'):self.assertEqual(cli.run(profile,self.args),0)
                self.assertTrue(cli.collect(self.args.run_dir)['usable'])
                request=cli.read(Path(self.args.run_dir)/'request.json')
                self.assertEqual(request['version'], 'opencode v2.upgraded' if backend=='opencode' else 'fake-2')
                self.assertEqual(request['profile_version'], 'opencode v2.fake' if backend=='opencode' else 'fake-1')
                self.assertEqual(cli.read(profile)['version'], request['profile_version'])

    def test_entrypoint_preflight_failure_has_exit_cause_and_preserves_exclusive_run(self):
        profile = self.root / 'profile.json'
        cli.save(profile, dict(backend='opencode', binary='/missing', version='v2'))
        argv = ['agent_cli.py', 'run', '--profile', str(profile), '--task', 'T1',
                '--cwd', str(self.root), '--model', 'test/model', '--effort', 'default',
                '--access', 'read', '--prompt-file', str(self.prompt), '--run-dir', self.args.run_dir]
        with patch.object(sys, 'argv', argv), patch.object(cli, 'check_profile', side_effect=ValueError('CLI cannot start')), \
                patch.object(cli.subprocess, 'Popen') as spawn, patch('builtins.print'):
            self.assertEqual(cli.main(), 2)
            report = cli.collect(self.args.run_dir)
            self.assertEqual(report['event'], 'CLI_LAUNCH_FAILED')
            self.assertEqual(report['launch_error']['phase'], 'preflight')
            self.assertFalse(report['launch_error']['started'])
            self.assertIn('CLI cannot start', report['problems'][0])
            spawn.assert_not_called()
            evidence = (Path(self.args.run_dir) / 'exit.json').read_bytes()
            self.assertEqual(cli.main(), 2)
            self.assertEqual((Path(self.args.run_dir) / 'exit.json').read_bytes(), evidence)
        self.assertFalse((Path(self.args.run_dir) / 'receipt.json').exists())

    def test_spawn_oserror_is_structured_and_never_claims_started(self):
        import errno
        profile = self.root / 'profile.json'
        cli.save(profile, dict(backend='opencode', binary='/missing', version='v2'))
        self.args.model = 'test/model'; self.args.effort = 'default'
        with patch.object(cli, 'check_profile', return_value={'version': 'v2'}), patch.object(cli.subprocess, 'Popen',
                side_effect=OSError(errno.E2BIG, 'argument too long')), patch('builtins.print'):
            self.assertEqual(cli.run(profile, self.args), 2)
        report = cli.collect(self.args.run_dir)
        self.assertEqual(report['event'], 'CLI_LAUNCH_FAILED')
        self.assertEqual(report['launch_error']['errno'], errno.E2BIG)
        self.assertFalse((Path(self.args.run_dir) / 'receipt.json').exists())

    def test_claude_token_file_reaches_child_by_descriptor_only(self):
        binary=self.root/'claude'
        binary.write_text('#!'+sys.executable+'''
import json,os,sys
if '--version' in sys.argv:print('fake-1');sys.exit(0)
if '--help' in sys.argv:print('--permission-mode --permission-prompts --disallowedTools --model --effort --resume --output-format SESSION_ID');sys.exit(0)
fd=os.environ.get('CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR')
token=open('/proc/self/fd/'+fd).read().strip() if fd else 'none'
cid=sys.argv[sys.argv.index('--session-id')+1]
print(json.dumps({'session_id':cid,'subtype':'success','is_error':False,'result':token+'|'+str('CLAUDE_CODE_OAUTH_TOKEN' in os.environ)}))
''')
        binary.chmod(0o700);profile=self.root/'claude.json';cli.probe('claude',profile,str(binary))
        token=self.root/'tok';token.write_text('secret-token\n');token.chmod(0o644)
        env={k:v for k,v in os.environ.items() if k not in {'CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR'}}
        env[cli.TOKEN_FILE_ENV]=str(token)
        self.args.conversation=None;self.args.resume_from=None
        with patch.dict(os.environ,env,clear=True):
            self.args.run_dir=str(self.root/'tok-run-bad')
            with self.assertRaisesRegex(ValueError,'0600'):cli.run(profile,self.args)
            token.chmod(0o600);self.args.run_dir=str(self.root/'tok-run')
            with patch('builtins.print'):self.assertEqual(cli.run(profile,self.args),0)
            self.assertEqual(cli.check_profile(profile)['auth']['source'],'token-file')
        self.assertEqual(cli.collect(self.args.run_dir)['response'],'secret-token|False')
        request=(Path(self.args.run_dir)/'request.json').read_text()
        self.assertNotIn('secret-token',request);self.assertIn('token-file',request)
        with patch.dict(os.environ,dict(env,CLAUDE_CODE_OAUTH_TOKEN='x'),clear=True):
            self.assertEqual(cli.claude_auth(),('inherited',None))

    def test_incomplete_run_does_not_claim_running(self):
        self.assertEqual(cli.collect(self.root)['event'],'CLI_INCOMPLETE')

    def test_default_inherits_configuration_without_sending_sentinel(self):
        self.args.effort='default'
        for backend in cli.REQUIRED:
            argv,_=cli.build_command(dict(backend=backend,binary='/bin/'+backend),self.args,'prompt')
            self.assertNotIn('--effort',argv)
            self.assertFalse(any('model_reasoning_effort=' in item for item in argv))
            if backend=='codex':
                self.assertIn('--skip-git-repo-check',argv)
                self.assertIn('read-only',argv)

    def test_all_backends_reject_unproven_resume_and_invalid_result_ids(self):
        for backend in cli.REQUIRED:
            self.args.model='test/model' if backend=='opencode' else 'explicit-model'
            self.args.effort='default'
            for cid in ['logical-context','01a1a540-b6eb-7ab0-aa1c-92f0cb428e69']:
                self.args.conversation=cid;self.args.resume_from=None
                with self.subTest(backend=backend,cid=cid),self.assertRaises(ValueError):
                    cli.validate_call(dict(backend=backend),self.args)
        self.args.effort='xhigh';self.args.conversation=None
        argv,_=cli.build_command(dict(backend='claude',binary='/bin/claude'),self.args,'prompt')
        self.assertEqual(argv[argv.index('--effort')+1],'xhigh')
        for backend,result in [('agy',dict(conversation_id='logical',status='SUCCESS',response='done')),
                               ('claude',dict(session_id='logical',subtype='success',is_error=False,result='done'))]:
            self.assertFalse(cli.inspect_result(backend,json.dumps(result),'',0)['usable'])

    def test_bad_effort_and_logical_resume_are_rejected_before_spawn(self):
        profile=self.root/'profile.json'
        cli.save(profile,dict(backend='codex'))
        for effort,cid in [('typo',None),('default','dec05_arch_ctx'),('high','01a1a540-b6eb-7ab0-aa1c-92f0cb428e69')]:
            self.args.effort=effort;self.args.conversation=cid
            with patch.object(cli.subprocess,'run') as child, self.assertRaises(ValueError):
                cli.run(profile,self.args)
            child.assert_not_called()
            self.assertFalse(Path(self.args.run_dir).exists())

    def test_failed_or_mismatched_run_cannot_be_resume_evidence(self):
        previous=self.root/'previous';previous.mkdir()
        cid='01a1a540-b6eb-7ab0-aa1c-92f0cb428e69'
        cli.save(previous/'request.json',dict(backend='codex',cwd=str(self.root),conversation=None,task='T',started_at='now'))
        cli.save(previous/'exit.json',dict(returncode=1,ended_at='now'))
        (previous/'stdout').write_text(json.dumps(dict(type='thread.started',thread_id=cid)))
        (previous/'stderr').write_text('')
        self.args.conversation=cid;self.args.resume_from=str(previous)
        with self.assertRaisesRegex(ValueError,'resume evidence'):
            cli.validate_call(dict(backend='codex'),self.args)
        with patch.object(cli,'collect',return_value=dict(usable=True,conversation_id=cid)):
            self.assertEqual(cli.validate_call(dict(backend='codex'),self.args)['conversation_id'],cid)
            self.args.conversation='01a1a540-b6eb-7ab0-aa1c-92f0cb428e60'
            with self.assertRaisesRegex(ValueError,'resume evidence'):
                cli.validate_call(dict(backend='codex'),self.args)

    def test_preflight_distinguishes_environment_visibility_and_version_change(self):
        profile=self.root/'profile.json'
        cli.save(profile,dict(format='batch-flow-cli/v1',platform=sys.platform,backend='codex',binary='/missing/codex',version='v1'))
        with self.assertRaisesRegex(ValueError,'does not prove a missing host installation'):
            cli.check_profile(profile)
        with patch.object(cli,'version',return_value='v1'):
            self.assertEqual(cli.check_profile(profile)['event'],'CLI_CHECKED')
            with patch.object(sys,'argv',['agent_cli.py','check','--profile',str(profile)]),patch('builtins.print'):
                self.assertEqual(cli.main(),0)
        with patch.object(cli,'version',return_value='v2'):
            self.assertEqual(cli.check_profile(profile)['version'],'v2')
            self.assertEqual(cli.read(profile)['version'],'v1')

    def test_opencode_requires_native_id_final_message_and_completion(self):
        cid='ses_ActualNative012345';msg='msg_final'
        events=[dict(type='text',sessionID=cid,part=dict(messageID='msg_earlier',text='Working...')),
                dict(type='text',sessionID=cid,part=dict(messageID=msg,text='结果：VERIFIED')),
                dict(type='step_finish',sessionID=cid,part=dict(messageID=msg,reason='stop'))]
        def inspect(items,code=0,expected=None):
            return cli.inspect_result('opencode','\n'.join(json.dumps(e) for e in items),'',code,expected)
        report=inspect(events)
        self.assertTrue(report['usable'],report)
        self.assertEqual(report['response'],'结果：VERIFIED')
        self.assertFalse(inspect(events,expected='ses_AnotherSession')['usable'])
        self.assertTrue(inspect(events[1:])['usable'])  # Earlier commentary is optional.
        for invalid in [events[:-1],events+[dict(type='error',sessionID=cid)],
                        events+[dict(type='text',sessionID='ses_Other',part=dict(text='wrong session'))],
                        events[:-1]+[dict(type='step_finish',sessionID=cid,part=dict(messageID=msg,reason='tool-calls'))],
                        [dict(e,sessionID='logical') for e in events],
                        [dict(e,sessionID='01a1a540-b6eb-7ab0-aa1c-92f0cb428e69') for e in events]]:
            self.assertFalse(inspect(invalid)['usable'],invalid)
        denial=dict(type='tool_use',sessionID=cid,part=dict(state=dict(status='denied')))
        self.assertFalse(inspect(events+[denial])['usable'])
        # A handled build/test failure remains valid transport and can be reported FAILED.
        handled=dict(type='tool_use',sessionID=cid,part=dict(state=dict(status='error',error='command exited 1')))
        self.assertTrue(inspect(events+[handled])['usable'])
        self.assertFalse(inspect(events,code=1)['usable'])

    def test_opencode_overlay_preserves_policy_and_credentials_without_recording_them(self):
        self.args.effort='default';self.args.model='deepseek/deepseek-flash'
        extra=self.root/'extra';extra.mkdir();self.args.add_dir=[str(extra)]
        settings=self.root/'settings.json'
        settings.write_text(json.dumps(dict(agents=dict(build=dict(permissions=[
            dict(action='shell',resource='git push *',effect='deny')])))))
        self.args.settings=str(settings)
        inherited=dict(providers=dict(secret=dict(options=dict(apiKey='DO_NOT_RECORD'))))
        with patch.dict(os.environ,OPENCODE_CONFIG_CONTENT=json.dumps(inherited)):
            env=cli.opencode_environment(self.args)
        config=json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(config['providers'],inherited['providers'])
        rules=config['agents']['build']['permissions']
        self.assertIn(dict(action='shell',resource='git push *',effect='deny'),rules)
        self.assertIn(dict(action='external_directory',resource=str(extra)+'/*',effect='allow'),rules)
        self.assertEqual(rules[-1],dict(action='edit',resource='*',effect='deny'))
        self.assertEqual(json.loads(settings.read_text())['agents']['build']['permissions'],rules[:1])

    def test_opencode_rejects_unsupported_effort_model_and_resume_before_spawn(self):
        for model,effort,cid in [('deepseek/deepseek-flash','high',None),('deepseek-flash','default',None),
                                 ('deepseek/deepseek-flash','default','logical'),
                                 ('deepseek/deepseek-flash','default','ses_RealButUnproven')]:
            self.args.model=model;self.args.effort=effort;self.args.conversation=cid
            with self.subTest(model=model,effort=effort,cid=cid),self.assertRaises(ValueError):
                cli.validate_call(dict(backend='opencode'),self.args)


if __name__=='__main__':unittest.main(verbosity=2)
