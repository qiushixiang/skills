"""OpenCode V2 front: isolated server, custom primary agent and a native tool/shell guard."""
import copy
import json
import os
from pathlib import Path
import re
import sys

AGENT = 'batch_flow_front'
DEFAULT_MODEL = 'deepseek/deepseek-flash'


def prepare(base, binary, runtime, instructions, model=None, account_switch=()):
    if model is not None and re.fullmatch(r'[^/\s#]+/[^\s#]+', model) is None:
        raise ValueError('OpenCode front --model needs provider/model; use opencode models')
    selected_model = model if model is not None else DEFAULT_MODEL
    plugin = runtime / 'plugin'
    plugin.mkdir(exist_ok=True)
    plugin_url = plugin.as_uri()
    args = [str(Path(__file__).with_name('front.py')), 'hook', '--backend', 'opencode',
            '--base', str(base), '--runtime', str(runtime)]
    for path in account_switch:
        args += ['--account-switch', path]
    # No environment or provider credentials are embedded in the plugin/config preview.
    config = {'python': sys.executable, 'hookArgs': args, 'agent': AGENT, 'plugin': plugin_url,
              'cwd': str(base.parent), 'loaded': str(runtime / 'plugin-ready.json'),
              'pollArgs': [str(Path(__file__).resolve()), '--base', str(base)],
              'notifications': str(runtime / 'notifications.log'),
              'pollMs': 5000, 'reportMs': 600000, 'coalesceMs': 30000}
    source = Path(__file__).with_name('opencode_front.mjs').read_text(encoding='utf-8')
    (plugin / 'index.mjs').write_text(source.replace('__BATCH_FLOW_FRONT_CONFIG__', json.dumps(config)), encoding='utf-8')
    (plugin / 'package.json').write_text(json.dumps({'name': 'batch-flow-front-guard', 'type': 'module',
                                                   'exports': './index.mjs'}), encoding='utf-8')
    overlay = {'agents': {AGENT: {'mode': 'primary', 'description': 'batch-flow operations and communication',
                                 'system': instructions,
                                 'permissions': [{'action': 'subagent', 'resource': '*', 'effect': 'deny'}]}},
               'plugins': [plugin_url]}
    argv = [binary, 'mini', '--standalone', '--agent', AGENT, '--prompt', '先运行 status 并展示状态，等用户指示。']
    argv += ['--model', selected_model]
    return {'argv': argv, 'cwd': str(base.parent), 'runtime': str(runtime), 'backend': 'opencode',
            'requested_model': model, 'model': selected_model, 'config_overlay': overlay}


def environment(spec, environ=None):
    env = dict(os.environ if environ is None else environ)
    # OpenCode uses the inherited logical directory; Popen(cwd=...) alone leaves PWD stale.
    env['PWD'] = spec['cwd']
    # OpenTUI treats any nonempty DEBUG (even "release") as permission to console.log
    # reconciler operations into the TUI. Preserve it for shell children, not the renderer.
    env.pop('BF_OPENCODE_INHERITED_DEBUG', None)
    if 'DEBUG' in env:
        env['BF_OPENCODE_INHERITED_DEBUG'] = env.pop('DEBUG')
    original = json.loads(env.get('OPENCODE_CONFIG_CONTENT') or '{}')
    if not isinstance(original, dict):
        raise ValueError('OPENCODE_CONFIG_CONTENT must be a JSON object')
    config = copy.deepcopy(original)
    agents = config.setdefault('agents', {})
    plugins = config.setdefault('plugins', [])
    if not isinstance(agents, dict) or not isinstance(plugins, list):
        raise ValueError('OpenCode agents must be an object and plugins an array')
    if AGENT in agents:
        raise ValueError('OpenCode agent %s already exists; refusing to overwrite it' % AGENT)
    agents.update(spec['config_overlay']['agents'])
    plugins.extend(spec['config_overlay']['plugins'])
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(config, ensure_ascii=False)
    return env


def hook(event, base, runtime, account_switch):
    from . import front, front_hosts
    args = event.get('input') or {}
    args = args if isinstance(args, dict) else {}
    name = event.get('tool')
    cwd = event.get('cwd') or str(base.parent)
    mapping = {'shell': ('Bash', {'command': args.get('command', '')}),
               'bash': ('Bash', {'command': args.get('command', '')}),
               'read': ('Read', {}), 'glob': ('Glob', {}), 'grep': ('Grep', {}),
               'skill': ('Skill', {}), 'write': ('Write', {'file_path': args.get('path', '')})}
    tool, inp = mapping.get(name, ('unknown', {}))
    allowed = front.hook_decision({'tool_name': tool, 'tool_input': inp, 'cwd': cwd}, base, account_switch)['decision'] == 'allow'
    if name == 'patch':
        allowed = front_hosts._patch_allowed(args.get('patch') or args.get('patchText') or '', base, cwd)
    if name in ('execute', 'question'):
        # execute is OpenCode's sandboxed code-mode container; nested native tools/shells are still guarded.
        allowed = True
    if event.get('sessionID'):
        (runtime / 'ready.json').write_text(json.dumps({'session': event['sessionID'], 'model': event.get('model')}), encoding='utf-8')
    return {'decision': 'allow' if allowed else 'deny',
            'reason': '' if allowed else 'batch-flow front: only bound bf commands, reads and new answers/ files are allowed'}


def notification_snapshot(base, since=None):
    """Read-only plugin polling; first read starts at the current revision, without replay."""
    from bf import api
    report = api.report(base)
    lines, revision = api.watch(base, report['revision'] if since is None else since)
    return {'report': report, 'events': lines, 'revision': revision}


if __name__ == '__main__':
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser(description='Read-only OpenCode front notification snapshot')
    parser.add_argument('--base', required=True)
    parser.add_argument('--since', type=int)
    options = parser.parse_args()
    print(json.dumps(notification_snapshot(options.base, options.since), ensure_ascii=False))
