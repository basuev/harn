#!/usr/bin/env python3
"""End-to-end checks against a local Responses fixture. No subscription requests."""
import http.server
import json
import os
from pathlib import Path
import select
import shlex
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / ".build/harn-test"


def call(name, arguments, ident):
    return dict(type="function_call", name=name, namespace="project", arguments=json.dumps(arguments),
                call_id=ident, id="fc_" + ident, status="completed")


def text(value):
    return dict(type="message", role="assistant", id="msg_" + str(time.monotonic_ns()), status="completed",
                content=[dict(type="output_text", text=value, annotations=[])])


class Fixture(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, handler=None):
        super().__init__(("127.0.0.1", 0), handler or Handler)
        self.requests = []
        self.auth_headers = []
        self.discovery_calls = 0
        self.standard_catalog = False
        self.model_id = "fixture-model"
        self.key_model_id = None
        self.key_window = None
        self.completed_only = False
        self.ui_stream = False
        self.ui_delta_hook = None
        self.response_status = 200
        self.mode = "basic"
        self.count = 0
        self.waiting = threading.Event()
        self.delay = 1.5
        self.token_calls = 0
        self.model_fetches = 0
        self.window = 200000
        self.extra_models = []
        self.model_metadata = {}
        self.model_status = 200
        self.model_bad = False
        self.model_present = True
        self.model_delay = 0
        self.model_waiting = threading.Event()
        self.auto_calls = 3
        self.usage = dict(input_tokens=100, output_tokens=10, total_tokens=110)
        self.response_reasoning = None
        self.skill_name = "fixture-skill"
        self.skill_content = None
        self.skill_error = False
        self.tool_plan = []
        self.plan_index = 0
        self.plan_checks = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def fixed(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/v1/models":
            self.server.model_fetches += 1
            if self.server.model_delay:
                self.server.model_waiting.set()
                time.sleep(self.server.model_delay)
            if not self.server.model_status:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.server.model_status != 200:
                self.fixed(dict(error=dict(message="Fixture catalog failure")), self.server.model_status)
                return
            if self.server.model_bad:
                self.fixed(dict(models="invalid"))
                return
            is_key = self.headers.get("Authorization", "").startswith("Bearer KEY_")
            model = dict(slug=self.server.key_model_id if is_key and self.server.key_model_id else self.server.model_id,
                         display_name="Fixture", visibility="list",
                         context_window=self.server.key_window if is_key and self.server.key_window else self.server.window)
            if self.server.window is None:
                del model["context_window"]
            model["supported_reasoning_levels"] = [dict(effort=level) for level in
                ("none", "minimal", "low", "medium", "high", "xhigh", "max")]
            model.update(self.server.model_metadata)
            if self.server.standard_catalog or is_key:
                model = dict(id=model.pop("slug"), **model)
                if "context_window" in model:
                    model["context_length"] = model.pop("context_window")
                self.fixed(dict(data=([model] if self.server.model_present else []) + self.server.extra_models))
            else:
                self.fixed(dict(models=([model] if self.server.model_present else []) + self.server.extra_models))
        else:
            self.fixed(dict(error=dict(message="Unknown fixture")), 404)

    def event(self, obj):
        data = b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"
        step = 3 if getattr(self.server, "fragment_events", False) else len(data)
        for offset in range(0, len(data), step):
            self.wfile.write(data[offset:offset + step])
            self.wfile.flush()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        request = json.loads(raw)
        s = self.server
        s.requests.append(request)
        s.auth_headers.append(self.headers.get("Authorization"))
        s.count += 1
        if s.response_status != 200:
            message = self.headers.get("Authorization") if getattr(s, "echo_auth_error", False) else "Fixture rejected key"
            self.fixed(dict(error=dict(code="invalid_api_key", message=message)), s.response_status)
            return
        mode, count = s.mode, s.count
        if mode == "fail" or mode == "retry" and count == 1:
            self.fixed(dict(error=dict(code="server_error", message="Fixture temporary failure")), 503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            if mode == "partial":
                self.event(dict(type="response.output_item.done", item=call("write", dict(path="forbidden", content="bad"), "partial")))
                return
            inputs = request["input"]
            users = [x["content"] for x in inputs if x.get("role") == "user"]
            last = users[-1] if users else ""
            outputs = [x for x in inputs if x.get("type") == "function_call_output"]
            results = {x["call_id"]: json.loads(x["output"]) if x["output"].startswith(("{", "[")) else x["output"] for x in outputs}
            if request["instructions"].startswith("Summarize"):
                output = [text("Goal: preserve KEEP_ME and NEVER_DELETE. Completed tools remain completed. User stopped job #1. Resume by inspecting current state.")]
            elif mode == "plan":
                check = s.plan_checks.get(s.plan_index)
                if check:
                    check(request, results)
                if s.plan_index < len(s.tool_plan):
                    output = s.tool_plan[s.plan_index]
                    s.plan_index += 1
                else:
                    output = [text("OK " + last)]
            elif mode == "diff-cases":
                if "replace" not in results:
                    output = [call("write", dict(path="replace.txt", content="one\nnew\nthree\nFOUR"), "replace")]
                elif "fragment" not in results:
                    output = [call("edit", dict(path="replace.txt", old="new", new=""), "fragment")]
                elif "empty" not in results:
                    output = [call("write", dict(path="replace.txt", content=""), "empty")]
                else:
                    output = [text("DIFF CASES DONE")]
            elif mode == "long-output":
                if not outputs:
                    output = [call("exec", dict(command="python3 -c \"print('BEGIN' + 'x' * 65536 + 'END')\""), "long")]
                else:
                    assert results["long"]["truncated"]
                    assert results["long"]["offset"] > 0
                    assert "BEGIN" not in results["long"]["output"]
                    assert "END" in results["long"]["output"]
                    assert Path(results["long"]["log_path"]).read_text().startswith("BEGIN")
                    output = [text("LONG OUTPUT CHECKED")]
            elif mode == "slow":
                if last == "WAIT":
                    s.waiting.set()
                    time.sleep(s.delay)
                output = [text("OK " + last)]
            elif mode == "auto":
                output = [call("read", dict(path="a.py"), "auto" + str(count))] if count <= s.auto_calls else [text("AUTO DONE")]
            elif mode == "paging":
                if not outputs:
                    output = [call("read", dict(path="long.txt"), "page1")]
                elif "page2" not in results:
                    assert results["page1"]["truncated"]
                    output = [call("read", dict(path="long.txt", offset=results["page1"]["next_offset"]), "page2")]
                else:
                    assert results["page1"]["content"] + results["page2"]["content"] == "Я" * 20000
                    assert not results["page2"]["truncated"]
                    output = [text("PAGING COMPLETE")]
            elif mode in ("skill", "skill-auto"):
                catalog = json.loads(request["instructions"].rsplit("\n", 1)[-1]) if "Available skills (name, description, path):" in request["instructions"] else {}
                pinned = [x["content"] for x in inputs if x.get("role") == "developer" and isinstance(x.get("content"), str) and x["content"].startswith("Active skill ")]
                ident = "skill-load-" + str(len(users))
                if last.startswith("PARTIAL SKILL") and "partial-skill" not in results:
                    output = [call("read", dict(path=catalog[s.skill_name]["path"], start_line=1, end_line=3), "partial-skill")]
                elif last.startswith(("LOAD SKILL", "USE SKILL")) and ident not in results and not pinned:
                    output = [call("read", dict(path=catalog[s.skill_name]["path"]), ident)]
                elif mode == "skill-auto" and "skill-extra-1" not in results:
                    output = [call("read", dict(path="a.py"), "skill-extra-1")]
                elif mode == "skill-auto" and "skill-extra-2" not in results:
                    output = [call("read", dict(path="a.py"), "skill-extra-2")]
                elif last.startswith("USE SKILL") and "skill-reference" not in results:
                    output = [call("read", dict(path=str(Path(catalog[s.skill_name]["path"]).parent / "references/note.txt")), "skill-reference")]
                elif last.startswith("USE SKILL") and "skill-script" not in results:
                    script = Path(catalog[s.skill_name]["path"]).parent / "scripts/run.sh"
                    output = [call("exec", dict(command="sh " + shlex.quote(str(script))), "skill-script")]
                else:
                    if ident in results and s.skill_error:
                        assert "error" in results[ident]
                    elif ident in results and s.skill_content is not None:
                        assert results[ident]["content"] == s.skill_content
                        assert not results[ident]["truncated"]
                    if mode == "skill-auto":
                        assert len(pinned) == 1 and s.skill_content in pinned[0]
                    if last.startswith("USE SKILL"):
                        assert results["skill-reference"]["content"] == "BUNDLED_REFERENCE"
                        assert results["skill-script"]["exit_code"] == 0
                    output = [text("OK " + last)]
            elif mode == "instructions-change":
                if not outputs:
                    output = [call("write", dict(path="AGENTS.md", content="TOOL_UPDATED_POLICY"), "instructions")]
                else:
                    assert "TOOL_UPDATED_POLICY" in request["instructions"]
                    assert "ORIGINAL_POLICY" not in request["instructions"]
                    output = [text("OK UPDATED INSTRUCTIONS")]
            elif mode == "two-jobs":
                if not outputs:
                    output = [call("exec", dict(command="python3 -u emit.py " + str(i), background=True), "bg" + str(i)) for i in (1, 2)]
                elif last == "CHECK LOGS":
                    if "inspect1" not in results:
                        output = [call("jobs", dict(action="output", id=i), "inspect" + str(i)) for i in (1, 2)]
                    else:
                        for i in (1, 2):
                            assert results["inspect" + str(i)]["output"] == "Привет " + str(i) + "\n\nlast " + str(i)
                        output = [text("OK RAW LOGS")]
                else:
                    output = [text("OK " + last)]
            elif mode == "basic":
                if "read" not in results:
                    output = [dict(type="reasoning", id="rs_fixture", summary=[], encrypted_content="opaque-fixture"),
                              call("read", dict(path="a.py"), "read"),
                              call("write", dict(path="new/utf8.txt", content="Привет\nsecond\n"), "write")]
                elif "bad" not in results:
                    assert results["read"]["content"] == "answer = 1\n"
                    output = [call("edit", dict(path="a.py", old="missing", new="2"), "bad")]
                elif "edit" not in results:
                    assert "error" in results["bad"]
                    output = [call("edit", dict(path="a.py", old="answer = 1", new="answer = 2"), "edit")]
                elif "exec" not in results:
                    output = [call("exec", dict(command="python3 verify.py"), "exec")]
                else:
                    assert results["exec"]["exit_code"] == 0
                    assert "CHECK PASSED" in results["exec"]["output"]
                    output = [text("Done. Привет.")]
            elif mode in ("jobs", "interrupt", "job-wait"):
                if "bg" not in results:
                    output = [call("exec", dict(command="python3 -u server.py", background=True), "bg")]
                    if mode == "job-wait":
                        output.extend([call("exec", dict(command="echo $$ > other.pid; exec sleep 300", background=True), "other"),
                                       call("jobs", dict(action="wait", id=1, timeout_ms=0), "waiting")])
                    if mode == "interrupt":
                        output.append(call("exec", dict(command="echo started > started; sleep 300"), "foreground"))
                elif "WAIT" in last:
                    s.waiting.set()
                    time.sleep(s.delay)
                    output = [text("WAIT FINISHED")]
                elif "CHECK STOP" in last:
                    assert any("User stopped job" in x.get("content", "") for x in inputs)
                    output = [text("STOP CONFIRMED")]
                elif "FOLLOWUP" in last:
                    if "status" not in results:
                        output = [call("jobs", dict(action="status", id=1), "status")]
                    else:
                        assert results["status"]["state"] == "running"
                        output = [text("BACKGROUND PRESERVED")]
                else:
                    output = [text("SERVER READY")]
            elif mode == "unknown":
                if "RESUME" in last:
                    assert any("Outcome unknown" in x["output"] for x in outputs)
                    output = [text("UNKNOWN RESULT PRESERVED")]
                else:
                    output = [call("exec", dict(command="echo once >> effects; echo $$ > group; sleep 300"), "unknown")]
            elif mode == "ambiguous":
                if not outputs:
                    output = [call("edit", dict(path="same.txt", old="same", new="changed"), "ambiguous")]
                else:
                    assert "Ambiguous" in results["ambiguous"]["error"]
                    output = [text("AMBIGUOUS PRESERVED")]
            elif mode == "resume":
                assert any(x.get("encrypted_content") == "opaque-fixture" for x in inputs)
                output = [text("RESUMED WITHOUT REPLAY")]
            else:
                output = [text("OK " + last)]
            for item in output:
                if s.standard_catalog:
                    item.pop("namespace", None)
                if item.get("type") == "message":
                    if s.ui_stream:
                        self.event(dict(type="response.output_item.added", item=item))
                    for part in item["content"]:
                        value = part["text"]
                        chunks = [value[i:i + 7] for i in range(0, len(value), 7)] if s.ui_stream else [value]
                        for chunk in chunks:
                            self.event(dict(type="response.output_text.delta", delta=chunk))
                            if s.ui_delta_hook:
                                s.ui_delta_hook(chunk)
            if not s.completed_only:
                for index, item in enumerate(output):
                    self.event(dict(type="response.output_item.done", output_index=index, item=item))
            response = dict(status="completed", output=output if s.completed_only else [], usage=s.usage)
            if s.response_reasoning is not None:
                response["reasoning"] = s.response_reasoning
            self.event(dict(type="response.completed", response=response))
        except (BrokenPipeError, ConnectionResetError):
            pass


class Process:
    def __init__(self, case, *arguments):
        self.case = case
        self.p = subprocess.Popen([str(BINARY), "-C", str(case.project), *getattr(case, "startup_args", ()), *arguments], env=case.env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.buffer = b""
        case.processes.append(self)

    def send(self, line):
        self.p.stdin.write(line.encode() + b"\n")
        self.p.stdin.flush()

    def wait(self, wanted, timeout=10):
        end = time.monotonic() + timeout
        needle = wanted.encode()
        while needle not in self.buffer and time.monotonic() < end:
            if select.select([self.p.stdout], [], [], .05)[0]:
                data = os.read(self.p.stdout.fileno(), 65536)
                if not data:
                    break
                self.buffer += data
        assert needle in self.buffer, (wanted, self.buffer.decode(errors="replace"))
        if wanted.startswith("OK ") or wanted in ("SERVER READY", "WAIT FINISHED", "STOP CONFIRMED", "BACKGROUND PRESERVED"):
            # A text delta is visible before response.completed. Wait for the
            # committed final item before issuing a command that requires idle.
            while time.monotonic() < end:
                try:
                    items = [r["data"] for r in self.case.records() if r["type"] == "item"]
                    if any(wanted in p.get("text", "") for i in items if i.get("role") == "assistant"
                           for p in i.get("content", []) if isinstance(p, dict)):
                        return self.buffer
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                time.sleep(.01)
            raise AssertionError("Response was displayed but never committed: " + wanted)
        return self.buffer

    def finish(self):
        if self.p.poll() is None:
            self.send("/quit")
        remaining, _ = self.p.communicate(timeout=5)
        self.buffer += remaining
        self.p.stdin.close()
        self.p.stdout.close()


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="harn-check-")
        self.processes = []
        self.path = Path(self.tmp.name)
        self.user_home = self.path / "home"
        self.user_home.mkdir()
        self.project = self.path / "project"
        self.project.mkdir()
        self.config = self.path / "config/harn"
        self.config.mkdir(parents=True)
        self.env = dict(os.environ, HOME=str(self.user_home), XDG_CONFIG_HOME=str(self.path / "config"), XDG_STATE_HOME=str(self.path / "state"), HARN_NO_BROWSER="1")
        self.env["HARN_FIXTURE_KEY"] = "NEVER_IN_JOURNAL"
        self.server = getattr(self, "fixture_class", Fixture)()
        self.env["HARN_API_BASE"] = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        (self.project / "a.py").write_text("answer = 1\n")
        (self.project / "a.py").chmod(0o755)
        (self.project / "verify.py").write_text("from a import answer\nassert answer == 2\nprint('CHECK PASSED')\n")
        (self.project / "server.py").write_text(
            "import http.server, pathlib, subprocess\n"
            "child = subprocess.Popen(['sleep', '300'])\n"
            "pathlib.Path('child.pid').write_text(str(child.pid))\n"
            "s = http.server.HTTPServer(('127.0.0.1', 0), http.server.SimpleHTTPRequestHandler)\n"
            "pathlib.Path('port').write_text(str(s.server_port))\n"
            "print('READY', flush=True)\ns.serve_forever()\n")

    def tearDown(self):
        for process in self.processes:
            if process.p.poll() is None:
                process.p.terminate()
                process.p.wait(timeout=5)
            process.p.stdin.close()
            process.p.stdout.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def run_batch(self, prompt, *extra, expected=0):
        p = subprocess.run([str(BINARY), "-C", str(self.project), *getattr(self, "startup_args", ()), *extra, "-p", prompt], env=self.env,
                           input=b"", capture_output=True, timeout=20)
        self.assertEqual(p.returncode, expected, p.stdout.decode(errors="replace") + p.stderr.decode(errors="replace"))
        return p

    def session(self):
        return sorted((self.path / "state/harn/sessions").iterdir())[-1]

    def records(self):
        # Poll only committed lines while the agent can still be writing the tail.
        data = (self.session() / "journal.jsonl").read_bytes()
        return [json.loads(line) for line in data.split(b"\n")[:-1]]

    def view_context(self):
        p = subprocess.run([str(BINARY), "-C", str(self.project), *getattr(self, "startup_args", ()), "--resume"], env=self.env,
                           input="/context\n/quit\n", capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(p.stdout.splitlines()), 3, p.stdout)
        return p.stdout

    def saved_requests(self):
        items, requests = [], []
        for record in self.records():
            if record["type"] == "item":
                items.append(record["data"])
            elif record["type"] == "request":
                entry = record["data"]
                request = dict(entry["request"])
                request["input"] = [items[x] if isinstance(x, int) else x for x in entry["selection"]]
                requests.append(request)
        return requests

    def wait_path(self, name):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            p = self.project / name
            if p.exists():
                return p
            time.sleep(.02)
        self.fail("No " + name)

    def write_skill(self, name="fixture-skill", body="FULL_SKILL_SENTINEL", scope="project", description="Use for the fixture task", directory=".agents"):
        root = self.project if scope == "project" else self.user_home
        path = root / directory / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: " + json.dumps(name) + "\ndescription: " + json.dumps(description) + "\n---\n" + body)
        return path

    def catalog(self, request=None):
        instructions = (request or self.server.requests[-1])["instructions"]
        return json.loads(instructions.rsplit("\n", 1)[-1]) if "Available skills (name, description, path):" in instructions else {}

    def selected_skills(self, request=None):
        selected = []
        for item in (request or self.server.requests[-1])["input"]:
            parts = item.get("content")
            if item.get("role") == "developer" and isinstance(parts, list):
                metadata = json.loads(parts[0]["text"])
                if metadata.get("user_selected"):
                    selected.append((metadata, parts[1]["text"]))
        return selected

    def prepare_background_logs(self):
        self.server.mode = "two-jobs"
        (self.project / "emit.py").write_text(
            "import pathlib, sys, time\n"
            "n = sys.argv[1]\n"
            "data = ('Привет ' + n + '\\n\\nlast ' + n).encode()\n"
            "for byte in data:\n"
            "    sys.stdout.buffer.write(bytes([byte])); sys.stdout.buffer.flush(); time.sleep(.002)\n"
            "pathlib.Path('ready' + n).touch()\n"
            "while True: time.sleep(.1)\n")

    def wait_background_logs(self):
        for i in (1, 2):
            self.wait_path("ready" + str(i))
            expected = "Привет " + str(i) + "\n\nlast " + str(i)
            log = self.session() / "jobs" / (str(i) + ".log")
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if log.read_bytes() == expected.encode():
                    break
                time.sleep(.02)
            self.assertEqual(log.read_bytes(), expected.encode())

    def assert_server_stopped(self):
        port = int((self.project / "port").read_text())
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    pass
            except OSError:
                break
            time.sleep(.02)
        else:
            self.fail("Server survived exit")
        pid = int((self.project / "child.pid").read_text())
        deadline = time.monotonic() + 4
        while Path("/proc/" + str(pid)).exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(Path("/proc/" + str(pid)).exists(), "Child process survived stop")

    def test_full_tool_cycle_journal_and_resume(self):
        result = self.run_batch("TASK basic")
        self.assertIn(b"Done", result.stdout)
        self.assertEqual((self.project / "a.py").read_text(), "answer = 2\n")
        self.assertEqual((self.project / "a.py").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.project / "new/utf8.txt").read_text(), "Привет\nsecond\n")
        content = (self.session() / "journal.jsonl").read_text()
        self.assertNotIn("NEVER_IN_JOURNAL", content)
        requests = [r["data"] for r in self.records() if r["type"] == "request"]
        self.assertEqual(len(requests), 5)
        first_requests = list(self.server.requests)
        self.assertEqual(self.saved_requests(), first_requests)
        self.assertEqual(json.loads((self.session() / "request.json").read_text()), first_requests[-1])
        self.server.mode = "resume"
        self.run_batch("CHECK RESUME", "--resume", self.session().name)
        self.assertEqual((self.project / "a.py").read_text(), "answer = 2\n")

    def test_batch_answer_and_tool_receipts_use_separate_streams(self):
        p = self.run_batch("TASK basic")
        self.assertEqual(p.stdout.decode(), "Done. Привет.\n")
        self.assertIn(b"CHECK PASSED", p.stderr)
        self.assertIn(b"exec #", p.stderr)
        self.assertNotIn(b"Exec #", p.stderr)
        self.assertIn(b"jobs #", p.stderr)
        self.assertEqual(p.stderr.decode().splitlines()[:4],
                         ["New session: " + self.session().name, "Project: " + str(self.project),
                          "Provider: fixture", "Model: fixture-model"])
        self.assertIn(b"@@ -1,1 +1,1 @@", p.stderr)
        self.assertIn(b"-answer = 1", p.stderr)
        self.assertIn(b"+answer = 2", p.stderr)
        self.assertIn(b"No exact match", p.stderr)

    @unittest.skipUnless(shutil.which("patch"), "Independent diff verification needs patch")
    def test_recorded_diffs_can_reverse_the_actual_changes(self):
        self.run_batch("TASK basic")
        changes = [r["data"] for r in self.records() if r["type"] == "change"]
        self.assertEqual([r["tool"] for r in changes], ["write", "edit"])
        for change in reversed(changes):
            p = subprocess.run(["patch", "--batch", "--reverse", "-p0"], input=change["diff"],
                               cwd=self.project, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, (p.stdout, p.stderr, change))
        self.assertEqual((self.project / "a.py").read_text(), "answer = 1\n")
        self.assertFalse((self.project / "new/utf8.txt").exists())

    @unittest.skipUnless(shutil.which("patch"), "Independent diff verification needs patch")
    def test_replacement_diffs_preserve_missing_newline_and_empty_files(self):
        self.server.mode = "diff-cases"
        original = "one\ntwo\nthree\nfour\n"
        path = self.project / "replace.txt"
        path.write_text(original)
        self.run_batch("DIFF CASES")
        self.assertEqual(path.read_text(), "")
        changes = [r["data"] for r in self.records() if r["type"] == "change"]
        self.assertEqual(len(changes), 3)
        for change in reversed(changes):
            p = subprocess.run(["patch", "--batch", "--reverse", "-p0"], input=change["diff"],
                               cwd=self.project, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, (p.stdout, p.stderr, change))
        self.assertEqual(path.read_text(), original)

    def test_context_view_preserves_separate_input_output_and_limit_source(self):
        self.server.mode = "echo"
        self.server.usage = dict(input_tokens=36000, output_tokens=100000, total_tokens=136000,
                                 input_tokens_details=dict(cached_tokens=32000))
        self.run_batch("MEASURE")
        usage = [r["data"] for r in self.records() if r["type"] == "usage"][-1]
        self.assertEqual(usage["usage"], self.server.usage)
        self.assertEqual(usage["limit_source"], "catalog")
        view = self.view_context()
        self.assertIn("~18.0% (36000/200000 saved), compact 160000", view)
        self.assertIn(str(self.session()), view)
        self.assertEqual(json.loads((self.session() / "request.json").read_text()), self.server.requests[-1])
        self.assertEqual(self.saved_requests(), self.server.requests)
        (self.project / "AGENTS.md").write_text("New project instructions")
        self.assertIn("Context: unmeasured", self.view_context())

    def test_context_does_not_present_fallback_as_a_reported_window(self):
        self.server.mode = "echo"
        self.server.window = None
        p = Process(self)
        try:
            p.send("/context")
            p.wait("catalog not loaded")
            self.assertEqual(self.server.requests, [])
            p.send("MEASURE UNKNOWN WINDOW")
            p.wait("OK MEASURE UNKNOWN WINDOW")
            p.buffer = b""
            p.send("/context")
            p.wait("fallback 200000, compact 160000")
            info = [r["data"] for r in self.records() if r["type"] == "model_info"][-1]
            self.assertNotIn("context_window", info["catalog"])
            self.assertEqual(info["limit_source"], "config.h fallback")
        finally:
            p.finish()

        self.assertIn("fallback 200000, compact 160000", self.view_context())

    def test_provider_effective_window_and_compaction_recommendation_are_preserved(self):
        self.server.mode = "echo"
        self.server.model_metadata = dict(effective_context_window_percent=95, auto_compact_token_limit=150000)
        self.run_batch("PROVIDER METADATA")
        info = [r["data"] for r in self.records() if r["type"] == "model_info"][-1]
        self.assertEqual(info["context_limit"], 190000)
        self.assertEqual(info["compact_limit"], 150000)
        self.assertEqual(info["compact_source"], "catalog")
        self.assertEqual(info["catalog"]["effective_context_window_percent"], 95)
        view = self.view_context()
        self.assertIn("190000 saved", view)
        self.assertIn("compact 150000", view)

    def test_skill_catalog_has_metadata_only_and_project_overrides_user(self):
        self.server.mode = "echo"
        self.write_skill(body="USER_BODY_ONLY", scope="user", description="User description")
        self.write_skill("user-only", scope="user", body="SECOND_BODY_ONLY")
        project = self.write_skill(body="PROJECT_BODY_ONLY", description="Project description")
        self.run_batch("INSPECT AVAILABLE SKILLS")
        catalog = self.catalog()
        self.assertEqual(set(catalog), {"fixture-skill", "user-only"})
        self.assertEqual(catalog["fixture-skill"]["path"], str(project))
        self.assertEqual(catalog["fixture-skill"]["description"], "Project description")
        for body in ("USER_BODY_ONLY", "SECOND_BODY_ONLY", "PROJECT_BODY_ONLY"):
            self.assertNotIn(body, self.server.requests[0]["instructions"])
        self.assertEqual([r for r in self.records() if r["type"] == "skill"], [])

    def test_skill_catalog_merges_agents_and_harn_with_scoped_precedence(self):
        self.server.mode = "echo"
        unique = {}
        shared = []
        for scope in ("user", "project"):
            for directory in (".agents", ".harn"):
                name = scope + "-" + directory[1:]
                unique[name] = self.write_skill(name, scope=scope, directory=directory)
                shared.append(self.write_skill("shared", scope=scope, directory=directory))
        self.write_skill("user-priority", scope="user")
        personal = self.write_skill("user-priority", scope="user", directory=".harn")
        self.write_skill("project-priority", scope="user", directory=".harn")
        project = self.write_skill("project-priority")
        self.run_batch("MERGED SKILL CATALOG")
        catalog = self.catalog()
        self.assertEqual(set(catalog), set(unique) | {"shared", "user-priority", "project-priority"})
        for name, path in unique.items():
            self.assertEqual(catalog[name]["path"], str(path))
            self.assertEqual(catalog[name]["source"], name.split("-", 1)[0])
        self.assertEqual(catalog["user-priority"]["path"], str(personal))
        self.assertEqual(catalog["project-priority"]["path"], str(project))
        for index, path in enumerate(reversed(shared)):
            if index:
                self.run_batch("REFRESH SKILL PRIORITY " + str(index), "--resume")
            self.assertEqual(self.catalog()["shared"]["path"], str(path))
            self.assertNotIn("FULL_SKILL_SENTINEL", self.server.requests[-1]["instructions"])
            path.unlink()
        self.run_batch("ALL SHARED SKILLS REMOVED", "--resume")
        self.assertNotIn("shared", self.catalog())
        self.assertTrue(set(unique).issubset(self.catalog()))
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_skill_catalog_refreshes_in_the_open_session(self):
        self.server.mode = "echo"
        p = Process(self)
        try:
            p.send("NO SKILLS YET")
            p.wait("OK NO SKILLS YET")
            self.assertEqual(self.catalog(), {})
            path = self.write_skill(description="First description")
            p.send("ADDED SKILL")
            p.wait("OK ADDED SKILL")
            self.assertEqual(self.catalog()["fixture-skill"]["description"], "First description")
            self.write_skill(description="Revised description")
            p.send("REVISED SKILL")
            p.wait("OK REVISED SKILL")
            self.assertEqual(self.catalog()["fixture-skill"]["description"], "Revised description")
            path.unlink()
            p.send("REMOVED SKILL")
            p.wait("OK REMOVED SKILL")
            self.assertEqual(self.catalog(), {})
        finally:
            p.finish()

    def test_skill_yaml_multiline_metadata_and_invalid_neighbors(self):
        self.server.mode = "echo"
        path = self.write_skill()
        path.write_bytes(('\ufeff---\r\nname: "fixture-skill"\r\ndescription: >-\r\n  First line\r\n  with Unicode: кириллица\r\nmetadata:\r\n  author: "A: B"\r\n---\r\nUNLOADED_BODY').encode())
        bad = self.write_skill("invalid")
        bad.write_text('---\nname: [broken\ndescription: invalid\n---\nBAD_BODY')
        self.write_skill("missing", description="")
        result = self.run_batch("INSPECT YAML SKILLS")
        self.assertEqual(set(self.catalog()), {"fixture-skill"})
        self.assertEqual(self.catalog()["fixture-skill"]["description"], "First line with Unicode: кириллица")
        self.assertIn(b"Cannot load skill", result.stderr)
        self.assertNotIn("UNLOADED_BODY", self.server.requests[0]["instructions"])

    def test_skill_activation_reads_full_text_and_records_original_request(self):
        self.server.mode = "skill"
        path = self.write_skill(body="FULL_SKILL_SENTINEL\n" + "Long instruction line\n" * 3000, directory=".harn")
        self.server.skill_content = path.read_text()
        result = self.run_batch("LOAD SKILL LARGE")
        self.assertIn(b"Using skill: fixture-skill", result.stderr)
        self.assertGreater(len(self.server.skill_content.encode()), 32768)
        events = [r["data"] for r in self.records() if r["type"] == "skill"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["path"], str(path))
        self.assertEqual(self.saved_requests(), self.server.requests)
        self.assertNotIn("FULL_SKILL_SENTINEL", self.server.requests[0]["instructions"])
        outputs = [x for x in self.server.requests[1]["input"] if x.get("type") == "function_call_output"]
        read = json.loads(outputs[-1]["output"])
        self.assertEqual(read["content"], self.server.skill_content)
        self.assertFalse(read["truncated"])

    def test_legacy_selected_snapshot_remains_available_after_resume_and_compaction(self):
        self.server.mode = "echo"
        self.run_batch("INITIAL")
        body = "LEGACY_SELECTED_RULE_581"
        metadata = dict(skill="legacy", path=str(self.project / "missing/SKILL.md"), user_selected=True)
        item = dict(role="developer", content=[dict(type="input_text", text=json.dumps(metadata)),
                                               dict(type="input_text", text=body)])
        with (self.session() / "journal.jsonl").open("a") as journal:
            journal.write(json.dumps(dict(type="item", data=item)) + "\n")
        p = Process(self, "--resume")
        try:
            for i in range(4):
                p.send("LEGACY FILL " + str(i))
                p.wait("OK LEGACY FILL " + str(i))
            p.send("/compact")
            p.wait("Context compacted:")
            p.send("CHECK LEGACY RULE")
            p.wait("OK CHECK LEGACY RULE")
            self.assertTrue(any(body in x.get("content", "")
                                for x in self.server.requests[-1]["input"]))
        finally:
            p.finish()
        self.run_batch("LEGACY RESTART", "--resume")
        self.assertTrue(any(body in x.get("content", "")
                            for x in self.server.requests[-1]["input"]))

    def test_partial_skill_read_does_not_activate_it(self):
        self.server.mode = "skill"
        self.write_skill()
        self.run_batch("PARTIAL SKILL HEADER")
        self.assertEqual([r for r in self.records() if r["type"] == "skill"], [])

    def test_symlinked_skill_can_read_resources_and_run_bundled_script(self):
        self.server.mode = "skill"
        external = self.path / "skill package"
        external.mkdir()
        path = external / "SKILL.md"
        path.write_text('---\nname: fixture-skill\ndescription: Use the bundled script\n---\nRead references/note.txt and run scripts/run.sh.')
        (external / "references").mkdir()
        (external / "references/note.txt").write_text("BUNDLED_REFERENCE")
        (external / "scripts").mkdir()
        (external / "scripts/run.sh").write_text("printf 'SCRIPT_RESULT' > skill-ran.txt\nprintf 'SKILL_SCRIPT_OK\\n'\n")
        base = self.project / ".harn/skills"
        base.mkdir(parents=True)
        (base / "fixture-skill").symlink_to(external, target_is_directory=True)
        self.run_batch("USE SKILL RESOURCES")
        self.assertEqual(self.catalog()["fixture-skill"]["path"], str(path))
        self.assertEqual((self.project / "skill-ran.txt").read_text(), "SCRIPT_RESULT")
        self.assertEqual(len([r for r in self.records() if r["type"] == "skill"]), 1)

    def test_skill_instructions_survive_automatic_compaction_without_a_summary_copy(self):
        self.server.mode = "skill-auto"
        path = self.write_skill(body="EXACT_SKILL_RULE_KEEP_794\nDo not paraphrase this rule.")
        self.server.skill_content = path.read_text()
        self.server.window = 1000
        self.server.usage = dict(input_tokens=900, output_tokens=10, total_tokens=910)
        self.run_batch("LOAD SKILL AUTOMATIC")
        self.assertTrue(any(r["type"] == "compact" for r in self.records()))
        request = self.server.requests[-1]
        pins = [x["content"] for x in request["input"] if x.get("role") == "developer" and x.get("content", "").startswith("Active skill ")]
        self.assertEqual(len(pins), 1)
        self.assertIn(self.server.skill_content, pins[0])
        self.assertNotIn("EXACT_SKILL_RULE_KEEP_794", (self.session() / "summary.txt").read_text())
        calls = [x["call_id"] for x in request["input"] if x.get("type") == "function_call"]
        results = [x["call_id"] for x in request["input"] if x.get("type") == "function_call_output"]
        self.assertEqual(calls, results)
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_skill_snapshot_survives_repeated_compaction_restart_and_missing_source(self):
        self.server.mode = "skill"
        path = self.write_skill(body="DURABLE_SKILL_SNAPSHOT_142")
        original = path.read_text()
        p = Process(self)
        try:
            p.send("LOAD SKILL DURABLE")
            p.wait("OK LOAD SKILL DURABLE")
            self.server.mode = "echo"
            path.unlink()
            for round_id in range(2):
                for i in range(3):
                    prompt = "FILL " + str(round_id) + " " + str(i)
                    p.send(prompt)
                    p.wait("OK " + prompt)
                p.buffer = b""
                p.send("/compact")
                p.wait("Context compacted:")
            p.send("AFTER SECOND COMPACTION")
            p.wait("OK AFTER SECOND COMPACTION")
            pins = [x for x in self.server.requests[-1]["input"] if x.get("role") == "developer" and original in x.get("content", "")]
            self.assertEqual(len(pins), 1)
        finally:
            p.finish()
        before = len([r for r in self.records() if r["type"] == "skill"])
        self.run_batch("AFTER SNAPSHOT RESTART", "--resume")
        self.assertEqual(len([r for r in self.records() if r["type"] == "skill"]), before)
        self.assertTrue(any(original in x.get("content", "") for x in self.server.requests[-1]["input"]))
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_skill_reload_replaces_the_snapshot_used_after_compaction(self):
        self.server.mode = "skill"
        path = self.write_skill(body="OLD_SKILL_BODY_925")
        p = Process(self)
        try:
            p.send("LOAD SKILL OLD")
            p.wait("OK LOAD SKILL OLD")
            self.write_skill(body="NEW_SKILL_BODY_237")
            p.send("LOAD SKILL NEW")
            p.wait("OK LOAD SKILL NEW")
            self.server.mode = "echo"
            for i in range(3):
                p.send("AFTER RELOAD " + str(i))
                p.wait("OK AFTER RELOAD " + str(i))
            p.buffer = b""
            p.send("/compact")
            p.wait("Context compacted:")
            p.send("CHECK NEW SNAPSHOT")
            p.wait("OK CHECK NEW SNAPSHOT")
            content = json.dumps(self.server.requests[-1]["input"])
            self.assertIn("NEW_SKILL_BODY_237", content)
            self.assertNotIn("OLD_SKILL_BODY_925", content)
        finally:
            p.finish()

    def test_skill_recovery_handles_exit_between_compaction_and_preservation(self):
        self.server.mode = "skill"
        path = self.write_skill(body="RECOVER_EXACT_SKILL_BODY_472")
        original = path.read_text()
        p = Process(self)
        try:
            p.send("LOAD SKILL CRASH")
            p.wait("OK LOAD SKILL CRASH")
            self.server.mode = "echo"
            for i in range(3):
                p.send("PREPARE CRASH " + str(i))
                p.wait("OK PREPARE CRASH " + str(i))
            p.send("/compact")
            p.wait("Context compacted:")
        finally:
            p.finish()
        records = self.records()
        end = next(i for i, r in enumerate(records) if r["type"] == "compact")
        records = [r for r in records[:end + 1] if r["type"] != "skill"]
        journal = self.session() / "journal.jsonl"
        journal.write_text("".join(json.dumps(r) + "\n" for r in records))
        path.unlink()
        self.run_batch("RECOVER COMPACTED SKILL", "--resume")
        self.assertTrue(any(original in x.get("content", "") for x in self.server.requests[-1]["input"]))
        self.assertTrue(any(r["type"] == "skill" and "pin" in r["data"] for r in self.records()))

    def test_failed_skill_read_is_not_activated(self):
        self.server.mode = "skill"
        self.server.skill_error = True
        path = self.write_skill()
        with path.open("ab") as f:
            f.write(b"\xff")
        result = self.run_batch("LOAD SKILL INVALID BODY")
        self.assertIn(b"Skill instructions must be UTF-8 text", result.stderr)
        self.assertEqual([r for r in self.records() if r["type"] == "skill"], [])

    def test_multiple_active_skills_survive_compaction_without_duplicate_pins(self):
        self.server.mode = "skill"
        originals = [self.write_skill(name, body="BODY_" + name).read_text() for name in ("first", "second")]
        p = Process(self)
        try:
            for name in ("first", "second"):
                self.server.skill_name = name
                p.send("LOAD SKILL " + name)
                p.wait("OK LOAD SKILL " + name)
            self.server.mode = "echo"
            for i in range(3):
                p.send("MULTI FILL " + str(i))
                p.wait("OK MULTI FILL " + str(i))
            p.send("/compact")
            p.wait("Context compacted:")
            p.send("CHECK BOTH SKILLS")
            p.wait("OK CHECK BOTH SKILLS")
            pins = [x["content"] for x in self.server.requests[-1]["input"] if x.get("role") == "developer" and x.get("content", "").startswith("Active skill ")]
            self.assertEqual(len(pins), 2)
            for original in originals:
                self.assertEqual(sum(original in pin for pin in pins), 1)
            self.assertEqual(self.saved_requests(), self.server.requests)
        finally:
            p.finish()

    def test_live_project_instructions_follow_changes_creation_and_removal(self):
        self.server.mode = "echo"
        path = self.project / "AGENTS.md"
        path.write_text("ORIGINAL_POLICY")
        p = Process(self)
        try:
            p.send("FIRST POLICY")
            p.wait("OK FIRST POLICY")
            self.assertIn("ORIGINAL_POLICY", self.server.requests[-1]["instructions"])
            path.write_text("REVISED_POLICY")
            p.send("SECOND POLICY")
            p.wait("OK SECOND POLICY")
            current = self.server.requests[-1]["instructions"]
            self.assertIn("REVISED_POLICY", current)
            self.assertNotIn("ORIGINAL_POLICY", current)
            path.unlink()
            p.send("REMOVED POLICY")
            p.wait("OK REMOVED POLICY")
            self.assertNotIn("Project instructions from AGENTS.md", self.server.requests[-1]["instructions"])
            path.write_text("NEW_POLICY")
            p.send("NEW POLICY")
            p.wait("OK NEW POLICY")
            self.assertIn("NEW_POLICY", self.server.requests[-1]["instructions"])
            self.assertEqual(self.saved_requests(), self.server.requests)
        finally:
            p.finish()

    def test_global_instructions_use_only_harn_config_and_refresh_on_resume(self):
        self.server.mode = "echo"
        old = self.user_home / ".agents/AGENTS.md"
        old.parent.mkdir()
        old.write_text("IGNORED_AGENTS_GLOBAL_318")
        home_global = self.user_home / ".config/harn/AGENTS.md"
        home_global.parent.mkdir(parents=True)
        home_global.write_text("IGNORED_HOME_GLOBAL_627")
        current = self.config / "AGENTS.md"
        p = Process(self)
        try:
            p.send("NO CONFIG INSTRUCTIONS")
            p.wait("OK NO CONFIG INSTRUCTIONS")
            current.write_text("HARN_GLOBAL_FIRST_946")
            p.send("CREATED CONFIG INSTRUCTIONS")
            p.wait("OK CREATED CONFIG INSTRUCTIONS")
            self.assertIn("HARN_GLOBAL_FIRST_946", self.server.requests[-1]["instructions"])
            current.write_text("HARN_GLOBAL_REVISED_512")
            p.send("REVISED CONFIG INSTRUCTIONS")
            p.wait("OK REVISED CONFIG INSTRUCTIONS")
            self.assertIn("HARN_GLOBAL_REVISED_512", self.server.requests[-1]["instructions"])
            self.assertNotIn("HARN_GLOBAL_FIRST_946", self.server.requests[-1]["instructions"])
        finally:
            p.finish()
        self.run_batch("RESUMED CONFIG INSTRUCTIONS", "--resume")
        self.assertIn("HARN_GLOBAL_REVISED_512", self.server.requests[-1]["instructions"])
        current.unlink()
        self.run_batch("REMOVED CONFIG INSTRUCTIONS", "--resume")
        self.assertNotIn("HARN_GLOBAL_REVISED_512", self.server.requests[-1]["instructions"])
        for request in self.server.requests:
            self.assertNotIn("IGNORED_AGENTS_GLOBAL_318", request["instructions"])
            self.assertNotIn("IGNORED_HOME_GLOBAL_627", request["instructions"])
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_global_instructions_default_to_home_config_when_xdg_is_unset_or_empty(self):
        self.server.mode = "echo"
        config = self.user_home / ".config/harn"
        config.mkdir(parents=True)
        global_file = config / "AGENTS.md"
        global_file.write_text("HOME_CONFIG_GLOBAL_753")
        for value in (None, ""):
            with self.subTest(xdg_config_home=value):
                if value is None:
                    self.env.pop("XDG_CONFIG_HOME", None)
                else:
                    self.env["XDG_CONFIG_HOME"] = value
                self.run_batch("DEFAULT CONFIG INSTRUCTIONS")
                self.assertIn("HOME_CONFIG_GLOBAL_753", self.server.requests[-1]["instructions"])
                sources = [r["data"] for r in self.records() if r["type"] == "instructions"][-1]
                self.assertEqual(sources["global"]["path"], str(global_file))

    def test_unreadable_global_config_blocks_turn_but_not_session_listing(self):
        (self.config / "AGENTS.md").mkdir()
        result = self.run_batch("UNREADABLE GLOBAL", expected=1)
        self.assertIn(b"Cannot read AGENTS.md", result.stderr)
        self.assertEqual(self.server.requests, [])
        listing = subprocess.run([str(BINARY), "-C", str(self.project), "--sessions"],
                                 env=self.env, capture_output=True, timeout=5)
        self.assertEqual(listing.returncode, 0, listing.stderr)

    def test_global_parent_and_project_instruction_chain_has_scopes_and_source_records(self):
        self.server.mode = "echo"
        global_file = self.config / "AGENTS.md"
        global_file.write_text("GLOBAL_RULE_591")
        (self.path / "AGENTS.md").write_text("PARENT_RULE_728")
        (self.project / "AGENTS.md").write_text("PROJECT_RULE_349")
        self.run_batch("CHECK INSTRUCTION CHAIN")
        instructions = self.server.requests[0]["instructions"]
        self.assertLess(instructions.index("GLOBAL_RULE_591"), instructions.index("PARENT_RULE_728"))
        self.assertLess(instructions.index("PARENT_RULE_728"), instructions.index("PROJECT_RULE_349"))
        sources = [r["data"] for r in self.records() if r["type"] == "instructions"][-1]
        self.assertEqual(sources["global"]["path"], str(global_file))
        self.assertEqual(sources[str(self.path)]["content"], "PARENT_RULE_728")
        self.assertEqual(sources[str(self.project)]["content"], "PROJECT_RULE_349")
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_global_instructions_can_be_shared_by_a_symlink(self):
        self.server.mode = "echo"
        existing = self.user_home / ".codex/AGENTS.md"
        existing.parent.mkdir()
        existing.write_text("SHARED_GLOBAL_RULE_369")
        shared = self.config / "AGENTS.md"
        shared.symlink_to(existing)
        self.run_batch("SHARED GLOBAL RULES")
        self.assertIn("SHARED_GLOBAL_RULE_369", self.server.requests[0]["instructions"])
        sources = [r["data"] for r in self.records() if r["type"] == "instructions"][-1]
        self.assertEqual(sources["global"]["path"], str(shared))

    def test_global_source_is_not_repeated_when_its_directory_or_alias_is_visited(self):
        global_file = self.config / "AGENTS.md"
        global_file.write_text("ONE_GLOBAL_SOURCE_731")
        alias = self.project / "policy"
        alias.mkdir()
        (alias / "AGENTS.md").symlink_to(global_file)
        (alias / "notes.txt").write_text("notes")
        self.server.mode = "plan"
        self.server.tool_plan = [
            [call("read", dict(path=str(global_file)), "personal"),
             call("read", dict(path="policy/notes.txt"), "alias")],
            [text("CHECKED GLOBAL SOURCES")],
        ]
        result = self.run_batch("VISIT PERSONAL AND ALIASED RULES")
        for request in self.server.requests:
            self.assertEqual(request["instructions"].count("ONE_GLOBAL_SOURCE_731"), 1)
        sources = [r["data"] for r in self.records() if r["type"] == "instructions"][-1]
        self.assertIn("global", sources)
        self.assertNotIn(str(global_file.parent), sources)
        self.assertNotIn(str(alias), sources)
        self.assertEqual(result.stderr.count(("read instructions: " + str(global_file)).encode()), 1)
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_nested_rules_are_loaded_on_read_and_do_not_load_untouched_branches(self):
        front = self.project / "frontend"
        back = self.project / "backend"
        front.mkdir()
        back.mkdir()
        (front / "AGENTS.md").write_text("FRONTEND_ONLY_438")
        (back / "AGENTS.md").write_text("BACKEND_ONLY_749")
        (front / "app.txt").write_text("original")
        self.server.mode = "plan"
        self.server.tool_plan = [[call("read", dict(path="frontend/app.txt"), "front-read")]]
        self.run_batch("READ FRONTEND")
        self.assertNotIn("FRONTEND_ONLY_438", self.server.requests[0]["instructions"])
        self.assertIn("FRONTEND_ONLY_438", self.server.requests[1]["instructions"])
        self.assertIn("Scope: " + str(front), self.server.requests[1]["instructions"])
        self.assertNotIn("BACKEND_ONLY_749", self.server.requests[1]["instructions"])

    def test_new_nested_rules_hold_write_before_touching_the_file_or_creating_parents(self):
        front = self.project / "frontend"
        front.mkdir()
        (front / "AGENTS.md").write_text("Use the revised content for new files.")
        path = "frontend/./new/../new/deep/note.txt"
        self.server.mode = "plan"
        self.server.tool_plan = [[call("write", dict(path=path, content="stale"), "stale-write")],
                                 [call("write", dict(path=path, content="revised"), "revised-write")]]
        def before_retry(request, results):
            self.assertTrue(results["stale-write"]["instructions_changed"])
            self.assertFalse((front / "new").exists())
            self.assertIn("Use the revised content", request["instructions"])
        self.server.plan_checks[1] = before_retry
        self.run_batch("WRITE AFTER LEARNING RULES")
        self.assertEqual((front / "new/deep/note.txt").read_text(), "revised")
        changes = [r["data"] for r in self.records() if r["type"] == "change"]
        self.assertEqual(len(changes), 1)
        self.assertNotIn("+stale", changes[0]["diff"])

    def test_read_and_edit_in_one_batch_require_a_new_model_request_for_nested_rules(self):
        front = self.project / "frontend"
        front.mkdir()
        (front / "AGENTS.md").write_text("Apply the newly discovered edit policy.")
        file = front / "app.txt"
        file.write_text("before")
        self.server.mode = "plan"
        self.server.tool_plan = [
            [call("read", dict(path="frontend/app.txt"), "scoped-read"),
             call("edit", dict(path="frontend/app.txt", old="before", new="stale"), "stale-edit")],
            [call("edit", dict(path="frontend/app.txt", old="before", new="after"), "fresh-edit")]]
        def before_retry(request, results):
            self.assertEqual(results["scoped-read"]["content"], "before")
            self.assertTrue(results["stale-edit"]["instructions_changed"])
            self.assertEqual(file.read_text(), "before")
        self.server.plan_checks[1] = before_retry
        self.run_batch("READ AND EDIT WITH NEW RULES")
        self.assertEqual(file.read_text(), "after")

    def test_unrelated_scoped_rules_do_not_hold_writes_to_a_sibling_with_a_similar_name(self):
        front = self.project / "frontend"
        other = self.project / "frontend-extra"
        front.mkdir()
        other.mkdir()
        (front / "AGENTS.md").write_text("FRONTEND_SCOPED_834")
        (front / "app.txt").write_text("front")
        self.server.mode = "plan"
        self.server.tool_plan = [[call("read", dict(path="frontend/app.txt"), "front"),
                                 call("write", dict(path="frontend-extra/app.txt", content="other"), "other")]]
        self.run_batch("SCOPED SIBLING WRITE")
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual((other / "app.txt").read_text(), "other")
        result = [x for x in self.server.requests[-1]["input"] if x.get("call_id") == "other" and x.get("type") == "function_call_output"][0]
        self.assertNotIn("error", json.loads(result["output"]))

    def test_symlink_targets_use_the_rules_of_the_actual_destination(self):
        external = self.path / "other package"
        external.mkdir()
        (external / "AGENTS.md").write_text("ACTUAL_DESTINATION_RULE_624")
        target = external / "target.txt"
        target.write_text("before")
        (self.project / "link.txt").symlink_to(target)
        (self.project / "alias").symlink_to(external, target_is_directory=True)
        self.server.mode = "plan"
        self.server.tool_plan = [
            [call("write", dict(path="link.txt", content="stale"), "link-old"),
             call("write", dict(path="alias/new/note.txt", content="stale"), "alias-old")],
            [call("write", dict(path="link.txt", content="after"), "link-new"),
             call("write", dict(path="alias/new/note.txt", content="created"), "alias-new")]]
        def before_retry(request, results):
            self.assertTrue(results["link-old"]["instructions_changed"])
            self.assertTrue(results["alias-old"]["instructions_changed"])
            self.assertEqual(target.read_text(), "before")
            self.assertFalse((external / "new").exists())
            self.assertIn("ACTUAL_DESTINATION_RULE_624", request["instructions"])
        self.server.plan_checks[1] = before_retry
        self.run_batch("SYMLINK DESTINATION")
        self.assertTrue((self.project / "link.txt").is_symlink())
        self.assertEqual(target.read_text(), "after")
        self.assertEqual((external / "new/note.txt").read_text(), "created")

    def test_changed_rules_during_a_model_request_hold_the_prepared_write(self):
        policy = self.project / "AGENTS.md"
        policy.write_text("ORIGINAL_IN_FLIGHT_POLICY_174")
        self.server.mode = "plan"
        self.server.tool_plan = [[call("write", dict(path="a.py", content="stale"), "old")],
                                 [call("write", dict(path="a.py", content="fresh"), "new")]]
        def change_during_request(request, results):
            self.assertIn("ORIGINAL_IN_FLIGHT_POLICY_174", request["instructions"])
            policy.write_text("REVISED_IN_FLIGHT_POLICY_672")
        def before_retry(request, results):
            self.assertTrue(results["old"]["instructions_changed"])
            self.assertEqual((self.project / "a.py").read_text(), "answer = 1\n")
            self.assertIn("REVISED_IN_FLIGHT_POLICY_672", request["instructions"])
        self.server.plan_checks = {0: change_during_request, 1: before_retry}
        self.run_batch("CONCURRENT POLICY UPDATE")
        self.assertEqual((self.project / "a.py").read_text(), "fresh")

    def test_unreadable_nested_rules_leave_other_scopes_usable(self):
        front = self.project / "frontend"
        front.mkdir()
        (front / "AGENTS.md").mkdir()
        (front / "app.txt").write_text("front")
        self.server.mode = "plan"
        self.server.tool_plan = [[call("read", dict(path="frontend/app.txt"), "unreadable")],
                                 [call("write", dict(path="other.txt", content="allowed"), "other")]]
        def inspect_error(request, results):
            self.assertIn("error", results["unreadable"])
            self.assertIn("Instructions unavailable", request["instructions"])
        self.server.plan_checks[1] = inspect_error
        self.run_batch("UNREADABLE SUBDIRECTORY")
        self.assertEqual((self.project / "other.txt").read_text(), "allowed")

    def test_nested_rules_are_refreshed_after_resume_and_removed_when_deleted(self):
        front = self.project / "frontend"
        front.mkdir()
        policy = front / "AGENTS.md"
        policy.write_text("ORIGINAL_NESTED_POLICY_927")
        (front / "app.txt").write_text("front")
        self.server.mode = "plan"
        self.server.tool_plan = [[call("read", dict(path="frontend/app.txt"), "front")]]
        self.run_batch("DISCOVER NESTED RULES")
        self.assertIn("~", self.view_context())
        self.server.mode = "echo"
        policy.write_text("UPDATED_NESTED_POLICY_428")
        self.run_batch("RESUME WITH CURRENT RULES", "--resume")
        self.assertIn("UPDATED_NESTED_POLICY_428", self.server.requests[-1]["instructions"])
        self.assertNotIn("ORIGINAL_NESTED_POLICY_927", self.server.requests[-1]["instructions"])
        policy.unlink()
        self.run_batch("RESUME WITHOUT DELETED RULES", "--resume")
        self.assertNotIn("UPDATED_NESTED_POLICY_428", self.server.requests[-1]["instructions"])
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_full_nested_rules_survive_compaction_outside_the_summary(self):
        front = self.project / "frontend"
        front.mkdir()
        policy = front / "AGENTS.md"
        content = "POLICY_START_629\n" + "Keep this long instruction line.\n" * 1500 + "POLICY_END_817"
        policy.write_text(content)
        (front / "app.txt").write_text("front")
        self.server.mode = "plan"
        self.server.tool_plan = [[call("read", dict(path="frontend/app.txt"), "front")]]
        p = Process(self)
        try:
            p.send("DISCOVER LONG RULES")
            p.wait("OK DISCOVER LONG RULES")
            self.server.mode = "echo"
            for i in range(3):
                p.send("LONG RULES FILL " + str(i))
                p.wait("OK LONG RULES FILL " + str(i))
            p.send("/compact")
            p.wait("Context compacted:")
            p.send("AFTER POLICY COMPACTION")
            p.wait("OK AFTER POLICY COMPACTION")
            self.assertIn(content, self.server.requests[-1]["instructions"])
            self.assertNotIn("POLICY_END_817", (self.session() / "summary.txt").read_text())
            self.assertEqual(self.saved_requests(), self.server.requests)
        finally:
            p.finish()

    def test_project_instructions_refresh_after_a_tool_changes_them(self):
        self.server.mode = "instructions-change"
        (self.project / "AGENTS.md").write_text("ORIGINAL_POLICY")
        self.run_batch("UPDATE INSTRUCTIONS")
        self.assertEqual(len(self.server.requests), 2)
        self.assertIn("ORIGINAL_POLICY", self.server.requests[0]["instructions"])
        self.assertIn("TOOL_UPDATED_POLICY", self.server.requests[1]["instructions"])

    def test_unreadable_project_instructions_can_be_corrected_in_the_open_session(self):
        self.server.mode = "echo"
        p = Process(self)
        path = self.project / "AGENTS.md"
        try:
            p.send("BEFORE BAD POLICY")
            p.wait("OK BEFORE BAD POLICY")
            path.mkdir()
            before = len(self.server.requests)
            p.send("BAD POLICY")
            p.wait("Cannot read AGENTS.md")
            self.assertEqual(len(self.server.requests), before)
            listing = subprocess.run([str(BINARY), "-C", str(self.project), "--sessions"],
                                     env=self.env, capture_output=True, timeout=5)
            self.assertEqual(listing.returncode, 0, listing.stderr)
            self.assertIn(self.session().name.encode(), listing.stdout)
            path.rmdir()
            path.write_text("RECOVERED_POLICY")
            p.send("AFTER BAD POLICY")
            p.wait("OK AFTER BAD POLICY")
            self.assertIn("RECOVERED_POLICY", self.server.requests[-1]["instructions"])
        finally:
            p.finish()

    def test_live_catalog_reuses_saved_data_during_transient_failures_and_recovers(self):
        self.server.mode = "echo"
        p = Process(self)
        try:
            p.send("FRESH CATALOG")
            p.wait("OK FRESH CATALOG")
            for status in (503, 429, 0):
                self.server.model_status = status
                p.send("CACHED " + str(status))
                p.wait("OK CACHED " + str(status))
                self.assertIn(b"Using saved model data", p.buffer)
                usage = [r["data"] for r in self.records() if r["type"] == "usage"][-1]
                self.assertEqual(usage["limit_source"], "catalog (saved)")
                self.assertEqual(usage["context_limit"], 200000)
            self.server.model_status = 200
            self.server.window = 100000
            p.send("RECOVERED CATALOG")
            p.wait("OK RECOVERED CATALOG")
            usage = [r["data"] for r in self.records() if r["type"] == "usage"][-1]
            self.assertEqual(usage["limit_source"], "catalog")
            self.assertEqual(usage["context_limit"], 100000)
            errors = [r["data"] for r in self.records() if r["type"] == "model_catalog_error"]
            self.assertEqual([r["http_status"] for r in errors], [503, 429, 0])
            self.assertTrue(all(r["saved"] for r in errors))
        finally:
            p.finish()

    def test_catalog_reuse_after_restart_preserves_provider_budget_and_trigger(self):
        self.server.mode = "echo"
        self.server.model_metadata = dict(effective_context_window_percent=95, auto_compact_token_limit=150000)
        self.run_batch("INITIAL CATALOG")
        self.server.model_status = 503
        result = self.run_batch("RESUMED WITH SAVED CATALOG", "--resume")
        self.assertIn(b"Using saved model data", result.stderr)
        info = [r["data"] for r in self.records() if r["type"] == "model_info"][-1]
        self.assertEqual(info["context_limit"], 190000)
        self.assertEqual(info["compact_limit"], 150000)
        self.assertEqual(info["limit_source"], "catalog (saved)")
        self.assertEqual(info["compact_source"], "catalog (saved)")

    def test_catalog_failures_without_matching_data_do_not_send_requests(self):
        self.server.mode = "echo"
        self.server.model_status = 503
        self.run_batch("NO SAVED CATALOG", expected=1)
        self.assertEqual(self.server.requests, [])
        self.server.model_status = 200
        self.run_batch("SAVE CATALOG", "--resume")
        before = len(self.server.requests)
        self.server.model_status = 503
        self.run_batch("OTHER MODEL", "--resume", "-m", "unknown-fixture", expected=1)
        self.assertEqual(len(self.server.requests), before)

    def test_catalog_reuse_does_not_override_auth_errors_invalid_data_or_model_removal(self):
        self.server.mode = "echo"
        self.run_batch("SAVE CATALOG")
        before = len(self.server.requests)
        for status in (401, 403, 404):
            self.server.model_status = status
            result = self.run_batch("CATALOG DENIED", "--resume", expected=1)
            self.assertNotIn(b"Using saved model data", result.stderr)
            self.assertEqual(len(self.server.requests), before)
        self.server.model_status = 200
        self.server.model_bad = True
        self.run_batch("INVALID CATALOG", "--resume", expected=1)
        self.assertEqual(len(self.server.requests), before)
        self.server.model_bad = False
        self.server.model_present = False
        p = Process(self, "--resume")
        try:
            p.send("REMOVED MODEL")
            p.wait("is unavailable")
            self.server.model_status = 503
            p.send("STILL REMOVED")
            p.wait("Could not load models")
            self.assertEqual(len(self.server.requests), before)
        finally:
            p.finish()

        self.run_batch("REMOVAL AFTER RESTART", "--resume", expected=1)
        self.assertEqual(len(self.server.requests), before)

    def test_interrupting_catalog_refresh_does_not_start_a_cached_request(self):
        self.server.mode = "echo"
        p = Process(self)
        try:
            p.send("WARM CATALOG")
            p.wait("OK WARM CATALOG")
            before = len(self.server.requests)
            self.server.model_delay = 1
            p.send("INTERRUPT CATALOG")
            self.assertTrue(self.server.model_waiting.wait(4))
            p.p.send_signal(signal.SIGINT)
            p.wait("Stopped by you")
            self.assertEqual(len(self.server.requests), before)
            self.assertFalse([r["data"] for r in self.records() if r["type"] == "model_catalog_error"][-1]["saved"])
        finally:
            p.finish()

    def test_background_log_lines_identify_the_job_and_preserve_raw_logs(self):
        self.prepare_background_logs()
        p = Process(self)
        try:
            p.send("BACKGROUND LOGS")
            p.wait("OK BACKGROUND LOGS")
            self.wait_background_logs()
            p.buffer = b""
            p.send("/jobs 1")
            p.send("/jobs 2")
            p.wait("jobs #2: last 2")
            shown = p.buffer.decode()
            for i in (1, 2):
                self.assertIn("jobs #" + str(i) + ": Привет " + str(i) + "\njobs #" + str(i) + ": \njobs #" + str(i) + ": last " + str(i) + "\n", shown)
            p.send("CHECK LOGS")
            p.wait("OK RAW LOGS")
        finally:
            p.finish()

    def test_catalog_is_refreshed_between_turns_and_by_the_model_command(self):
        self.server.mode = "echo"
        p = Process(self)
        try:
            p.send("FIRST WINDOW")
            p.wait("OK FIRST WINDOW")
            before = self.server.model_fetches
            self.server.window = 100000
            self.server.model_metadata = dict(effective_context_window_percent=90, auto_compact_token_limit=70000)
            p.send("SECOND WINDOW")
            p.wait("OK SECOND WINDOW")
            self.assertGreater(self.server.model_fetches, before)
            usage = [r["data"] for r in self.records() if r["type"] == "usage"][-1]
            self.assertEqual(usage["context_limit"], 90000)
            self.assertEqual(usage["model_info"]["compact_limit"], 70000)
            before = self.server.model_fetches
            self.server.window = 80000
            p.send("/model")
            deadline = time.monotonic() + 5
            while self.server.model_fetches == before and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertGreater(self.server.model_fetches, before)
        finally:
            p.finish()

    def test_provider_compaction_threshold_drives_the_tool_loop(self):
        self.server.mode = "auto"
        self.server.window = 1000
        self.server.model_metadata = dict(auto_compact_token_limit=50)
        self.server.usage = dict(input_tokens=60, output_tokens=10, total_tokens=70)
        self.run_batch("PROVIDER COMPACT")
        self.assertTrue(any(r["type"] == "compact" for r in self.records()))
        self.assertTrue(any(r["type"] == "compaction_usage" for r in self.records()))
        info = [r["data"] for r in self.records() if r["type"] == "model_info"][-1]
        self.assertEqual(info["compact_limit"], 50)

    def test_auto_compaction_uses_the_displayed_input_measurement(self):
        self.server.mode = "echo"
        self.server.usage = dict(input_tokens=36000, output_tokens=140000, total_tokens=176000)
        p = Process(self)
        try:
            for i in range(5):
                p.send("LOW INPUT " + str(i))
                p.wait("OK LOW INPUT " + str(i))
            self.assertFalse(any(r["type"] == "compact" for r in self.records()))
        finally:
            p.finish()

    def test_legacy_usage_remains_unknown_and_old_cli_budget_is_not_restored(self):
        self.server.mode = "echo"
        self.run_batch("BUDGET")
        path = self.session() / "journal.jsonl"
        records = self.records()
        for record in records:
            if record["type"] == "usage":
                record["data"]["context_limit"] = 12345
                record["data"]["limit_source"] = "command line"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        self.assertNotIn("12345", self.view_context())
        for record in records:
            if record["type"] == "usage":
                record["data"] = 136000
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        view = self.view_context()
        self.assertIn("Context: unmeasured", view)
        self.assertNotIn("~", view)
        self.assertTrue(any(r["type"] == "usage" and r["data"] == 136000 for r in self.records()))

    def test_catalog_without_effort_metadata_only_allows_default(self):
        self.server.mode = "echo"
        self.server.model_metadata = dict(supported_reasoning_levels=None)
        self.run_batch("DEFAULT ONLY", "--effort", "default")
        self.assertNotIn("reasoning", self.server.requests[-1])
        result = self.run_batch("UNSUPPORTED", "--effort", "high", expected=1)
        self.assertIn(b"Unsupported effort", result.stderr)
        self.assertEqual(len(self.server.requests), 1)

    def test_effort_choices_and_preferences_follow_model_and_resume(self):
        self.server.mode = "echo"
        field = "id" if self.server.standard_catalog else "slug"
        self.server.extra_models = [{field: "default-only", "visibility": "list"},
            {field: "future-model", "visibility": "list",
             "supported_reasoning_levels": [dict(effort="ultra")]}]
        p = Process(self)
        try:
            p.send("FIRST")
            p.wait("OK FIRST")
            p.send("/effort high")
            p.wait("Effort: high")
            p.send("/model default-only")
            p.wait("Model: default-only")
            p.buffer = b""
            p.send("/effort high")
            p.wait("Unsupported effort")
            p.send("/effort default")
            p.wait("Effort: default")
            p.send("/model future-model")
            p.wait("Model: future-model")
            p.send("/effort ultra")
            p.wait("Effort: ultra")
            p.send("FUTURE")
            p.wait("OK FUTURE")
            self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="ultra"))
            p.send("/model fixture-model")
            p.wait("Model: fixture-model")
            p.send("RESTORED")
            p.wait("OK RESTORED")
            self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="high"))
        finally:
            p.finish()
        self.run_batch("RESUME FUTURE", "--resume", "-m", "future-model")
        self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="ultra"))
        self.run_batch("RESUME SELECTED MODEL", "--resume")
        self.assertEqual(self.server.requests[-1]["model"], "future-model")
        self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="ultra"))

    def test_request_error_records_transport_and_stream_details(self):
        self.server.response_status = 400
        self.run_batch("FAIL", expected=1)
        error = [r["data"] for r in self.records() if r["type"] == "request_error"][-1]
        self.assertEqual(error["http_status"], 400)
        self.assertEqual(error["curl_code"], 0)
        self.assertIsInstance(error["message"], str)
        self.assertFalse(error["invalid_stream"])
        self.assertIn("response_status", error)

    def test_effort_default_and_cli_are_sent_and_reported_separately(self):
        self.server.mode = "echo"
        self.server.response_reasoning = dict(effort="high")
        self.run_batch("EXPLICIT EFFORT", "--effort", "xhigh")
        self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="xhigh"))
        usage = [r["data"] for r in self.records() if r["type"] == "usage"][-1]
        self.assertEqual(usage["requested_effort"], "xhigh")
        self.assertEqual(usage["response_reasoning"], dict(effort="high"))
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_effort_change_resume_override_and_provider_default(self):
        self.server.mode = "echo"
        p = Process(self)
        try:
            p.send("FIRST")
            p.wait("OK FIRST")
            self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="medium"))
            p.send("/effort high")
            p.wait("Effort: high")
            p.send("SECOND")
            p.wait("OK SECOND")
            self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="high"))
            p.buffer = b""
            p.send("/effort invented")
            p.wait("Unsupported effort")
            p.send("/effort")
            p.wait("Supported levels depend on the model")
            self.assertIn(b"Effort: high", p.buffer)
            self.assertEqual(len(self.server.requests), 2)
        finally:
            p.finish()
        self.run_batch("RESTORED", "--resume")
        self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="high"))
        self.run_batch("OVERRIDE", "--resume", "--effort", "max")
        self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="max"))
        self.run_batch("PROVIDER DEFAULT", "--resume", "--effort", "default")
        self.assertNotIn("reasoning", self.server.requests[-1])
        self.run_batch("DEFAULT RESTORED", "--resume")
        self.assertNotIn("reasoning", self.server.requests[-1])
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_legacy_session_keeps_provider_default_until_explicitly_changed(self):
        self.server.mode = "echo"
        self.run_batch("OLD SESSION")
        path = self.session() / "meta.json"
        meta = json.loads(path.read_text())
        meta.pop("effort")
        path.write_text(json.dumps(meta))
        journal = self.session() / "journal.jsonl"
        journal.write_text("".join(json.dumps(r) + "\n" for r in self.records()
                                   if r["type"] != "effort"))
        self.run_batch("LEGACY RESUME", "--resume")
        self.assertNotIn("reasoning", self.server.requests[-1])
        self.run_batch("EXPLICIT RESUME", "--resume", "--effort", "low")
        self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="low"))

    def test_unsupported_effort_is_not_silently_retried_as_default(self):
        self.server.mode = "echo"
        self.server.response_status = 400
        self.run_batch("REJECT UNSUPPORTED EFFORT", "--effort", "max", expected=1)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["reasoning"], dict(effort="max"))

    def test_invalid_cli_effort_fails_without_requests(self):
        result = self.run_batch("DO NOT RUN", "--effort", "invented", expected=1)
        self.assertIn(b"Unsupported effort", result.stderr)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(self.server.model_fetches, 1)

    def test_effort_change_during_inference_is_rejected_without_changing_request(self):
        self.server.mode = "slow"
        p = Process(self, "--effort", "high")
        try:
            p.send("WAIT")
            self.assertTrue(self.server.waiting.wait(5))
            p.send("/effort low")
            p.wait("Interrupt the current turn before changing the effort")
            p.wait("OK WAIT")
            self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="high"))
            p.buffer = b""
            p.send("/effort")
            p.wait("Supported levels depend on the model")
            self.assertIn(b"Effort: high", p.buffer)
        finally:
            p.finish()

    def test_effort_is_used_by_compaction_and_followup(self):
        self.server.mode = "echo"
        p = Process(self, "--effort", "max")
        try:
            for i in range(4):
                p.send("HISTORY " + str(i))
                p.wait("OK HISTORY " + str(i))
            p.send("/compact")
            p.wait("Full journal preserved")
            p.send("CONTINUE")
            p.wait("OK CONTINUE")
        finally:
            p.finish()
        self.assertTrue(any(r["instructions"].startswith("Summarize") for r in self.server.requests))
        self.assertTrue(all(r["reasoning"] == dict(effort="max") for r in self.server.requests))
        self.assertEqual(self.saved_requests(), self.server.requests)

    def test_in_instance_resume_restores_effort_and_does_not_reapply_cli_overrides(self):
        self.server.mode = "echo"
        self.run_batch("ORIGINAL", "--effort", "high")
        target = self.session().name
        p = Process(self, "--effort", "low")
        try:
            p.wait("New session:")
            p.send("/resume " + target)
            p.wait("Resumed session: " + target)
            self.session = lambda: self.path / "state/harn/sessions" / target
            p.send("FOLLOWUP")
            p.wait("OK FOLLOWUP")
            self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="high"))
            self.assertEqual([item["content"] for item in self.server.requests[-1]["input"]
                              if item.get("role") == "user"], ["ORIGINAL", "FOLLOWUP"])
            p.send("/resume missing-session")
            p.wait("Session belongs to a different project")
            p.send("AFTER FAILURE")
            p.wait("OK AFTER FAILURE")
            self.assertEqual(self.server.requests[-1]["reasoning"], dict(effort="high"))
            self.assertIn("ORIGINAL", [item.get("content") for item in self.server.requests[-1]["input"]
                                       if item.get("role") == "user"])
        finally:
            p.finish()
        self.run_batch("LOCK RELEASED", "--resume", target)

    def test_in_instance_resume_stops_old_jobs_only_after_success(self):
        self.server.mode = "echo"
        self.run_batch("TARGET HISTORY")
        target = self.session().name
        self.server.mode = "jobs"
        p = Process(self)
        try:
            p.wait("New session:")
            current = p.buffer.split(b"New session: ", 1)[1].splitlines()[0].decode()
            self.session = lambda: self.path / "state/harn/sessions" / current
            p.send("TASK jobs")
            p.wait("SERVER READY")
            self.wait_path("port")
            port = int((self.project / "port").read_text())
            p.send("/resume missing-session")
            p.wait("Session belongs to a different project")
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
            p.send("/resume " + target)
            p.wait("Resumed session: " + target)
            self.assert_server_stopped()
        finally:
            p.finish()

    def test_in_instance_resume_locked_target_preserves_current_session(self):
        self.server.mode = "echo"
        self.run_batch("LOCKED HISTORY")
        target = self.session().name
        owner = Process(self, "--resume", target)
        p = Process(self)
        try:
            owner.wait("Resumed session:")
            p.wait("New session:")
            current = p.buffer.split(b"New session: ", 1)[1].splitlines()[0].decode()
            self.session = lambda: self.path / "state/harn/sessions" / current
            p.send("/resume " + target)
            p.wait("Session is already open")
            p.send("CURRENT HISTORY")
            p.wait("OK CURRENT HISTORY")
            self.assertEqual([item["content"] for item in self.server.requests[-1]["input"]
                              if item.get("role") == "user"], ["CURRENT HISTORY"])
            p.send("/context")
            p.wait("Session: " + str(self.path / "state/harn/sessions" / current))
            # A failed switch must retain the original journal lock as well.
            result = self.run_batch("DO NOT RUN", "--resume", current, expected=1)
            self.assertIn(b"Session is already open", result.stderr)
        finally:
            p.finish()
            owner.finish()

    def test_commands_and_plain_continuation(self):
        self.server.mode = "echo"
        p = Process(self)
        try:
            p.send("/help")
            p.wait("Ctrl-C: interrupt.")
            commands = set(line.split()[0].decode() for line in p.buffer.splitlines() if line.startswith(b"/"))
            self.assertEqual(commands, {"/jobs", "/context", "/provider", "/model", "/effort", "/resume", "/skill", "/compact", "/quit", "/help"})
            removed = ["/continue", "/models", "/skills", "/sessions", "/login", "/logout",
                       "/context raw", "/context summary", "/context 2"]
            for command in removed:
                p.send(command)
            p.send("/context")
            p.wait("Session: " + str(self.session()))
            self.assertEqual(p.buffer.count(b"Unknown command"), len(removed))
            self.assertEqual(self.server.requests, [])
            p.send("продолжай")
            p.wait("OK продолжай")
            self.assertEqual(self.server.requests[-1]["input"][-1]["content"], "продолжай")
        finally:
            p.finish()

    def test_task_file_and_startup_session_selection(self):
        self.server.mode = "echo"
        task = self.project / "task.txt"
        task.write_text("Первая строка\nSecond line\n")
        p = subprocess.run([str(BINARY), "-C", str(self.project), "-f", str(task)], env=self.env,
                           input=b"", capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.server.requests[-1]["input"][-1]["content"], task.read_text())
        session = self.session().name
        p = subprocess.run([str(BINARY), "-C", str(self.project), "--sessions"], env=self.env,
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(session, p.stdout)
        self.run_batch("NEXT TASK", "--resume", session)
        users = [i["content"] for i in self.server.requests[-1]["input"] if i.get("role") == "user"]
        self.assertEqual(users, [task.read_text(), "NEXT TASK"])

    def test_technical_cli_options_and_extra_aliases_are_removed(self):
        for option in ["--context", "--context-limit", "--keep-cycles", "--state-dir", "--model", "--prompt"]:
            p = subprocess.run([str(BINARY), option, "unused"], env=self.env,
                               capture_output=True, text=True, timeout=5)
            self.assertEqual(p.returncode, 2, (option, p.stdout, p.stderr))
            self.assertNotIn(option, p.stdout.split())
        self.assertEqual(self.server.requests, [])

    def test_skill_catalog_is_local_and_has_no_pending_selection(self):
        self.server.mode = "echo"
        self.write_skill(scope="user", body="USER_VERSION", description="User description")
        self.write_skill(body="PROJECT_VERSION", description="Project description")
        self.write_skill("user-only", scope="user", description="Personal skill")
        p = Process(self)
        try:
            p.send("/skill")
            p.wait("user-only (user)")
            self.assertIn(b"fixture-skill (project)", p.buffer)
            self.assertIn(b"Project description", p.buffer)
            self.assertNotIn(b"Skill for next task", p.buffer)
            self.assertEqual(self.server.requests, [])
            self.assertEqual(self.server.model_fetches, 0)
            p.send("/skill fixture-skill")
            p.wait("Unknown command")
            p.send("Use fixture-skill for this task")
            p.wait("OK Use fixture-skill for this task")
            self.assertEqual(self.selected_skills(), [])
            self.assertEqual(self.server.requests[0]["input"][-1]["content"],
                             "Use fixture-skill for this task")
            self.assertEqual(self.saved_requests(), self.server.requests)
        finally:
            p.finish()

    def test_skill_command_reports_an_empty_catalog_without_a_model_request(self):
        p = Process(self)
        try:
            p.send("/skill")
            p.wait("No skills found")
            self.assertEqual(self.server.requests, [])
            self.assertEqual(self.server.model_fetches, 0)
        finally:
            p.finish()

    def test_skill_catalog_during_inference_does_not_modify_queued_messages(self):
        self.server.mode = "slow"
        self.server.delay = 2
        self.write_skill(body="CATALOG_ONLY_INSTRUCTIONS")
        p = Process(self)
        try:
            p.send("WAIT")
            self.assertTrue(self.server.waiting.wait(4))
            p.send("/skill")
            p.wait("fixture-skill (project)")
            p.send("Use fixture-skill for queued task")
            p.wait("Message queued (1 waiting)")
            p.wait("OK Use fixture-skill for queued task")
            self.assertEqual(self.selected_skills(), [])
            self.assertEqual(self.server.requests[1]["input"][-1]["content"],
                             "Use fixture-skill for queued task")
        finally:
            p.finish()

    def test_model_command_lists_selects_and_uses_cached_models_during_a_request(self):
        self.server.mode = "slow"
        self.server.extra_models = [dict(slug="fixture-alt", visibility="list", context_window=100000)]
        p = Process(self)
        try:
            p.send("/model")
            p.wait("  fixture-alt")
            self.assertEqual(self.server.requests, [])
            p.send("/model fixture-alt")
            p.send("WAIT")
            self.assertTrue(self.server.waiting.wait(5))
            before = self.server.model_fetches
            p.buffer = b""
            p.send("/model")
            p.wait("  fixture-alt")
            self.assertEqual(self.server.model_fetches, before)
            self.assertEqual(self.server.requests[-1]["model"], "fixture-alt")
            p.wait("OK WAIT")
            p.buffer = b""
            p.send("/context")
            p.wait("Session: " + str(self.session()))
            self.assertIn(b"100/100000", p.buffer)
        finally:
            p.finish()

    def test_long_output_is_explicitly_truncated_and_full_log_is_accessible(self):
        self.server.mode = "long-output"
        p = Process(self)
        try:
            p.send("LONG")
            p.wait("LONG OUTPUT CHECKED")
            self.assertIn(b"Model receives output bytes", p.buffer)
            p.buffer = b""
            p.send("/jobs 1")
            p.wait("END\n")
            self.assertIn(b"Full output is in the log.", p.buffer)
            log = self.session() / "jobs/1.log"
            self.assertTrue(log.read_text().startswith("BEGIN"))
            self.assertTrue(log.read_text().endswith("END\n"))
        finally:
            p.finish()

    def test_queued_messages_are_confirmed_when_included_in_the_next_request(self):
        self.server.mode = "slow"
        p = Process(self)
        try:
            p.send("WAIT")
            self.assertTrue(self.server.waiting.wait(4))
            p.send("FIRST CLARIFICATION")
            p.send("SECOND CLARIFICATION")
            p.wait("Queued messages included in request #2: 2")
            p.wait("OK FIRST CLARIFICATION")
            self.assertEqual(self.server.requests[-1]["input"][-1]["content"],
                             "FIRST CLARIFICATION\nSECOND CLARIFICATION")
            delivery = [r["data"] for r in self.records() if r["type"] == "delivery"]
            self.assertEqual(delivery, [dict(request=2, messages=2)])
        finally:
            p.finish()

    def test_read_pages_a_long_unicode_line_without_losing_bytes(self):
        self.server.mode = "paging"
        (self.project / "long.txt").write_text("Я" * 20000)
        self.assertIn(b"PAGING COMPLETE", self.run_batch("READ LONG FILE").stdout)

    def test_program_identity_and_home_paths(self):
        for option in ("--help", "--version"):
            result = subprocess.run([str(BINARY), option], env=self.env,
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith(b"harn "), result.stdout)
            if option == "--help":
                self.assertIn(b"Usage: harn ", result.stdout)
        env = self.env.copy()
        env.pop("XDG_CONFIG_HOME")
        env.pop("XDG_STATE_HOME")
        result = subprocess.run([str(BINARY), "-C", str(self.project), "--sessions"],
                                env=env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.user_home / ".config/harn").is_dir())
        self.assertTrue((self.user_home / ".local/state/harn").is_dir())
        self.assertEqual(self.server.requests, [])

    def test_startup_identifies_new_and_explicitly_resumed_sessions(self):
        self.server.mode = "echo"
        first = self.run_batch("Investigate the timeout\nKeep existing behavior")
        session = self.session().name
        self.assertIn(("New session: " + session).encode(), first.stderr)
        self.assertIn(("Project: " + str(self.project)).encode(), first.stderr)
        self.run_batch("Check the regression tests", "--resume", session)
        resumed = self.run_batch("Continue", "--resume", session)
        self.assertIn(("Resumed session: " + session).encode(), resumed.stderr)
        self.assertIn(b"Started with: Investigate the timeout...", resumed.stderr)
        self.assertIn(b"Last request: Check the regression tests", resumed.stderr)
        self.assertNotIn(b"Last request: Continue", resumed.stderr)
        self.assertNotIn(b"Resumed session:", resumed.stdout)

    def test_resume_selects_latest_activity_instead_of_random_session_suffix(self):
        self.server.mode = "echo"
        self.run_batch("OLDER CONVERSATION")
        old = self.session()
        newer = old.parent / "20261105-040000-aaaaaa"
        old.rename(old.parent / "20261105-040000-zzzzzz")
        old = old.parent / "20261105-040000-zzzzzz"
        import shutil
        shutil.copytree(old, newer)
        records = (newer / "journal.jsonl").read_text().replace("OLDER CONVERSATION", "NEWER CONVERSATION")
        (newer / "journal.jsonl").write_text(records)
        os.utime(old / "journal.jsonl", (100, 100))
        os.utime(newer / "journal.jsonl", (200, 200))
        resumed = self.run_batch("WHICH CONVERSATION", "--resume")
        self.assertIn(("Resumed session: " + newer.name).encode(), resumed.stderr)
        self.assertIn(b"Started with: NEWER CONVERSATION", resumed.stderr)
        users = [i["content"] for i in self.server.requests[-1]["input"] if i.get("role") == "user"]
        self.assertIn("NEWER CONVERSATION", users)
        self.assertNotIn("OLDER CONVERSATION", users)

    def test_ambiguous_edit_preserves_file(self):
        self.server.mode = "ambiguous"
        (self.project / "same.txt").write_text("same same\n")
        self.run_batch("TASK ambiguous")
        self.assertEqual((self.project / "same.txt").read_text(), "same same\n")

    def test_retry_limit_and_no_incomplete_execution(self):
        self.server.mode = "retry"
        self.run_batch("retry")
        self.assertEqual(self.server.count, 2)
        self.server.mode = "fail"
        self.server.count = 0
        self.run_batch("fail", expected=1)
        self.assertEqual(self.server.count, 3)
        self.server.mode = "partial"
        self.server.count = 0
        self.run_batch("partial", expected=1)
        self.assertEqual(self.server.count, 3)
        self.assertFalse((self.project / "forbidden").exists())

    def test_background_local_stop_during_network_and_resume(self):
        self.server.mode = "jobs"
        p = Process(self)
        try:
            p.send("TASK jobs")
            p.wait("SERVER READY")
            port = int(self.wait_path("port").read_text())
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
            p.send("WAIT")
            self.assertTrue(self.server.waiting.wait(4))
            p.send("/jobs stop 1")
            self.assert_server_stopped()
            p.wait("WAIT FINISHED")
            p.send("CHECK STOP")
            p.wait("STOP CONFIRMED")
        finally:
            p.finish()
        self.server.mode = "resume"
        # No reasoning fixture in this scenario, so use a plain continuation.
        self.server.mode = "echo"
        self.run_batch("AFTER EXIT", "--resume", self.session().name)
        self.assert_server_stopped()

    def test_interrupt_foreground_keeps_background(self):
        self.server.mode = "interrupt"
        p = Process(self)
        try:
            p.send("TASK interrupt")
            self.wait_path("started")
            self.wait_path("port")
            p.p.send_signal(signal.SIGINT)
            p.wait("Stopped by you")
            p.wait("1 background job still running")
            self.assertEqual(p.buffer.count(b"Stopped by you"), 1)
            p.send("FOLLOWUP")
            p.wait("BACKGROUND PRESERVED")
        finally:
            p.finish()
        self.assert_server_stopped()

    def test_interrupt_model_network_request_keeps_background_and_accepts_clarification(self):
        self.server.mode = "jobs"
        p = Process(self)
        try:
            p.send("TASK jobs")
            p.wait("SERVER READY")
            self.wait_path("port")
            p.send("WAIT")
            self.assertTrue(self.server.waiting.wait(4))
            p.p.send_signal(signal.SIGINT)
            p.wait("Stopped by you")
            p.send("FOLLOWUP")
            p.wait("BACKGROUND PRESERVED")
        finally:
            p.finish()
        self.assert_server_stopped()

    def test_interrupt_background_wait_then_stop_only_one_job(self):
        self.server.mode = "job-wait"
        p = Process(self)
        try:
            p.send("TASK wait")
            other = int(self.wait_path("other.pid").read_text())
            self.wait_path("port")
            p.p.send_signal(signal.SIGINT)
            p.wait("Stopped by you")
            p.send("FOLLOWUP")
            p.wait("BACKGROUND PRESERVED")
            p.send("/jobs stop 1")
            self.assert_server_stopped()
            self.assertTrue(Path("/proc/" + str(other)).exists())
        finally:
            p.finish()
        self.assertFalse(Path("/proc/" + str(other)).exists())

    def test_terminal_close_stops_process_tree(self):
        self.server.mode = "jobs"
        p = Process(self)
        p.send("TASK jobs")
        p.wait("SERVER READY")
        self.wait_path("port")
        p.p.send_signal(signal.SIGHUP)
        p.p.wait(timeout=5)
        self.assert_server_stopped()

    def test_unknown_action_after_crash_is_not_replayed(self):
        self.server.mode = "unknown"
        p = Process(self)
        p.send("TASK unknown")
        pgid = int(self.wait_path("group").read_text())
        p.p.kill()
        p.p.wait(timeout=5)
        os.killpg(pgid, signal.SIGKILL)
        self.run_batch("RESUME UNKNOWN", "--resume", self.session().name)
        self.assertEqual((self.project / "effects").read_text(), "once\n")

    def test_automatic_compaction_inside_tool_loop_keeps_whole_cycles_and_user_request(self):
        self.server.mode = "auto"
        self.server.usage = dict(input_tokens=110, output_tokens=10, total_tokens=120)
        (self.project / "AGENTS.md").write_text("Preserve sentinel.txt and public API.")
        self.server.window = 128
        self.run_batch("KEEP ORIGINAL USER GOAL")
        self.assertTrue(any(r["type"] == "compact" for r in self.records()))
        request = self.server.requests[-1]
        self.assertIn("Preserve sentinel.txt", request["instructions"])
        self.assertTrue(any(x.get("content") == "KEEP ORIGINAL USER GOAL" for x in request["input"]))
        calls = [x["call_id"] for x in request["input"] if x.get("type") == "function_call"]
        outputs = [x["call_id"] for x in request["input"] if x.get("type") == "function_call_output"]
        self.assertEqual(calls, ["auto2", "auto3"])
        self.assertEqual(calls, outputs)

    def test_partial_journal_tail_remains_resumable_after_two_restarts(self):
        self.server.mode = "echo"
        self.run_batch("INITIAL")
        path = self.session() / "journal.jsonl"
        with path.open("ab") as f:
            f.write(b'{"type":"item","data":')
        prefix = path.read_bytes()
        self.run_batch("FIRST RESTART", "--resume")
        self.assertTrue(path.read_bytes().startswith(prefix))
        self.run_batch("SECOND RESTART", "--resume")
        self.assertIn("OK SECOND RESTART", path.read_text())

    def test_compaction_preserves_source_and_exact_request(self):
        self.server.mode = "echo"
        p = Process(self)
        try:
            for n in range(5):
                p.send("KEEP_ME NEVER_DELETE " + str(n))
                p.wait("OK KEEP_ME NEVER_DELETE " + str(n))
            before = (self.session() / "journal.jsonl").read_bytes()
            p.send("/compact")
            p.wait("Full journal preserved")
            self.assertTrue((self.session() / "journal.jsonl").read_bytes().startswith(before))
            p.send("/context")
            p.wait("Context: unmeasured")
            self.assertIn("KEEP_ME", (self.session() / "summary.txt").read_text())
            p.send("CONTINUE")
            p.wait("OK CONTINUE")
            request = self.server.requests[-1]
            self.assertTrue(any("Earlier conversation summary" in i.get("content", "") for i in request["input"]))
            self.assertTrue(any(i.get("content") == "CONTINUE" for i in request["input"]))
        finally:
            p.finish()
        self.run_batch("AFTER COMPACTION", "--resume", self.session().name)
        self.assertTrue(any("Earlier conversation summary" in i.get("content", "") for i in self.server.requests[-1]["input"]))
        self.assertEqual(self.saved_requests(), self.server.requests)


if __name__ == "__main__":
    unittest.main(verbosity=2)
