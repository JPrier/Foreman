import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from smart_harness.cli import main
from smart_harness.config import DEFAULTS
from smart_harness.engine import BudgetError, Engine
from smart_harness.storage import execution_lock, load, new_task, save


class ForceContinueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, SMART_HARNESS_HOME=self.tmp.name)
        self.env.start()
        self.cfg = copy.deepcopy(DEFAULTS)
        self.cfg['limits']['budget_usd'] = 20.0
        self.state = new_task('keep working', 'mock')
        self.state.update(status='budget_exhausted', budget_reported_usd=25.0,
                          budget_estimated_usd=5.0, budget_start_usd=10.0)
        save(self.state)
        self.config = patch('smart_harness.cli.load_config', return_value=self.cfg)
        self.config.start()

    def tearDown(self):
        self.config.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_refresh_preserves_history_and_enforces_new_allowance(self):
        for command in (['force-continue'], ['continue', '--force']):
            with self.subTest(command=command), patch('smart_harness.cli.Engine') as engine:
                engine.return_value.run.side_effect = lambda s, **kw: s
                self.assertEqual(main([*command, '--task', self.state['id']]), 1)
                s = load(self.state['id'])
                self.assertEqual(s['budget_start_usd'], 30.0)
                self.assertEqual(s['status'], 'running')
                self.assertEqual(s['budget_reported_usd'], 25.0)
                self.assertEqual(s['budget_estimated_usd'], 5.0)
                self.assertEqual(s['events'][-1]['budget_usd'], 20.0)
                self.assertTrue(engine.return_value.run.call_args.kwargs['already_locked'])
                checker = Engine(self.cfg, log=lambda _: None)
                s['mode'] = 'live'
                checker._check_budget(s)
                s['budget_reported_usd'] += 20.0
                with self.assertRaises(BudgetError):
                    checker._check_budget(s)

    def test_ordinary_continue_does_not_refresh(self):
        with patch('smart_harness.cli.Engine') as engine:
            engine.return_value.run.side_effect = lambda s, **kw: s
            main(['continue'])
        self.assertEqual(load()['budget_start_usd'], 10.0)
        self.assertEqual(load()['events'], [])

    def test_incomplete_or_invalid_accounting_cannot_refresh(self):
        for changes in ({'budget_cost_complete': False}, {'budget_reported_usd': float('nan')},
                        {'budget_estimated_usd': -1}, {'budget_reported_usd': None}):
            with self.subTest(changes=changes), patch('smart_harness.cli.Engine') as engine:
                save(dict(self.state, **changes))
                self.assertEqual(main(['force-continue']), 2)
                engine.assert_not_called()
                self.assertEqual(load()['budget_start_usd'], 10.0)

    def test_live_process_cannot_refresh(self):
        for recovery in ('worker_running', 'coordinator_running'):
            with patch('smart_harness.recovery.reconcile', return_value=recovery), patch('smart_harness.cli.Engine') as engine:
                self.assertEqual(main(['force-continue']), 2)
                engine.assert_not_called()
                self.assertEqual(load()['budget_start_usd'], 10.0)

    def test_locked_task_cannot_refresh(self):
        with execution_lock(self.state['id']), patch('smart_harness.cli.Engine') as engine:
            self.assertEqual(main(['force-continue']), 2)
            engine.assert_not_called()
        self.assertEqual(load()['budget_start_usd'], 10.0)

    def test_force_still_requires_followup_for_input_or_done(self):
        for status in ('needs_input', 'done'):
            save(dict(self.state, status=status))
            with patch('smart_harness.cli.Engine') as engine:
                self.assertEqual(main(['force-continue']), 2)
                engine.assert_not_called()
            self.assertEqual(load()['budget_start_usd'], 10.0)
