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
from smart_harness.contracts import DecisionError, parse_decision, director_user_message
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

    def test_usd_budget_blocks_before_execution(self):
        state = new_task('task', 'live')
        state['budget_reported_usd'] = self.cfg['limits']['budget_usd']
        decision = {'tasks': [{'id': 'a', 'prompt': 'a'}]}
        from smart_harness.engine import BudgetError
        with self.assertRaises(BudgetError):
            Engine(self.cfg, log=lambda _: None)._dispatch(state, decision)
        self.assertEqual(load(state['id'])['worker_count'], 0)

    def test_interrupted_dispatch_followup_preserves_warning(self):
        state = new_task('task', 'mock')
        state['status'] = 'workers_running'
        state['director_calls'] = 2
        state['events'].append({'kind': 'worker', 'id': 'old', 'summary': 'result'})
        save(state)
        self.assertEqual(main(['continue', 'Please proceed']), 1)
        notes = [e.get('message', '') for e in load(state['id'])['events'] if e['kind'] == 'note']
        self.assertTrue(any('interrupted' in n for n in notes))

    def test_numeric_task_id_is_rejected(self):
        with self.assertRaises(DecisionError):
            parse_decision(json.dumps({'action': 'dispatch', 'tasks': [{'id': 1, 'prompt': 'x'}]}), 3)

    def test_all_followups_remain_in_context(self):
        state = new_task('original', 'mock')
        state['followups'] = [f'correction {i}' for i in range(8)]
        packet = director_user_message(state, 22000)
        for correction in state['followups']:
            self.assertIn(correction, packet)

    def test_interrupted_assignment_is_preserved_and_blocks_completion(self):
        from smart_harness.backends import MockDirector
        state = new_task('task', 'mock')
        state['status'] = 'workers_running'
        state['director_calls'] = 2
        state['assignments'] = {'old': {'status': 'running', 'prompt': 'modify file'}}
        state['events'] = [{'kind': 'worker', 'id': 'prior', 'summary': 'earlier'}]
        out = Engine(self.cfg, director=MockDirector(), log=lambda _: None).run(state)
        self.assertEqual(out['assignments']['old']['status'], 'interrupted')
        self.assertFalse(out['worker_cost_complete'])
        self.assertEqual(out['status'], 'partial')
