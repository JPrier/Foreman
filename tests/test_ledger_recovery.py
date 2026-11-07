import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_harness.backends import DirectorResult, WorkerResult, run_opencode
from smart_harness.config import DEFAULTS
from smart_harness.engine import Engine, subscription_equivalent
from smart_harness.ledger import apply_updates, initialize, outstanding, git_snapshot
from smart_harness.recovery import reconcile
from smart_harness.storage import new_task, save, load, save_worker_output, execution_lock


class Director:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.prompts = []
    def call(self, system, prompt):
        self.prompts.append(prompt)
        return DirectorResult(json.dumps(next(self.decisions)), None, {})


class Worker:
    def call(self, prompt, read_only):
        return WorkerResult("summary", "Full report: completed check", True, None, False)


class LedgerRecoveryTests(unittest.TestCase):
    def test_completed_and_reconciled_assignment_reviews_are_repeatable(self):
        for status in ("completed", "reconciled"):
            with self.subTest(status=status):
                state = new_task("task", "mock")
                state["assignments"] = {"work": {"status": status}}
                state["events"].append({"kind": "worker", "id": "review", "ok": True})
                director = Director([
                    {"action": "ask_user", "question": "next", "resolved_assignments": {"work": "review"}},
                    {"action": "ask_user", "question": "next", "resolved_assignments": {"work": "review"}},
                ])
                engine = Engine(self.cfg, director=director, worker=Worker(), log=lambda _: None)
                for _ in range(2):
                    out = engine.run(state)
                    self.assertEqual(out["status"], "needs_input")
                    self.assertEqual(out["assignments"]["work"], {"status": status, "review_worker": "review"})

    def test_assignment_review_still_requires_separate_successful_worker(self):
        for reviewer, ok in (("work", True), ("review", False), ("missing", True)):
            with self.subTest(reviewer=reviewer, ok=ok):
                state = new_task("task", "mock")
                state["assignments"] = {"work": {"status": "completed"}}
                state["events"].append({"kind": "worker", "id": "work" if reviewer == "work" else "review", "ok": ok})
                director = Director([{"action": "ask_user", "question": "next", "resolved_assignments": {"work": reviewer}}])
                out = Engine(self.cfg, director=director, worker=Worker(), log=lambda _: None).run(state)
                self.assertEqual(out["status"], "error")
                self.assertEqual(out["assignments"]["work"], {"status": "completed"})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"SMART_HARNESS_HOME": self.tmp.name})
        self.env.start()
        self.cfg = copy.deepcopy(DEFAULTS)
    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_delivery_and_findings_gate_completion(self):
        state = new_task("deliver", "mock")
        decisions = [
            {"action":"dispatch", "tasks":[{"id":"work", "prompt":"work"}],
             "ledger_updates":[{"id":"deliver", "kind":"delivery", "text":"push and verify", "status":"open"},
                               {"id":"bug", "kind":"finding", "text":"unresolved defect", "status":"confirmed"}]},
            {"action":"finish", "answer":"done", "ledger_updates":[{"id":"objective", "status":"satisfied", "evidence":[{"worker_id":"work"}]}]},
        ]
        out = Engine(self.cfg, director=Director(decisions), worker=Worker(), log=lambda _: None).run(state)
        self.assertEqual(out["status"], "partial")
        self.assertIn("push and verify", out["answer"])
        self.assertIn("unresolved defect", out["answer"])
        self.assertIsNone(next(e for e in out["events"] if e.get("kind") == "worker")["reported_cost_usd"])
        self.assertFalse(out["worker_cost_complete"])

    def test_evidence_is_stale_when_artifact_changes(self):
        state = new_task("task", "mock")
        artifact = Path(self.tmp.name) / "file.txt"
        artifact.write_text("first")
        sha = hashlib.sha256(b"first").hexdigest()
        report = save_worker_output(state, "check", f"{artifact} {sha}")
        state["events"].append({"kind":"worker", "id":"check", "ok":True, "output_path":report})
        initialize(state)
        apply_updates(state, [{"id":"objective", "status":"satisfied", "evidence":[{"worker_id":"check", "artifacts":{str(artifact):sha}}]}])
        self.assertEqual(outstanding(state), [])
        artifact.write_text("second")
        self.assertIn("stale", outstanding(state)[0])

    def test_delivery_declaration_requires_delivery_entry(self):
        state = new_task("deliver", "mock")
        decisions = [
            {"action":"dispatch", "tasks":[{"id":"work", "prompt":"work"}]},
            {"action":"finish", "answer":"done", "requirements_reviewed":True, "delivery_required":True,
             "ledger_updates":[{"id":"objective", "status":"satisfied", "evidence":[{"worker_id":"work"}]}]},
        ]
        out = Engine(self.cfg, director=Director(decisions), worker=Worker(), log=lambda _: None).run(state)
        self.assertEqual(out["status"], "partial")
        self.assertIn("delivery has no ledger entry", out["answer"])

    def test_unscoped_evidence_invalidated_by_followup(self):
        state = new_task("task", "mock")
        path = save_worker_output(state, "check", "original check")
        state["events"].append({"kind":"worker", "id":"check", "ok":True, "output_path":path})
        initialize(state)
        apply_updates(state, [{"id":"objective", "status":"satisfied", "evidence":[{"worker_id":"check"}]}])
        self.assertFalse(outstanding(state))
        state["events"].append({"kind":"followup", "message":"new requirement"})
        self.assertIn("stale", outstanding(state)[0])

    def test_selective_retrieval_is_traced(self):
        state = new_task("task", "mock")
        decisions = [
            {"action":"dispatch", "tasks":[{"id":"check", "prompt":"check"}]},
            {"action":"finish", "answer":"later", "retrieve_reports":["check"]},
            {"action":"finish", "answer":"done", "requirements_reviewed":True, "delivery_required":False, "ledger_updates":[{"id":"objective", "status":"satisfied", "evidence":[{"worker_id":"check"}]}]},
        ]
        director = Director(decisions)
        out = Engine(self.cfg, director=director, worker=Worker(), log=lambda _: None).run(state)
        self.assertEqual(out["status"], "done")
        self.assertNotIn("Full report: completed check", director.prompts[1])
        self.assertIn("Full report: completed check", director.prompts[2])
        self.assertEqual(next(e for e in out["events"] if e.get("kind") == "retrieval")["worker_ids"], ["check"])

    def test_report_excerpt_selects_requested_range(self):
        state = new_task('task', 'mock')
        director = Director([
            {'action':'dispatch', 'tasks':[{'id':'check','prompt':'check'}]},
            {'action':'finish', 'answer':'later', 'retrieve_reports':[{'id':'check','start':0,'end':11}]},
            {'action':'finish', 'answer':'partial'},
        ])
        out = Engine(self.cfg, director=director, worker=Worker(), log=lambda _: None).run(state)
        self.assertEqual(out['status'], 'partial')
        self.assertIn('Full report', director.prompts[2])
        self.assertNotIn('completed check', director.prompts[2])

    def test_interruption_then_review_and_finish(self):
        state = new_task("deliver task", "mock")
        state.update(status="workers_running", coordinator_pid=99999999)
        state["assignments"] = {"lost":{"status":"running", "prompt":"modify"}}
        save(state)
        state = load(state["id"])
        state["followups"].extend([f"correction {i}" for i in range(5)])
        decisions = [
            {"action":"dispatch", "tasks":[{"id":"inspect", "prompt":"Inspect prior side effects", "read_only":True}]},
            {"action":"dispatch", "tasks":[{"id":"deliver", "prompt":"Complete and verify delivery"}], "resolved_assignments":{"lost":"inspect"}},
            {"action":"finish", "answer":"delivered", "requirements_reviewed":True, "delivery_required":True, "ledger_updates":[{"id":ident, "status":"satisfied", "evidence":[{"worker_id":"deliver"}]} for ident in ["objective"] + [f"followup_{i}" for i in range(1,6)]] + [{"id":"delivery", "kind":"delivery", "text":"deliver and verify", "status":"satisfied", "evidence":[{"worker_id":"deliver"}]}]},
        ]
        director = Director(decisions)
        with patch("smart_harness.recovery.process_exists", return_value=False):
            out = Engine(self.cfg, director=director, worker=Worker(), log=lambda _: None).run(state)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["assignments"]["lost"]["status"], "reconciled")
        self.assertFalse(out["worker_cost_complete"])
        self.assertTrue(all("correction 0" in p for p in director.prompts))

    def test_live_orphan_blocks_resumption(self):
        state = new_task("task", "mock")
        state.update(status="workers_running", coordinator_pid=12345)
        state["assignments"] = {"a":{"status":"running", "pid":22222}}
        with patch("smart_harness.recovery.process_exists", side_effect=lambda pid: pid == 22222):
            self.assertEqual(reconcile(state), "worker_running")
        self.assertEqual(state["status"], "recovery_blocked")

    def test_task_lock_rejects_concurrent_coordinator(self):
        state = new_task('task', 'mock')
        with execution_lock(state['id']):
            with self.assertRaises(RuntimeError):
                with execution_lock(state['id']):
                    pass

    def test_equivalent_estimate_requires_complete_categories_and_rates(self):
        cfg = self.cfg['director']
        for key, rate in [('input', 2), ('output', 4), ('cache_read', 1), ('cache_write', 3)]:
            cfg[f'equivalent_{key}_per_million'] = rate
        usage = {'token_events_complete': True, 'token_events': [{'input':100, 'output':20, 'cache':{'read':30, 'write':10}}]}
        self.assertAlmostEqual(subscription_equivalent(usage, cfg), .00034)
        del usage['token_events'][0]['cache']['write']
        self.assertIsNone(subscription_equivalent(usage, cfg))

    def test_comparison_does_not_treat_unknown_costs_as_zero(self):
        from tools.compare_runs import compare
        result = compare([
            {'task_id':'a', 'method':'foreman', 'outcome_correct':True, 'reported_provider_cost_usd':None},
            {'task_id':'a', 'method':'direct', 'outcome_correct':False, 'reported_provider_cost_usd':.2},
            {'task_id':'b', 'method':'foreman'},
        ])
        self.assertEqual(result['unpaired_tasks'], ['b'])
        self.assertIsNone(result['totals']['foreman']['reported_provider_cost_usd']['known_total'])
        self.assertEqual(result['totals']['foreman']['reported_provider_cost_usd']['unknown_count'], 1)
        self.assertEqual(result['totals']['direct']['reported_provider_cost_usd']['known_total'], .2)

    def test_git_fingerprint_detects_dirty_and_untracked_changes(self):
        repo = Path(self.tmp.name) / 'repo'
        repo.mkdir()
        def git(*args):
            subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
        git('init')
        git('config', 'user.email', 'test@example.com')
        git('config', 'user.name', 'test')
        (repo / 'tracked').write_text('a')
        git('add', 'tracked')
        git('commit', '-m', 'test')
        baseline = git_snapshot(str(repo))
        self.assertIsNotNone(baseline)
        (repo / 'tracked').write_text('b')
        changed = git_snapshot(str(repo))
        self.assertNotEqual(baseline, changed)
        (repo / 'new').write_text('new')
        self.assertNotEqual(changed, git_snapshot(str(repo)))

    def test_worker_budget_stops_sequential_batch_between_calls(self):
        class Costly:
            def call(self, prompt, read_only):
                return WorkerResult('ok', 'ok', True, 0.1, True)
        self.cfg['limits']['budget_usd'] = 0.1
        self.cfg['mode'] = 'live'
        state = new_task('task', 'live')
        director = Director([{'action':'dispatch','tasks':[{'id':'one','prompt':'one'}, {'id':'two','prompt':'two'}]}])
        director.call = lambda system, prompt: DirectorResult(json.dumps({'action':'dispatch','tasks':[{'id':'one','prompt':'one'}, {'id':'two','prompt':'two'}]}), 0.0, {})
        out = Engine(self.cfg, director=director, worker=Costly(), log=lambda _: None).run(state)
        self.assertEqual(out['status'], 'budget_exhausted')
        self.assertEqual(out['assignments']['two']['status'], 'blocked')
        self.assertEqual(len([e for e in out['events'] if e.get('kind') == 'worker']), 1)

    def test_failed_retry_escalates_without_count_limit(self):
        self.cfg['worker']['medium_model'] = 'provider/stronger'
        class Fail:
            def call(self, prompt, read_only):
                return WorkerResult('failed', 'failed', False, None, False)
        state = new_task('task', 'mock')
        director = Director([
            {'action':'dispatch','tasks':[{'id':'one','prompt':'do'}]},
            {'action':'dispatch','tasks':[{'id':'two','prompt':'retry','retry_of':'one'}]},
            {'action':'dispatch','tasks':[{'id':'three','prompt':'retry','retry_of':'two'}]},
            {'action':'finish','answer':'partial'},
        ])
        out = Engine(self.cfg, director=director, worker=Fail(), log=lambda _: None).run(state)
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(next(e for e in out['events'] if e.get('id') == 'two')['model'], 'provider/stronger')
        self.assertEqual(out['worker_count'], 3)

    @unittest.skipIf(os.name == 'nt', 'POSIX executable fixture')
    def test_live_process_streams_identity_and_usage(self):
        executable = Path(self.tmp.name) / 'opencode'
        executable.write_text('#!/usr/bin/env python3\nimport json\nprint(json.dumps({"type":"text","sessionID":"ses_live","part":{"messageID":"m","text":"done"}}), flush=True)\nprint(json.dumps({"type":"step_finish","part":{"reason":"stop","cost":0.02,"tokens":{"input":12,"cache":{"read":4}}}}), flush=True)\n')
        executable.chmod(0o755)
        updates = []
        with patch('smart_harness.backends.shutil.which', return_value=str(executable)):
            result = run_opencode('task', model='x', agent='a', cwd=Path(self.tmp.name), timeout=5, auto_approve=False, lifecycle=updates.append)
        self.assertEqual(result[:5], ('done', .02, True, 'ses_live', 0))
        self.assertTrue(any(x.get('pid') for x in updates))
        self.assertTrue(any(x.get('session_id') == 'ses_live' for x in updates))
        self.assertTrue(any(x.get('event', {}).get('type') == 'step_finish' for x in updates))


if __name__ == "__main__":
    unittest.main()
