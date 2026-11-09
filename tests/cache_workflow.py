#!/usr/bin/env python3
"""Opt-in live coding workflows with independent checks and balanced run order."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time

from cache_report import summarize

POLICY = "Use the standard library only. Read relevant files before editing. Run unittest after changes.\n"
UPDATED_POLICY = POLICY + "Use ROUND_HALF_UP when formatting monetary totals.\n"
FILES = {
    "README.md": """# Merchant ledger

An importable Python module for daily payment reports. Inputs are dictionaries
with timestamp (ISO 8601), amount (decimal string), currency and optional status.
Missing status means settled. Cancelled payments do not contribute. Dates in
reports are UTC dates. Naive timestamps are interpreted as UTC. Output amounts
are Decimal values and currency groups must never be combined.

Run the smoke tests with python3 -m unittest discover -s tests -v.
""",
    "ledger.py": '''"""Daily settlement reports."""
from datetime import datetime
from decimal import Decimal


def daily_totals(rows):
    totals = {}
    for row in rows:
        if row.get("status", "settled") == "cancelled":
            continue
        day = datetime.fromisoformat(row["timestamp"]).date().isoformat()
        key = (day, row["currency"])
        totals[key] = totals.get(key, Decimal("0")) + Decimal(row["amount"])
    return totals
''',
    "tests/test_ledger.py": '''import unittest
from decimal import Decimal
from ledger import daily_totals


class LedgerTests(unittest.TestCase):
    def test_smoke(self):
        rows = [dict(timestamp="2026-01-01T12:00:00", amount="1.20", currency="USD")]
        self.assertEqual(daily_totals(rows), {("2026-01-01", "USD"): Decimal("1.20")})
''',
}
PROMPTS = [
    "Inspect README.md, ledger.py and the existing tests. Fix daily_totals so dates follow the documented UTC semantics, including offset timestamps crossing midnight and naive timestamps. Preserve Decimal arithmetic, currency separation and cancelled-payment behavior. Add regression tests and run them.",
    "Extend the ledger for refunds. An optional kind field is sale by default. Sale adds amount, refund subtracts amount, and any other kind raises ValueError for a non-cancelled row. Add regression tests, including mixed sales and refunds, and run the suite. Preserve the previous behavior.",
    "Add totals_by_currency(rows), returning currency-to-Decimal totals using the same settlement and refund semantics as daily_totals. Empty input returns an empty dict. Accept any iterable, including generators. Add tests and run the suite. Follow the current project instructions.",
    "Add format_totals(rows), returning a list of dicts with date, currency and total, sorted by date then currency. Aggregate with daily_totals and format totals as fixed two-decimal strings using the current rounding rule in AGENTS.md. Add boundary-rounding tests and run the suite. Preserve all previous APIs and behavior.",
]
# Runs outside the project, independently of tests that the agent can modify.
ORACLE = '''import importlib.util, sys
from decimal import Decimal
spec = importlib.util.spec_from_file_location("ledger", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
stage = int(sys.argv[2])
def row(ts="2026-01-02T00:15:00+02:00", amount="0.10", currency="USD", **kw):
    return dict(timestamp=ts, amount=amount, currency=currency, **kw)
rows = [row(), row(amount="0.20"), row(ts="2026-01-01T23:30:00-02:00", amount="4.00"),
        row(ts="2026-01-01T12:00:00", amount="2.50", currency="EUR"),
        row(amount="999.00", status="cancelled")]
expected = {("2026-01-01", "USD"): Decimal("0.30"),
            ("2026-01-02", "USD"): Decimal("4.00"),
            ("2026-01-01", "EUR"): Decimal("2.50")}
assert m.daily_totals(iter(rows)) == expected
assert m.daily_totals([]) == {}
assert all(isinstance(v, Decimal) for v in m.daily_totals(rows).values())
if stage >= 2:
    refunds = rows + [row(amount="0.05", kind="refund")]
    assert m.daily_totals(refunds)[("2026-01-01", "USD")] == Decimal("0.25")
    assert m.daily_totals([row(kind="other", status="cancelled")]) == {}
    try:
        m.daily_totals([row(kind="other")])
    except ValueError:
        pass
    else:
        raise AssertionError("Unknown non-cancelled kind must raise ValueError")
if stage >= 3:
    assert m.totals_by_currency(iter(refunds)) == {"USD": Decimal("4.25"), "EUR": Decimal("2.50")}
    assert m.totals_by_currency([]) == {}
if stage >= 4:
    formatted = m.format_totals(iter(rows + [row(amount="0.005", currency="GBP"),
        row(amount="0.005", currency="CAD", kind="refund")]))
    assert formatted == [
        {"date": "2026-01-01", "currency": "CAD", "total": "-0.01"},
        {"date": "2026-01-01", "currency": "EUR", "total": "2.50"},
        {"date": "2026-01-01", "currency": "GBP", "total": "0.01"},
        {"date": "2026-01-01", "currency": "USD", "total": "0.30"},
        {"date": "2026-01-02", "currency": "USD", "total": "4.00"}]
    assert m.format_totals([]) == []
print("Independent checks passed")
'''


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def records(state):
    paths = list((state / "harn/sessions").glob("*/journal.jsonl"))
    if not paths:
        return []
    if len(paths) != 1:
        raise RuntimeError("Expected exactly one benchmark session")
    lines = paths[0].read_bytes().splitlines(keepends=True)
    return [json.loads(line) for line in lines if line.endswith(b"\n")]


def final_response(events, marker):
    cycle = max((i for i, e in enumerate(events) if e["type"] == "cycle"), default=-1)
    items = [e["data"] for e in events[cycle + 1:] if e["type"] == "item"]
    if cycle < 0 or any(i.get("type") == "function_call" for i in items):
        return False
    return any(marker in p.get("text", "") for i in items if i.get("role") == "assistant"
               for p in i.get("content", []) if isinstance(p, dict))


class Session:
    def __init__(self, binary, project, output, state, provider, model, resume):
        self.output, self.state = output, state
        self.logs = [output.with_suffix(".stdout").open("wb"), output.with_suffix(".stderr").open("wb")]
        command = [str(binary), "-C", str(project), "--provider", provider, "-m", model]
        if resume:
            command.append("--resume")
        self.process = subprocess.Popen(command, env=dict(os.environ, XDG_STATE_HOME=str(state)),
                                        stdin=subprocess.PIPE, stdout=self.logs[0], stderr=self.logs[1])

    def send(self, command, timeout, compact=False):
        start = time.monotonic()
        before = len(records(self.state))
        self.process.stdin.write((command + "\n").encode())
        self.process.stdin.flush()
        while time.monotonic() - start < timeout:
            events = records(self.state)[before:]
            done = any(e["type"] == "compact" for e in events) if compact else final_response(events, command.rsplit(" ", 1)[-1])
            if done:
                elapsed = round((time.monotonic() - start) * 1000)
                # The committed final item precedes return to the idle event loop.
                time.sleep(.1)
                return elapsed
            if self.process.poll() is not None:
                raise RuntimeError(f"harn exited with {self.process.returncode}. See {self.output}")
            time.sleep(.02)
        raise TimeoutError(f"No committed completion within {timeout}s. See {self.output}")

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.stdin.write(b"/quit\n")
                self.process.stdin.flush()
                self.process.wait(timeout=10)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.process.stdin.close()
            for log in self.logs:
                log.close()
        if self.process.returncode:
            raise RuntimeError(f"harn exited with {self.process.returncode}. See {self.output}")


def evaluate(project, stage):
    oracle = subprocess.run(["python3", "-I", "-c", ORACLE, str(project / "ledger.py"), str(stage)],
                            capture_output=True, text=True, timeout=15)
    tests = subprocess.run(["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
                           cwd=project, capture_output=True, text=True, timeout=30)
    return dict(passed=oracle.returncode == 0 and tests.returncode == 0,
                oracle_exit=oracle.returncode, oracle_output=oracle.stdout + oracle.stderr,
                tests_exit=tests.returncode, tests_output=tests.stdout + tests.stderr)


def usage_totals(groups):
    total = {key: sum(g[key] for g in groups) for key in
             ("attempts", "completed", "input_tokens", "cached_tokens", "cache_input_tokens", "cache_measured")}
    total["input_not_reported_reused"] = (total["cache_input_tokens"] - total["cached_tokens"]
                                           if total["cache_measured"] == total["attempts"] else None)
    total["cache_read_percent"] = (round(100 * total["cached_tokens"] / total["cache_input_tokens"], 2)
                                   if total["cache_input_tokens"] else None)
    return total


def run_workflow(args, output, project, mode, repetition):
    if project.exists():
        shutil.rmtree(project)
    project.mkdir()
    for name, content in {**FILES, "AGENTS.md": POLICY}.items():
        path = project / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
    output.mkdir()
    state, session = output / "state", None
    result = dict(mode=mode, repetition=repetition, turns=[], compaction=None)
    try:
        for index, prompt in enumerate(PROMPTS):
            if index == 2:
                (project / "AGENTS.md").write_text(UPDATED_POLICY)
            if index == 3:
                if session is None:
                    session = Session(args.binary, project, output / "compact", state, args.provider, args.model, True)
                result["compaction"] = dict(wall_ms=session.send("/compact", args.timeout, compact=True))
                if mode == "resume":
                    session.close()
                    session = None
                if args.pause_seconds:
                    time.sleep(args.pause_seconds)
            start = time.monotonic()
            if session is None:
                session = Session(args.binary, project, output / f"turn-{index + 1}", state,
                                  args.provider, args.model, index > 0)
            marker = f"WORKFLOW_STAGE_{index + 1}_DONE"
            launch_ms = (time.monotonic() - start) * 1000
            elapsed = session.send(prompt + " Finish your final answer with " + marker, args.timeout)
            wall = round(launch_ms + elapsed)
            if mode == "resume":
                session.close()
                session = None
            check = evaluate(project, index + 1)
            row = dict(stage=index + 1, wall_ms=wall, **check)
            result["turns"].append(row)
            print(json.dumps(dict(run=output.name, stage=index + 1, passed=row["passed"], wall_ms=wall)), flush=True)
            save(output / "result.json", result)
            if not row["passed"]:
                break
    finally:
        if session is not None:
            session.close()
    result["successful_tasks"] = sum(t["passed"] for t in result["turns"])
    result["workflow_passed"] = result["successful_tasks"] == len(PROMPTS)
    result["task_wall_ms"] = sum(t["wall_ms"] for t in result["turns"])
    result["workflow_wall_ms"] = result["task_wall_ms"] + (result["compaction"] or {}).get("wall_ms", 0)
    groups = summarize((state / "harn/sessions").glob("*/journal.jsonl"))
    result["usage"] = groups
    result.update(usage_totals(groups))
    success = result["successful_tasks"]
    result["ms_per_successful_task"] = result["workflow_wall_ms"] / success if success else None
    uncached = result["input_not_reported_reused"]
    result["uncached_per_successful_task"] = uncached / success if success and uncached is not None else None
    save(output / "result.json", result)
    shutil.copytree(project, output / "final-project")
    return result


def aggregate(runs):
    result = []
    for mode in dict.fromkeys(r["mode"] for r in runs):
        cohort = [r for r in runs if r["mode"] == mode]
        turns = [t["wall_ms"] for r in cohort for t in r["turns"]]
        successes = sum(r["successful_tasks"] for r in cohort)
        uncached = [r["input_not_reported_reused"] for r in cohort]
        result.append(dict(mode=mode, runs=len(cohort), successful_workflows=sum(r["workflow_passed"] for r in cohort),
            successful_tasks=successes, attempted_tasks=len(turns),
            wall_ms_per_successful_task=sum(r["workflow_wall_ms"] for r in cohort) / successes if successes else None,
            uncached_per_successful_task=sum(uncached) / successes if successes and None not in uncached else None,
            turn_wall_ms_median=statistics.median(turns) if turns else None,
            turn_wall_ms_p95=sorted(turns)[math.ceil(.95 * len(turns)) - 1] if turns else None,
            input_tokens=sum(r["input_tokens"] for r in cohort), cached_tokens=sum(r["cached_tokens"] for r in cohort),
            attempts=sum(r["attempts"] for r in cohort)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("output", type=Path, help="New directory. Contains private prompts and logs.")
    parser.add_argument("--provider", default="chatgpt")
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--modes", nargs="+", choices=("persistent", "resume"), default=["persistent", "resume"])
    parser.add_argument("--pause-seconds", type=float, default=0)
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    if args.repetitions < 1 or args.pause_seconds < 0 or args.timeout <= 0:
        parser.error("Repetitions and timeout must be positive, pause must be nonnegative")
    args.binary, args.output = args.binary.resolve(), args.output.resolve()
    args.output.mkdir(parents=True)
    manifest = dict(started_at=datetime.now(timezone.utc).isoformat(), binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        provider=args.provider, model=args.model, repetitions=args.repetitions, modes=args.modes, pause_seconds=args.pause_seconds,
        workload_sha256=hashlib.sha256(json.dumps([FILES, POLICY, UPDATED_POLICY, PROMPTS, ORACLE]).encode()).hexdigest(),
        acceptance="Repeated >=10% less task time or >=20% less reported uncached input, with no correctness regression. Small-sample p95 is descriptive only.")
    save(args.output / "manifest.json", manifest)
    runs = []
    for repetition in range(args.repetitions):
        order = args.modes if repetition % 2 == 0 else list(reversed(args.modes))
        for mode in order:
            run = run_workflow(args, args.output / f"{repetition + 1}-{mode}", args.output / "project", mode, repetition + 1)
            runs.append(run)
            save(args.output / "results.json", dict(manifest=manifest, runs=runs, cohorts=aggregate(runs)))
    print(json.dumps(aggregate(runs), indent=2))


if __name__ == "__main__":
    main()
