import copy
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from smart_harness.backends import DirectorResult, WorkerResult
from smart_harness.cli import main
from smart_harness.config import DEFAULTS
from smart_harness.contracts import DecisionError, parse_decision
from smart_harness.engine import Engine
from smart_harness.storage import load, new_task, save


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"SMART_HARNESS_HOME": self.tmp.name})
        self.env.start()
        self.cfg = copy.deepcopy(DEFAULTS)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_parallel_read_only_workers_overlap_and_are_persisted(self):
        barrier = threading.Barrier(2, timeout=3)
        class Worker:
            def call(self, prompt, read_only):
                assert read_only
                barrier.wait()
                return WorkerResult(prompt, prompt, True, 0, True)
        state = new_task('task', 'mock')
        decision = {'tasks': [{'id': 'a', 'prompt': 'a', 'read_only': True},
                              {'id': 'b', 'prompt': 'b', 'read_only': True}], 'parallel': True}
        Engine(self.cfg, worker=Worker(), log=lambda _: None)._dispatch(state, decision)
        self.assertEqual(len([e for e in load(state['id'])['events'] if e['kind'] == 'worker']), 2)

    def test_total_worker_limit_blocks_before_execution(self):
        class Worker:
            def call(self, *args):
                self.fail('worker should not run')
        state = new_task('task', 'mock')
        self.cfg['limits']['max_total_workers'] = 1
        decision = {'tasks': [{'id': 'a', 'prompt': 'a'}, {'id': 'b', 'prompt': 'b'}]}
        from smart_harness.engine import BudgetError
        with self.assertRaises(BudgetError):
            Engine(self.cfg, worker=Worker())._dispatch(state, decision)
        self.assertEqual(load(state['id'])['worker_count'], 0)

    def test_interrupted_dispatch_followup_preserves_warning(self):
        state = new_task('task', 'mock')
        state['status'] = 'workers_running'
        state['director_calls'] = 2
        state['events'].append({'kind': 'worker', 'id': 'old', 'summary': 'result'})
        save(state)
        self.assertEqual(main(['continue', 'Please proceed']), 0)
        notes = [e.get('message', '') for e in load(state['id'])['events'] if e['kind'] == 'note']
        self.assertTrue(any('interrupted' in n for n in notes))

    def test_numeric_task_id_is_rejected(self):
        with self.assertRaises(DecisionError):
            parse_decision(json.dumps({'action': 'dispatch', 'tasks': [{'id': 1, 'prompt': 'x'}]}), 3)
