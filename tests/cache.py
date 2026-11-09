#!/usr/bin/env python3
"""Cache integration through real HTTP requests, journals and session resume."""
import json
import unittest

import check
from cache_report import summarize


class CacheChecks(unittest.TestCase):
    setUp = check.Checks.setUp
    tearDown = check.Checks.tearDown
    run_batch = check.Checks.run_batch
    session = check.Checks.session
    records = check.Checks.records
    view_context = check.Checks.view_context

    def events(self, kind):
        return [r["data"] for r in self.records() if r["type"] == kind]

    def test_live_policy_changes_preserve_the_sent_prefix_and_replace_the_current_snapshot(self):
        self.server.mode = "echo"
        policy = self.project / "AGENTS.md"
        policy.write_text("FIRST_POLICY")
        self.run_batch("FIRST")
        first = self.server.requests[-1]
        policy.write_text("REVISED_POLICY")
        self.run_batch("SECOND", "--resume")
        second = self.server.requests[-1]
        self.assertEqual(first["instructions"], second["instructions"])
        self.assertEqual(second["input"][:len(first["input"])], first["input"])
        self.assertNotIn("FIRST_POLICY", check.policy_text(second))
        self.assertIn("REVISED_POLICY", check.policy_text(second))
        policy.unlink()
        self.run_batch("THIRD", "--resume")
        self.assertNotIn("REVISED_POLICY", check.policy_text(self.server.requests[-1]))

    def test_catalog_changes_are_appended_and_deleted_skills_leave_the_current_catalog(self):
        self.server.mode = "echo"
        self.run_batch("FIRST")
        first = self.server.requests[-1]
        skill = check.Checks.write_skill(self)
        self.run_batch("SECOND", "--resume")
        second = self.server.requests[-1]
        self.assertEqual(second["input"][:len(first["input"])], first["input"])
        self.assertIn("fixture-skill", check.policy_text(second))
        skill.unlink()
        self.run_batch("THIRD", "--resume")
        self.assertNotIn("fixture-skill", check.policy_text(self.server.requests[-1]))

    def test_usage_metadata_and_latency_are_recorded_once_and_restored(self):
        self.server.mode = "echo"
        self.server.usage = dict(input_tokens=1000, output_tokens=10,
                                input_tokens_details=dict(cached_tokens=800, cache_write_tokens=100),
                                attribution=dict(items=dict(duplicate=dict(input_tokens=1000, cached_tokens=800))))
        self.server.response_metadata = dict(id="resp_fixture", model="served-model", provider="upstream",
                                            service_tier="standard", prompt_cache_diagnostics=dict(type="cache_hit"))
        self.server.response_headers = {"X-Request-ID": "request_fixture", "Set-Cookie": "PRIVATE_HEADER"}
        self.run_batch("FIRST")
        attempt = self.events("request_attempt")[0]
        self.assertEqual(attempt["response_id"], "resp_fixture")
        self.assertEqual(attempt["response_model"], "served-model")
        self.assertEqual(attempt["upstream_provider"], "upstream")
        self.assertEqual(attempt["service_tier"], "standard")
        self.assertEqual(attempt["request_id"], "request_fixture")
        self.assertNotIn("PRIVATE_HEADER", (self.session() / "journal.jsonl").read_text())
        self.assertEqual(attempt["usage"], {k: v for k, v in self.server.usage.items() if k != "attribution"})
        self.assertEqual(self.events("usage")[0]["usage"], self.server.usage)
        self.assertIsNone(attempt["timing"]["first_tool_ms"])
        self.assertGreaterEqual(attempt["timing"]["first_text_ms"], 0)
        self.assertGreaterEqual(attempt["timing"]["duration_ms"], attempt["timing"]["first_text_ms"])
        view = self.view_context()
        self.assertIn("80.0% (800/1000 input tokens)", view)
        self.assertIn("Cache writes: 100 tokens", view)
        self.assertIn("1/1 attempts measured", view)
        self.assertIn("Cache diagnosis: cache_hit", view)
        self.run_batch("SECOND", "--resume")
        self.assertIn("2/2 attempts measured", self.view_context())
        report = summarize([self.session() / "journal.jsonl"])[0]
        self.assertEqual(report["attempts"], 2)
        self.assertEqual(report["input_tokens"], 2000)
        self.assertEqual(report["cached_tokens"], 1600)

    def test_missing_usage_is_unknown_and_retries_are_separate_attempts(self):
        self.server.mode = "retry"
        self.server.usage = dict(input_tokens=100)
        self.run_batch("RETRY")
        attempts = self.events("request_attempt")
        self.assertEqual(len(attempts), 2)
        self.assertEqual([a["attempt"] for a in attempts], [1, 2])
        self.assertNotIn("usage", attempts[0])
        view = self.view_context()
        self.assertIn("Cache last request: unknown", view)
        self.assertIn("Cache writes: unknown", view)
        self.assertIn("unknown (2 attempts)", view)

    def test_incomplete_stream_records_attempt_without_committing_or_executing_tools(self):
        self.server.mode = "partial"
        self.run_batch("PARTIAL", expected=1)
        self.assertFalse((self.project / "forbidden").exists())
        self.assertTrue(self.events("request_attempt"))
        self.assertFalse(self.events("usage"))
        self.assertTrue(all(a["outcome"] != "completed" for a in self.events("request_attempt")))

    def test_catalog_advertisements_do_not_enable_experimental_wire_fields(self):
        self.server.model_metadata = dict(supports_reasoning_effort_updates=True,
            prompt_cache=dict(key=True, ttl="30m", static_ttl="12h", diagnostics=True))
        self.server.mode = "echo"
        self.run_batch("FIRST", "--effort", "low")
        self.run_batch("SECOND", "--resume", "--effort", "high")
        self.run_batch("THIRD", "--resume", "--effort", "default")
        first, second, third = self.server.requests
        self.assertEqual(first["reasoning"], dict(effort="low"))
        self.assertEqual(second["reasoning"], dict(effort="high"))
        self.assertNotIn("reasoning", third)
        self.assertEqual(second["input"][:len(first["input"])], first["input"])
        for request in self.server.requests:
            for field in ("prompt_cache_key", "prompt_cache_options"):
                self.assertNotIn(field, request)
            self.assertNotIn("prompt_cache_breakpoint", json.dumps(request))
            self.assertFalse(any(i.get("type") == "configuration_update" for i in request["input"]))
        self.assertFalse(self.events("cache_request"))
        self.assertFalse(self.events("reasoning_context"))
        self.assertEqual(json.loads((self.session() / "request.json").read_text()), third)

    def test_compaction_uses_summary_instructions_and_pins_current_policy(self):
        self.server.mode = "echo"
        (self.project / "AGENTS.md").write_text("CURRENT_POLICY")
        p = check.Process(self, "--effort", "low")
        try:
            for i in range(4):
                if i == 1:
                    p.send("/effort high")
                    p.wait("Effort: high")
                p.send("HISTORY " + str(i))
                p.wait("OK HISTORY " + str(i))
            p.send("/compact")
            p.wait("Full journal preserved")
            p.send("CONTINUE")
            p.wait("OK CONTINUE")
        finally:
            p.finish()
        first = self.server.requests[0]
        compact = next(r for r in self.server.requests if check.is_compaction(r))
        self.assertNotIn("tools", compact)
        self.assertIn("Summarize the conversation", compact["instructions"])
        self.assertNotEqual(compact["instructions"], first["instructions"])
        self.assertEqual(compact["reasoning"], dict(effort="high"))
        self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="high"))
        self.assertEqual(self.server.requests[-1]["instructions"], first["instructions"])
        self.assertIn("CURRENT_POLICY", check.policy_text(self.server.requests[-1]))
        self.assertEqual(self.events("compaction_usage")[0]["cache_accounted"], True)

    def test_legacy_experimental_records_do_not_affect_resume(self):
        self.server.mode = "echo"
        self.run_batch("FIRST", "--effort", "low")
        old_events = [dict(type="cache_request", data=dict(provider="fixture", fields={})),
            dict(type="reasoning_context", data=dict(base="low", selected="high")),
            dict(type="item", data=dict(type="configuration_update", reasoning=dict(effort="high")))]
        with (self.session() / "journal.jsonl").open("a") as journal:
            for event in old_events:
                journal.write(json.dumps(dict(time=0, **event)) + "\n")
        self.run_batch("SECOND", "--resume", "--effort", "high")
        request = self.server.requests[-1]
        self.assertEqual(request["reasoning"], dict(effort="high"))
        self.assertFalse(any(i.get("type") == "configuration_update" for i in request["input"]))
        self.assertNotIn("prompt_cache_key", request)
        self.assertEqual(len(self.events("request_attempt")), 2)
        self.assertIn("unknown (2 attempts)", self.view_context())


if __name__ == "__main__":
    unittest.main(verbosity=2)
