#!/usr/bin/env python3
"""Read-only capability recovery, binding isolation, and real CLI status decoration."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import flow
from test_flow import plan


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=self.root/'.batch-flow/flow.json'
        self.launch_id='a'*32
        self.runtime=self.store.parent/'.agy-launch'/self.launch_id
        self.runtime.mkdir(parents=True)
        self.env=dict(os.environ,BATCH_FLOW_AGY_LAUNCH=self.launch_id)
        self.profile=self.runtime/'codex-profile.json'
        self.profile.write_text(json.dumps(dict(format='batch-flow-cli/v1',backend='codex',version='fixture-version',binary='/fixture/codex',private_field='DO_NOT_EXPOSE')))
        self.spec=dict(launch_id=self.launch_id,runtime=str(self.runtime),store=str(self.store),workspace=str(self.root),task_base=str(self.root),python=sys.executable,profiles=[str(self.profile)])
        (self.runtime/'spec.json').write_text(json.dumps(self.spec))
        (self.runtime/'owner.json').write_text(json.dumps(dict(conversation_id='owner')))

    def tearDown(self):self.tmp.cleanup()

    def cli(self,command):
        return subprocess.run([sys.executable,str(Path(flow.__file__)),command,'--store',str(self.store)],env=self.env,capture_output=True,text=True)

    def test_query_recovers_bound_profiles_without_a_ledger_or_private_fields(self):
        before={p.name:p.read_bytes() for p in self.runtime.iterdir()}
        result=self.cli('capabilities');self.assertEqual(result.returncode,0)
        data=json.loads(result.stdout)
        self.assertEqual(data['profiles']['codex']['path'],str(self.profile))
        self.assertNotIn('DO_NOT_EXPOSE',result.stdout)
        self.assertFalse(self.store.exists())
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.runtime.iterdir()})

    def test_missing_environment_does_not_guess_a_recent_launch(self):
        self.assertEqual(flow.host_capabilities(self.store,{})['status'],'unbound')
        self.assertEqual(flow.host_capabilities(self.store,{'BATCH_FLOW_AGY_LAUNCH':'../other'})['status'],'unavailable')
        self.assertEqual(flow.host_capabilities(self.store.with_name('other.json'),self.env)['status'],'unavailable')

    def test_profiles_cannot_escape_runtime_and_missing_profiles_do_not_fall_back(self):
        original=self.profile.read_bytes();self.profile.unlink()
        old=self.runtime.parent/('b'*32);old.mkdir();(old/'codex-profile.json').write_bytes(original)
        self.assertNotIn('codex',flow.host_capabilities(self.store,self.env)['profiles'])
        outside=self.root/'outside.json';outside.write_bytes(original);self.profile.symlink_to(outside)
        result=flow.host_capabilities(self.store,self.env)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['profiles'],{})

    def test_status_returns_profile_for_ready_codex_review_without_writes(self):
        p=plan();p['roles']['reviewer'].update(tool='codex',model='gpt-6.1-sol',effort='default')
        proof=self.root/'proof.txt';proof.write_text('fixture')
        state=None
        operations=[('architect','init',dict(plan=p,simulation=True,task_mode='local',context=dict(conversation_id='planner',mode='fresh',reason='fixture'))),
                    ('orchestrator','authorize',dict(batches=['B'],evidence=[str(proof)])),
                    ('orchestrator','start',dict(task='execute',call=dict(p['roles']['executor'],conversation_id='executor'))),
                    ('executor','finish',dict(attempt='A0001',result='VERIFIED',evidence=[str(proof)]))]
        for i,(actor,kind,data) in enumerate(operations):
            state,_=flow.apply_request(state,dict(request_id=str(i),expected_revision=state['revision'] if state else 0,actor=actor,operations=[dict(type=kind,data=data)]),self.store.parent)
        flow.write_atomic(self.store,state);before=self.store.read_bytes()
        result=self.cli('status');self.assertEqual(result.returncode,0)
        data=json.loads(result.stdout)
        action=next(a for a in data['actions'] if a['target']=='regular')
        self.assertEqual(action['cli_adapter']['profile'],str(self.profile))
        self.assertIn('capabilities',action['cli_adapter']['capabilities_command'])
        self.assertEqual(data['host_capabilities']['status'],'bound')
        self.assertEqual(self.store.read_bytes(),before)

    def test_next_actions_get_capabilities_but_plain_workflow_objects_stay_unmodified(self):
        original=dict(actions=[dict(cli_adapter=dict(backend='codex'))])
        with patch.dict(os.environ,self.env,clear=True):
            result=flow.attach_host_capabilities(dict(next=copy.deepcopy(original)),self.store)
        self.assertEqual(result['next']['actions'][0]['cli_adapter']['profile'],str(self.profile))
        self.assertNotIn('profile',original['actions'][0]['cli_adapter'])


if __name__=='__main__':unittest.main(verbosity=2)
