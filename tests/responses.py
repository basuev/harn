#!/usr/bin/env python3
"""Both authorization modes and in-process connection selection in one binary."""
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import termios
import time
import unittest

import check

MODEL = "deepseek/deepseek-v4.1-flash-abliterated"


class ResponsesChecks(unittest.TestCase):
    run_batch = check.Checks.run_batch
    session = check.Checks.session
    records = check.Checks.records
    saved_requests = check.Checks.saved_requests
    view_context = check.Checks.view_context
    tearDown = check.Checks.tearDown
    test_effort_default_and_cli_are_sent_and_reported_separately = check.Checks.test_effort_default_and_cli_are_sent_and_reported_separately
    test_effort_is_used_by_compaction_and_followup = check.Checks.test_effort_is_used_by_compaction_and_followup
    test_unsupported_effort_is_not_silently_retried_as_default = check.Checks.test_unsupported_effort_is_not_silently_retried_as_default

    def setUp(self):
        check.Checks.setUp(self)
        self.startup_args = ("--provider", "allmodels")
        self.server.standard_catalog = True
        self.server.model_id = MODEL
        self.server.mode = "echo"
        self.env["ALLMODELS_API_KEY"] = "KEY_NEVER_SAVED"
        self.env["HARN_AUTH_ISSUER"] = "http://127.0.0.1:" + str(self.server.server_port)
        self.credentials = (self.config / "credentials.json").read_bytes()

    def command(self, *args, provider="allmodels"):
        selection = ["--provider", provider] if provider else []
        return subprocess.run([str(check.BINARY), "-C", str(self.project), *selection, *args],
                              env=self.env, input=b"", capture_output=True, timeout=20)

    def test_key_is_sent_but_never_saved_and_oauth_credentials_are_untouched(self):
        self.run_batch("HELLO")
        self.assertEqual(self.server.auth_headers, ["Bearer KEY_NEVER_SAVED"])
        self.assertEqual(self.server.discovery_calls, 0)
        self.assertEqual(self.server.token_calls, 0)
        self.assertEqual((self.config / "credentials.json").read_bytes(), self.credentials)
        for path in (self.path / "state").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"KEY_NEVER_SAVED", path.read_bytes(), str(path))

    def test_missing_and_empty_key_do_not_use_chatgpt_credentials(self):
        for value in (None, ""):
            if value is None:
                self.env.pop("ALLMODELS_API_KEY", None)
            else:
                self.env["ALLMODELS_API_KEY"] = value
            result = self.run_batch("NO KEY", expected=1)
            self.assertIn(b"Set ALLMODELS_API_KEY", result.stderr)
        self.assertEqual(self.server.model_fetches, 0)
        self.assertEqual(self.server.discovery_calls, 0)
        self.assertEqual(self.server.requests, [])

    def test_key_401_and_balance_402_do_not_refresh_or_retry(self):
        for status in (401, 402):
            self.server.response_status = status
            self.run_batch("REJECTED", expected=1)
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.server.token_calls, 0)
        self.assertEqual(self.server.discovery_calls, 0)
        self.assertEqual((self.config / "credentials.json").read_bytes(), self.credentials)

    def test_rate_limit_retries_are_bounded(self):
        self.server.response_status = 429
        self.run_batch("RATE LIMITED", expected=1)
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(self.server.token_calls, 0)

    def test_configured_endpoint_is_used_without_fixture_override(self):
        self.env.pop("HARN_API_BASE")
        self.env.pop("ALLMODELS_API_KEY")
        result = self.run_batch("NO OVERRIDE", expected=1)
        self.assertIn(b"Set ALLMODELS_API_KEY to the API key for https://api.allmodels.io/oai", result.stderr)
        self.assertEqual(self.server.model_fetches, 0)

    def test_model_list_uses_ids(self):
        self.server.extra_models = [dict(id="author/other-model", context_length=262144)]
        result = self.command("--models")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"author/other-model", result.stdout)
        self.assertIn(MODEL.encode(), result.stdout)

    def test_invalid_standard_catalog_blocks_inference(self):
        for entry in ({"slug": "wrong"}, {"id": "bad", "context_length": -1},
                      {"id": "bad", "context_length": "large"}):
            self.server.extra_models = [entry]
            result = self.run_batch("INVALID", expected=1)
            self.assertIn(b"Could not load models", result.stderr)
        self.assertEqual(self.server.requests, [])

    def test_catalog_reuse_and_removal_are_authoritative(self):
        self.server.window = 262144
        self.run_batch("FIRST")
        self.server.model_status = 503
        self.run_batch("SAVED", "--resume", self.session().name)
        self.assertIn("262144 saved", self.view_context())
        self.server.model_status = 200
        self.server.model_present = False
        self.run_batch("REMOVED", "--resume", self.session().name, expected=1)
        self.assertEqual(len(self.server.requests), 2)

    def test_resume_restores_provider_in_the_same_binary(self):
        self.run_batch("FIRST")
        session = self.session()
        metadata = json.loads((session / "meta.json").read_text())
        self.assertEqual(metadata["provider"], "allmodels")
        self.assertNotIn("api_key_env", metadata)
        result = self.command("--resume", session.name, "-p", "SECOND", provider=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.auth_headers, ["Bearer KEY_NEVER_SAVED"] * 2)
        self.assertTrue(any(x.get("content") == "FIRST" for x in self.server.requests[-1]["input"]))
        self.assertEqual(self.server.discovery_calls, 0)

    def test_legacy_endpoint_metadata_maps_to_configured_connection(self):
        self.run_batch("FIRST")
        path = self.session() / "meta.json"
        metadata = json.loads(path.read_text())
        del metadata["provider"]
        metadata.update(api_base=self.env["HARN_API_BASE"], api_key_env="ALLMODELS_API_KEY")
        path.write_text(json.dumps(metadata))
        result = self.command("--resume", self.session().name, "-p", "SECOND", provider=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.auth_headers, ["Bearer KEY_NEVER_SAVED"] * 2)

    def test_removed_connection_is_not_silently_replaced(self):
        self.run_batch("FIRST")
        path = self.session() / "meta.json"
        metadata = json.loads(path.read_text())
        metadata["provider"] = "removed"
        path.write_text(json.dumps(metadata))
        result = self.command("--resume", self.session().name, "-p", "SECOND", provider=None)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"Provider removed is not configured", result.stderr)
        self.assertEqual(len(self.server.requests), 1)

    def test_login_checks_key_and_logout_does_not_revoke_chatgpt(self):
        self.assertEqual(self.command("--login").returncode, 0)
        result = self.command("--logout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"Unset it in the launching shell", result.stderr)
        self.assertEqual((self.config / "credentials.json").read_bytes(), self.credentials)
        self.assertEqual(self.server.discovery_calls, 0)

    def test_full_tool_cycle_journal_and_resume(self):
        self.server.mode = "basic"
        check.Checks.test_full_tool_cycle_journal_and_resume(self)
        tools = self.server.requests[0]["tools"]
        self.assertEqual([tool["type"] for tool in tools], ["function"] * 5)
        self.assertEqual([tool["name"] for tool in tools], ["read", "write", "edit", "exec", "jobs"])

    def test_completed_response_can_supply_output_without_item_done_events(self):
        self.server.mode = "basic"
        self.server.completed_only = True
        check.Checks.test_full_tool_cycle_journal_and_resume(self)

    test_context_view_preserves_separate_input_output_and_limit_source = check.Checks.test_context_view_preserves_separate_input_output_and_limit_source
    test_context_does_not_present_fallback_as_a_reported_window = check.Checks.test_context_does_not_present_fallback_as_a_reported_window
    test_retry_limit_and_no_incomplete_execution = check.Checks.test_retry_limit_and_no_incomplete_execution
    test_compaction_preserves_source_and_exact_request = check.Checks.test_compaction_preserves_source_and_exact_request


class SwitchingChecks(unittest.TestCase):
    session = check.Checks.session
    records = check.Checks.records
    saved_requests = check.Checks.saved_requests
    run_batch = check.Checks.run_batch
    tearDown = check.Checks.tearDown

    def setUp(self):
        check.Checks.setUp(self)
        self.server.mode = "echo"
        self.server.key_model_id = MODEL
        self.server.key_window = 262144
        self.env["ALLMODELS_API_KEY"] = "KEY_NEVER_SAVED"
        self.env["HARN_AUTH_ISSUER"] = "http://127.0.0.1:" + str(self.server.server_port)

    def test_one_process_switches_both_authorization_modes_and_resumes_last_selection(self):
        p = check.Process(self)
        try:
            p.send("FIRST")
            p.wait("OK FIRST")
            p.send("/provider allmodels")
            p.wait("Model: " + MODEL)
            p.send("SECOND")
            p.wait("OK SECOND")
            p.send("/provider chatgpt")
            p.wait("Provider: chatgpt")
            p.send("THIRD")
            p.wait("OK THIRD")
        finally:
            p.finish()
        self.assertEqual(self.server.auth_headers, ["Bearer NEVER_IN_JOURNAL", "Bearer KEY_NEVER_SAVED", "Bearer NEVER_IN_JOURNAL"])
        self.assertEqual([r["model"] for r in self.server.requests], ["gpt-6.1-sol", MODEL, "gpt-6.1-sol"])
        self.assertEqual([r["data"]["provider"] for r in self.records() if r["type"] == "usage"], ["chatgpt", "allmodels", "chatgpt"])
        users = [i["content"] for i in self.server.requests[-1]["input"] if i.get("role") == "user"]
        self.assertEqual(users, ["FIRST", "SECOND", "THIRD"])
        self.assertEqual(self.saved_requests(), self.server.requests)
        self.run_batch("AFTER RESTART", "--resume", self.session().name)
        self.assertEqual(self.server.auth_headers[-1], "Bearer NEVER_IN_JOURNAL")

    def test_resumes_a_provider_selected_inside_the_process(self):
        p = check.Process(self)
        try:
            p.send("/provider allmodels")
            p.wait("Model: " + MODEL)
            p.send("KEY SESSION")
            p.wait("OK KEY SESSION")
        finally:
            p.finish()
        self.run_batch("RESTART", "--resume", self.session().name)
        self.assertEqual(self.server.requests[-1]["model"], MODEL)
        self.assertEqual(self.server.auth_headers[-1], "Bearer KEY_NEVER_SAVED")

    def test_discovery_is_local_and_missing_or_unknown_provider_keeps_selection(self):
        self.env.pop("ALLMODELS_API_KEY")
        p = check.Process(self)
        try:
            p.send("/provider")
            p.wait("ALLMODELS_API_KEY missing")
            self.assertEqual(self.server.model_fetches, 0)
            p.send("/provider allmodels")
            p.wait("Set ALLMODELS_API_KEY before selecting allmodels")
            p.send("/provider unknown")
            p.wait("Provider unknown is not configured")
            p.send("UNCHANGED")
            p.wait("OK UNCHANGED")
        finally:
            p.finish()
        self.assertEqual(self.server.auth_headers, ["Bearer NEVER_IN_JOURNAL"])
        self.assertFalse(any(r["type"] == "provider" for r in self.records()))

    def test_busy_provider_switch_is_rejected_but_listing_remains_available(self):
        self.server.mode = "slow"
        p = check.Process(self)
        try:
            p.send("WAIT")
            self.assertTrue(self.server.waiting.wait(5))
            p.send("/provider")
            p.wait("ALLMODELS_API_KEY set")
            p.send("/provider allmodels")
            p.wait("Interrupt the current turn before changing the provider")
            p.wait("OK WAIT")
        finally:
            p.finish()
        self.assertEqual(self.server.requests[0]["model"], "gpt-6.1-sol")

    def test_catalog_and_usage_are_not_reused_across_providers_with_the_same_model_id(self):
        self.server.key_model_id = "gpt-6.1-sol"
        p = check.Process(self)
        try:
            p.send("MEASURE")
            p.wait("OK MEASURE")
            p.send("/provider allmodels")
            p.wait("Model: " + MODEL)
            p.send("/model gpt-6.1-sol")
            p.wait("Model: gpt-6.1-sol")
            p.send("/context")
            p.wait("Context: unknown (catalog not loaded)")
            self.server.model_status = 503
            p.send("OUTAGE")
            p.wait("Could not load models")
        finally:
            p.finish()
        self.assertEqual(len(self.server.requests), 1)

    def test_transfer_keeps_tool_results_and_text_without_foreign_opaque_items_or_replay(self):
        self.server.mode = "basic"
        self.run_batch("TASK basic")
        old_records = (self.session() / "journal.jsonl").read_bytes()
        self.server.mode = "echo"
        self.run_batch("TRANSFER", "--resume", self.session().name, "--provider", "allmodels")
        request = self.server.requests[-1]
        self.assertFalse(any(i.get("type") == "reasoning" for i in request["input"]))
        calls = [i for i in request["input"] if i.get("type") == "function_call"]
        self.assertTrue(calls)
        self.assertTrue(all(set(i) == {"type", "name", "arguments", "call_id"} for i in calls))
        self.assertTrue(any(i.get("type") == "function_call_output" for i in request["input"]))
        self.assertTrue(any("Done." in i.get("content", "") for i in request["input"] if i.get("role") == "assistant"))
        self.assertEqual((self.project / "a.py").read_text(), "answer = 2\n")
        self.assertTrue((self.session() / "journal.jsonl").read_bytes().startswith(old_records))
        self.assertEqual(self.saved_requests(), self.server.requests)
        self.run_batch("TRANSFER RESTART", "--resume", self.session().name)
        self.assertFalse(any(i.get("type") == "reasoning" for i in self.server.requests[-1]["input"]))
        self.assertEqual(self.server.auth_headers[-1], "Bearer KEY_NEVER_SAVED")

    def test_switch_preserves_a_running_local_job(self):
        self.server.mode = "plan"
        self.server.tool_plan = [
            [check.call("exec", dict(command="sleep 300", background=True), "keep")],
            [check.text("OK JOB READY")],
        ]
        p = check.Process(self)
        try:
            p.send("START JOB")
            p.wait("OK JOB READY")
            self.server.mode = "echo"
            p.send("/provider allmodels")
            p.wait("Model: " + MODEL)
            p.send("WITH JOB")
            p.wait("OK WITH JOB")
            p.buffer = b""
            p.send("/jobs 1")
            p.wait("jobs #1: running")
            p.send("/jobs stop 1")
        finally:
            p.finish()
        self.assertEqual(len([r for r in self.records() if r["type"] == "job" and r["data"].get("state") == "running"]), 1)

    def test_compaction_after_switch_preserves_history_and_resumes_on_the_selected_provider(self):
        p = check.Process(self)
        try:
            for n in range(5):
                p.send("BEFORE " + str(n))
                p.wait("OK BEFORE " + str(n))
            p.send("/provider allmodels")
            p.wait("Model: " + MODEL)
            p.send("/compact")
            p.wait("Full journal preserved")
            p.send("AFTER COMPACT")
            p.wait("OK AFTER COMPACT")
        finally:
            p.finish()
        self.assertEqual(self.server.auth_headers[-1], "Bearer KEY_NEVER_SAVED")
        self.assertTrue(any(r["type"] == "compaction_usage" and r["data"]["provider"] == "allmodels" for r in self.records()))
        self.run_batch("AFTER RESTART", "--resume", self.session().name)
        self.assertTrue(any("Earlier conversation summary" in i.get("content", "") for i in self.server.requests[-1]["input"] if isinstance(i.get("content"), str)))
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_each_connection_remembers_its_model_in_the_running_process(self):
        p = check.Process(self)
        try:
            p.send("/model custom-chatgpt")
            p.wait("Model: custom-chatgpt")
            p.send("/provider allmodels")
            p.wait("Model: " + MODEL)
            p.send("/model custom-key")
            p.wait("Model: custom-key")
            p.buffer = b""
            p.send("/provider chatgpt")
            p.wait("Model: custom-chatgpt")
            p.buffer = b""
            p.send("/provider allmodels")
            p.wait("Model: custom-key")
        finally:
            p.finish()
        self.assertEqual(self.server.requests, [])

    def test_resumed_connection_does_not_overwrite_another_connections_default_model(self):
        self.run_batch("FIRST", "--provider", "allmodels")
        p = check.Process(self, "--resume", self.session().name)
        try:
            p.wait("Model: " + MODEL)
            p.send("/provider chatgpt")
            p.wait("Model: gpt-6.1-sol")
            p.send("RETURN")
            p.wait("OK RETURN")
        finally:
            p.finish()
        self.assertEqual(self.server.auth_headers[-1], "Bearer NEVER_IN_JOURNAL")

    def test_removed_earlier_connection_does_not_block_the_current_one(self):
        self.run_batch("FIRST")
        self.run_batch("SECOND", "--resume", self.session().name, "--provider", "allmodels")
        path = self.session() / "meta.json"
        metadata = json.loads(path.read_text())
        metadata["provider"] = "removed-earlier-provider"
        path.write_text(json.dumps(metadata))
        self.run_batch("RESTART", "--resume", self.session().name)
        self.assertEqual(self.server.auth_headers[-1], "Bearer KEY_NEVER_SAVED")

    def test_legacy_chatgpt_metadata_restores_chatgpt(self):
        self.run_batch("FIRST")
        path = self.session() / "meta.json"
        metadata = json.loads(path.read_text())
        del metadata["provider"]
        path.write_text(json.dumps(metadata))
        self.run_batch("LEGACY", "--resume", self.session().name)
        self.assertEqual(self.server.auth_headers[-1], "Bearer NEVER_IN_JOURNAL")

    def test_provider_tab_completion_in_narrow_and_wide_terminals_is_local(self):
        for width in (40, 90):
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
            process = subprocess.Popen([str(check.BINARY), "-C", str(self.project)],
                                       env=dict(self.env, TERM="xterm"), stdin=slave, stdout=slave,
                                       stderr=slave, start_new_session=True)
            os.close(slave)
            def wait_for(needle):
                data = b""
                end = time.monotonic() + 5
                while needle not in data and time.monotonic() < end:
                    if select.select([master], [], [], .05)[0]:
                        data += os.read(master, 65536)
                self.assertIn(needle, data)
            try:
                wait_for(b"ctx:")
                os.write(master, b"/provider a\t\r")
                wait_for(b"Provider: allmodels")
                os.write(master, b"/provider c\t\r")
                wait_for(b"Provider: chatgpt")
                os.write(master, b"/quit\r")
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(self.server.model_fetches, 0)
                self.assertEqual(self.server.requests, [])
                path = max((self.path / "state/harn/sessions").glob("*/journal.jsonl"),
                           key=lambda p: p.stat().st_mtime_ns)
                records = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual([r["data"]["provider"] for r in records if r["type"] == "provider"],
                                 ["allmodels", "chatgpt"])
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                os.close(master)

    def test_unknown_startup_provider_fails_without_network(self):
        result = self.run_batch("NO REQUEST", "--provider", "absent", expected=2)
        self.assertIn(b"Provider absent is not configured", result.stderr)
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
