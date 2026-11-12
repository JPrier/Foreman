import copy, io, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from smart_harness.backends import OpenCodeWorker
from smart_harness.config import DEFAULTS, load_config
from smart_harness.engine import Engine

class IntegrationRegressionTests(unittest.TestCase):
    def test_jsonc_configuration_and_legacy_director(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'config.jsonc'
            p.write_text('{ // user settings\n "mode":"live", "director":{"backend":"opencode","model":"openai/gpt-6.1-sol","agent":"smart-harness-director","review_stipulations":["See https://example.test/"]}, "limits":{"budget_usd":10}}', encoding='utf-8')
            c = load_config(p)
            self.assertEqual(c['director']['model'], 'openai/gpt-6.1-sol')
            self.assertEqual(c['director']['review_stipulations'], ['See https://example.test/'])
            self.assertEqual(c['limits']['budget_usd'], 10)
            p.write_text(json.dumps({'director': {'backend':'opencode','opencode_model':'openai/gpt-6.1-sol#high','opencode_agent':'smart-harness-director'}}), encoding='utf-8')
            self.assertEqual(load_config(p)['director']['model'], 'openai/gpt-6.1-sol')

    def test_read_only_honors_auto_approve(self):
        cfg = copy.deepcopy(DEFAULTS['worker']); cfg['auto_approve'] = True
        with patch('smart_harness.backends.run_opencode', return_value=('ok', 0.0, True, None, 0, '')) as run:
            OpenCodeWorker(cfg).call('inspect', True)
        self.assertTrue(run.call_args.kwargs['auto_approve'])

    def test_cost_prefers_reported_and_charges_cache_reads(self):
        engine = Engine(copy.deepcopy(DEFAULTS), log=lambda _: None)
        self.assertEqual(engine._cost(0.2, {}, 'missing/model', 'live'), (0.2, 'reported'))
        payload = {'data':[{'id':'provider/model','pricing':{'prompt':'0.01','completion':'0.02'}}]}
        usage = {'token_events':[{'input':10,'output':3,'reasoning':2,'cache':{'read':100,'write':0}}]}
        with patch('smart_harness.engine.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
            value, source = engine._cost(None, usage, 'openrouter/provider/model', 'live')
        self.assertEqual(source, 'estimated')
        self.assertAlmostEqual(value, 1.2)

if __name__ == '__main__': unittest.main()
