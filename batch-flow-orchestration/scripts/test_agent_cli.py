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
            for access in ['read','write']:
                for resume in [None,'actual-session']:
                    with self.subTest(backend=backend,access=access,resume=resume):
                        self.args.access=access;self.args.conversation=resume
                        argv,stdin=cli.build_command(dict(backend=backend,binary='/bin/'+backend),self.args,self.prompt.read_text())
                        self.assertIn('explicit-model',argv)
                        for flag in ['--continue','--last','--dangerously-skip-permissions']:
                            self.assertNotIn(flag,argv)
                        if backend=='codex':
                            self.assertEqual(stdin,self.prompt.read_text())
                            self.assertIn('read-only' if access=='read' else 'workspace-write',argv)
                            if resume:
                                self.assertLess(argv.index('-s'),argv.index('resume'))
                                self.assertEqual(argv[-3:],['resume',resume,'-'])
                        elif backend=='agy':
                            self.assertEqual(argv[-1],self.prompt.read_text())
                            self.assertIn('accept-edits',argv)
                            self.assertNotIn('plan',argv)
                            if access=='read':
                                deny=json.loads(argv[argv.index('--settings')+1])['permissions']['deny']
                                self.assertIn('write_file(*)',deny)
                            else:
                                self.assertNotIn('--settings',argv)
                        else:
                            self.assertEqual(stdin,self.prompt.read_text())
                            self.assertIn('default' if access=='read' else 'acceptEdits',argv)
                            self.assertNotIn('plan',argv)
                            if access=='read':
                                self.assertEqual(argv[argv.index('--disallowedTools')+1],'Edit Write NotebookEdit')
                            else:
                                self.assertNotIn('--disallowedTools',argv)

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
        for backend in cli.REQUIRED:
            with self.subTest(backend=backend):
                binary=self.root/backend
                binary.write_text('#!'+sys.executable+'\n'+'''
import json,sys
if '--version' in sys.argv:
 print('fake-1');sys.exit(0)
if '--help' in sys.argv:
 print('--mode --model --effort --output-format --conversation --settings --sandbox --config --json --add-dir --skip-git-repo-check --permission-mode --permission-prompts --disallowedTools --resume SESSION_ID');sys.exit(0)
name=__file__.split('/')[-1]
if name=='codex':
 for e in [{'type':'thread.started','thread_id':'01a1a540-b6eb-7ab0-aa1c-92f0cb428e69'},{'type':'item.completed','item':{'type':'agent_message','text':sys.stdin.read()}},{'type':'turn.completed'}]:print(json.dumps(e))
elif name=='claude':
 flag='--resume' if '--resume' in sys.argv else '--session-id'
 cid=sys.argv[sys.argv.index(flag)+1]
 print(json.dumps({'session_id':cid,'subtype':'success','is_error':False,'result':sys.stdin.read()}))
else:
 print(json.dumps({'conversation_id':'01a1a540-b6eb-7ab0-aa1c-92f0cb428e69','status':'SUCCESS','response':sys.argv[-1]}))
''')
                binary.chmod(0o700);profile=self.root/(backend+'.json')
                cli.probe(backend,profile,str(binary))
                self.args.conversation=None
                self.args.resume_from=None
                self.args.run_dir=str(self.root/(backend+'-run'))
                with patch('builtins.print'):self.assertEqual(cli.run(profile,self.args),0)
                report=cli.collect(self.args.run_dir)
                self.assertEqual(report['response'],self.prompt.read_text())
                self.assertTrue((Path(self.args.run_dir)/'receipt.json').exists())
                before=(Path(self.args.run_dir)/'stdout').read_bytes()
                with self.assertRaises(FileExistsError):cli.run(profile,self.args)
                self.assertEqual(before,(Path(self.args.run_dir)/'stdout').read_bytes())
                self.assertFalse((self.root/'should-not-exist').exists())
                self.args.conversation=report['conversation_id']
                self.args.resume_from=self.args.run_dir
                self.args.run_dir=str(self.root/(backend+'-resume'))
                with patch('builtins.print'):self.assertEqual(cli.run(profile,self.args),0)
                self.assertEqual(cli.collect(self.args.run_dir)['conversation_id'],report['conversation_id'])
                binary.write_text('#!'+sys.executable+'\nprint("changed-version")\n')
                with self.assertRaisesRegex(ValueError,'version changed'):cli.run(profile,self.args)

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
        with patch.object(cli,'version',return_value='v2'),self.assertRaisesRegex(ValueError,'version changed'):
            cli.check_profile(profile)


if __name__=='__main__':unittest.main(verbosity=2)
