"""Offline model metadata and oral-name regression tests; no inference calls."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_cli
import model_catalog as catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.profile = dict(backend='agy', binary='/fake/agy', version='1')

    def query(self, text, backend='agy'):
        profile = dict(self.profile, backend=backend)
        with patch.object(catalog.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, text, '')) as run:
            result = catalog.query(profile)
            self.assertNotIn('run', run.call_args.args[0][1:])
        return profile, result

    def test_oral_family_requires_real_variant_choice(self):
        p, c = self.query('gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n'
                          'gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n')
        result = catalog.resolve(p, c, 'gemini 3.8 flash')
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertNotIn('config', result)
        result = catalog.resolve(p, c, 'Gemini 3.8 Flash high')
        self.assertEqual(result['config'], dict(tool='agy', model='gemini-3.8-flash-high', effort='default'))

    def test_opencode_preserves_provider_and_omits_private_fields(self):
        p, c = self.query(json.dumps([dict(providerID='deepseek', id='deepseek-flash',
            name='DeepSeek V4.1 Flash 1017', apiKey='secret')]), 'opencode')
        self.assertNotIn('secret', json.dumps(c))
        self.assertEqual(catalog.resolve(p, c, 'deepseek 4.1 flash 1017')['status'], 'resolved')
        self.assertEqual(catalog.resolve(p, c, 'DeepSeek V4.1 Flash 1017')['config']['model'], 'deepseek/deepseek-flash')
        c['models'].append(dict(id='other/flash', name='DeepSeek V4.1 Flash 1017'))
        self.assertEqual(catalog.resolve(p, c, 'DeepSeek V4.1 Flash 1017')['status'], 'needs_clarification')
        self.assertEqual(catalog.resolve(p, c, 'deepseek/deepseek-flash')['status'], 'resolved')

    def test_no_silent_substitution_or_stale_catalogue(self):
        p, c = self.query('model-high\tModel (High)\n')
        self.assertEqual(catalog.resolve(p, c, 'another')['status'], 'not_found')
        for key, value in [('backend', 'codex'), ('profile_sha256', 'other'), ('version', '2'),
                           ('checked_at', (dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=2)).isoformat())]:
            changed = copy.deepcopy(c); changed[key] = value
            with self.assertRaises(ValueError):
                catalog.resolve(p, changed, 'Model')
        with self.assertRaises(ValueError):
            catalog.resolve(p, c, 'Model', 'invented')

    def test_claude_aliases_do_not_claim_full_model_list(self):
        p, c = self.query(" --model <model> Model alias (e.g. 'opus' or 'sonnet')\n --other Something 'unrelated'", 'claude')
        self.assertFalse(c['complete'])
        self.assertEqual([v['id'] for v in c['models']], ['opus', 'sonnet'])
        self.assertEqual(catalog.resolve(p, c, 'opus 5.5')['status'], 'unverified')

    def test_codex_metadata_rpc_and_effort(self):
        # Real child transport, fake server: assert only metadata methods are sent.
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp)/'codex'
            fake.write_text('#!' + sys.executable + '\n' + '''import json, sys
for line in sys.stdin:
 m=json.loads(line)
 assert m['method'] in ['initialize','initialized','model/list']
 if 'id' in m:
  result={} if m['method']=='initialize' else {'data':[{'model':'gpt-test','displayName':'GPT Test','supportedReasoningEfforts':[{'reasoningEffort':'low'}],'defaultReasoningEffort':'low'}], 'nextCursor':None}
  print(json.dumps({'id':m['id'],'result':result}),flush=True)
''')
            fake.chmod(0o700)
            p = dict(backend='codex', binary=str(fake), version='1')
            c = catalog.query(p)
            self.assertEqual(catalog.resolve(p, c, 'GPT Test')['status'], 'resolved')
            with self.assertRaises(ValueError):
                catalog.resolve(p, c, 'GPT Test', 'high')

    def test_adapter_entry_saves_catalogue_exclusively_and_does_not_set_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile, output = Path(tmp)/'profile.json', Path(tmp)/'models.json'
            profile.write_text(json.dumps(self.profile))
            args = ['agent_cli.py', 'models', '--profile', str(profile), '--output', str(output)]
            with patch.object(sys, 'argv', args), patch.object(agent_cli, 'check_profile'), \
                 patch.object(catalog, 'query', return_value={'models': []}), patch('builtins.print'):
                self.assertEqual(agent_cli.main(), 0)
                self.assertEqual(agent_cli.main(), 2)
            self.assertEqual(json.loads(output.read_text()), {'models': []})

if __name__ == '__main__':
    unittest.main()
