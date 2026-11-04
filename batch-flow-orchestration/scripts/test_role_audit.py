#!/usr/bin/env python3
import json
from pathlib import Path
import tempfile
import unittest
import role_audit as audit


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.log=self.root/'audit.jsonl';self.management=self.root/'management';self.management.mkdir()
        audit.append_event(self.log,'claude','owner',dict(session_id='owner'),begin=True)

    def tearDown(self):self.tmp.cleanup()

    def tool(self,name,inputs,session='owner'):
        audit.append_event(self.log,'claude','owner',dict(session_id=session,tool_name=name,tool_input=inputs,cwd=str(self.root)))

    def report(self):return audit.audit(self.log,'owner',[self.management])

    def test_git_write_and_product_build(self):
        for command in ['git -C repo commit -m fix','git add file','git merge branch',
                        'git status && git commit -m fix','bash ./build.sh','python3 tests/test_driver.py','ninja all']:
            with self.subTest(command=command):
                self.assertEqual(audit.shell_check(command,str(self.root),[]) [0],'violation')

    def test_dispatch_payload_does_not_count_as_direct_execution(self):
        self.tool('Agent',dict(prompt='Run git commit and ./build.sh in the executor'))
        self.assertEqual(self.report()['status'],'clean')

    def test_shell_helpers_are_exact_and_do_not_scan_payload(self):
        script=audit.SCRIPTS/'agent_cli.py'
        self.assertEqual(audit.shell_check('python3 '+str(script)+' run --prompt-file /tmp/prompt',str(self.root),[])[0],'clean')
        for command in ['python3 /tmp/agent_cli.py run','bash -c "git commit"','python3 -c "print(1)"','echo $(git commit)']:
            self.assertEqual(audit.shell_check(command,str(self.root),[])[0],'unavailable')
        self.assertEqual(audit.shell_check('python3 '+str(audit.SCRIPTS/'flow.py')+' record -- make',str(self.root),[])[0],'violation')

    def test_product_edits_and_management_records(self):
        self.tool('Write',dict(file_path=str(self.management/'receipt.json')))
        self.assertEqual(self.report()['status'],'clean')
        self.tool('Edit',dict(file_path='third_party/lib/file.c'))
        result=self.report();self.assertEqual(result['status'],'violation')
        self.assertEqual(result['findings'][0]['line'],3)
        status,_=audit.tool_check(dict(tool='Write',inputs=dict(file_path=str(self.management/'flow.json'))),[self.management])
        self.assertEqual(status,'violation')

    def test_patch_move_and_symlink_escape(self):
        alias=self.management/'alias';alias.symlink_to(self.root/'kernel')
        self.tool('Edit',dict(file_path=str(alias/'file.c')))
        self.assertEqual(self.report()['status'],'violation')
        audit.append_event(self.log,'claude','owner',dict(session_id='owner'),begin=True)
        self.tool('apply_patch',dict(patch='*** Update File: '+str(self.management/'a')+'\n*** Move to: '+str(self.root/'kernel/a')))
        self.assertEqual(self.report()['status'],'violation')

    def test_other_sessions_and_new_scope_do_not_inherit_old_violation(self):
        self.tool('Bash',dict(command='git commit'),session='child')
        self.assertEqual(self.report()['status'],'clean')
        self.tool('Bash',dict(command='git commit'))
        self.assertEqual(self.report()['status'],'violation')
        audit.append_event(self.log,'claude','owner',dict(session_id='owner'),begin=True)
        self.assertEqual(self.report()['status'],'clean')
        self.assertIn('git commit',self.log.read_text())

    def test_bad_or_missing_audit_never_passes(self):
        self.assertEqual(audit.audit(self.root/'missing','owner',[])['status'],'unavailable')
        self.assertEqual(audit.audit(self.log,'other',[])['status'],'unavailable')
        self.log.write_text('{broken')
        self.assertEqual(self.report()['status'],'unavailable')

    def test_unknown_tool_or_command_never_certified(self):
        self.tool('unknown_writer',dict(path='kernel/a'))
        self.assertEqual(self.report()['status'],'unavailable')

    def test_agy_and_codex_event_normalization(self):
        for host,event in [('agy',dict(conversationId='owner',toolCall=dict(name='run_command',args=dict(CommandLine='git add file',Cwd=str(self.root))))),
                           ('codex',dict(session_id='owner',tool_name='Bash',tool_input=dict(command='git add file')))]:
            with self.subTest(host=host):
                log=self.root/(host+'.jsonl')
                audit.append_event(log,host,'owner',event,begin=True)
                audit.append_event(log,host,'owner',event)
                self.assertEqual(audit.audit(log,'owner',[])['status'],'violation')


if __name__=='__main__':unittest.main(verbosity=2)
