#!/usr/bin/env python3
"""Check terminal presentation through a PTY against the local Responses fixture."""
import errno
import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import termios
import threading
import time
import unittest
import check
from check import BINARY, call, text


class Presentation(unittest.TestCase):
    def setUp(self):
        self.case = check.Checks()
        self.case.setUp()
        self.case.server.mode = "plan"
        self.case.server.ui_stream = True
        self.master = None
        self.process = None
        self.output = b""

    def tearDown(self):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=5)
        if self.master is not None:
            os.close(self.master)
        self.case.tearDown()

    def start(self, width=80, height=36, args=(), **env):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        self.process = subprocess.Popen([str(BINARY), "-C", str(self.case.project), *args],
                                        env=dict(self.case.env, **env), stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.read_until(lambda: b"ctx:" in self.output)

    def read_until(self, condition):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if select.select([self.master], [], [], .03)[0]:
                try:
                    self.output += os.read(self.master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
            if condition():
                return
        self.fail(self.output.decode(errors="replace")[-5000:])

    def send(self, value):
        os.write(self.master, value.encode())

    def wait_answer(self, value):
        self.read_until(lambda: any(value in part.get("text", "")
                                   for record in self.case.records() if record["type"] == "item"
                                   for part in record["data"].get("content", []) if isinstance(part, dict)))
        # The journal is committed after stream presentation. Drain the final prompt as well.
        self.read_until(lambda: re.search(rb"(?:\x1b\[0K|\r\n)ctx:[^\r\n]*\r\n> ", self.output[self.output.rfind(b"model ctx:"):]))

    def assert_project_prompt(self, output, status):
        plain = re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", output)
        plain = plain.replace(b"\r", b"").replace(b"\n", b"")
        self.assertIn(str(self.case.project).encode() + b"fixture-model medium" + status, plain)

    def test_full_project_path_stays_in_prompt_during_inference_and_resize(self):
        # A path longer than the former prompt buffer, with wide UTF-8 characters.
        project = self.case.project / ("deep-" * 20) / ("проект界" * 12)
        project.mkdir(parents=True)
        self.case.project = project
        self.assertGreater(len(str(project).encode()), 256)
        gate = threading.Event()
        def hold(request, results):
            self.case.server.waiting.set()
            gate.wait(5)
        self.case.server.plan_checks[0] = hold
        self.case.server.tool_plan = [[text("FINISHED")]]
        self.start(width=80)
        self.assert_project_prompt(self.output, b"ctx:")
        mark = len(self.output)
        self.send("TASK\r")
        self.assertTrue(self.case.server.waiting.wait(5))
        self.read_until(lambda: b"model ctx:" in self.output[mark:])
        self.assert_project_prompt(self.output[mark:], b"model ctx:")
        mark = len(self.output)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 42, 0, 0))
        self.send("/mo")
        self.read_until(lambda: b"\x1b[7m/model" in self.output[mark:])
        self.assert_project_prompt(self.output[mark:], b"model ctx:")
        gate.set()
        mark = len(self.output)
        self.wait_answer("FINISHED")
        self.assert_project_prompt(self.output[mark:], b"ctx:")
        self.assertEqual(self.case.server.requests[0]["reasoning"], dict(effort="medium"))
        self.assertNotRegex(self.output, rb"\x1b\[-[0-9]+A")

    def test_model_and_default_effort_follow_selection_without_network(self):
        self.start(width=42)
        name = "selected-model-" + "long-name-" * 8 + "界"
        mark = len(self.output)
        self.send("/model " + name + "\r")
        def has_model_prompt():
            plain = re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", self.output[mark:])
            plain = plain.replace(b"\r", b"").replace(b"\n", b"")
            return (name + " defaultctx:").encode() in plain
        self.read_until(has_model_prompt)
        self.assertEqual(self.case.server.requests, [])
        self.assertEqual(self.case.server.model_fetches, 0)
        self.assertNotRegex(self.output, rb"\x1b\[-[0-9]+A")

    def load_catalog(self):
        mark = len(self.output)
        self.send("/model\r")
        self.read_until(lambda: b"  fixture-model" in self.output[mark:])

    def test_effort_completion_only_offers_the_selected_models_catalog_levels(self):
        self.case.server.model_metadata = dict(supported_reasoning_levels=None)
        self.start(width=42)
        self.load_catalog()
        mark = len(self.output)
        self.send("/effort ")
        self.read_until(lambda: b"\x1b[7mdefault" in self.output[mark:])
        self.assertNotIn(b"xhigh", self.output[mark:])
        self.assertNotIn(b"\r\n  medium", self.output[mark:])
        self.assertEqual(self.case.server.requests, [])

    def test_effort_picker_loads_catalog_on_first_use(self):
        self.start(width=42)
        self.send("/ef\r")
        self.read_until(lambda: b"\x1b[7mdefault" in self.output and b"xhigh" in self.output)
        self.assertEqual(self.case.server.model_fetches, 1)
        self.send("hi\r")
        self.read_until(lambda: b"fixture-model high" in self.output)
        self.assertEqual(self.case.server.requests, [])

    def test_resume_suggestions_filter_project_and_sort_by_activity(self):
        base = self.case.path / "state/harn/sessions"
        for name, root, nanos in [("older", self.case.project, 1),
                                  ("recent", self.case.project, 2),
                                  ("foreign", self.case.path, 3)]:
            folder = base / name
            folder.mkdir(parents=True)
            (folder / "meta.json").write_text(json.dumps(dict(cwd=str(root), model="fixture-model",
                                                             provider="fixture")))
            log = folder / "journal.jsonl"
            log.write_text(json.dumps(dict(type="item", data=dict(role="user", content=name + " request"))) + "\n")
            os.utime(log, ns=(1_700_000_000_000_000_000 + nanos,) * 2)
        self.start(width=140)
        self.send("/res\r")
        self.read_until(lambda: b"\x1b[7mrecent" in self.output and b"older request" in self.output)
        self.assertNotIn(b"foreign", self.output)
        self.send("\x1b[B\r")
        self.read_until(lambda: b"Resumed session: older" in self.output)
        self.assertEqual(self.case.server.requests, [])
        self.assertEqual(self.case.server.model_fetches, 0)

    def test_resume_suggestions_switch_conversation_without_restart(self):
        self.case.server.ui_stream = False
        self.case.server.mode = "echo"
        self.case.run_batch("PREVIOUS SESSION")
        previous = self.case.session().name
        self.start(width=80)
        self.send("/res\r")
        self.read_until(lambda: previous.encode() in self.output and b"PREVIOUS SESSION" in self.output)
        self.assertIn(b"\x1b[7m", self.output)
        self.send("\r")
        self.read_until(lambda: ("Resumed session: " + previous).encode() in self.output)
        self.send("CONTINUATION\r")
        self.read_until(lambda: len(self.case.server.requests) == 2)
        users = [item.get("content") for item in self.case.server.requests[-1]["input"]
                 if item.get("role") == "user"]
        self.assertEqual(users, ["PREVIOUS SESSION", "CONTINUATION"])

    def test_effort_completion_sets_value_and_updates_persistent_prompt(self):
        self.start(width=42)
        self.load_catalog()
        self.send("/effort x")
        self.read_until(lambda: b"\x1b[7mxhigh" in self.output)
        mark = len(self.output)
        self.send("\r")
        self.read_until(lambda: b"fixture-model xhigh" in self.output[mark:])
        self.assertNotIn(b"effort:", self.output[mark:])
        self.assertEqual(self.case.server.requests, [])
        self.assertEqual(self.case.server.model_fetches, 1)

    def test_automatic_command_suggestions_are_local_and_tab_accepts(self):
        self.start()
        self.send("/")
        self.read_until(lambda: b"\x1b[7m/compact" in self.output)
        self.assertIn(b"Compact context", self.output)
        self.assertEqual(self.case.server.requests, [])
        self.assertEqual(self.case.server.model_fetches, 0)
        self.send("\x1b[B\t\r")
        self.read_until(lambda: b"Context:" in self.output)
        self.assertEqual(self.case.server.requests, [])
        self.assertNotIn(b"idle ctx:", self.output)

    def test_enter_opens_argument_suggestions_and_escape_preserves_the_draft(self):
        self.start(width=42)
        self.send("/pro")
        self.read_until(lambda: b"\x1b[7m/provider" in self.output)
        mark = len(self.output)
        self.send("\r")
        self.read_until(lambda: b"> /provider " in self.output[mark:])
        self.assertIn(b"\x1b[7mfixture", self.output[mark:])
        self.assertNotIn(b"Unknown command", self.output)
        self.assertEqual(self.case.server.model_fetches, 0)
        self.send("\x03")
        mark = len(self.output)
        self.send("/pro")
        self.read_until(lambda: b"> /pro" in self.output[mark:] and self.output.endswith(b"\x1b[6C"))
        mark = len(self.output)
        self.send("\x1b")
        self.read_until(lambda: b"/pro" in self.output[mark:])
        self.assertNotIn(b"\x1b[7m", self.output[mark:])
        self.send("\r")
        self.read_until(lambda: b"Unknown command" in self.output[mark:])
        self.assertEqual(self.case.server.requests, [])

    def test_enter_executes_arrow_selection_without_arguments(self):
        self.start()
        self.send("/")
        self.read_until(lambda: b"\x1b[7m/compact" in self.output)
        self.send("\x1b[B")
        self.read_until(lambda: b"\x1b[7m/context" in self.output)
        mark = len(self.output)
        self.send("\r")
        self.read_until(lambda: b"Context:" in self.output[mark:])
        self.assertIn(b"> /context", self.output[mark:])
        self.assertEqual(self.case.server.requests, [])

    def test_enter_executes_model_command_when_no_cached_arguments_exist(self):
        self.start()
        self.send("/mo")
        self.read_until(lambda: b"\x1b[7m/model" in self.output)
        self.send("\r")
        self.read_until(lambda: b"  fixture-model" in self.output)
        self.assertEqual(self.case.server.model_fetches, 1)
        self.assertEqual(self.case.server.requests, [])

    def test_enter_chains_effort_command_then_executes_selected_argument(self):
        self.start(width=42)
        self.load_catalog()
        self.send("/ef\r")
        self.read_until(lambda: b"> /effort " in self.output and b"\x1b[7mdefault" in self.output)
        self.assertEqual(self.case.server.requests, [])
        self.send("hi\r")
        self.read_until(lambda: b"fixture-model high" in self.output)
        self.assertEqual(self.case.server.model_fetches, 1)
        self.assertEqual(self.case.server.requests, [])

    def test_enter_chains_job_subcommand_without_duplicate_spaces(self):
        self.case.server.tool_plan = [
            [call("exec", dict(command="sleep 30", background=True), "background")],
            [text("JOB RUNNING")],
        ]
        self.start(width=42)
        self.send("TASK\r")
        self.wait_answer("JOB RUNNING")
        self.send("/jo\r")
        self.read_until(lambda: b"> /jobs " in self.output)
        self.send("s\r")
        self.read_until(lambda: b"> /jobs stop " in self.output)
        mark = len(self.output)
        self.send("\r")
        self.read_until(lambda: b"Stopping #1" in self.output[mark:])
        self.assertNotIn(b"/jobs stop  ", self.output)
        self.assertEqual(len(self.case.server.requests), 2)

    def test_resume_identifies_conversation_at_narrow_width(self):
        self.case.server.tool_plan = [[text("SAVED ANSWER")]]
        self.case.run_batch("Fix the fixture timeout")
        session = self.case.session().name
        self.start(width=42, args=("--resume", session))
        self.read_until(lambda: b"Started with:" in self.output)
        plain = re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", self.output)
        self.assertIn(b"Resumed session:", plain)
        self.assertIn(session.encode(), plain.replace(b"\r\n  ", b""))
        self.assertIn(b"Fix the fixture timeout", plain)
        self.assertEqual(len(self.case.server.requests), 1)

    def test_escape_returns_arrows_to_history_and_tab_can_reopen_completion(self):
        self.start()
        self.send("/help\r")
        self.read_until(lambda: b"Ctrl-Z: suspend" in self.output)
        self.send("/mo")
        self.read_until(lambda: b"\x1b[7m/model" in self.output)
        mark = len(self.output)
        self.send("\x1b")
        self.read_until(lambda: b"/mo" in self.output[mark:])
        self.send("\x1b[A\r")
        self.read_until(lambda: b"Ctrl-Z: suspend" in self.output[mark:])
        self.assertEqual(self.case.server.model_fetches, 0)
        mark = len(self.output)
        self.send("/con")
        self.read_until(lambda: b"\x1b[7m/context" in self.output[mark:])
        self.send("\x1b\t\r")
        self.read_until(lambda: b"Context:" in self.output[mark:])
        self.assertEqual(self.case.server.requests, [])

    def test_alt_backspace_deletes_utf8_word_with_split_escape(self):
        self.case.server.mode = "echo"
        self.start(width=42)
        self.send("KEEP Привет  ")
        self.read_until(lambda: "KEEP Привет  ".encode() in self.output)
        mark = len(self.output)
        self.send("\x1b")
        self.read_until(lambda: b"KEEP " in self.output[mark:])
        self.send("\x7fмир\r")
        self.wait_answer("OK KEEP мир")
        self.assertEqual(self.case.server.requests[-1]["input"][-1]["content"], "KEEP мир")

    def test_alt_backspace_ctrl_h_and_cursor_editing(self):
        self.case.server.mode = "echo"
        self.start()
        cases = [
            ("ONE TWO\x1b\x08THREE\r", "ONE THREE"),
            ("alpha beta tail" + "\x02" * 5 + "\x1b\x7fNEW\r", "alpha NEW tail"),
            ("\x1b\x7fAFTER EMPTY\r", "AFTER EMPTY"),
            ("keep word\x17tail\r", "keep tail"),
            ("plainx\x7f\r", "plain"),
        ]
        for keys, expected in cases:
            with self.subTest(expected=expected):
                self.send(keys)
                self.wait_answer("OK " + expected)
                self.assertEqual(self.case.server.requests[-1]["input"][-1]["content"], expected)

    def test_ctrl_n_and_ctrl_p_select_suggestions_with_wraparound(self):
        self.start(width=42)
        self.send("/")
        self.read_until(lambda: b"\x1b[7m/compact" in self.output)
        for key, selected in [("\x0e", "/context"), ("\x10", "/compact"),
                              ("\x10", "/skill"), ("\x0e", "/compact"), ("\x0e", "/context")]:
            mark = len(self.output)
            self.send(key)
            self.read_until(lambda: b"\x1b[7m" + selected.encode() in self.output[mark:])
        self.send("\r")
        self.read_until(lambda: b"Context:" in self.output)
        self.assertEqual(self.case.server.requests, [])
        self.assertEqual(self.case.server.model_fetches, 0)

    def test_ctrl_n_and_ctrl_p_keep_history_navigation_without_suggestions(self):
        self.start()
        self.load_catalog()
        self.send("/help\r")
        self.read_until(lambda: b"Ctrl-Z: suspend" in self.output)
        mark = len(self.output)
        self.send("draft\x10")
        self.read_until(lambda: b"> /help" in self.output[mark:])
        mark = len(self.output)
        self.send("\x0e")
        self.read_until(lambda: b"> draft" in self.output[mark:])
        self.send("\x15/effort h")
        self.read_until(lambda: b"\x1b[7mhigh" in self.output)
        mark = len(self.output)
        self.send("\x1b")
        self.read_until(lambda: b"/effort h" in self.output[mark:])
        mark = len(self.output)
        self.send("\x10\r")
        self.read_until(lambda: b"Ctrl-Z: suspend" in self.output[mark:])
        self.assertEqual(self.case.server.requests, [])

    def test_suggestion_range_shows_hidden_items_and_tracks_scroll_and_wrap(self):
        self.start(width=42)
        self.send("/")
        self.read_until(lambda: b"1-5 of 10" in self.output)
        for key, position in [("\x10", b"6-10 of 10"), ("\x10", b"5-9 of 10"),
                              ("\x0e", b"6-10 of 10"), ("\x0e", b"1-5 of 10")]:
            mark = len(self.output)
            self.send(key)
            self.read_until(lambda: position in self.output[mark:])
        self.assertEqual(self.case.server.requests, [])
        self.assertEqual(self.case.server.model_fetches, 0)

    def test_suggestion_range_adapts_to_short_terminal_without_hiding_selection(self):
        # Keep the path on one row so TMPDIR does not change the suggestion space.
        width = max(42, len(str(self.case.project).encode()) + 1)
        self.start(width=width, height=7)
        self.send("/")
        self.read_until(lambda: b"1-1 of 10" in self.output)
        self.assertIn(b"\x1b[7m/compact", self.output)
        mark = len(self.output)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 6, width, 0, 0))
        self.send("\x0e")
        self.read_until(lambda: b"2/10" in self.output[mark:])
        self.assertIn(b"\x1b[7m/context", self.output[mark:])
        mark = len(self.output)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 80, 0, 0))
        self.send("\x10")
        self.read_until(lambda: b"1-5 of 10" in self.output[mark:]
                        and b"\x1b[7m/compact" in self.output[mark:])
        self.assertNotRegex(self.output, rb"\x1b\[-[0-9]+A")
        self.assertEqual(self.case.server.requests, [])

    def test_suggestion_range_disappears_when_filtered_list_fits_or_is_dismissed(self):
        self.start(width=42)
        self.load_catalog()
        self.send("/effort ")
        self.read_until(lambda: b"1-5 of 8" in self.output)
        mark = len(self.output)
        self.send("x")
        self.read_until(lambda: b"\x1b[7mxhigh" in self.output[mark:])
        self.assertNotIn(b" of ", self.output[mark:])
        mark = len(self.output)
        self.send("\x1b")
        self.read_until(lambda: b"> /effort x" in self.output[mark:])
        self.assertNotIn(b" of ", self.output[mark:])
        self.assertNotIn(b"\x1b[7m", self.output[mark:])
        self.assertEqual(self.case.server.requests, [])

    def test_suggestions_survive_output_and_resize_without_changing_the_draft(self):
        gate = threading.Event()
        def hold(request, results):
            self.case.server.waiting.set()
            gate.wait(5)
        self.case.server.plan_checks[0] = hold
        self.case.server.tool_plan = [[text("FINISHED")]]
        self.start()
        self.send("TASK\r")
        self.assertTrue(self.case.server.waiting.wait(5))
        self.send("/mo")
        self.read_until(lambda: b"\x1b[7m/model" in self.output)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 42, 0, 0))
        gate.set()
        self.wait_answer("FINISHED")
        mark = len(self.output)
        self.send("\r")
        self.read_until(lambda: b"> /model " in self.output[mark:])
        self.assertNotIn(b"Unknown command", self.output[mark:])
        self.assertEqual(len(self.case.server.requests), 1)
        self.assertNotRegex(self.output, rb"\x1b\[-[0-9]+A")

    def test_stop_is_one_emphasized_event_and_idle_ctrl_c_is_quiet(self):
        self.case.server.tool_plan = [[call("exec", dict(command="touch started; sleep 30"), "wait")]]
        self.start()
        self.send("TASK\r")
        self.case.wait_path("started")
        self.send("\x03")
        self.read_until(lambda: b"Stopped by you" in self.output)
        self.assertEqual(self.output.count(b"Stopped by you"), 1)
        self.assertIn(b"\x1b[1m  Stopped by you", self.output)
        self.assertNotIn(b"Turn interrupted", self.output)
        self.assertNotIn(b"  Interrupted", self.output)
        mark = len(self.output)
        self.send("discard me\x03")
        self.read_until(lambda: b"> " in self.output[mark:])
        self.assertEqual(self.output.count(b"Stopped by you"), 1)

    def test_tools_are_compact_and_full_output_is_available_locally(self):
        commentary = text("Inspecting the timeout.")
        commentary["phase"] = "commentary"
        answer = text("Fixed the timeout.\nTests passed. Привет.")
        answer["phase"] = "final_answer"
        self.case.server.tool_plan = [
            [commentary, call("read", dict(path="a.py"), "read")],
            [call("exec", dict(command="printf 'CHECK PASSED\\n'"), "exec")],
            [answer],
        ]
        self.start()
        self.send("Fix timeout\r")
        self.wait_answer("Fixed the timeout.")
        output = self.output
        self.assertIn(b"\x1b[2m  read a.py:", output)
        self.assertIn(b"\x1b[2m  exec #1:", output)
        self.assertNotIn(b"  CHECK PASSED\r\n", output)
        self.assertIn(b"jobs #1 exited (0), /jobs 1", output)
        self.assertIn(b"\x1b[22m", output)
        self.assertNotIn(b"Answer", output)
        self.assertIn(b"ctx:<1%\r\n> ", output)
        self.assertEqual((self.case.session() / "jobs/1.log").read_bytes(), b"CHECK PASSED\n")
        saved = [r["data"] for r in self.case.records() if r["type"] == "item"]
        self.assertTrue(any(i.get("content") == "Fix timeout" for i in saved))
        self.assertTrue(any(i == answer for i in saved))
        requests = len(self.case.server.requests)
        self.send("/jobs 1\r")
        self.read_until(lambda: b"jobs #1: CHECK PASSED" in self.output)
        self.assertEqual(len(self.case.server.requests), requests)

    def test_narrow_stream_and_diagnostics_preserve_a_multiline_draft(self):
        answer = text("Привет " * 32 + "\nDone.")
        answer["phase"] = "final_answer"
        self.case.server.tool_plan = [[answer]]
        gate = threading.Event()
        def hold(request, results):
            self.case.server.waiting.set()
            gate.wait(5)
        self.case.server.plan_checks[0] = hold
        self.start(width=42)
        self.send("TASK\r")
        self.assertTrue(self.case.server.waiting.wait(5))
        self.send("\x1b[200~draft text\nКириллица\x1b[201~")
        self.read_until(lambda: "Кириллица".encode() in self.output)
        self.assertNotIn(b"pasted lines", self.output)
        gate.set()
        self.wait_answer("Done.")
        self.send("\r")
        self.wait_answer("OK draft text")
        users = [r["data"]["content"] for r in self.case.records()
                 if r["type"] == "item" and r["data"].get("role") == "user"]
        self.assertEqual(users, ["TASK", "draft text\nКириллица"])
        self.assertNotIn(b"\xef\xbf\xbd", self.output)

    def test_editor_replays_presentation_roles_and_preserves_the_draft(self):
        answer = text("EDITOR RESPONSE")
        answer["phase"] = "final_answer"
        self.case.server.tool_plan = [[call("read", dict(path="a.py"), "r")], [answer]]
        gate = threading.Event()
        def hold(request, results):
            self.case.server.waiting.set()
            gate.wait(5)
        self.case.server.plan_checks[0] = hold
        script = self.case.project / "editor.sh"
        script.write_text("touch editor-open\nwhile [ ! -f editor-close ]; do sleep .02; done\n")
        self.start(EDITOR="sh " + str(script))
        self.send("TASK\r")
        self.assertTrue(self.case.server.waiting.wait(5))
        self.send("Keep draft")
        self.read_until(lambda: b"Keep draft" in self.output)
        self.send("\x07")
        self.case.wait_path("editor-open")
        gate.set()
        self.read_until(lambda: any(r["type"] == "item" and r["data"] == answer
                                   for r in self.case.records()))
        self.assertNotIn(b"EDITOR RESPONSE", self.output)
        (self.case.project / "editor-close").touch()
        self.wait_answer("EDITOR RESPONSE")
        self.assertIn(b"\x1b[2m  read a.py:", self.output)
        self.assertIn(b"\x1b[22m", self.output)
        self.send("\r")
        self.wait_answer("OK Keep draft")
        users = [r["data"]["content"] for r in self.case.records()
                 if r["type"] == "item" and r["data"].get("role") == "user"]
        self.assertEqual(users, ["TASK", "Keep draft"])

    def test_errors_and_failed_job_status_are_not_dimmed(self):
        self.case.server.tool_plan = [
            [call("read", dict(path="missing.txt"), "missing")],
            [call("exec", dict(command="printf 'FAILED\\n'; exit 7"), "failed")],
        ]
        self.start(width=42)
        self.send("TASK\r")
        self.wait_answer("OK TASK")
        self.assertIn(b"  read: No such file or directory\r\n", self.output)
        self.assertIn(b"  FAILED\r\n", self.output)
        self.assertIn(b"  jobs #1 exited (7), /jobs 1", self.output)
        self.assertNotIn(b"\x1b[2m  read:", self.output)
        self.assertNotIn(b"\x1b[2m  FAILED", self.output)
        self.assertNotIn(b"\x1b[2m  jobs #1 exited (7)", self.output)

    def test_batch_stream_has_no_presentation_bytes(self):
        source = "## Exact answer\n\n**Привет.**\n\n```sh\nmake check\n```"
        answer = text(source)
        answer["phase"] = "final_answer"
        self.case.server.tool_plan = [[call("read", dict(path="a.py"), "r")], [answer]]
        result = self.case.run_batch("TASK")
        self.assertEqual(result.stdout, (source + "\n").encode())
        self.assertNotIn(b"\x1b[", result.stderr)
        self.assertIn(b"read a.py:", result.stderr)

    def test_markdown_is_rendered_and_the_journal_keeps_the_source(self):
        source = "## Result\n\n**Passed** with `a_b`.\n\n- " + "long item " * 15
        source += "\n\n```sh\nprintf '**literal**'\n```\n\nEND"
        answer = text(source)
        answer["phase"] = "final_answer"
        self.case.server.tool_plan = [[answer]]
        self.start(width=42)
        self.send("TASK\r")
        self.wait_answer("END")
        self.assertIn(b"\x1b[1mResult", self.output)
        self.assertIn(b"\x1b[1mPassed", self.output)
        self.assertIn(b"\x1b[7ma_b", self.output)
        self.assertIn(b"printf '**literal**'", self.output)
        self.assertNotIn(b"```", self.output)
        saved = [r["data"] for r in self.case.records() if r["type"] == "item"]
        self.assertIn(answer, saved)

    def test_resize_after_a_long_stream_keeps_the_draft_and_scroll_geometry(self):
        source = "Before " * 500 + "GATE_OK" + " after " * 20 + "\nEND"
        answer = text(source)
        answer["phase"] = "final_answer"
        self.case.server.tool_plan = [[answer]]
        gate = threading.Event()
        def hold(chunk):
            if chunk == "GATE_OK":
                gate.wait(5)
        self.case.server.ui_delta_hook = hold
        self.start(width=42)
        self.send("TASK\r")
        self.read_until(lambda: b"GATE_OK" in self.output)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 140, 0, 0))
        self.send("keep draft")
        self.read_until(lambda: b"keep draft" in self.output)
        gate.set()
        self.wait_answer("END")
        self.assertNotRegex(self.output, rb"\x1b\[-[0-9]+A")
        moves = [int(value) for value in re.findall(rb"\x1b\[([0-9]+)A", self.output)]
        self.assertLessEqual(max(moves), 36)
        self.send("\r")
        self.wait_answer("OK keep draft")
        users = [r["data"]["content"] for r in self.case.records()
                 if r["type"] == "item" and r["data"].get("role") == "user"]
        self.assertEqual(users, ["TASK", "keep draft"])


if __name__ == "__main__":
    unittest.main()
