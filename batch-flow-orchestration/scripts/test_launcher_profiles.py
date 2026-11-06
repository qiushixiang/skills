#!/usr/bin/env python3
"""Explicit launcher profile maintenance, without model calls or ledger mutations."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_cli
import flow
import launcher_profiles
import orchestrator_guard


class LauncherProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=self.root/'.batch-flow/flow.json';self.store.parent.mkdir()
        self.store.write_text('{"unchanged":"ledger"}')
        self.runtime=self.store.parent/'.claude-launch'/('a'*32);self.runtime.mkdir(parents=True)
        self.spec=self.runtime/'spec.json'
        self.spec.write_text(json.dumps(dict(launch_id=self.runtime.name,runtime=str(self.runtime),
                                            store=str(self.store),profiles=[],session_id='owner',
                                            python=sys.executable,workspace=str(self.root))))
        self.binary=self.root/'opencode'
        self.binary.write_text('#!'+sys.executable+'\nimport sys\n'
            'if "--version" in sys.argv: print("opencode v2.fake")\n'
            'elif "--help" in sys.argv: print("--model --format --session --agent --standalone")\n'
            'else: raise RuntimeError("must never invoke a model")\n')
        self.binary.chmod(0o700)

    def tearDown(self):self.tmp.cleanup()

    def test_real_probe_registration_is_bound_idempotent_and_ledger_readonly(self):
        before=self.store.read_bytes()
        # An existing unregistered file is not trusted or overwritten.
        stray=self.runtime/'opencode-profile.json';stray.write_text('untrusted')
        result=launcher_profiles.register(self.store,self.runtime,'opencode',str(self.binary))
        self.assertTrue(result['changed'])
        profile=agent_cli.read(result['profile'])
        self.assertEqual(profile['binary'],str(self.binary))
        self.assertEqual(profile['version'],'opencode v2.fake')
        self.assertEqual(stray.read_text(),'untrusted')
        spec_after=self.spec.read_bytes()
        repeated=launcher_profiles.register(self.store,self.runtime,'opencode',str(self.binary))
        self.assertFalse(repeated['changed'])
        self.assertEqual(spec_after,self.spec.read_bytes())
        caps=flow.host_capabilities(self.store,{'BATCH_FLOW_CLAUDE_LAUNCH':self.runtime.name})
        self.assertEqual(caps['profiles']['opencode']['path'],result['profile'])
        self.assertEqual(before,self.store.read_bytes())

    def test_wrong_store_or_identity_and_probe_failure_preserve_spec(self):
        original=self.spec.read_bytes()
        other=self.root/'other/flow.json';other.parent.mkdir();other.write_text('{}')
        with self.assertRaisesRegex(ValueError,'belonging to this store'):
            launcher_profiles.register(other,self.runtime,'opencode',str(self.binary))
        with patch.object(agent_cli,'probe',side_effect=ValueError('unsupported CLI')):
            with self.assertRaisesRegex(ValueError,'unsupported CLI'):
                launcher_profiles.register(self.store,self.runtime,'opencode',str(self.binary))
        self.assertEqual(original,self.spec.read_bytes())
        spec=json.loads(original);spec['launch_id']='b'*32;self.spec.write_text(json.dumps(spec))
        with self.assertRaisesRegex(ValueError,'identity mismatch'):
            launcher_profiles.register(self.store,self.runtime,'opencode',str(self.binary))

    def test_symlink_profile_is_not_adopted_and_restricted_orchestrator_cannot_run_helper(self):
        outside=self.root/'outside.json';outside.write_text('{}')
        linked=self.runtime/'linked.json';linked.symlink_to(outside)
        spec=json.loads(self.spec.read_text());spec['profiles']=[str(linked)];self.spec.write_text(json.dumps(spec))
        with self.assertRaisesRegex(ValueError,'outside the bound launcher or is a symlink'):
            launcher_profiles.register(self.store,self.runtime,'opencode',str(self.binary))
        command=sys.executable+' '+str(Path(launcher_profiles.__file__).resolve())+' --store '+str(self.store)
        denial=orchestrator_guard.check_bash(dict(command=command),str(self.root),[self.root])
        self.assertIsNotNone(denial)


if __name__=='__main__':unittest.main()
