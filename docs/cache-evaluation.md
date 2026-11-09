# Cache evaluation

Measured on 2026-11-09 using ChatGPT subscription OAuth and `gpt-6.1-sol`.
The baseline binary was built from core revision `88c501a`. The implementation
binary was built from the working tree during this change. Binary SHA-256 values,
individual routing observations and aggregate measurements are retained in
[cache-evaluation.json](cache-evaluation.json). Raw local sessions and process
logs are under `.build/cache-evaluation/`. They contain personal instructions and
are deliberately excluded from the public repository.

## Starting state

The historical 2026-11-05 through 2026-11-09 logs contained 1,320 ordinary requests
explicitly labeled `chatgpt`, with 191,065,074 input tokens and 94,585,088 cache
reads, a weighted read rate of 49.50%. AllModels and Refuseless reported 98.69%
and 94.94%, respectively, on different workloads. These are descriptive account
observations, not matched provider comparisons or the result of this change.

The original implementation rewrote initial instructions when AGENTS.md or the
skill catalog changed. Compaction used different instructions and omitted tools.
It retained root usage but did not record every network attempt, wire-level
changes or first-text/tool latency. No cache accounting was exposed by `/context`.

## Targets and observed outcomes

- Live policy updates should preserve the previously submitted prefix while
  applying the current rules. Before, the initial instruction field changed.
  After, rule and catalog changes append snapshots. Local integration checks
  verify preservation, deletion, resume and unchanged write-delivery checks.
- Every HTTP inference attempt should have a journal record. The paired run
  changed from zero attempt records to 18 records for 18 calls, with monotonic
  latency measurements. Retry and incomplete-stream cases are also verified.
- Warm reuse of an identical long prompt should exceed 90% when the service
  retains it. In the routing probe, the final two keyed requests reached 99.63%.
  This target was met in that probe, not established for arbitrary agent turns.
- Compaction should reuse part of the normal prefix. Reported reads changed from
  0 of 39,716 input tokens to 34,176 of 42,050, or 81.27%. Both runs continued
  successfully after compaction. The reuse target was met.
- The complete workload should reduce repeated processing and elapsed time.
  This target was not met in the measured pair. Keep measuring real workloads
  before claiming lower daily subscription consumption or faster task completion.

## Matched agent workflow

`tests/cache_live.py` ran six individual file-reading turns followed by six
short follow-ups. It changed AGENTS.md on turn 8, selected high effort on turn
10 and restored provider-controlled effort on turn 11. Each build made 18
inference requests and returned all 12 exact expected answers. Each run used
an isolated state directory and project with equivalent generated fixtures.
The before run preceded the after run under the same account.

Before:

- Input: 499,502 tokens, including 22,400 reported cache reads.
- Weighted cache read rate: 4.48%.
- Input not reported as reused: 477,102 tokens.
- Sum of process wall times: 74,192 ms.
- Median process wall time for follow-up turns: 4,341 ms.

After:

- Input: 503,620 tokens, including 20,992 reported cache reads.
- Weighted cache read rate: 4.17%.
- Input not reported as reused: 482,628 tokens.
- Sum of process wall times: 76,724 ms.
- Median process wall time for follow-up turns: 4,552.5 ms.
- Median measured network-request duration: 3,048 ms.
- Median first-text time: 2,793 ms over the 12 text-producing responses.
- Median first-tool time: 3,116 ms over the six tool-producing responses.

There is no demonstrated overall speedup. The additional policy snapshot after
the instruction change adds input, and cache reuse remains service-dependent.
The old binary does not provide comparable first-text timings. One sequential
pair cannot distinguish a small regression from ordinary service variance.

## Routing isolation probe

Eight identical approximately 42K-token requests alternated between no cache
key and the stable key `harn:controlled-comparison`. No tools were executed.

- Four requests without a key: 0 reads out of 167,540 input tokens.
- Four with the key: 97,920 reads out of 167,540, or 58.45% including warm-up.
- The last two keyed requests each read 41,728 of 41,885 tokens, or 99.63%.
- Median first-text times were 2,973 ms without the key and 3,568.5 ms with it.

This supports stable routing keys for this subscription connection but does not
demonstrate a latency gain. Repeated identical prompts isolate reuse better than
an agent conversation, which grows and can change model-side configuration.

## Compaction pair

After the matched workloads, each binary compacted its own session and answered
the exact follow-up `COMPACTION_CONTINUED` successfully.

- Before: 39,716 input tokens, zero reads, 227 output tokens, 8,546 ms wall time.
- After: 42,050 input tokens, 34,176 reads, 251 output tokens, 9,811 ms wall time.
- The after request measured 8,450 ms network duration and 3,352 ms to first text.

The after request reused 81.27% of its input. The number of input tokens not
reported as reused fell from 39,716 to 7,874. Elapsed time did not improve in
this pair, and the summaries differed in size. No monetary or quota savings are
inferred from these counters.

## Scope and limitations

Exploratory short and multi-file batches are retained in the JSON artifact. Their
results also failed to demonstrate an overall gain. The multi-file runs differed
in tool batching, so the matched individual-read workflow is the main comparison.

Subscription capability probes rejected API TTL, breakpoint and configuration
update fields. The implementation respects those restrictions instead of treating
the generic model catalog as proof of authorization-specific support. See
[prompt caching](prompt-caching.md) for the exact findings.

The separate OpenAI API-key adapter has local fixture verification. No OpenAI
API key was available for a live API run. Its fixtures establish request encoding,
stream handling, accounting and continuation, not actual API cache-hit rates.
