#!/usr/bin/env python3
"""Opt-in controlled ChatGPT replays. Generated tool calls are never executed."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.request
import uuid

from cache_workflow import save

POLICY_PREFIX = "Current harn policy snapshot:"
LEGACY_SUMMARY = (
    "Summarize the conversation for a coding agent continuing the same task. Preserve the "
    "user's goal and all constraints, project instructions, completed changes and checks, "
    "important paths, tool errors, unresolved work, and current job states including "
    "user-requested stops. Distinguish confirmed results from unknown outcomes. Do not repeat "
    "actions or invent results. Return a concise factual continuation note.")


def content(item):
    value = item.get("content", "")
    return value if isinstance(value, str) else "".join(p.get("text", "") for p in value)


def wire_requests(journal):
    items, requests = [], []
    for line in journal.read_text().splitlines():
        event = json.loads(line)
        if event["type"] == "item":
            items.append(event["data"])
        elif event["type"] == "request":
            data = event["data"]
            if data.get("provider") != "chatgpt":
                raise ValueError("Controlled replay currently supports ChatGPT OAuth journals only")
            wire = copy.deepcopy(data["request"])
            wire["input"] = [copy.deepcopy(items[i] if isinstance(i, int) else i) for i in data["selection"]]
            requests.append(wire)
    return requests

def fixed_reply(wire):
    wire = copy.deepcopy(wire)
    wire["input"].append(dict(role="user", content="Reply exactly REPLAY_READY. Do not use tools."))
    wire["tool_choice"] = "none"
    return wire


def rewrite_policy(wire):
    wire = copy.deepcopy(wire)
    snapshots = [i for i in wire["input"] if i.get("role") == "developer" and content(i).startswith(POLICY_PREFIX)]
    if not snapshots:
        raise ValueError("No harn policy snapshot in source request")
    wire["input"] = [i for i in wire["input"] if i not in snapshots]
    wire["instructions"] += "\n\n" + content(snapshots[-1])
    return wire


def select_sources(requests):
    compact = next(i for i, r in enumerate(requests) if r.get("tool_choice") == "none")
    policy_change = next(i for i, r in enumerate(requests[:compact]) if sum(
        it.get("role") == "developer" and content(it).startswith(POLICY_PREFIX) for it in r["input"]) > 1)
    if policy_change < 1 or compact < 1:
        raise ValueError("Source must contain work before policy change and compaction")
    return requests[policy_change - 1], requests[policy_change], requests[compact - 1], requests[compact]


def request(wire, credentials, endpoint, timeout):
    # Credentials are read only in this process and never copied into artifacts.
    token = json.loads(credentials.read_text())["access_token"]
    raw = json.dumps(wire, separators=(",", ":")).encode()
    req = urllib.request.Request(endpoint, data=raw, headers={
        "Authorization": "Bearer " + token, "Content-Type": "application/json"})
    start = time.monotonic()
    result = dict(outcome="incomplete", wire_sha256=hashlib.sha256(raw).hexdigest(),
                  first_text_ms=None, first_tool_ms=None)
    response, items = None, []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as stream:
            result["http_status"] = stream.status
            for line in stream:
                if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                    continue
                event = json.loads(line[6:])
                kind = event.get("type", "")
                elapsed = round((time.monotonic() - start) * 1000)
                if kind == "response.output_text.delta" and result["first_text_ms"] is None:
                    result["first_text_ms"] = elapsed
                if kind == "response.output_item.added" and event.get("item", {}).get("type") == "function_call":
                    result["first_tool_ms"] = result["first_tool_ms"] or elapsed
                if kind == "response.output_item.done":
                    items.append(event["item"])
                if kind == "response.completed":
                    response = event["response"]
                    result["outcome"] = "completed" if response.get("status") == "completed" else "incomplete"
                if kind in ("response.failed", "error"):
                    result["outcome"] = "service_error"
    except urllib.error.HTTPError as error:
        result.update(outcome="http_error", http_status=error.code)
    except (OSError, ValueError, http.client.HTTPException):
        result["outcome"] = "transport_error"
    result["duration_ms"] = round((time.monotonic() - start) * 1000)
    if response:
        usage = copy.deepcopy(response.get("usage") or {})
        usage.pop("attribution", None)
        result.update(usage=usage, response_id=response.get("id"), response_model=response.get("model"),
                      service_tier=response.get("service_tier"))
        if response.get("output"):
            items = response["output"]
    result["reply"] = "".join(content(i) for i in items if i.get("role") == "assistant").strip()
    result["tool_calls"] = sum(i.get("type") == "function_call" for i in items)
    return result


def totals(rows):
    measured = [r for r in rows if isinstance(r.get("usage", {}).get("input_tokens"), int)
                and isinstance(r.get("usage", {}).get("input_tokens_details", {}).get("cached_tokens"), int)]
    inputs = sum(r["usage"]["input_tokens"] for r in measured)
    cached = sum(r["usage"]["input_tokens_details"]["cached_tokens"] for r in measured)
    times = [r["duration_ms"] for r in rows]
    first = [r["first_text_ms"] for r in rows if r["first_text_ms"] is not None]
    return dict(attempts=len(rows), completed=sum(r["outcome"] == "completed" for r in rows),
        valid=sum(r["valid"] for r in rows), measured=len(measured), input_tokens=inputs, cached_tokens=cached,
        output_tokens=sum(r.get("usage", {}).get("output_tokens", 0) for r in rows),
        input_not_reported_reused=inputs-cached if len(measured) == len(rows) else None,
        cache_read_percent=round(100*cached/inputs, 2) if inputs else None,
        duration_ms_median=statistics.median(times) if times else None,
        first_text_ms_median=statistics.median(first) if first else None)


def aggregate(rows):
    groups = []
    for factor, variant in dict.fromkeys((r["factor"], r["variant"]) for r in rows):
        cohort = [r for r in rows if r["factor"] == factor and r["variant"] == variant]
        groups.append(dict(factor=factor, variant=variant, all_calls=totals(cohort),
                           probes=totals([r for r in cohort if r["phase"] == "probe"])))
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path, help="Completed cache_workflow session journal")
    parser.add_argument("output", type=Path, help="New private artifact directory")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--factors", nargs="+", choices=("key", "policy", "compaction"), default=["key", "policy", "compaction"])
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--endpoint", default="https://api.openai.com/v1/responses")
    parser.add_argument("--credentials", type=Path, default=Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home()/".config"))) / "harn/credentials.json")
    args = parser.parse_args()
    if args.repetitions < 1 or args.warmups < 1 or args.timeout <= 0:
        parser.error("Repetitions, warmups and timeout must be positive")
    args.output.mkdir(parents=True)
    before_policy, after_policy, ordinary, compact = select_sources(wire_requests(args.journal))
    save(args.output / "manifest.json", dict(started_at=datetime.now(timezone.utc).isoformat(),
        source_sha256=hashlib.sha256(args.journal.read_bytes()).hexdigest(), repetitions=args.repetitions,
        warmups=args.warmups, factors=args.factors, endpoint=args.endpoint,
        controls="Same recorded wire/history, balanced order, no tool execution, cold-inclusive and probe results reported separately."))
    rows = []
    for factor in args.factors:
        variants = dict(key=["no-key", "stable-key"], policy=["rewrite", "append"], compaction=["distinct", "shared"])[factor]
        for repetition in range(args.repetitions):
            order = variants if repetition % 2 == 0 else list(reversed(variants))
            for variant in order:
                key = "harn:probe:" + uuid.uuid4().hex
                if factor == "policy":
                    warm, probe = fixed_reply(before_policy), fixed_reply(after_policy)
                    if variant == "rewrite":
                        warm, probe = rewrite_policy(warm), rewrite_policy(probe)
                elif factor == "compaction":
                    warm, probe = fixed_reply(ordinary), copy.deepcopy(compact)
                    if variant == "distinct":
                        probe["instructions"] = LEGACY_SUMMARY
                        probe.pop("tools", None)
                else:
                    warm = probe = fixed_reply(ordinary)
                for phase_index in range(args.warmups + 1):
                    phase = "probe" if phase_index == args.warmups else "warmup"
                    wire = copy.deepcopy(probe if phase == "probe" else warm)
                    if factor == "key" and variant == "no-key":
                        wire.pop("prompt_cache_key", None)
                    else:
                        wire["prompt_cache_key"] = key
                    # Full requests stay private and contain no authorization headers.
                    name = f"{factor}-{repetition + 1}-{variant}-{phase_index}"
                    save(args.output / (name + ".request.json"), wire)
                    row = request(wire, args.credentials, args.endpoint, args.timeout)
                    row.update(factor=factor, variant=variant, repetition=repetition+1, phase=phase)
                    summary = factor == "compaction" and phase == "probe"
                    row["valid"] = row["outcome"] == "completed" and not row["tool_calls"] and (bool(row["reply"]) if summary else row["reply"] == "REPLAY_READY")
                    rows.append(row)
                    save(args.output / "results.json", dict(rows=rows, groups=aggregate(rows)))
                    print(json.dumps({k: row[k] for k in ("factor", "variant", "repetition", "phase", "outcome", "valid", "duration_ms")}), flush=True)
                    if row["outcome"] != "completed":
                        raise SystemExit("Replay failed. See private artifacts. No automatic retry performed.")
    print(json.dumps(aggregate(rows), indent=2))


if __name__ == "__main__":
    main()
