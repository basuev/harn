"""Reusable black-box acceptance suite for source provider adapters."""
import os
import fcntl
import struct
import termios
from pathlib import Path
import pty
import select
import subprocess
import sys
import threading
import time
import unittest

import check


class Contract(unittest.TestCase):
    adapter_id = None
    key_env = None
    configure = None

    def setUp(self):
        check.Checks.setUp(self)
        self.env["HARN_SECOND_KEY"] = "KEY_SECOND_NEVER_SAVED"
        self.server.standard_catalog = self.catalog_format == "data"
        if self.key_env:
            self.env[self.key_env] = "KEY_PROVIDER_NEVER_SAVED"
        if self.configure:
            self.configure()

    def tearDown(self):
        try:
            for path in (self.path / "state").rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    for secret in (b"KEY_PROVIDER_NEVER_SAVED", b"KEY_SECOND_NEVER_SAVED",
                                   b"ACCESS_NEVER_SAVED", b"REFRESH_NEVER_SAVED"):
                        self.assertNotIn(secret, data, str(path))
        finally:
            check.Checks.tearDown(self)

    def test_switch_isolates_connections_and_preserves_jobs(self):
        self.server.mode = "plan"
        self.server.tool_plan = [
            [check.call("exec", dict(command="sleep 30", background=True), "keep")],
            [check.text("OK JOB READY")],
        ]
        p = check.Process(self)
        try:
            p.send("START")
            p.wait("OK JOB READY")
            self.server.mode = "echo"
            self.server.model_metadata = dict(supported_reasoning_levels=None)
            p.send("/provider fixture-key")
            p.wait("Provider: fixture-key")
            p.send("SECOND")
            p.wait("OK SECOND")
            self.assertNotIn("reasoning", self.server.requests[-1])
            self.assertEqual(self.server.auth_headers[-1], "Bearer KEY_SECOND_NEVER_SAVED")
            self.assertFalse(any(item.get("type") == "reasoning" for item in self.server.requests[-1]["input"]))
            self.server.model_metadata = {}
            p.send("/provider " + self.adapter_id)
            p.wait("Provider: " + self.adapter_id)
            p.send("RETURN")
            p.wait("OK RETURN")
            self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="medium"))
            p.buffer = b""
            p.send("/jobs 1")
            p.wait("jobs #1: running")
            p.send("/jobs stop 1")
        finally:
            p.finish()
        self.run_batch("RESUMED", "--resume", self.session().name)
        self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="medium"))

    def test_authentication_errors_do_not_fall_back_to_another_connection(self):
        self.server.mode = "echo"
        if self.key_env:
            value = self.env.pop(self.key_env)
            self.run_batch("MISSING KEY", expected=1)
            self.assertEqual(self.server.requests, [])
            self.env[self.key_env] = value
        self.server.response_status = 401
        self.run_batch("REJECTED", expected=1)
        self.assertEqual(len(self.server.requests), 1 if self.key_env else 3)
        self.assertNotIn("Bearer KEY_SECOND_NEVER_SAVED", self.server.auth_headers)
        count = len(self.server.requests)
        refreshes = self.server.token_calls
        self.server.response_status = 402
        self.run_batch("NO BALANCE", expected=1)
        self.assertEqual(len(self.server.requests), count + 1)
        self.assertEqual(self.server.token_calls, refreshes)

    def test_error_diagnostics_redact_authorization(self):
        self.server.response_status = 400
        self.server.echo_auth_error = True
        result = self.run_batch("REJECTED", expected=1)
        self.assertIn(b"[redacted]", result.stderr)
        for secret in (b"KEY_PROVIDER_NEVER_SAVED", b"ACCESS_NEVER_SAVED"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_fragmented_stream_and_tools(self):
        self.server.fragment_events = True
        self.test_full_tool_cycle_journal_and_resume()

    def test_text_is_visible_before_completion_in_a_terminal(self):
        self.server.mode = "plan"
        self.server.ui_stream = True
        self.server.tool_plan = [[check.text("VISIBLE GATE_OK FINISHED")]]
        gate = threading.Event()
        def hold(chunk):
            if chunk == "GATE_OK":
                gate.wait(5)
        self.server.ui_delta_hook = hold
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 80, 0, 0))
        process = subprocess.Popen([str(check.BINARY), "-C", str(self.project)], env=self.env,
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        data = b""
        def wait_for(needle):
            nonlocal data
            end = time.monotonic() + 8
            while needle not in data and time.monotonic() < end:
                if select.select([master], [], [], .03)[0]:
                    data += os.read(master, 65536)
            self.assertIn(needle, data)
        try:
            wait_for(b"ctx:")
            os.write(master, b"TASK\r")
            wait_for(b"GATE_OK")
            self.assertFalse(any(r["type"] == "item" and r["data"].get("role") == "assistant"
                                 for r in self.records()))
            gate.set()
            wait_for(b"FINISHED")
        finally:
            gate.set()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            os.close(master)


# Reuse agent-level checks without inheriting provider-specific assumptions.
for name, value in vars(check.Checks).items():
    if callable(value) and not name.startswith("test_") and name not in ("setUp", "tearDown"):
        setattr(Contract, name, value)
for name in (
    "test_full_tool_cycle_journal_and_resume",
    "test_context_view_preserves_separate_input_output_and_limit_source",
    "test_context_does_not_present_fallback_as_a_reported_window",
    "test_effort_default_and_cli_are_sent_and_reported_separately",
    "test_effort_choices_and_preferences_follow_model_and_resume",
    "test_unsupported_effort_is_not_silently_retried_as_default",
    "test_catalog_reuse_does_not_override_auth_errors_invalid_data_or_model_removal",
    "test_request_error_records_transport_and_stream_details",
    "test_retry_limit_and_no_incomplete_execution",
    "test_interrupt_model_network_request_keeps_background_and_accepts_clarification",
    "test_interrupting_catalog_refresh_does_not_start_a_cached_request",
    "test_compaction_preserves_source_and_exact_request",
    "test_effort_is_used_by_compaction_and_followup",
    "test_unknown_action_after_crash_is_not_replayed",
):
    setattr(Contract, name, getattr(check.Checks, name))


def run(adapter_id, key_env=None, configure=None, fixture_class=check.Fixture, extra=(), catalog_format="data"):
    root = Path(__file__).resolve().parents[1]
    binary = root / ".build" / ("harn-" + adapter_id)
    arguments = ["make", "CONFIG=providers/" + adapter_id + "/test-config.h",
                 "BUILD=.build/" + adapter_id, "BIN=" + str(binary),
                 "EXTRA_SOURCES=tests/fixture.c"]
    subprocess.run(arguments, cwd=root, check=True)
    prefix = root / ".build" / ("installed-" + adapter_id)
    subprocess.run(arguments + ["install", "PREFIX=" + str(prefix)], cwd=root, check=True)
    check.BINARY = prefix / "bin/harn"
    subprocess.run([str(check.BINARY), "--version"], env=dict(os.environ, PATH="/nonexistent"), check=True)
    attrs = dict(adapter_id=adapter_id, key_env=key_env, configure=configure,
                 fixture_class=fixture_class, catalog_format=catalog_format)
    case = type(adapter_id.title() + "Contract", (Contract,), attrs)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
    for cls in extra:
        cls.adapter_id, cls.key_env, cls.configure, cls.fixture_class = adapter_id, key_env, configure, fixture_class
        cls.catalog_format = catalog_format
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
