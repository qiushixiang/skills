#!/usr/bin/env python3
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import flow
import role_audit
from test_flow import plan

SCRIPT=Path(__file__).with_name('stop_hook_pre_yield.py')

class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        req={'request_id':'init','expected_revision':0,'actor':'architect','operations':[{'type':'init','data':{'plan':plan(),'simulation':True,'task_mode':'local','context':{'conversation_id':'planner','mode':'fresh','reason':'fixture'}}}]}
        state,_=flow.apply_request(None,req,self.root)
        proof=self.root/'authorization.txt';proof.write_text('authorized')
        auth={'request_id':'auth','expected_revision':state['revision'],'actor':'orchestrator','operations':[{'type':'authorize','data':{'batches':['B'],'evidence':[str(proof)]}}]}
        state,_=flow.apply_request(state,auth,self.root)
        self.store=self.root/'flow.json';self.store.write_text(json.dumps(state))
    def tearDown(self):self.tmp.cleanup()
    def run_hook(self,host,event,role='orchestrator',conversation='owner-1',audit_log=None):
        argv=[sys.executable,str(SCRIPT),'--host',host,'--store',str(self.store),'--role',role,'--conversation',conversation]
        if audit_log:argv+=['--audit-log',str(audit_log)]
        run=subprocess.run(argv,input=json.dumps(event),capture_output=True,text=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr)
        return json.loads(run.stdout)
    def test_host_specific_continue_and_session_binding(self):
        agy=self.run_hook('agy',{'conversationId':'owner-1','terminationReason':'model_stop'})
        codex=self.run_hook('codex',{'session_id':'owner-1','stop_hook_active':False})
        self.assertEqual(agy['decision'],'continue')
        self.assertEqual(codex['decision'],'block')
        self.assertEqual(self.run_hook('agy',{'conversationId':'other'}),{'decision':'stop'})
        self.assertEqual(self.run_hook('codex',{'session_id':'other'}),{})
    def test_claude_host_and_loop_guard(self):
        self.assertEqual(self.run_hook('claude',{'session_id':'other'}),{})
        first=self.run_hook('claude',{'session_id':'owner-1','stop_hook_active':False})
        self.assertEqual(first['decision'],'block')
        # Host continued because of the hook but nothing was registered: allow stop.
        self.assertEqual(self.run_hook('claude',{'session_id':'owner-1','stop_hook_active':True}),{})
        # A new registration changes the revision: block again.
        state=json.loads(self.store.read_text());state['revision']+=1;self.store.write_text(json.dumps(state))
        self.assertEqual(self.run_hook('claude',{'session_id':'owner-1','stop_hook_active':True})['decision'],'block')
        # A fresh stop request (not hook-driven) is always evaluated normally.
        self.assertEqual(self.run_hook('claude',{'session_id':'owner-1','stop_hook_active':False})['decision'],'block')

    def test_user_stop_and_missing_store_do_not_block(self):
        self.assertEqual(self.run_hook('agy',{'conversationId':'owner-1','lastUserInput':'先停一下'}),{'decision':'stop'})
        self.assertEqual(self.run_hook('codex',{'session_id':'owner-1','terminationReason':'max_steps_exceeded'}),{})
        self.store.unlink()
        self.assertEqual(self.run_hook('codex',{'session_id':'owner-1'}),{})

    def test_missing_binding_or_session_does_not_intercept(self):
        self.assertEqual(self.run_hook('codex',{}),{})
        self.assertEqual(self.run_hook('agy',{'conversationId':'owner-1'},conversation=''),{'decision':'stop'})
        run=subprocess.run([sys.executable,str(SCRIPT),'--host','codex','--store',str(self.store),
            '--role','orchestrator'],input=json.dumps({'session_id':'owner-1'}),capture_output=True,text=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(run.stdout),{})

    def test_empty_or_hanging_stdin_does_not_hang(self):
        import os
        # Empty stdin returns safe default immediately
        run_empty=subprocess.run([sys.executable,str(SCRIPT),'--host','agy','--store',str(self.store),
            '--role','orchestrator'],input='',capture_output=True,text=True,timeout=5)
        self.assertEqual(run_empty.returncode,0)
        self.assertEqual(json.loads(run_empty.stdout),{'decision':'stop'})

        # Unclosed pipe without data exits within bounded timeout
        r,w=os.pipe()
        try:
            run_pipe=subprocess.run([sys.executable,str(SCRIPT),'--host','codex','--store',str(self.store),
                '--role','orchestrator'],stdin=r,capture_output=True,text=True,timeout=5)
            self.assertEqual(run_pipe.returncode,0)
            self.assertEqual(json.loads(run_pipe.stdout),{})
        finally:
            os.close(r);os.close(w)

    def test_role_violation_is_not_waived_by_loop_guard(self):
        log=self.root/'audit.jsonl'
        event=dict(session_id='owner-1',tool_name='Bash',tool_input=dict(command='git commit -m unauthorized'))
        role_audit.append_event(log,'claude','owner-1',event,begin=True)
        role_audit.append_event(log,'claude','owner-1',event)
        for host in ['claude','codex','agy']:
            bound={'conversationId':'owner-1'} if host=='agy' else {'session_id':'owner-1'}
            for active in [False,True]:
                report=self.run_hook(host,dict(bound,stop_hook_active=active),audit_log=log)
                self.assertEqual(report['decision'],'continue' if host=='agy' else 'block')
                self.assertIn('role audit violation',report['reason'])
            self.assertEqual(self.run_hook(host,dict(bound,terminationReason='user_canceled'),audit_log=log),{'decision':'stop'} if host=='agy' else {})
        # Ordinary task-state loop prevention still applies to a local architect;
        # orchestrator-specific auditing does not inspect the architect's work.
        report=self.run_hook('claude',dict(session_id='owner-1'),role='architect',audit_log=log)
        self.assertNotIn('role audit violation',report.get('reason',''))

    def test_enabled_missing_audit_blocks_and_foreign_session_is_ignored(self):
        missing=self.root/'missing.jsonl'
        report=self.run_hook('claude',dict(session_id='owner-1'),audit_log=missing)
        self.assertEqual(report['decision'],'block')
        self.assertIn('role audit unavailable',report['reason'])
        self.assertEqual(self.run_hook('claude',dict(session_id='child'),audit_log=missing),{})

if __name__=='__main__': unittest.main()
