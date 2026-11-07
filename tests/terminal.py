#!/usr/bin/env python3
"""Exercise the real UI in st and Vim on an isolated X11 display. No paid requests."""
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
from check import BINARY, Checks, call, text


def until(condition, description, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = condition()
        if result:
            return result
        time.sleep(.03)
    raise AssertionError("Timed out: " + description)


class Terminal:
    def __init__(self, case, env, artifacts, name, editor, width=90):
        self.case, self.artifacts, self.name = case, artifacts, name
        self.window = subprocess.Popen(["st", "-t", "HarnTerminalCheck", "-g", str(width) + "x30",
                                        "-o", str(artifacts / (name + ".log")), "-e", "env",
                                        "HOME=" + case.env["HOME"], str(BINARY),
                                        "-C", str(case.project)],
                                       env=dict(case.env, **env, EDITOR=editor), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
        self.tool = subprocess.Popen([os.environ.get("COMPUTER_USE", "computer-use"), "serve"],
                                     env=dict(case.env, **env), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True)
        self.seq = 0
        def own_window():
            return next((w["window"] for w in self.call("computer_windows", {})["windows"]
                         if w["pid"] == self.window.pid), None)
        self.wid = until(own_window, "st window")
        self.action(dict(type="focus_window"))
        until(lambda: list((case.path / "state/harn/sessions").glob("*/journal.jsonl")), "session")

    def call(self, tool, arguments):
        self.seq += 1
        self.tool.stdin.write(json.dumps(dict(id=self.seq, tool=tool, arguments=arguments)) + "\n")
        self.tool.stdin.flush()
        response = json.loads(self.tool.stdout.readline())
        assert "result" in response, response
        result = response["result"]
        assert not result.get("error"), result
        return result

    def observe(self, label=None):
        result = self.call("computer_observe", dict(window=self.wid, screenshot=bool(label), max_nodes=16))
        if label:
            (self.artifacts / (self.name + "-" + label + ".png")).write_bytes(
                base64.b64decode(result["screenshot_png_base64"]))
        return result

    def action(self, action):
        view = self.observe()
        result = self.call("computer_action", dict(view_id=view["view_id"], action=action,
                           observe_after=dict(screenshot=False, max_nodes=16, settle_ms=80)))
        assert result.get("status") in ("submitted", "verified"), result

    def key(self, *keys):
        self.action(dict(type="key", keys=list(keys)))

    def text(self, text):
        self.action(dict(type="type_text", text=text))

    def paste(self, text):
        self.action(dict(type="paste", text=text, terminal=True))

    def send(self, text):
        self.paste(text)
        self.key("Return")

    def items(self):
        return [r["data"] for r in self.case.records() if r["type"] == "item"]

    def users(self):
        return [i["content"] for i in self.items() if i.get("role") == "user"]

    def wait_answer(self, value):
        return until(lambda: any(value in part.get("text", "") for i in self.items()
                                 for part in i.get("content", []) if isinstance(part, dict)), value)

    def close(self):
        for p in (self.window, self.tool):
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=4)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        self.tool.stdin.close()
        self.tool.stdout.close()


def main():
    for command in ("st", "xauth", "vim", os.environ.get("XVFB", "Xvfb"),
                    os.environ.get("COMPUTER_USE", "computer-use")):
        if not shutil.which(command):
            raise SystemExit("Missing terminal-check dependency: " + command)
    artifacts = Path(os.environ.get("TERMINAL_ARTIFACTS", tempfile.mkdtemp(prefix="harn-terminal-results-")))
    artifacts.mkdir(parents=True, exist_ok=True)
    desktop = tempfile.TemporaryDirectory(prefix="harn-terminal-x11-")
    display = next(":" + str(n) for n in range(96, 128) if not Path("/tmp/.X11-unix/X" + str(n)).exists())
    env = dict(DISPLAY=display, XAUTHORITY=str(Path(desktop.name) / "Xauthority"), COMPUTER_USE_INPUT="core")
    subprocess.run(["xauth", "-f", env["XAUTHORITY"], "add", display, ".", secrets.token_hex(16)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    xserver = subprocess.Popen([os.environ.get("XVFB", "Xvfb"), display, "-screen", "0", "1900x1100x24",
                                "-auth", env["XAUTHORITY"], "-nolisten", "tcp"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    case = terminal = None
    results = {}
    try:
        until(lambda: Path("/tmp/.X11-unix/X" + display[1:]).exists(), "private X11")
        case = Checks()
        case.setUp()
        case.server.mode = "jobs"
        case.server.usage = dict(input_tokens=36000, output_tokens=100000, total_tokens=136000)
        terminal = t = Terminal(case, env, artifacts, "vim", "vim -Nu NONE -n -i NONE --noplugin")
        t.send("TASK jobs")
        t.wait_answer("SERVER READY")
        case.wait_path("port")
        case.server.delay = 5
        t.send("WAIT")
        assert case.server.waiting.wait(5)
        draft = "Черновик\nKeep this line"
        t.paste(draft)
        t.observe("model-with-draft")
        t.key("Control", "g")
        t.observe("editor-open")
        t.wait_answer("WAIT FINISHED")
        t.observe("editor-after-model-response")
        assert t.users() == ["TASK jobs", "WAIT"]
        t.key("Escape")
        t.text(":%s/Keep this line/Changed line/")
        t.key("Return")
        t.text(":wq")
        t.key("Return")
        t.observe("restored-draft")
        assert t.users() == ["TASK jobs", "WAIT"], "Editor return submitted a request"
        case.server.mode = "echo"
        t.key("Return")
        t.wait_answer("OK Черновик")
        assert t.users()[-1] == "Черновик\nChanged line\n", t.users()
        results["vim_multiline_and_output_while_editor_open"] = True
        t.key("Up")
        t.observe("history")
        t.key("Return")
        until(lambda: len(t.users()) == 4, "multiline history submission")
        assert t.users()[-1] == t.users()[-2]
        t.paste("Discard this")
        t.key("Control", "c")
        t.send("After interrupt")
        t.wait_answer("OK After interrupt")
        assert t.users()[-1] == "After interrupt"
        results["history_and_ctrl_c"] = True
        t.send("/jobs stop 1")
        case.assert_server_stopped()
        t.send("/jobs 1")
        t.observe("stopped-job")
        results["native_local_stop_and_children"] = True
        wrap = "wrap " * 40
        t.text(wrap)
        t.key("Left")
        t.key("Left")
        t.observe("wrapped-draft")
        t.text("X")
        t.key("Return")
        until(lambda: t.users()[-1] == wrap[:-2] + "X" + wrap[-2:], "wrapped input with cursor")
        t.wait_answer("OK wrap")
        t.observe("wrapped-model-output")
        results["wrapped_stream_and_cursor"] = True
        t.key("Control", "d")
        assert t.window.wait(timeout=5) == 0
        t.close()
        terminal = None
        case.tearDown()
        case = None
        case = Checks()
        case.setUp()
        case.prepare_background_logs()
        terminal = t = Terminal(case, env, artifacts, "logs", "vim -Nu NONE -n -i NONE --noplugin", width=42)
        t.send("BACKGROUND LOGS")
        t.wait_answer("OK BACKGROUND LOGS")
        case.wait_background_logs()
        t.send("/jobs 1")
        t.send("/jobs 2")
        draft = "Черновик\nKeep the draft"
        t.paste(draft)
        t.observe("identified-jobs-with-draft")
        log = (artifacts / "logs.log").read_text()
        for i in (1, 2):
            assert "jobs #" + str(i) + ": Привет " + str(i) in log, log
            assert "jobs #" + str(i) + ": last " + str(i) in log, log
        assert t.users() == ["BACKGROUND LOGS"]
        t.key("Return")
        t.wait_answer("OK Черновик")
        assert t.users()[-1] == draft
        t.key("Control", "d")
        assert t.window.wait(timeout=5) == 0
        results["background_log_labels_and_multiline_draft"] = True
        t.close()
        terminal = None
        case.tearDown()
        case = None
        for name, editor in (("missing", "/no/such/harn-editor"),
                             ("failed", "sh -c 'printf changed > \"$1\"; exit 7' editor"),
                             ("cancel", "vim -Nu NONE -n -i NONE --noplugin")):
            case = Checks()
            case.setUp()
            case.server.mode = "echo"
            terminal = t = Terminal(case, env, artifacts, name, editor, width=42)
            draft = "Сохрани черновик\nSecond line"
            t.paste(draft)
            t.key("Control", "g")
            if name == "cancel":
                t.key("Escape")
                t.text("GoIgnored")
                t.key("Escape")
                t.text(":q!")
                t.key("Return")
            else:
                time.sleep(.3)
            t.observe("draft-preserved")
            assert not t.users()
            t.text(" + kept")
            t.key("Return")
            t.wait_answer("OK Сохрани")
            assert t.users() == [draft + " + kept"], t.users()
            t.key("Control", "d")
            assert t.window.wait(timeout=5) == 0
            results[name + "_preserves_draft"] = True
            t.close()
            terminal = None
            case.tearDown()
            case = None
        case = Checks()
        case.setUp()
        case.server.mode = "slow"
        case.server.delay = 4
        case.server.usage = dict(input_tokens=36000, output_tokens=100000, total_tokens=136000)
        terminal = t = Terminal(case, env, artifacts, "status", "vim -Nu NONE -n -i NONE --noplugin", width=42)
        t.send("MEASURE")
        t.wait_answer("OK MEASURE")
        t.send("WAIT")
        assert case.server.waiting.wait(5)
        t.send("CLARIFICATION")
        draft = "Keep this draft\nКириллица и переносы"
        t.paste(draft)
        t.observe("queued-with-draft")
        t.wait_answer("OK CLARIFICATION")
        t.observe("delivered-with-draft")
        log = (artifacts / "status.log").read_text()
        assert "q:1" in log and "ctx:~18%" in log, log[-2000:]
        assert "Queued messages included" in log
        assert len(t.users()) == 3
        t.key("Return")
        t.wait_answer("OK Keep this draft")
        assert t.users()[-1] == draft, t.users()
        for i in range(3):
            t.send("FILLER " + str(i))
            t.wait_answer("OK FILLER " + str(i))
        t.send("/compact")
        until(lambda: any(r["type"] == "compact" for r in case.records()), "compact checkpoint")
        t.send("/context")
        t.observe("compacted-context")
        t.send("/help")
        t.observe("commands")
        t.send("/model")
        t.observe("model-list")
        assert "KEEP_ME" in (case.session() / "summary.txt").read_text()
        log = (artifacts / "status.log").read_text()
        assert "ctx:?" in log
        assert "Context: unmeasured" in log
        t.key("Control", "d")
        assert t.window.wait(timeout=5) == 0
        results["narrow_status_queue_preserves_multiline_draft"] = True
        results["compaction_shows_unknown_until_measured"] = True
        t.close()
        terminal = None
        case.tearDown()
        case = None
        case = Checks()
        case.setUp()
        case.server.mode = "skill"
        skill = case.write_skill(body="PRESERVE_NATIVE_SKILL_578")
        original = skill.read_text()
        terminal = t = Terminal(case, env, artifacts, "skills", "vim -Nu NONE -n -i NONE --noplugin", width=42)
        t.send("LOAD SKILL TERMINAL")
        t.wait_answer("OK LOAD SKILL TERMINAL")
        draft = "Черновик\nKeep this line"
        t.paste(draft)
        t.observe("applied-with-draft")
        log = (artifacts / "skills.log").read_text()
        assert "Using skill: fixture-skill" in log, log
        t.key("Return")
        t.wait_answer("OK Черновик")
        assert t.users()[-1] == draft
        for i in range(3):
            t.send("SKILL FILL " + str(i))
            t.wait_answer("OK SKILL FILL " + str(i))
        t.send("/compact")
        until(lambda: any(r["type"] == "compact" for r in case.records()), "compact active skill")
        t.send("AFTER SKILL COMPACTION")
        t.wait_answer("OK AFTER SKILL COMPACTION")
        assert any(original in item.get("content", "") for item in case.server.requests[-1]["input"])
        t.observe("preserved-after-compaction")
        t.key("Control", "d")
        assert t.window.wait(timeout=5) == 0
        results["skill_activation_compaction_and_multiline_draft"] = True
        t.close()
        terminal = None
        case.tearDown()
        case = None
        case = Checks()
        case.setUp()
        case.server.mode = "jobs"
        skill = case.write_skill("alpha", body="EXPLICIT_NATIVE_SKILL_392")
        case.write_skill("beta", scope="user", description="Personal native skill")
        terminal = t = Terminal(case, env, artifacts, "completion", "vim -Nu NONE -n -i NONE --noplugin")
        t.send("TASK jobs")
        t.wait_answer("SERVER READY")
        case.wait_path("port")
        t.text("/sk")
        t.key("Tab")
        t.key("Return")
        t.observe("local-skill-list")
        log = (artifacts / "completion.log").read_text()
        assert "alpha (project)" in log and "beta (user)" in log, log
        case.server.delay = 3
        t.send("WAIT")
        assert case.server.waiting.wait(5)
        t.text("/mo")
        t.observe("suggestions-during-inference")
        t.wait_answer("WAIT FINISHED")
        t.observe("response-with-suggestions-open")
        assert t.users() == ["TASK jobs", "WAIT"]
        t.key("Return")
        t.observe("model-argument-suggestions")
        log = (artifacts / "completion.log").read_text()
        assert "> /model " in log and "Unknown command" not in log, log[-3000:]
        t.key("Control", "c")
        draft = "Черновик\nLiteral\ttab"
        t.paste(draft)
        t.key("Control", "g")
        t.key("Escape")
        t.text(":wq")
        t.key("Return")
        t.observe("restored-draft")
        assert t.users() == ["TASK jobs", "WAIT"]
        case.server.mode = "echo"
        t.key("Return")
        t.wait_answer("OK Черновик")
        assert t.users()[-1] == draft + "\n", t.users()
        assert not case.selected_skills()
        t.send("/jobs stop 1")
        case.assert_server_stopped()
        t.key("Control", "d")
        assert t.window.wait(timeout=5) == 0
        results["suggestions_keep_inference_and_job_controls_live"] = True
        results["editor_preserves_literal_pasted_tab"] = True
        t.close()
        terminal = None
        case.tearDown()
        case = None
        case = Checks()
        case.setUp()
        case.server.mode = "echo"
        long_name = "alpha-very-long-skill-name-for-narrow-terminal-selection-check"
        case.write_skill("alpha-short")
        case.write_skill(long_name)
        terminal = t = Terminal(case, env, artifacts, "completion-narrow", "vim -Nu NONE -n -i NONE --noplugin", width=42)
        t.send("MEASURE")
        t.wait_answer("OK MEASURE")
        fetches = case.server.model_fetches
        t.text("/model gpt")
        t.key("Tab")
        t.observe("cached-model-preview")
        t.key("Return")
        assert case.server.model_fetches == fetches
        t.text("/")
        t.observe("automatic-command-list")
        t.key("Down")
        t.observe("selected-context")
        t.key("Tab")
        t.key("Return")
        assert "Context:" in (artifacts / "completion-narrow.log").read_text()
        t.text("/pro")
        t.observe("filtered-provider")
        t.key("Escape")
        t.observe("dismissed-with-draft")
        t.key("Return")
        assert "Unknown command" in (artifacts / "completion-narrow.log").read_text()
        t.send("/skill")
        t.observe("catalog-without-selection")
        log = (artifacts / "completion-narrow.log").read_text()
        assert "alpha-short" in log and "Skill for next task" not in log, log[-3000:]
        t.paste("Draft to discard\nКириллица")
        t.key("Control", "c")
        t.observe("draft-cleared")
        t.send("AFTER CLEAR")
        t.wait_answer("OK AFTER CLEAR")
        assert t.users() == ["MEASURE", "AFTER CLEAR"]
        assert not case.selected_skills()
        t.key("Control", "d")
        assert t.window.wait(timeout=5) == 0
        results["cached_model_and_automatic_command_suggestions"] = True
        results["narrow_catalog_and_quiet_idle_draft_clear"] = True
        t.close()
        terminal = None
        case.tearDown()
        case = None
        case = Checks()
        case.setUp()
        global_file = case.config / "AGENTS.md"
        global_file.write_text("GLOBAL_UI_RULE_814")
        (case.path / "AGENTS.md").write_text("PARENT_UI_RULE_193")
        (case.project / "AGENTS.md").write_text("ROOT_UI_RULE_651")
        front = case.project / "frontend"
        front.mkdir()
        (front / "AGENTS.md").write_text("FRONT_UI_RULE_274")
        file = front / "app.txt"
        file.write_text("before")
        case.server.mode = "plan"
        case.server.tool_plan = [[call("write", dict(path="frontend/app.txt", content="stale"), "old")],
                                 [call("write", dict(path="frontend/app.txt", content="fresh"), "new")]]
        def before_retry(request, output):
            assert output["old"]["instructions_changed"]
            assert file.read_text() == "before"
        case.server.plan_checks[1] = before_retry
        terminal = t = Terminal(case, env, artifacts, "instructions", "vim -Nu NONE -n -i NONE --noplugin")
        t.observe("startup-sources")
        t.send("SCOPED RULES")
        t.wait_answer("OK SCOPED RULES")
        assert file.read_text() == "fresh"
        draft = "Черновик\nKeep this line"
        t.paste(draft)
        t.observe("changed-rules-with-draft")
        log = (artifacts / "instructions.log").read_text()
        assert "scope global" in log and "instructions changed for this path" in log, log
        assert "+stale" not in log
        for value in ("GLOBAL_UI_RULE_814", "PARENT_UI_RULE_193", "ROOT_UI_RULE_651", "FRONT_UI_RULE_274"):
            assert value in case.server.requests[-1]["instructions"]
        t.key("Return")
        t.wait_answer("OK Черновик")
        assert t.users()[-1] == draft
        t.key("Control", "d")
        assert t.window.wait(timeout=5) == 0
        results["hierarchical_instructions_write_guard_and_multiline_draft"] = True
        t.close()
        terminal = None
        case.tearDown()
        case = None
        for width in (42, 90, 140):
            case = Checks()
            case.setUp()
            case.server.mode = "plan"
            case.server.ui_stream = True
            source = "## Результат\n\nПроверил **обработку ответа** в `provider.c`. "
            source += "Текст использует всю ширину терминала и переносится по словам.\n\n"
            source += "- " + "Продолжение пункта сохраняет отступ. " * 3
            source += "\n- Результаты инструментов доступны после перезапуска.\n\n"
            source += "```sh\nmake check\nharn --resume\n```\n\nEND"
            answer = text(source)
            answer["phase"] = "final_answer"
            case.server.tool_plan = [[call("exec", dict(command="printf 'CHECK PASSED\\n'"), "e")],
                                     [answer]]
            name = "layout-" + str(width)
            terminal = t = Terminal(case, env, artifacts, name,
                                   "vim -Nu NONE -n -i NONE --noplugin", width=width)
            t.send("Проверь исправление и запусти тесты.")
            t.wait_answer("END")
            t.observe("answer")
            draft = "Проверь переносы.\nСохрани структуру списков.\nПокажи результаты."
            t.paste(draft)
            t.observe("multiline-draft")
            t.key("Return")
            t.wait_answer("OK Проверь переносы.")
            t.observe("submitted")
            assert t.users()[-1] == draft
            assert answer in t.items()
            assert (case.session() / "jobs/1.log").read_bytes() == b"CHECK PASSED\n"
            t.key("Control", "d")
            assert t.window.wait(timeout=5) == 0
            results["markdown_and_full_width_" + str(width)] = True
            t.close()
            terminal = None
            case.tearDown()
            case = None
        (artifacts / "results.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(dict(results=results, artifacts=str(artifacts)), indent=2))
    finally:
        if terminal:
            terminal.close()
        if case:
            case.tearDown()
        xserver.terminate()
        xserver.wait(timeout=4)
        desktop.cleanup()


if __name__ == "__main__":
    main()
