#!/usr/bin/env python3
"""Opt-in live cache evaluation using an installed harn and its existing login."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from cache_report import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("output", type=Path, help="New directory for an isolated project, state and measurements")
    parser.add_argument("--provider", default="chatgpt")
    parser.add_argument("--model", default="gpt-6.1-sol")
    args = parser.parse_args()
    binary, output = args.binary.resolve(), args.output.resolve()
    output.mkdir(parents=True)
    project = output / "project"
    project.mkdir()
    policy = project / "AGENTS.md"
    policy.write_text("Use read for fixture reads. Do not modify files.\n")
    for shard in range(6):
        (project / f"fixture-{shard}.txt").write_text("".join(
            f"Shard {shard} record {i:04}: cedar river copper violet stable evidence {i*17:06}\n"
            for i in range(360)))
    prompts = [f"Read fixture-{i}.txt with read. Reply exactly BENCH_{i}." for i in range(6)]
    prompts += [f"Use the already read fixtures. Reply exactly BENCH_{i}. Do not use tools." for i in range(6, 12)]
    env = dict(os.environ, XDG_STATE_HOME=str(output / "state"))
    turns = []
    for i, prompt in enumerate(prompts):
        if i == 8:
            policy.write_text("Use read for fixture reads. Do not modify files. Reply concisely.\n")
        command = [str(binary), "-C", str(project), "--provider", args.provider, "-m", args.model, "-p", prompt]
        if i:
            command.append("--resume")
        if i == 10:
            command += ["--effort", "high"]
        if i == 11:
            command += ["--effort", "default"]
        start = time.monotonic()
        process = subprocess.run(command, env=env, capture_output=True, timeout=180)
        row = dict(turn=i, wall_ms=round((time.monotonic()-start)*1000), exit=process.returncode,
                   reply=process.stdout.decode(errors="replace").strip())
        turns.append(row)
        (output / f"turn-{i}.stderr").write_bytes(process.stderr)
        (output / "turns.json").write_text(json.dumps(turns, indent=2) + "\n")
        print(json.dumps(row), flush=True)
        if process.returncode:
            raise SystemExit(process.returncode)
    report = summarize((output / "state/harn/sessions").glob("*/journal.jsonl"))
    (output / "usage.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
