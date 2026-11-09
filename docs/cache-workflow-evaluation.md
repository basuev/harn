# Coding workflow cache evaluation

Measured on 2026-11-09 with ChatGPT subscription OAuth and `gpt-6.1-sol`.
Aggregates, individual trials, hashes and limitations are retained in
[cache-workflow-evaluation.json](cache-workflow-evaluation.json). Private source
journals and logs remain under `.build/cache-workflow/`. These results describe
the experimental binary before the subsequent production cleanup.

## Observed outcome

There is still no demonstrated overall performance improvement. Six complete
coding workflows passed all 24 independent task checks. Before/after comparison
uses eight successful tasks per binary in persistent mode. Two additional current
resume workflows passed another eight tasks.

- Before: 64.07 seconds and 54,782 input tokens not reported as reused per
  successful task. Both compaction and local tool time are included.
- Target: at most 57.66 seconds or 43,826 such input tokens per task, without
  losing correctness.
- Current: 61.70 seconds and 58,180 such input tokens per task. Observed time
  decreased 3.70%, while non-reused reported input increased 6.20%.
- Weighted reads increased from 3.49% to 5.26%. Total input increased 8.18%.
  More cache reads did not produce lower total non-reused input.
- The two time comparisons moved in opposite directions: -8.61% and +1.90%.
  Output tokens fell 7.46%, providing another possible explanation for the
  aggregate time difference. Cache causality is not established.
- Descriptive task p95 was 67,553 ms before and 66,506 ms after. Eight task
  samples per binary do not establish a tail-latency guarantee.

Current resume mode measured 60.84 seconds per successful task, 1.40% below
current persistent mode. The model made 63 requests instead of 67 and produced
different edits and output. This does not establish a process-lifetime cache
advantage. There is no evidence here that users need to keep harn open for cache
performance.

One additional current workflow ended externally with status 143 after three
successful stages. It recorded 25 attempts and an open 26th request without final
usage. It is retained separately and replaced by a fresh complete workflow.
The interrupted replicate was slower, so censoring can bias complete-case
comparisons. Its unfinished remote consumption remains unknown and is not
included in claims about total experiment cost.

## Mechanism results

All 36 controlled calls completed and passed the protocol checks. Each variant
has two trials with two warm-up calls and one measured probe.

- Key: measured probes reported zero reads both with and without a key. Across
  all calls, the no-key variant read 20.26% and the keyed variant read zero.
  This does not reproduce the earlier key association. A reliable routing or
  speed benefit has not been established for this subscription connection.
- Policy: measured probes read 84.30% with appended snapshots and zero with
  rewritten initial instructions. Including warm-up, non-reused input was
  39,802 versus 57,728 tokens, a reduction of 31.05%. Both trials improved this
  narrow metric. Probe duration medians were 3,748.5 and 3,861 ms, respectively.
  This supports prefix preservation during a policy change, not a whole-day
  savings claim. The appended request itself contained 1,013 more input tokens.
- Compaction: shared-prefix probes read 30.97%, compared with 49.18% for distinct
  summary instructions. Including warm-up, non-reused input was 51,000 versus
  49,938 tokens. Summary duration medians were 24,507.5 and 24,997 ms, with 2,088
  and 2,105 output tokens. The earlier large reuse advantage did not repeat.
  Summary requests can reuse input, but an efficiency improvement is unproven.

## Decisions and daily work

Retain the verified policy snapshot semantics and their removal, resume and
compaction behavior. The policy probe supports this architectural choice.
Delta delivery is deferred because its additional semantics have no demonstrated
end-to-end benefit in this workload. Snapshot growth should be measured on
ordinary project-rule and catalog changes before changing this contract.

After reviewing these results, the user selected a smaller production scope.
Generated cache keys, explicit TTL and breakpoints, effort-update state, runtime
prefix fingerprints and the shared compaction prefix were removed. Compaction
uses dedicated summary instructions without tool definitions. Selected effort
is sent as an ordinary request field. The API-key adapter remains optional and
uses the ordinary Responses codec. Prefix inspection is an offline operation.
See [the current scope](prompt-caching.md).

Users keep the existing workflow: project rules apply automatically, compaction
preserves continuation, and sessions can be resumed. `/context` exposes reported
reuse and timing. No warm-up inference, padding or extra manual cache action is
part of ordinary work. Live evaluation is explicit and separate from local tests.

The next performance decision should use multiple project workloads, balanced
binary order and provider diagnostics when available. The API-key adapter needs
its own live evaluation before an API recommendation. No OpenAI API key was
available here. Until then, the time and input acceptance thresholds remain
unmet. The new runner and independent grader make that failure reproducible.

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

The same workload ran against the retained pre-change binary from
revision `88c501a`, identified by the SHA-256 in the first evaluation. The two
binary suites run sequentially, so their comparison remains exposed to service
drift. Model-generated edits and request counts can differ even when both pass
the same independent checks. A lower aggregate number alone is not sufficient
to attribute an improvement to caching. Controlled probes address narrower
mechanisms under identical recorded histories.

The primary before/after comparison uses two persistent workflows per binary.
The current binary also has two resume workflows to check process lifetime and
restoration separately. Baseline and current suites use different generated
project directories. There is no long-pause or representative-production-traffic
claim from these short isolated runs.

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

The earlier read-only workload is retained in commit `8b8e88f` as
`tests/cache_live.py` for reproducing [historical measurements](cache-evaluation.md).
It was removed from the current tree. The coding workflow is the retained
acceptance workload.

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

Offline prefix inspection also accepts journals without policy updates or
compaction and does not need credentials:

```sh
python3 tests/cache_probe.py /path/to/journal.jsonl .build/cache-inspection --inspect
```

Live compaction probes explicitly construct both request variants, regardless of
which strategy was used by the recorded source binary. Experimental parameters
in these probes are not production defaults.
