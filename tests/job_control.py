#!/usr/bin/env python3
"""Exercise real shell job control on a controlling PTY. No paid requests."""
import errno
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import unittest
import check
from check import BINARY, call, text


class JobControl(unittest.TestCase):
    def setUp(self):
        self.case = check.Checks()
        self.case.setUp()
        self.case.server.mode = "echo"
        self.shell_process = None
        self.shell = self.master = self.agent = None
        self.output = b""

    def tearDown(self):
        if self.agent:
            try:
                os.killpg(self.agent, signal.SIGCONT)
                os.killpg(self.agent, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.shell:
            try:
                os.kill(self.shell, signal.SIGHUP)
            except ProcessLookupError:
                pass
            self.shell_process.wait(timeout=5)
        if self.master is not None:
            os.close(self.master)
        self.case.tearDown()

    def until(self, condition, description="terminal state"):
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
        self.fail(description + "\n" + self.output.decode(errors="replace")[-5000:])

    def send(self, value):
        os.write(self.master, value.encode())

    def expect(self, value, since=0):
        if isinstance(value, str):
            value = value.encode()
        self.until(lambda: value in self.output[since:], repr(value))

    def start(self, width=80, **env):
        self.master, slave = pty.openpty()
        bootstrap = ("import fcntl, os, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
                     "os.execv('/bin/bash', ['bash', '--noprofile', '--norc', '-i'])")
        self.shell_process = subprocess.Popen(
            [sys.executable, "-c", bootstrap], stdin=slave, stdout=slave, stderr=slave,
            cwd=self.case.project, start_new_session=True,
            env=dict(self.case.env, TERM="xterm", PS1="HARN_SHELL> ", PS2="CONT> ", **env))
        os.close(slave)
        self.shell = self.shell_process.pid
        self.resize(width)
        self.expect("HARN_SHELL> ")
        mark = len(self.output)
        self.send(shlex.quote(str(BINARY)) + "\n")
        self.expect("ctx:", mark)
        self.agent = os.tcgetpgrp(self.master)
        self.assertNotEqual(self.agent, self.shell)

    def resize(self, width):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, width, 0, 0))

    def state(self, pid):
        try:
            return Path("/proc/" + str(pid) + "/status").read_text().split("State:", 1)[1].split()[0]
        except FileNotFoundError:
            return None

    def suspend(self, external=False):
        mark = len(self.output)
        if external:
            os.kill(self.agent, signal.SIGTSTP)
        else:
            self.send("\x1a")
        self.until(lambda: self.state(self.agent) == "T" and b"HARN_SHELL> " in self.output[mark:])
        self.assertEqual(os.tcgetpgrp(self.master), self.shell)
        return mark

    def resume(self, prompt="ctx:"):
        mark = len(self.output)
        self.send("fg\n")
        self.expect(prompt, mark)
        self.assertEqual(os.tcgetpgrp(self.master), self.agent)
        self.assertFalse(termios.tcgetattr(self.master)[3] & termios.ICANON)
        return mark

    def answer(self, value):
        self.until(lambda: any(value in part.get("text", "")
                               for r in self.case.records() if r["type"] == "item"
                               for part in r["data"].get("content", []) if isinstance(part, dict)), value)
        self.until(lambda: re.search(rb"(?:\x1b\[0K|\r\n)ctx:[^\r\n]*\r\n> ", self.output[self.output.rfind(b"model ctx:"):]))

    def users(self):
        return [r["data"]["content"] for r in self.case.records()
                if r["type"] == "item" and r["data"].get("role") == "user"]

    def test_repeated_suspend_preserves_multiline_draft_cursor_and_resize(self):
        self.start(width=42)
        self.send("\x1b[200~Черновик\nKeep text\x1b[201~")
        self.expect("Keep text")
        # Keep a non-final cursor position across both suspension and resize.
        self.send(" END\x02\x02")
        self.expect(" END")
        mark = self.suspend()
        self.assertIn(b"\x1b[?2004l", self.output[mark:])
        self.send("printf 'SHELL WORKS\\n'\n")
        self.expect("SHELL WORKS\r\n", mark)
        self.resize(90)
        mark = self.resume()
        self.assertIn(b"\x1b[?2004h", self.output[mark:])
        self.suspend(external=True)
        self.resume()
        self.send("X\r")
        self.answer("OK Черновик")
        self.assertEqual(self.users(), ["Черновик\nKeep text EXND"])

    def test_bg_does_not_take_over_the_shell_terminal(self):
        self.start()
        self.send("Keep draft")
        self.expect("Keep draft")
        self.suspend()
        mark = len(self.output)
        self.send("bg\n")
        self.until(lambda: self.state(self.agent) == "T" and b"HARN_SHELL> " in self.output[mark:])
        self.assertNotIn(b"ctx:", self.output[mark:])
        self.assertNotIn(b"Keep draft", self.output[mark:])
        self.assertEqual(os.tcgetpgrp(self.master), self.shell)
        self.resume()
        self.send("\r")
        self.answer("OK Keep draft")
        self.assertEqual(self.users(), ["Keep draft"])

    def test_foreground_command_pauses_background_job_keeps_running_and_nothing_replays(self):
        project = self.case.project
        (project / "worker.py").write_text(
            "import os, pathlib, sys, time\n"
            "name = sys.argv[1]\n"
            "pathlib.Path(name + '.pid').write_text(str(os.getpid()))\n"
            "with open(name + '.starts', 'a') as f: f.write('once\\n')\n"
            "while not pathlib.Path(name + '.finish').exists():\n"
            "    with open(name + '.ticks', 'a') as f: f.write('tick\\n')\n"
            "    time.sleep(.03)\n"
            "print(name + ' finished', flush=True)\n")
        self.case.server.mode = "plan"
        self.case.server.tool_plan = [
            [call("exec", dict(command="python3 worker.py bg", background=True), "bg")],
            [call("exec", dict(command="python3 worker.py fg"), "fg")],
            [text("WORK COMPLETE")],
        ]
        self.start()
        self.send("TASK\r")
        self.until(lambda: all((project / (name + ".ticks")).exists() for name in ("fg", "bg")))
        foreground = int((project / "fg.pid").read_text())
        self.send("draft")
        self.expect("draft")
        self.suspend()
        self.until(lambda: self.state(foreground) == "T")
        ticks = (project / "fg.ticks").stat().st_size
        background_ticks = (project / "bg.ticks").stat().st_size
        self.until(lambda: (project / "bg.ticks").stat().st_size > background_ticks)
        self.assertEqual((project / "fg.ticks").stat().st_size, ticks)
        self.resume("exec #2")
        (project / "fg.finish").touch()
        self.answer("WORK COMPLETE")
        self.send("\r")
        self.answer("OK draft")
        for name in ("fg", "bg"):
            self.assertEqual((project / (name + ".starts")).read_text(), "once\n")
        self.assertEqual(self.users(), ["TASK", "draft"])

    def test_suggestion_is_not_accepted_by_suspend(self):
        self.start()
        self.send("/pro")
        self.expect("/provider")
        self.suspend()
        self.resume()
        self.assertIn(b"> /pro\r\n", self.output)
        mark = len(self.output)
        self.send("\r")
        self.expect("> /provider ", mark)
        self.assertNotIn(b"Unknown command", self.output[mark:])
        self.assertEqual(self.case.server.model_fetches, 0)
        self.assertEqual(self.case.server.requests, [])

    def test_partial_network_stream_survives_suspend_without_resending_the_request(self):
        server = self.case.server
        server.mode = "plan"
        server.ui_stream = True
        response = text("Partial response. Привет.\nComplete.")
        response["phase"] = "final_answer"
        server.tool_plan = [[response]]
        gate = threading.Event()
        first_chunk = threading.Event()
        def hold(chunk):
            if not first_chunk.is_set():
                first_chunk.set()
                gate.wait(5)
        server.ui_delta_hook = hold
        self.start(width=42)
        self.send("TASK\r")
        self.expect("Partial")
        self.send("stream draft")
        self.expect("stream draft")
        self.suspend()
        self.assertEqual(len(server.requests), 1)
        gate.set()
        self.send("printf 'SHELL DURING STREAM\\n'\n")
        self.expect("SHELL DURING STREAM\r\n")
        mark = self.resume("model ctx:")
        self.answer("Complete.")
        self.assertIn(b"Partial", self.output[mark:])
        self.assertEqual(len(server.requests), 1)
        self.send("\r")
        self.answer("OK stream draft")
        self.assertEqual(self.users(), ["TASK", "stream draft"])
        self.assertEqual(len(server.requests), 2)

    def test_editor_suspend_returns_to_shell_and_resumes_the_same_editor(self):
        project = self.case.project
        (project / "editor.sh").write_text(
            "echo $$ > editor.pid\n"
            "while [ ! -f editor.finish ]; do sleep .03; done\n")
        self.start(EDITOR="sh " + str(project / "editor.sh"))
        self.send("Editor draft")
        self.expect("Editor draft")
        self.send("\x07")
        self.until(lambda: (project / "editor.pid").exists())
        editor = int((project / "editor.pid").read_text())
        # In an external editor the kernel, not linenoise, handles VSUSP.
        self.suspend()
        self.assertEqual(self.state(editor), "T")
        mark = len(self.output)
        self.send("fg\n")
        self.until(lambda: self.state(self.agent) != "T" and self.state(editor) != "T")
        (project / "editor.finish").touch()
        self.expect("Editor draft", mark)
        self.send("\r")
        self.answer("OK Editor draft")
        self.assertEqual(self.users(), ["Editor draft"])

    @unittest.skipUnless(shutil.which("vim"), "Vim is not installed")
    def test_vim_ctrl_z_and_fg_restore_the_editor_then_the_draft(self):
        project = self.case.project
        editor = "vim -Nu NONE -n -i NONE --noplugin -c \"call writefile([string(getpid())], 'vim.pid')\""
        self.start(EDITOR=editor)
        self.send("Vim draft")
        self.expect("Vim draft")
        self.send("\x07")
        self.until(lambda: (project / "vim.pid").exists())
        vim = int((project / "vim.pid").read_text())
        for attempt in range(3):
            self.suspend(external=attempt == 2)
            self.assertEqual(self.state(vim), "T")
            mark = len(self.output)
            self.send("fg\n")
            self.expect(b"\x1b[?1049h", mark)
            self.until(lambda: self.state(self.agent) != "T" and self.state(vim) != "T")
        self.send(":wq\r")
        self.expect("ctx:", mark)
        self.send("\r")
        self.answer("OK Vim draft")
        self.assertEqual(self.users(), ["Vim draft\n"])


if __name__ == "__main__":
    unittest.main()
