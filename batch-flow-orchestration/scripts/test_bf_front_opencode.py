"""OpenCode V2 front/plugin transport tests. Node and Python run locally; no model calls."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bf import api, front, front_opencode
from test_bf_front import Base

HERE = Path(__file__).resolve().parent


class OpenCode(Base):
    def test_project_launcher_defaults_to_deepseek_flash(self):
        result = subprocess.run([str(HERE/'start-batch-flow.sh'),str(self.base.parent),'--backend','opencode',
                                 '--dry-run','--binary','/missing/not-executed'],
                                capture_output=True,text=True,check=True)
        spec = json.loads(result.stdout)
        self.assertIsNone(spec['requested_model'])
        self.assertEqual(spec['model'],'deepseek/deepseek-flash')
        self.assertEqual(spec['argv'][spec['argv'].index('--model')+1],'deepseek/deepseek-flash')

    def prepare(self, model='test/model'):
        runtime = self.root/'opencode-runtime'
        runtime.mkdir(exist_ok=True)
        return front_opencode.prepare(self.base, 'opencode', runtime, 'Front rules', model)

    def test_project_launcher_previews_interactive_private_server(self):
        result = subprocess.run([str(HERE/'start-batch-flow.sh'),str(self.base.parent),'--backend','opencode',
                                 '--model','test/model','--dry-run','--binary','/missing/not-executed'],
                                capture_output=True,text=True,check=True)
        spec = json.loads(result.stdout)
        self.assertEqual(spec['argv'][:3], ['/missing/not-executed','mini','--standalone'])
        self.assertEqual(spec['argv'][spec['argv'].index('--agent')+1],front_opencode.AGENT)
        self.assertEqual(spec['argv'][spec['argv'].index('--model')+1],'test/model')
        self.assertEqual(spec['cwd'],str(self.base.parent))
        self.assertNotIn('--auto',spec['argv'])
        self.assertFalse((Path(spec['runtime'])/'ready.json').exists())

    def test_overlay_preserves_existing_configuration_without_recording_credentials(self):
        original = {'providers':{'test':{'options':{'apiKey':'PRIVATE_SENTINEL'}}},
                    'permissions':[{'action':'shell','resource':'*','effect':'ask'}],
                    'agents':{'custom':{'description':'user agent'}},'plugins':['user-plugin']}
        spec = self.prepare()
        env = front_opencode.environment(spec, {'OPENCODE_CONFIG_CONTENT':json.dumps(original),'DEBUG':'release'})
        merged = json.loads(env['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(env['PWD'],spec['cwd'])
        self.assertNotIn('DEBUG',env)
        self.assertEqual(env['BF_OPENCODE_INHERITED_DEBUG'],'release')
        for key in ('providers','permissions'):
            self.assertEqual(merged[key],original[key])
        self.assertEqual(merged['agents']['custom'],original['agents']['custom'])
        self.assertIn('user-plugin',merged['plugins'])
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(spec))
        self.assertNotIn('PRIVATE_SENTINEL',(Path(spec['runtime'])/'plugin/index.mjs').read_text())
        with self.assertRaisesRegex(ValueError,'already exists'):
            front_opencode.environment(spec,{'OPENCODE_CONFIG_CONTENT':env['OPENCODE_CONFIG_CONTENT']})

    def test_native_plugin_enforces_shared_policy_and_cleans_child_environment(self):
        spec = self.prepare()
        original = {'providers':{'test':{'options':{'apiKey':'PRIVATE_SENTINEL'}}},
                    'permissions':[{'action':'shell','resource':'*','effect':'ask'}], 'plugins':['user-plugin']}
        env = front_opencode.environment(spec,dict(__import__('os').environ, DEBUG='release', OPENCODE_CONFIG_CONTENT=json.dumps(original)))
        command = shlex.join([sys.executable,str(HERE/'bf/cli.py'),'--base',str(self.base),'report','--text'])
        script = self.root/'exercise.mjs'
        script.write_text('const spec='+json.dumps(spec)+';\nconst command='+json.dumps(command)+';\n'+r'''
import { pathToFileURL } from 'node:url';
const module = await import(spec.config_overlay.plugins[0]+'/index.mjs');
let tool, shell;
const session={get:async()=>({agent:'other'}),prompt:async()=>{}};
await module.default.setup({session,tool:{hook:async (name,fn)=>{tool=fn}},shell:{hook:async (name,fn)=>{shell=fn}}});
async function invoke(name,input) { await tool({tool:name,input,sessionID:'ses_test'}); }
async function blocked(name,input) {
  try { await invoke(name,input); return false; } catch { return true; }
}
await invoke('shell',{command});
await invoke('write',{path:spec.cwd+'/.batch-flow/answers/new.txt'});
const badSource=await blocked('write',{path:spec.cwd+'/source.py'});
const foreign=await blocked('shell',{command:command.replace('/.batch-flow','/other/.batch-flow')});
const subagent=await blocked('subagent',{agent:'build'});
const e={command,cwd:spec.cwd,env:{...process.env}};
await shell(e);
const inherited=JSON.parse(e.env.OPENCODE_CONFIG_CONTENT);
const clean=!inherited.agents?.batch_flow_front && !inherited.plugins.includes(spec.config_overlay.plugins[0]);
const preserved=inherited.providers.test.options.apiKey==='PRIVATE_SENTINEL' && inherited.plugins.includes('user-plugin') && inherited.permissions[0].effect==='ask' && e.env.DEBUG==='release' && !('BF_OPENCODE_INHERITED_DEBUG' in e.env);
let badShell=false;
try { await shell({command:'touch /tmp/forbidden',cwd:spec.cwd,env:{}}); } catch {badShell=true;}
console.log(JSON.stringify({badSource,foreign,subagent,clean,preserved,badShell}));
''')
        (self.base/'answers').mkdir()
        result = subprocess.run(['node',str(script)],env=env,capture_output=True,text=True,check=True,timeout=15)
        self.assertTrue(all(json.loads(result.stdout).values()),result.stdout)
        self.assertNotIn('PRIVATE_SENTINEL',result.stdout)

    def test_opencode_rejects_non_provider_model_and_unverified_effort(self):
        with self.assertRaisesRegex(ValueError,'provider/model'):
            self.prepare('flash')
        with self.assertRaisesRegex(ValueError,'effort'):
            front.launch(self.base,backend='opencode',effort='high',dry_run=True)

    def test_snapshot_is_read_only_and_does_not_replay_history(self):
        before = (self.base/'orders.json').read_bytes()
        initial = front_opencode.notification_snapshot(self.base)
        self.assertEqual(initial['events'], [])
        api.stop(self.base)
        changed = front_opencode.notification_snapshot(self.base, initial['revision'])
        self.assertTrue(changed['events'])
        self.assertTrue(changed['report']['stopped'])
        again = front_opencode.notification_snapshot(self.base, changed['revision'])
        self.assertEqual(again['events'], [])
        after = (self.base/'orders.json').read_bytes()
        front_opencode.notification_snapshot(self.base)
        self.assertEqual(after, (self.base/'orders.json').read_bytes())
        self.assertNotEqual(before, after)

    def test_notifications_periodic_events_busy_retry_isolation_and_cleanup(self):
        spec = self.prepare()
        fixture = self.root/'snapshot.json'
        poll = self.root/'poll.py'
        poll.write_text('import json,sys\nfrom pathlib import Path\n'
                        'x=json.loads(Path(sys.argv[1]).read_text())\n'
                        'since=int(sys.argv[3]) if len(sys.argv)>3 else x["revision"]\n'
                        'if x["revision"]<=since: x["events"]=[]\n'
                        'print(json.dumps(x))\n')
        script = self.root/'notifications.mjs'
        script.write_text('const spec='+json.dumps(spec)+';\nconst fixture='+json.dumps(str(fixture))+';\n'+
                          'const poll='+json.dumps(str(poll))+';\nconst python='+json.dumps(sys.executable)+';\n'+r'''
import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
const module=await import(spec.config_overlay.plugins[0]+'/index.mjs');
const settings={python,pollArgs:[poll,fixture],agent:'batch_flow_front',cwd:spec.cwd,
  notifications:fixture+'.log',reportMs:600000,pollMs:1000000,coalesceMs:30000};
let now=0, busy=false, wrongAgent=false, attempts=[], fail=false, reads=0;
let snapshot={revision:1,events:[],report:{stopped:false,batches:[{state:'active'}],
  driver:{alive:true},running:[],recent:[],ask_user:[],ops:[]}};
function save() { writeFileSync(fixture,JSON.stringify(snapshot)); }
save();
const ctx={session:{
  get:async({sessionID})=>{ reads++; return {agent:wrongAgent?'other':'batch_flow_front',
    parentID:sessionID==='child'?'parent':undefined,location:{directory:spec.cwd},
    time:busy?{busy:2,idle:1}:{busy:1,idle:2}}; },
  prompt:async(p)=>{attempts.push(p); if(fail) throw Error('admission response lost');},
}};
// Any unsolicited write would corrupt the active TUI. Include failure paths in this check.
const stdout=process.stdout.write, stderr=process.stderr.write;
let terminalWrites=0;
process.stdout.write=process.stderr.write=function(){terminalWrites++; return true;};
const n=module.notifications(ctx,settings,()=>now);
await n.bind('child');
await n.tick();
assert.equal(attempts.length,0);
await n.bind('root');
await n.bind('another-root');
now=599999; await n.tick(); assert.equal(attempts.length,0);
now=600000; await n.tick(); assert.equal(attempts.length,1);
assert.equal(attempts[0].sessionID,'root');
assert.equal(attempts[0].delivery,'queue');
assert.equal(attempts[0].metadata.source,'batch-flow-notification');
assert.match(attempts[0].text,/不是用户原话或批准/);
await n.tick(); assert.equal(attempts.length,1);
busy=true;
for(let revision=2; revision<=4; revision++) {
  snapshot.revision=revision; snapshot.events=['new event']; save();
  now+=30000; await n.tick();
}
assert.equal(attempts.length,1);
busy=false; await n.tick(); assert.equal(attempts.length,2);
now+=30000; await n.tick(); assert.equal(attempts.length,2);
snapshot.report.stopped=true; snapshot.revision++; save();
now+=30000; await n.tick(); assert.equal(attempts.length,3);
now+=600000; await n.tick(); assert.equal(attempts.length,3);
snapshot.report.stopped=false; snapshot.revision++; save();
await n.tick(); assert.equal(attempts.length,4);
snapshot.report.batches=[{state:'done'}]; snapshot.revision++; save();
now+=30000; await n.tick(); assert.equal(attempts.length,5);
now+=600000; await n.tick(); assert.equal(attempts.length,5);
snapshot.report.batches=[{state:'active'}]; snapshot.revision++; save();
wrongAgent=true; now+=30000; await n.tick(); assert.equal(attempts.length,5);
wrongAgent=false; fail=true;
await n.tick(); const id=attempts.at(-1).id;
for(let i=0;i<4;i++) { now+=30000; await n.tick(); }
assert.equal(attempts.length,8); // three attempts, same ID for idempotent admission
assert.ok(attempts.slice(-3).every(p=>p.id===id));
snapshot.revision++; save(); fail=false;
now+=30000; await n.tick(); assert.equal(attempts.length,9);
const before=reads; n.close();
snapshot.revision++; save(); now+=600000;
await n.tick(); await n.bind('another');
assert.equal(reads,before); assert.equal(attempts.length,9);
process.stdout.write=stdout; process.stderr.write=stderr;
assert.equal(terminalWrites,0,'plugin must never write directly into the TUI terminal');
console.log('notification behavior verified');
''')
        result = subprocess.run(['node',str(script)],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('behavior verified',result.stdout)
        self.assertEqual(result.stderr,'')

    def test_prompt_uses_native_notifications_without_granting_authorization(self):
        text = front.prompt(self.base,backend='opencode',runtime=self.root)
        self.assertIn('插件每 10 分钟',text)
        self.assertIn('不是用户原话或批准',text)
        self.assertNotIn('不会自动唤醒',text)

    def test_launcher_delivers_front_overlay_to_the_interactive_process(self):
        capture = self.root/'capture.json'
        binary = self.root/'fake_opencode'
        command = shlex.join([sys.executable,str(HERE/'bf/cli.py'),'--base',str(self.base),'status'])
        javascript = '''
const config=JSON.parse(process.env.OPENCODE_CONFIG_CONTENT);
const module=await import(config.plugins[config.plugins.length-1]+'/index.mjs');
let tool;
const session={get:async()=>({agent:'other'}),prompt:async()=>{}};
await module.default.setup({session,tool:{hook:async(name,fn)=>{tool=fn}},shell:{hook:async()=>{}}});
await tool({tool:'shell',input:{command:COMMAND},sessionID:'ses_front'});
'''.replace('COMMAND',json.dumps(command))
        binary.write_text('#!%s\n' % sys.executable +
                          'import json,os,subprocess,sys\nfrom pathlib import Path\n' +
                          'subprocess.run(["node","--input-type=module","-e",%r],check=True)\n' % javascript +
                          'config=json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])\n' +
                          'Path(%r).write_text(json.dumps({"argv":sys.argv[1:],"agent": "batch_flow_front" in config["agents"]}))\n' % str(capture) +
                          'sys.exit(23)\n')
        binary.chmod(0o755)
        result = subprocess.run([str(HERE/'start-batch-flow.sh'),str(self.base.parent),'--backend','opencode',
                                 '--model','test/model','--binary',str(binary)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,23,result.stderr)
        observed = json.loads(capture.read_text())
        self.assertTrue(observed['agent'])
        self.assertIn('--standalone',observed['argv'])
        self.assertTrue((self.base/'front/opencode/ready.json').is_file())


if __name__ == '__main__':
    unittest.main()
