#!/usr/bin/env python3
"""Summarize reported cache usage without calling a service or assuming prices."""
import argparse
import json
from pathlib import Path
import statistics


def summarize(paths):
    groups = {}
    for path in paths:
        with Path(path).open() as journal:
            for line in journal:
                event = json.loads(line)
                kind, data = event.get("type"), event.get("data")
                if not isinstance(data, dict):
                    continue
                if kind in ("usage", "compaction_usage"):
                    if data.get("cache_accounted"):
                        continue
                    data = dict(data, kind="compaction" if kind == "compaction_usage" else "inference",
                                outcome="completed")
                elif kind != "request_attempt":
                    continue
                key = (data.get("provider", "unknown"), data.get("model", "unknown"), data.get("kind", "inference"))
                group = groups.setdefault(key, dict(provider=key[0], model=key[1], kind=key[2],
                    attempts=0, completed=0, input_tokens=0, cached_tokens=0, cache_input_tokens=0,
                    cache_write_tokens=0, input_measured=0, output_tokens=0, output_measured=0,
                    cache_measured=0, writes_measured=0,
                    durations_ms=[], first_text_ms=[], first_tool_ms=[]))
                group["attempts"] += 1
                group["completed"] += data.get("outcome") == "completed"
                usage = data.get("usage") or {}
                details = usage.get("input_tokens_details") or {}
                total, cached, writes = usage.get("input_tokens"), details.get("cached_tokens"), details.get("cache_write_tokens")
                if isinstance(usage.get("output_tokens"), int):
                    group["output_measured"] += 1
                    group["output_tokens"] += usage["output_tokens"]
                if isinstance(total, int):
                    group["input_measured"] += 1
                    group["input_tokens"] += total
                    if isinstance(cached, int):
                        group["cache_measured"] += 1
                        group["cache_input_tokens"] += total
                        group["cached_tokens"] += cached
                if isinstance(writes, int):
                    group["writes_measured"] += 1
                    group["cache_write_tokens"] += writes
                timing = data.get("timing") or {}
                for source, target in (("duration_ms", "durations_ms"), ("first_text_ms", "first_text_ms"),
                                       ("first_tool_ms", "first_tool_ms")):
                    if isinstance(timing.get(source), int):
                        group[target].append(timing[source])
    result = []
    for group in groups.values():
        denominator = group["cache_input_tokens"]
        group["cache_read_percent"] = round(100 * group["cached_tokens"] / denominator, 2) if denominator else None
        for key in ("durations_ms", "first_text_ms", "first_tool_ms"):
            values = group.pop(key)
            group[key + "_median"] = statistics.median(values) if values else None
            group[key + "_measured"] = len(values)
        result.append(group)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journals", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.journals), indent=2))
