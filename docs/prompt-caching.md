# Prompt caching

The production scope is stable instruction delivery and factual cache accounting.
Live project rules must remain correct through edits, compaction and resume.
Reuse is an observed property of a provider, model, authorization and workload.
Accepted parameters alone do not establish faster work or lower consumption.

## Policy delivery

Stable system instructions define scope and precedence. A developer message
contains the current AGENTS.md sources and available skill catalog. A later full
snapshot replaces that source list and catalog, including deletions. Changes
append where they take effect instead of rewriting the submitted prefix.

Direct writes still compare current applicable sources with those delivered in
the preceding inference request. Changing rules during a tool cycle does not
authorize a prepared write under the new rules. Already read skill instructions
retain their exact-snapshot semantics independently of the available catalog.

## Requests and compaction

Ordinary Responses adapters use `responses_encode`. The core sends the selected
request-level effort, or omits it for the provider-controlled default. Catalog
advertisements do not enable cache keys, TTL, breakpoints or configuration-update
items. The optional OpenAI API adapter also uses ordinary Responses requests.

Compaction uses dedicated summary instructions, historical input and
`tool_choice: "none"`, without tool definitions. Its final user message explicitly
treats earlier tasks as history. No tools execute during summarization. The latest
policy snapshot and read skill bodies are preserved outside the summary when
needed, so continuation does not depend on a lossy note remembering every rule.
The existing 80% trigger remains unchanged.

Earlier experimental effort-update items are ignored on resume. The selected
effort is restored from its ordinary journal events and sent on each request.
No experimental state machine is retained for those historical items.

## Accounting

`request` events and the latest `request.json` represent encoded wire requests.
History references avoid duplicating growing conversation input. Each HTTP
attempt that returns control to the application produces `request_attempt`,
including retries, cancellation and incomplete streams. It records reported root
usage, response metadata, available cache diagnostics and the whitelisted
`x-request-id` header. Timing uses a monotonic clock for duration and the first
text or tool indication. A streamed tool timestamp does not measure execution.

Successful `usage` events retain full attribution. Attempt records omit its
nested attribution tree to avoid duplicating it. Root and nested usage are never
added together. Legacy usage is readable without counting new usage events twice.

`/context` shows the last request and session accounting by provider, model and
purpose. Missing counters remain unknown. Read percentage divides summed reported
reads by input from attempts reporting both values. Abrupt process termination
can leave an open `request` without final outcome or consumption. Completed
attempt totals do not establish its remote bill or subscription quota impact.

There is no runtime prefix fingerprinting or cache-key generation. Compare saved
wire input offline when diagnosing a prefix change:

```sh
python3 tests/cache_probe.py /path/to/journal.jsonl .build/cache-inspection --inspect
python3 tests/cache_report.py /path/to/journal.jsonl
```

Inspection compares recorded JSON fields and matching input items. It does not
contact a provider or reproduce the provider's hidden prompt rendering. Live
mechanism probes in the same script are explicit research operations.

## Decision evidence

The [coding workflow evaluation](cache-workflow-evaluation.md) measured a 31.05%
reduction in non-reused input for controlled policy updates including warm-up.
It did not establish overall savings, a reliable key benefit or a repeatable
compaction cache advantage. The user therefore selected the smaller production
scope above. Experimental request controls, per-item runtime fingerprints and
the old read-only benchmark were removed.

Those measurements describe the experimental binary before cleanup. The
[initial evaluation](cache-evaluation.md) remains historical evidence, not a
performance claim for the cleaned implementation. Performance acceptance remains
at least 10% less task time or 20% less non-reused reported input without losing
correctness, assessed on successful tasks across repeated workloads.

## Official references

- [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [Cache diagnostics](https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics)
- [Subscription inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
