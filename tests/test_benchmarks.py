import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_harness.backends import WorkerResult
from smart_harness.benchmarks import run_suite, load_suite, fixture_revision, compare, snapshot
from smart_harness.cli import main
from smart_harness.config import DEFAULTS
from smart_harness.telemetry import Telemetry, now


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, SMART_HARNESS_HOME=str(self.base / 'state'))
        self.env.start()
        fixture = self.base/'fixture'
        fixture.mkdir()
        (fixture/'result').write_text('broken')
        (self.base/'prompt.md').write_text('repair result')
        self.registry = {'score': [sys.executable, '-c', "from pathlib import Path; assert Path('result').read_text()=='fixed'"]}
        self.suite = {'schema_version':1,'suite_id':'test','tasks':[{'case_id':'repair','repo_fixture':'fixture',
            'revision':fixture_revision(fixture),'prompt_file':'prompt.md','acceptance':[
            {'kind':'command','id':'score','registered_check':'score'},
            {'kind':'git-diff','id':'paths','allowed_paths':['result']}]}],
            'variants':{'baseline':{'strategy':'direct'},'helper':{'strategy':'delegate-with-helper'}}}
        self.path = self.base/'suite.json'
        self.path.write_text(json.dumps(self.suite))

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def test_pinned_isolated_trials_and_independent_scoring(self):
        seen = []
        class Worker:
            def __init__(self, cfg): self.cwd = Path(cfg['working_directory'])
            def call(self, prompt, read_only, lifecycle=None):
                seen.append(str(self.cwd))
                if not read_only:
                    self.assert_pristine = (self.cwd/'result').read_text() == 'broken'
                    if not self.assert_pristine: raise AssertionError('fixture reused')
                    (self.cwd/'result').write_text('fixed')
                return WorkerResult('plan','private output',True,None,False)
        cfg = copy.deepcopy(DEFAULTS); cfg['mode']='live'
        result = run_suite(self.path,None,2,cfg,self.registry,worker_factory=Worker)
        self.assertEqual(len(result),4)
        self.assertTrue(all(r['status']=='passed' for r in result))
        self.assertTrue(all(r['evidence']['pass']==2 for r in result))
        self.assertEqual(len(set(seen)),4)
        self.assertEqual((self.base/'fixture'/'result').read_text(),'broken')
        self.assertTrue(all(not Path(path).exists() for path in seen))
        comparison = compare(Telemetry(),'test','baseline','helper')
        self.assertEqual(comparison['verdict'],'inconclusive')
        helpers = [r for r in result if r['variant_id']=='helper']
        self.assertEqual(helpers[0]['calls_by_role']['micro_helper'],1)

    def test_worker_claim_does_not_override_failing_score(self):
        class Worker:
            def __init__(self,_): pass
            def call(self,*args,**kwargs): return WorkerResult('ALL TESTS PASS','',True,None,False)
        cfg = copy.deepcopy(DEFAULTS); cfg['mode']='live'
        result = run_suite(self.path,['baseline'],1,cfg,self.registry,worker_factory=Worker)
        self.assertEqual(result[0]['status'],'failed')
        self.assertEqual(result[0]['evidence']['fail'],1)

    def test_unknown_registry_and_changed_fixture_rejected(self):
        with self.assertRaises(ValueError): load_suite(self.path,{})
        (self.base/'fixture'/'result').write_text('changed')
        with self.assertRaises(ValueError): load_suite(self.path,self.registry)

    def test_symlink_and_traversal_rejected(self):
        try:
            (self.base/'fixture'/'link').symlink_to(self.base/'prompt.md')
        except OSError:
            pass  # Windows hosts may disallow creating symlinks.
        else:
            with self.assertRaises(ValueError): snapshot(self.base/'fixture')
            (self.base/'fixture'/'link').unlink()
        self.suite['tasks'][0]['repo_fixture']='../'
        self.path.write_text(json.dumps(self.suite))
        with self.assertRaises(ValueError): load_suite(self.path,self.registry)

    def test_mock_never_claims_success(self):
        rows = run_suite(self.path,['baseline'],1,copy.deepcopy(DEFAULTS),self.registry)
        self.assertEqual(rows[0]['status'],'mock')
        self.assertEqual(compare(Telemetry(),'test','baseline','helper')['verdict'],'inconclusive')

    def test_quality_coverage_and_repeated_cost_gate(self):
        store = Telemetry()
        for variant, amount in [('base','1'),('new','.5')]:
            for repeat in range(3):
                run = variant+str(repeat)
                store.run(run,suite_id='economic',variant_id=variant,status='passed',finished_utc=now(),
                    case_id='fixed',repo_revision='a',prompt_sha256='b',config_sha256='c',suite_sha256='d',mode='live')
                store.update_call(run,run_id=run,actor_role='worker',generation_id=run)
                store.charge(run,'default',run,'openrouter_settled',amount)
        self.assertEqual(compare(store,'economic','base','new')['verdict'],'candidate_wins')
        store.run('new2',status='failed')
        self.assertEqual(compare(store,'economic','base','new')['verdict'],'correctness_regression')
        store.run('new2',status='passed',repo_revision='different')
        self.assertEqual(compare(store,'economic','base','new')['verdict'],'inconclusive')

    def test_cli_metrics_json_and_other_inspection_commands(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            main(['--mock','run','demo','--metrics-json'])
        data = json.loads(out.getvalue())
        run = data['run_id']
        task = data['task_root_id']
        for argv in [['metrics','--run',run], ['observe','tree','--run',run],
            ['observe','timeline','--run',run], ['observe','task','--run',run,'--task',task],
            ['costs','audit','--run',run], ['bench','import','--task',task,'--from-existing-state']]:
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(argv),0)
                json.loads(out.getvalue())

    def test_modified_trusted_scorer_invalidates_trial(self):
        scorer = self.base/'score.py'
        scorer.write_text('raise SystemExit(1)')
        registry = {'score':[sys.executable,str(scorer)]}
        class Worker:
            def __init__(self,_): pass
            def call(self,*args,**kwargs):
                scorer.write_text('raise SystemExit(0)')
                return WorkerResult('done','',True,None,False)
        cfg=copy.deepcopy(DEFAULTS); cfg['mode']='live'
        results=run_suite(self.path,['baseline'],1,cfg,registry,worker_factory=Worker)
        self.assertEqual(results[0]['status'],'failed')
        self.assertEqual(results[0]['failure'],'ValueError')
