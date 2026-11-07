import copy
import unittest
from unittest.mock import patch
from smart_harness.backends import exported_session_result
from smart_harness.recovery import recover_worker_accounting

class ExportRecoveryTests(unittest.TestCase):
    def session(self):
        return {'info': {'id': 'ses_test'}, 'messages': [
            {'info': {'id': 'm1', 'role': 'assistant', 'cost': .4, 'finish': 'stop', 'time': {'completed': 1}},
             'parts': [{'type': 'step-finish', 'cost': .4}, {'type': 'text', 'text': 'Recovered report'}]}]}

    def test_reject_incomplete_or_ambiguous_export(self):
        for change in ('cost', 'terminal', 'pending', 'duplicate', 'identity'):
            s = self.session()
            if change == 'cost': s['messages'][0]['info']['cost'] = .5
            if change == 'terminal': s['messages'][0]['info']['finish'] = 'tool-calls'
            if change == 'pending': s['messages'][0]['parts'].append({'type': 'tool', 'state': {'status': 'running'}})
            if change == 'duplicate': s['messages'] *= 2
            if change == 'identity': s['info']['id'] = 'other'
            with self.subTest(change=change), self.assertRaises(ValueError):
                exported_session_result(s, 'ses_test')

    @patch('smart_harness.storage.save_worker_output', return_value='report.txt')
    @patch('smart_harness.recovery.save')
    def test_replace_partial_charge_idempotently(self, save, output):
        state = {'assignments': {'w': {'status': 'failed'}}, 'events': [
            {'kind': 'director', 'budget_cost_usd': .1, 'budget_cost_source': 'estimated'},
            {'kind': 'worker', 'id': 'w', 'reported_cost_usd': .2, 'budget_cost_usd': .2, 'budget_cost_source': 'reported'}]}
        for _ in range(2):
            recover_worker_accounting(state, 'w', self.session(), 'ses_test')
            self.assertEqual(state['budget_reported_usd'], .4)
            self.assertEqual(state['budget_estimated_usd'], .1)
            self.assertTrue(state['budget_cost_complete'])
            self.assertEqual(state['assignments']['w']['status'], 'failed')


class TimeoutTests(unittest.TestCase):
    def test_timeout_retains_stream_cost_and_session(self):
        import io
        import json
        import subprocess
        from pathlib import Path
        from types import SimpleNamespace
        from smart_harness.backends import run_opencode
        event = {"type": "step_finish", "sessionID": "ses_timeout",
                 "part": {"reason": "tool-calls", "cost": .25}}
        proc = SimpleNamespace(pid=123, stdin=io.StringIO(),
                               stdout=io.StringIO(json.dumps(event) + "\n"),
                               wait=unittest.mock.Mock(side_effect=[subprocess.TimeoutExpired("opencode", 1), 0]),
                               kill=unittest.mock.Mock())
        updates = []
        with patch("smart_harness.backends.shutil.which", return_value="opencode"), patch("smart_harness.backends.subprocess.Popen", return_value=proc):
            result = run_opencode("task", model="same", agent="build", cwd=Path.cwd(), timeout=1,
                                  auto_approve=False, lifecycle=updates.append)
        self.assertEqual(result[1:5], (.25, False, "ses_timeout", 124))
        self.assertTrue(any(u.get("session_id") == "ses_timeout" for u in updates))
        proc.kill.assert_called_once()
