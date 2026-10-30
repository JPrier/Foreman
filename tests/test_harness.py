import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_harness.backends import DirectorResult, WorkerResult, run_opencode
from smart_harness.cli import main
from smart_harness.config import DEFAULTS, load_config
from smart_harness.contracts import DecisionError, director_user_message, parse_decision
from smart_harness.engine import Engine, estimated_reservation
from smart_harness.storage import load, new_task


class SequenceDirector:
    def __init__(self, responses, cost=0.02):
        self.responses = iter(responses)
        self.cost = cost
        self.seen = []

    def call(self, system, prompt):
        self.seen.append(prompt)
        return DirectorResult(json.dumps(next(self.responses)), self.cost, {"test": True})


class CollectWorker:
    def __init__(self, succeed=True):
        self.calls = []
        self.succeed = succeed

    def call(self, prompt, read_only):
        self.calls.append((prompt, read_only))
        return WorkerResult("Verified: sample result", "Full worker result and evidence", self.succeed, 0.001, True)


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"SMART_HARNESS_HOME": self.temp.name})
        self.env.start()
        self.cfg = copy.deepcopy(DEFAULTS)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_shorthand_mock_from_non_git_directory(self):
        self.assertEqual(main(["A short task with no code or repository"]), 0)
        state = load()
        self.assertEqual(state["status"], "done")
        self.assertEqual(state["worker_count"], 2)
        self.assertIn("MOCK", state["answer"])
        self.assertTrue(Path(self.temp.name, "tasks", state["id"], "workers", "execute.txt").is_file())

    def test_director_worker_loop(self):
        decisions = [
            {"action": "dispatch", "parallel": False, "tasks": [
                {"id": "one", "prompt": "Investigate", "read_only": True}], "memory": "Remember evidence"},
            {"action": "dispatch", "parallel": False, "tasks": [
                {"id": "two", "prompt": "Finish task", "read_only": False}], "memory": ""},
            {"action": "finish", "answer": "Done, validated", "memory": ""},
        ]
        director = SequenceDirector(decisions)
        worker = CollectWorker()
        state = new_task("Arbitrary task unrelated to code", "mock")
        result = Engine(self.cfg, director=director, worker=worker, log=lambda _: None).run(state)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["answer"], "Done, validated")
        self.assertEqual([x[1] for x in worker.calls], [True, False])
        self.assertIn("Verified: sample result", director.seen[-1])
        self.assertIn("original_objective", director.seen[-1])
        self.assertEqual(result["worker_cost_reported_usd"], 0.002)

    def test_invalid_director_schema_rejected(self):
        with self.assertRaises(DecisionError):
            parse_decision('{"action":"dispatch","tasks":[]}', 3)
        with self.assertRaises(DecisionError):
            parse_decision('{"action":"dispatch","tasks":[{"id":"../bad","prompt":"x"}]}', 3)
        with self.assertRaises(DecisionError):
            parse_decision('{"action":"finish","answer":""}', 3)

    def test_finishing_without_evidence_is_rejected(self):
        state = new_task("Something", "mock")
        director = SequenceDirector([{"action": "finish", "answer": "Fake completion"}])
        out = Engine(self.cfg, director=director, worker=CollectWorker(), log=lambda _: None).run(state)
        self.assertEqual(out["status"], "error")
        self.assertIn("before at least one worker", out["answer"])

    def test_worker_failure_returned_to_director(self):
        decisions = [
            {"action": "dispatch", "tasks": [{"id": "fail1", "prompt": "Try something"}]},
            {"action": "finish", "answer": "Worker failed; cannot verify completion."},
        ]
        out = Engine(self.cfg, director=SequenceDirector(decisions), worker=CollectWorker(False), log=lambda _: None).run(new_task("task", "mock"))
        self.assertEqual(out["status"], "done")
        self.assertFalse(next(e for e in out["events"] if e.get("kind") == "worker")["ok"])

    def test_preflight_blocks_before_paid_call(self):
        self.cfg["mode"] = "live"
        self.cfg["limits"]["premium_budget_usd"] = 0.001
        director = SequenceDirector([])
        out = Engine(self.cfg, director=director, worker=CollectWorker(), log=lambda _: None).run(new_task("task", "live"))
        self.assertEqual(out["status"], "budget_exhausted")
        self.assertEqual(len(director.seen), 0)

    def test_director_budget_usage_is_reported(self):
        self.cfg["mode"] = "live"
        self.cfg["limits"]["premium_budget_usd"] = 1.0
        decisions = [
            {"action": "dispatch", "tasks": [{"id": "one", "prompt": "Do"}]},
            {"action": "finish", "answer": "Done"},
        ]
        out = Engine(self.cfg, director=SequenceDirector(decisions, cost=.05), worker=CollectWorker(), log=lambda _: None).run(new_task("task", "live"))
        self.assertEqual(out["premium_spent_usd"], .1)
        self.assertEqual(out["premium_reserved_usd"], 0)

    def test_max_worker_rounds_allows_final_review(self):
        self.cfg["limits"]["max_rounds"] = 1
        decisions = [
            {"action": "dispatch", "tasks": [{"id": "one", "prompt": "Do"}]},
            {"action": "finish", "answer": "Verified"},
        ]
        out = Engine(self.cfg, director=SequenceDirector(decisions), worker=CollectWorker(), log=lambda _: None).run(new_task("task", "mock"))
        self.assertEqual(out["status"], "done")

    def test_resume_paused_task_and_no_repo(self):
        first = SequenceDirector([{"action": "ask_user", "question": "Which topic?", "memory": "ask first"}])
        state = new_task("Research topic", "mock")
        result = Engine(self.cfg, director=first, log=lambda _: None).run(state)
        self.assertEqual(result["status"], "needs_input")
        result["followups"].append("The topic is bees")
        second = SequenceDirector([
            {"action": "dispatch", "tasks": [{"id": "research", "prompt": "Research bees", "read_only": True}]},
            {"action": "finish", "answer": "Research summarized"},
        ])
        out = Engine(self.cfg, director=second, worker=CollectWorker(), log=lambda _: None).run(result)
        self.assertEqual(out["status"], "done")
        self.assertIn("bees", second.seen[0])

    def test_mock_resume_keeps_dispatch_ids_unique(self):
        from smart_harness.backends import MockDirector, MockWorker
        state = new_task("a task", "mock")
        state["director_calls"] = 2
        state["round"] = 2
        state["worker_count"] = 2
        state["events"] = [{"kind": "worker", "id": "investigate", "summary": "Mock"}]
        out = Engine(self.cfg, director=MockDirector(), worker=MockWorker(), log=lambda _: None).run(state)
        self.assertEqual(out["status"], "done")

    def test_context_compaction_keeps_objective(self):
        state = new_task("Find a poem", "mock")
        state["events"] = [{"kind": "worker", "id": f"w{i}", "summary": "a" * 2000} for i in range(20)]
        packet = director_user_message(state, 3500)
        self.assertIn("Find a poem", packet)
        self.assertLess(len(packet), 3600)

    def test_unknown_configuration_rejected(self):
        f = Path(self.temp.name) / "bad.json"
        f.write_text('{"force_paid":true}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(f)

    def test_cost_reservation_grows_with_prompt(self):
        a = estimated_reservation(self.cfg["director"], "sys", "short")
        b = estimated_reservation(self.cfg["director"], "sys", "long" * 10000)
        self.assertGreater(b, a)

    def test_opencode_json_event_extraction(self):
        from unittest.mock import Mock
        events = "\n".join([
            json.dumps({"type": "text", "sessionID": "ses_1", "part": {"messageID": "m1", "text": "first"}}),
            json.dumps({"type": "step_finish", "part": {"reason": "tool-calls", "cost": 0.01}}),
            json.dumps({"type": "text", "sessionID": "ses_1", "part": {"messageID": "m2", "text": "FINAL"}}),
            json.dumps({"type": "step_finish", "part": {"reason": "stop", "cost": 0.02}}),
        ])
        with patch("smart_harness.backends.shutil.which", return_value="/usr/bin/opencode"), patch("smart_harness.backends.subprocess.run", return_value=Mock(stdout=events, stderr="", returncode=0)):
            text, cost, complete, session, code, _ = run_opencode("test", model="a/b", agent="build", cwd=Path.home(), timeout=3, auto_approve=False)
        self.assertEqual(text, "FINAL")
        self.assertAlmostEqual(cost, 0.03)
        self.assertTrue(complete)
        self.assertEqual(session, "ses_1")


if __name__ == "__main__":
    unittest.main()
