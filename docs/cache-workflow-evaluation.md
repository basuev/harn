# Coding workflow cache evaluation

## Decision and acceptance criteria

The first cache change established prefix preservation and observability, but
did not establish faster daily coding. This follow-up evaluates successful code
changes, rather than exact replies after synthetic reads. The working acceptance
criterion, selected before measurement, is a repeatable reduction of at least 10%
in elapsed time per successful task or 20% in input not reported as cache reads,
without losing correctness. Subscription counters are not a monetary or quota
bill. Small-sample p95 observations cannot establish a latency-tail guarantee.

The experiment does not change production defaults to chase a cache percentage.
API-key tests remain separate from ChatGPT subscription OAuth tests.

## Workload and controls

`tests/cache_workflow.py` creates a small merchant-ledger project. Four user
turns fix UTC date grouping, implement refunds, add currency totals and format
monetary output. The model must edit code, add tests and run them. An evaluator
outside the project separately checks all accumulated behavior after every turn.
It covers UTC boundaries, naive timestamps, Decimal arithmetic, cancellations,
refund validation, generators, currency separation, sorting and signed rounding.
The existing project test suite must also pass.

After the second turn, AGENTS.md gains a rounding rule. After the third, harn
compacts context before implementing the formatting feature. This tests effective
policy delivery and continuation through compaction in the complete workflow.

The two modes use the same binary, project path, starting files, prompts and
account. Persistent mode holds one process open. Resume mode starts a process
for every turn and for compaction. Each run has fresh isolated session state.
Two repetitions use the order persistent, resume, resume, persistent to reduce
simple ordering bias. This is a generated coding workload, not a random sample
of the user's projects. Model actions and service conditions remain variable.
Both modes use pipes, so this comparison tests process lifetime, not terminal
rendering or editor latency.

The same workload will also run against the retained pre-change binary from
revision `88c501a`, identified by the SHA-256 in the first evaluation. The two
binary suites run sequentially, so their comparison remains exposed to service
drift. Model-generated edits and request counts can differ even when both pass
the same independent checks. A lower aggregate number alone is not sufficient
to attribute an improvement to caching. Controlled probes address narrower
mechanisms under identical recorded histories.

Wall time includes model requests and local tools. Resume turn time also
includes process startup. Compaction is charged to the complete workflow.
Independent evaluation and inter-turn pauses are excluded. The runner waits for
committed final replies, not streamed text. Failed tasks remain in the cost
numerator. No cache counter is inferred when the service omits it.

`--pause-seconds` optionally adds an intentional pause after compaction. Its
default is zero. An immediate-resume experiment establishes no long-pause TTL.
Run artifacts include a pre-run manifest, binary and workload hashes, journals,
logs, independent check outputs and final project copies. They contain personal
instructions and belong under the ignored `.build/` directory.

```sh
python3 tests/cache_workflow.py ~/.local/bin/harn .build/cache-workflow/run-1
```

The earlier read-only workload remains `tests/cache_live.py` to reproduce the
historical measurements in [cache-evaluation.md](cache-evaluation.md). It is not
the acceptance workload for this follow-up.

## Controlled mechanism probes

`tests/cache_probe.py` reconstructs actual requests from a completed workflow
journal, including history references and inline summary items. It uses the
existing ChatGPT OAuth access token without storing it in artifacts. It does not
execute generated tool calls, refresh credentials or retry failures. Run harn
first so its ordinary authentication code can refresh the login if needed.

Each trial makes two warm-up calls and one probe. Two repetitions reverse the
variant order. A fresh stable key identifies each keyed trial. The no-key control
uses the service's implicit behavior, so neither isolation from other service
traffic nor cache eviction is assumed. Results report both all calls, including
warm-up, and the probe subset. The exact requests, hashes, output and root usage
remain in private artifacts.

- Key: identical recorded context and fixed reply, with or without a key.
- Policy: warm the preceding policy, then submit the recorded changed-policy
  request. Compare appended snapshots against a counterfactual that moves the
  latest full snapshot into the initial instructions and removes snapshot items.
  This isolates the delivery strategy, not every difference in the old binary.
- Compaction: warm the ordinary request, then submit the recorded summary
  request. Compare shared instructions and tools against the earlier distinct
  summary instructions without tools. The historical input and final summary
  task are the same. A nonempty summary without tool calls is protocol success,
  not an independent assessment of summary quality. The coding workflow checks
  actual continuation quality separately.

Fixed-reply probes suppress tool execution with `tool_choice: "none"`. Summary
lengths and output token counts can differ. Probe latency includes a fresh Python
HTTPS connection and must not be presented as harn's interactive latency.

```sh
python3 tests/cache_probe.py \
  .build/cache-workflow/run-1/1-persistent/state/harn/sessions/SESSION/journal.jsonl \
  .build/cache-workflow/probes-1
```
