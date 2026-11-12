import copy
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from smart_harness.accounting import normalize, OpenRouterClient
from smart_harness.backends import OpenRouterDirector
from smart_harness.config import DEFAULTS
from smart_harness.engine import Engine
from smart_harness.observe import api, serve, session_fetcher
from smart_harness.recovery import import_session_tree
from smart_harness.storage import new_task
from smart_harness.telemetry import Telemetry, Recorder, safe_opencode_event

FIXTURES = Path(__file__).parent / 'fixtures' / 'telemetry'


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, SMART_HARNESS_HOME=self.tmp.name)
        self.env.start()
        self.store = Telemetry()
        self.store.run('r')
        self.store.task('r', 'root')

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def call(self, ident='a', task='root', generation=None):
        self.store.update_call(ident, run_id='r', task_id=task, actor_role='worker',
            provider_id='openrouter', model_id='test/model', generation_id=generation)

    def test_no_false_zero_after_timeout(self):
        self.call()
        result = self.store.metrics('r')
        self.assertIsNone(result['cost']['selected_total_usd'])
        self.assertEqual(result['cost']['unknown_cost_calls'], 1)
        self.assertIsNone(result['usage'].get('input_total_tokens'))

    def test_stream_reimport_is_idempotent_and_exact(self):
        events = [json.loads(line) for line in (FIXTURES / 'complete.jsonl').read_text().splitlines()]
        for _ in range(2):
            for event in events:
                self.store.opencode('r', 'root', event, model='openrouter/test/model')
        result = self.store.metrics('r')
        self.assertEqual(result['llm_calls'], 2)
        self.assertEqual(result['cost']['selected_known_usd'], '0.3')
        self.assertEqual(result['usage']['output_total_tokens'], 12)
        self.assertEqual(result['usage']['input_total_tokens'], 30)
        self.assertEqual(result['usage']['coverage'], 'incomplete')  # unknown children
        self.assertIsNone(result['cost']['selected_total_usd'])

    def test_unique_generation_across_sources(self):
        self.call('a', generation='g')
        self.call('b', generation='g')
        self.store.charge('a', 'default', 'g', 'openrouter_response', '0.2')
        self.store.charge('b', 'default', 'g', 'opencode_observed', '0.3')
        self.store.charge('a', 'default', 'g', 'openrouter_settled', '0.1')
        result = self.store.metrics('r')
        self.assertEqual(result['llm_calls'], 1)
        self.assertEqual(result['cost']['selected_total_usd'], '0.1')
        self.assertEqual(result['cost']['opencode_observed_usd'], '0.3')

    def test_nested_direct_and_inclusive_costs(self):
        self.store.task('r', 'child', parent_task_id='root')
        self.store.task('r', 'grandchild', parent_task_id='child')
        for ident, cost, task in [('a', '0.1', 'root'), ('b', '0.2', 'child'), ('c', '0.3', 'grandchild')]:
            self.call(ident, task)
            self.store.charge(ident, 'default', None, 'opencode_observed', cost)
        tree = {t['task_id']: t for t in self.store.tree('r')}
        self.assertEqual(tree['root']['inclusive_known_usd'], '0.6')
        self.assertEqual(tree['root']['direct_known_usd'], '0.1')
        self.assertEqual(tree['child']['descendant_known_usd'], '0.3')
        self.assertEqual(self.store.metrics('r')['cost']['selected_total_usd'], '0.6')

    def test_reasoning_subset_and_cache_semantics(self):
        usage = normalize({'prompt_tokens':100, 'completion_tokens':50, 'total_tokens':150,
            'prompt_tokens_details':{'cached_tokens':20, 'cache_write_tokens':10},
            'completion_tokens_details':{'reasoning_tokens':30}}, 'openrouter')
        self.assertEqual(usage['output_total_tokens'], 50)
        self.assertEqual(usage['input_uncached_tokens'], 70)
        self.assertEqual(usage['total_tokens'], 150)
        self.assertIsNone(normalize({'prompt_tokens':100, 'completion_tokens':50}, 'openrouter')['input_uncached_tokens'])
        self.assertFalse(normalize({'tokens':100}, 'unknown')['usage_complete'])

    def test_provider_disagreement_snapshots_byok_and_idempotence(self):
        self.call(generation='g')
        self.store.charge('a', 'default', 'g', 'opencode_observed', '.2')
        raw = {'data': {'id':'g','total_cost':.1,'model':'test/model','is_byok':True,'upstream_inference_cost':.9}}
        client = OpenRouterClient('secret', opener=lambda *a, **k: io.BytesIO(json.dumps(raw).encode()), sleep=lambda _:None)
        client.reconcile(self.store, 'r')
        client.reconcile(self.store, 'r')
        charges = self.store.call_charges('a')
        self.assertEqual(len(charges), 3)
        self.assertEqual(self.store.metrics('r')['cost']['selected_total_usd'], '0.1')
        self.assertEqual(self.store.metrics('r')['cost']['upstream_provider_usd'], '0.9')
        self.assertEqual(self.store.calls('r')[0]['reconciliation_status'], 'provider_disagreement')
        raw['data']['total_cost'] = .11
        client.reconcile(self.store, 'r')
        self.assertEqual(len(self.store.call_charges('a')), 5)
        self.assertEqual(self.store.metrics('r')['cost']['selected_total_usd'], '0.11')

    def test_provider_retries_and_status_classification(self):
        self.call(generation='g')
        for code, status, count in [(404,'not_found',3),(429,'rate_limited',3),(500,'provider_error',3),(401,'auth_failure',1)]:
            with self.subTest(code=code):
                def fail(*args, **kwargs):
                    raise urllib.error.HTTPError('https://openrouter.ai',code,'failure',{},None)
                with patch('urllib.request.urlopen', side_effect=fail) as opener:
                    result = OpenRouterClient('secret', sleep=lambda _:None).reconcile(self.store,'r',retries=2)
                    self.assertEqual(result[0]['status'], status)
                    self.assertEqual(opener.call_count, count)
        self.assertIsNone(self.store.metrics('r')['cost']['selected_total_usd'])

    def test_missing_generation_never_fuzzy_joined(self):
        self.call('a')
        self.call('b')
        client = OpenRouterClient('secret', opener=lambda *_: self.fail('lookup without known ID'))
        self.assertEqual({r['status'] for r in client.reconcile(self.store,'r')}, {'missing_generation_id'})

    def test_nested_session_recovery_and_unknown_descendants(self):
        def fetch(path):
            session = path.split('/')[2]
            if path.endswith('/children'):
                return [{'id':'s2'},{'id':'s3'}] if session == 's1' else []
            return [{'info':{'id':'m'+session,'role':'assistant','modelID':'openrouter/test/model'},
                     'parts':[{'id':'p'+session,'type':'step-finish','reason':'stop','cost':.1,
                               'tokens':{'input':1,'output':1,'reasoning':0,'cache':{'read':0,'write':0}}}]}]
        for _ in range(2):
            import_session_tree(self.store,'r','root','s1',fetch)
        self.assertEqual(self.store.metrics('r')['llm_calls'], 3)
        self.assertEqual(self.store.metrics('r')['cost']['selected_total_usd'], '0.3')
        self.assertEqual(self.store.metrics('r')['usage']['coverage'], 'complete')
        import_session_tree(self.store,'r','root','bad',lambda _: {'new_schema':True})
        self.assertIsNone(self.store.metrics('r')['cost']['selected_total_usd'])

    def test_privacy_and_version_failures(self):
        event = {'type':'tool_use','sessionID':'s','part':{'id':'p','tool':'bash','state':{'input':'SECRET', 'output':'SOURCE','status':'completed'}}}
        self.assertNotIn('SECRET',json.dumps(safe_opencode_event(event)))
        self.store.opencode('r','root',event)
        self.store.opencode('r','root',{'type':'step_finish'})
        self.store.event('r','test',payload={'Authorization':'secret', 'api_key':'secret','prompt':'secret'})
        content = json.dumps(self.store.timeline('r'))
        self.assertNotIn('SECRET',content)
        self.assertNotIn('secret',content)
        self.assertTrue(any(e['kind']=='adapter.failure' for e in self.store.timeline('r')))

    def test_readonly_loopback_http(self):
        with self.assertRaises(ValueError):
            serve(self.store,'0.0.0.0')
        with self.assertRaises(ValueError):
            session_fetcher('https://example.com')
        server = serve(self.store,port=0)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            url = 'http://127.0.0.1:' + str(server.server_port)
            self.assertEqual(json.load(urllib.request.urlopen(url+'/api/v1/runs'))[0]['run_id'],'r')
            for suffix in ('', '/metrics','/tree','/timeline','/costs','/tasks/root','/tasks/root/events'):
                with urllib.request.urlopen(url+'/api/v1/runs/r'+suffix) as response:
                    self.assertEqual(response.status,200)
            req = urllib.request.Request(url+'/api/v1/runs',data=b'{}',method='POST')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(req)
            self.assertEqual(error.exception.code,501)
            req = urllib.request.Request(url+'/api/v1/runs',headers={'Host':'evil.test'})
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(req)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_metadata_captured_before_malformed_content(self):
        obj = {'id':'g','usage':{'cost':.12},'choices':[]}
        backend = OpenRouterDirector(DEFAULTS['director'])
        backend.on_response = lambda response: self.store.update_call('a',run_id='r',generation_id=response['id'])
        with patch.dict(os.environ, OPENROUTER_API_KEY='secret'), patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(obj).encode())):
            with self.assertRaises(RuntimeError): backend.call('system','private')
        self.assertEqual(self.store.calls('r')[0]['generation_id'],'g')

    def test_normal_outage_and_strict_failure(self):
        state = {}
        with patch('smart_harness.telemetry.Telemetry',side_effect=OSError('disk')):
            with self.assertWarns(RuntimeWarning): recorder = Recorder(state)
            recorder.event('r','anything')
            self.assertTrue(state['telemetry_incomplete'])
            with self.assertRaises(OSError): Recorder({},strict=True)

    def test_restart_preserves_run_and_changes_attempt(self):
        state = new_task('demo','mock')
        Engine(copy.deepcopy(DEFAULTS),log=lambda *_:None).run(state)
        run_id = state['telemetry_run_id']
        state['status']='running'
        Engine(copy.deepcopy(DEFAULTS),log=lambda *_:None).run(state)
        self.assertEqual(state['telemetry_run_id'],run_id)
        attempts = {e['attempt_id'] for e in self.store.timeline(run_id) if e['kind']=='run.started'}
        self.assertEqual(len(attempts),2)

    def test_legacy_import_idempotent(self):
        state = new_task('private objective','live')
        state['events']=[{'kind':'director','cost_usd':.1,'usage':{'prompt_tokens':1,'completion_tokens':1}}]
        a = self.store.import_state(state)
        self.store.import_state(state)
        self.assertEqual(self.store.metrics(a)['llm_calls'],1)
        self.assertNotIn('private objective',json.dumps(self.store.runs()))

    def test_recovered_history_resolves_stream_gap(self):
        self.store.event('r','session.incomplete',task_id='root',payload={'session_id':'s'})
        message = [{'info':{'id':'m','role':'assistant','finish':'stop','tokens':{'input':1,'output':2,'reasoning':0,'cache':{'read':0,'write':0}},'cost':.1},'parts':[]}]
        import_session_tree(self.store,'r','root','s',lambda path: [] if path.endswith('children') else message)
        self.assertEqual(self.store.metrics('r')['coverage_gaps'],0)
        self.assertEqual(self.store.metrics('r')['cost']['selected_total_usd'],'0.1')

    def test_background_reconciliation_is_durable(self):
        from smart_harness.accounting import ReconciliationQueue
        self.call(generation='g')
        raw = {'data':{'id':'g','model':'test/model','total_cost':.1}}
        client = OpenRouterClient('secret',opener=lambda *a,**kw:io.BytesIO(json.dumps(raw).encode()))
        queue = ReconciliationQueue(self.store,client)
        queue.submit('r'); queue.close()
        self.assertEqual(self.store.metrics('r')['cost']['selected_total_source'],'openrouter_settled')

    def test_tree_selects_settled_charge_on_alias(self):
        self.call('a',generation='g'); self.call('b',generation='g')
        self.store.charge('a','default','g','opencode_observed','.2')
        self.store.charge('b','default','g','openrouter_settled','.1')
        self.assertEqual(self.store.tree('r')[0]['direct_known_usd'],'0.1')

    def test_complete_and_interrupted_cli_streams(self):
        from smart_harness.backends import run_opencode
        from types import SimpleNamespace
        complete = (FIXTURES/'complete.jsonl').read_text()
        for stream, expected, amount in [(complete,True,.3),(complete + complete,True,.3),((FIXTURES/'incomplete.jsonl').read_text(),False,.1)]:
            with patch('smart_harness.backends.shutil.which',return_value='opencode'), patch('smart_harness.backends.subprocess.run',return_value=SimpleNamespace(stdout=stream,stderr='',returncode=0)):
                result = run_opencode('private',model='test/model',agent='build',cwd=Path(self.tmp.name),timeout=1,auto_approve=False)
                self.assertEqual(result[2],expected)
                self.assertAlmostEqual(result[1],amount)

    def test_recording_outage_does_not_change_orchestration(self):
        state = new_task('demo','mock')
        with patch('smart_harness.telemetry.Telemetry',side_effect=OSError('disk')):
            with self.assertWarns(RuntimeWarning):
                outcome = Engine(copy.deepcopy(DEFAULTS),log=lambda *_:None).run(state)
        self.assertEqual(outcome['worker_count'],2)
        self.assertTrue(outcome['telemetry_incomplete'])
        self.assertEqual(outcome['status'],'partial')

    def test_cache_read_write_tiers_and_invalid_counts(self):
        engine = Engine(copy.deepcopy(DEFAULTS),log=lambda *_:None)
        engine.prices = {'m':{'prompt':'.1','completion':'.2','input_cache_read':'.01','input_cache_write':'.3',
            'overrides':[{'min_prompt_tokens':100,'prompt':'.2','input_cache_read':'.02'}]}}
        engine.prices_updated_at = __import__('time').monotonic()
        usage = {'token_events':[{'input':10,'output':3,'reasoning':2,'cache':{'read':100,'write':1}}], 'token_events_complete':True}
        self.assertAlmostEqual(engine._cost(None,usage,'m','live')[0],5.3)
        usage['token_events'][0]['cache']['read']=-1
        self.assertEqual(engine._cost(None,usage,'m','live'),(None,'unavailable'))

    def test_subscription_zero_is_not_reported_as_free_api_charge(self):
        event = json.loads((FIXTURES/'complete.jsonl').read_text().splitlines()[0])
        event['part']['cost']=0
        self.store.opencode('r','root',event,role='director',model='openai/subscription',billing_mode='subscription')
        self.assertEqual(self.store.metrics('r')['cost']['unknown_cost_calls'],1)
        self.assertEqual(self.store.metrics('r')['cost']['selected_known_usd'],'0')
        self.assertIsNone(self.store.metrics('r')['cost']['selected_total_usd'])

    def test_missing_categories_and_child_evidence_are_incomplete(self):
        usage=normalize({'prompt_tokens':10,'completion_tokens':5},'openrouter')
        self.assertFalse(usage['usage_complete'])
        self.store.task('r','child',parent_task_id='root')
        self.store.event('r','session.linked',task_id='child',payload={'session_id':'unseen'})
        self.assertTrue(all(not t['cost_complete'] for t in self.store.tree('r')))
