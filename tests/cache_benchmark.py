#!/usr/bin/env python3
"""Local checks for live benchmark completion, grading and accounting."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess

import check
import cache_probe as probe
import cache_workflow as workflow
from cache_report import summarize


class BenchmarkChecks(unittest.TestCase):
    def test_prefix_inspection_is_offline_and_works_without_compaction_or_credentials(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            journal = root / "journal.jsonl"
            item = dict(role="user", content="Question")
            events = [dict(type="item", data=item),
                dict(type="request", data=dict(provider="fixture", request=dict(model="one"), selection=[0])),
                dict(type="request", data=dict(provider="fixture", request=dict(model="two"), selection=[0, dict(role="user", content="Next")]))]
            journal.write_text("".join(json.dumps(e) + "\n" for e in events))
            subprocess.run(["python3", str(Path(probe.__file__)), str(journal), str(root/"inspection"),
                "--inspect", "--credentials", str(root/"missing-credentials.json")], check=True, capture_output=True, timeout=5)
            result = json.loads((root/"inspection/prefixes.json").read_text())
            self.assertEqual(result[0]["changed_fields"], ["model"])
            self.assertEqual(result[0]["shared_items"], 1)
            self.assertTrue(result[0]["history_appended"])

    def test_summary_controls_are_independent_of_the_recorded_compaction_strategy(self):
        ordinary = dict(instructions="CODING", tools=[dict(type="function", name="read")])
        compact = dict(instructions=probe.LEGACY_SUMMARY, input=[dict(role="user", content="History")], tool_choice="none")
        shared = probe.summary_variant(ordinary, compact, True)
        self.assertEqual(shared["instructions"], ordinary["instructions"])
        self.assertEqual(shared["tools"], ordinary["tools"])
        self.assertEqual(probe.summary_variant(ordinary, shared, False), compact)
        self.assertNotIn("tools", compact)

    def test_streamed_reply_and_mixed_tool_cycle_are_not_final(self):
        item = dict(role="assistant", content=[dict(type="output_text", text="STAGE_DONE")])
        self.assertFalse(workflow.final_response([dict(type="item", data=item)], "STAGE_DONE"))
        events = [dict(type="cycle", data={}), dict(type="item", data=item)]
        self.assertTrue(workflow.final_response(events, "STAGE_DONE"))
        events.append(dict(type="item", data=dict(type="function_call")))
        self.assertFalse(workflow.final_response(events, "STAGE_DONE"))

    def test_partial_journal_lines_are_not_committed(self):
        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            session = state / "harn/sessions/session"
            session.mkdir(parents=True)
            (session / "journal.jsonl").write_text('{"type":"item","data":{}}\n{"type":')
            self.assertEqual(workflow.records(state), [dict(type="item", data={})])

    def test_independent_grader_detects_bug_despite_passing_project_tests(self):
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            for file, text in workflow.FILES.items():
                path = project / file
                path.parent.mkdir(exist_ok=True)
                path.write_text(text)
            result = workflow.evaluate(project, 1)
            self.assertEqual(result["tests_exit"], 0)
            self.assertNotEqual(result["oracle_exit"], 0)
            self.assertFalse(result["passed"])
            # A UTC-aware implementation must also accept documented naive input.
            source = workflow.FILES["ledger.py"].replace("import datetime", "import datetime, timezone")
            source = source.replace('day = datetime.fromisoformat(row["timestamp"]).date().isoformat()',
                'dt = datetime.fromisoformat(row["timestamp"])\n'
                '        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt\n'
                '        day = dt.astimezone(timezone.utc).date().isoformat()')
            (project / "ledger.py").write_text(source)
            self.assertTrue(workflow.evaluate(project, 1)["passed"])

    def test_wire_reconstruction_preserves_inline_items_and_old_history(self):
        item = dict(role="user", content="Original question")
        inline = dict(role="developer", content="Pinned summary")
        events = [dict(type="item", data=item), dict(type="compact", data=dict(cut=1)),
                  dict(type="request", data=dict(provider="chatgpt", request=dict(model="model"), selection=[inline, 0]))]
        with tempfile.TemporaryDirectory() as name:
            journal = Path(name) / "journal.jsonl"
            journal.write_text("".join(json.dumps(e) + "\n" for e in events))
            self.assertEqual(probe.wire_requests(journal), [dict(model="model", input=[inline, item])])

    def test_policy_counterfactual_replaces_only_policy_snapshots(self):
        wire = dict(instructions="Stable rules", input=[
            dict(role="developer", content=probe.POLICY_PREFIX + " OLD"),
            dict(role="user", content="Keep this"),
            dict(role="developer", content=probe.POLICY_PREFIX + " NEW")])
        original = copy.deepcopy(wire)
        result = probe.rewrite_policy(wire)
        self.assertEqual(wire, original)
        self.assertEqual(result["input"], [wire["input"][1]])
        self.assertIn("NEW", result["instructions"])
        self.assertNotIn("OLD", result["instructions"])

    def test_unknown_cache_usage_is_not_zero_uncached_cost(self):
        group = dict(attempts=2, completed=1, input_tokens=100, cached_tokens=80,
                     cache_input_tokens=100, cache_measured=1)
        result = workflow.usage_totals([group])
        self.assertIsNone(result["input_not_reported_reused"])
        self.assertEqual(result["cache_read_percent"], 80)

    def test_failed_task_cost_is_included_in_success_denominator(self):
        run = dict(mode="persistent", workflow_passed=False, successful_tasks=1,
                   turns=[dict(wall_ms=1000, passed=True), dict(wall_ms=3000, passed=False)],
                   workflow_wall_ms=6000, input_not_reported_reused=200, input_tokens=1000,
                   cached_tokens=800, attempts=4)
        result = workflow.aggregate([run])[0]
        self.assertEqual(result["wall_ms_per_successful_task"], 6000)
        self.assertEqual(result["uncached_per_successful_task"], 200)
        self.assertEqual(result["attempted_tasks"], 2)
        self.assertEqual(result["successful_workflows"], 0)

    def test_replay_accepts_committed_items_when_completed_output_is_empty(self):
        server = check.Fixture()
        server.mode = "echo"
        server.usage = dict(input_tokens=1000, output_tokens=2,
                            input_tokens_details=dict(cached_tokens=800))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as name:
                credentials = Path(name) / "credentials.json"
                credentials.write_text(json.dumps(dict(access_token="FIXTURE_SECRET")))
                result = probe.request(dict(model="fixture", input=[dict(role="user", content="REPLAY_READY")]),
                    credentials, f"http://127.0.0.1:{server.server_port}/v1/responses", 5)
                self.assertEqual(result["outcome"], "completed")
                self.assertIn("REPLAY_READY", result["reply"])
                self.assertEqual(result["usage"]["input_tokens_details"]["cached_tokens"], 800)
                self.assertNotIn("FIXTURE_SECRET", json.dumps(result))
                self.assertIsNotNone(result["first_text_ms"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_report_retains_output_volume_and_marks_missing_output_usage(self):
        events = [dict(type="request_attempt", data=dict(provider="fixture", model="model", kind="inference",
                      outcome="completed", usage=dict(input_tokens=100, output_tokens=20))),
                  dict(type="request_attempt", data=dict(provider="fixture", model="model", kind="inference",
                      outcome="transport_error"))]
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "journal.jsonl"
            path.write_text("".join(json.dumps(e) + "\n" for e in events))
            result = summarize([path])[0]
            self.assertEqual(result["attempts"], 2)
            self.assertEqual(result["output_tokens"], 20)
            self.assertEqual(result["output_measured"], 1)


if __name__ == "__main__":
    unittest.main()
