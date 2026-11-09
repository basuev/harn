# Prompt caching

The harness should preserve reusable conversation input while continuing to apply
live project rules. Cache performance is an observed property of a connection,
model, authorization method and workload. An accepted parameter or a model name
alone does not establish a cache hit, a latency improvement or subscription savings.

## Policy delivery

The stable system instructions define scope and precedence. A developer message
contains the current AGENTS.md sources and available skill catalog. A later full
snapshot replaces the earlier source list, including deletions. Changes are
appended where they take effect instead of rewriting the conversation prefix.
Compaction pins the latest snapshot if its original item leaves active history.
Read skill bodies retain their separate exact-snapshot semantics.

Direct writes still compare current applicable sources with those delivered in
the preceding inference request. Discovering or changing rules during a tool
cycle does not authorize a prepared write under those new rules.

## Connection capabilities

Adapters supply optional normalized model metadata:

- `prompt_cache.key`: send a stable session routing key outside prompt content.
- `prompt_cache.ttl`: enable Responses cache options with this request lifetime.
- `prompt_cache.static_ttl`: lifetime of the initial developer-prefix breakpoint.
- `prompt_cache.diagnostics`: compare with the preceding completed response from
  the same connection and model.
- `supports_reasoning_effort_updates`: preserve the request-level effort and
  append `configuration_update` items for explicit effort changes.

These fields are adapter capabilities, not a claim that every compatible endpoint
implements the same protocol. The core does not infer them from model names.
Saved capabilities support the existing temporary catalog-outage behavior.

The optional OpenAI API adapter documents confirmed capabilities for
`gpt-6.1-sol`. Its request and static lifetimes default to `"30m"`. Personal
`config.h` may set `OPENAI_PROMPT_CACHE_TTL` and `OPENAI_STATIC_CACHE_TTL` to
`"30m"` or `"12h"`. Longer retention has a higher write price. There is no
background prewarming, keep-alive inference or prompt padding.

The API effort setting remains unchanged across explicit updates. Returning to
`default` starts a new configuration epoch and omits explicit overrides, so the
service controls its default again. Model changes, connection changes and local
compaction also start a new epoch. Updates from another connection are never
forwarded. Portable text compaction does not promise preserved opaque reasoning.

## Subscription verification

Live checks on 2026-11-09 with ChatGPT subscription OAuth and `gpt-6.1-sol`
established a narrower protocol than the public API documentation:

- `prompt_cache_key` is accepted.
- `prompt_cache_options` is rejected as unsupported on this model.
- `prompt_cache_breakpoint` is rejected as unsupported on this model.
- `prompt_cache_retention` is rejected as an unsupported parameter.
- `configuration_update` is rejected with
  `subscription_sharing_unsupported_capability`, despite the catalog advertising
  `supports_reasoning_effort_updates: true`.

The subscription adapter therefore uses a stable session key and normal explicit
effort fields. It overrides the inapplicable API capability advertisement. It
does not repeatedly discover these restrictions by failing user requests.
Modern API controls remain available through the separate API-key adapter.
Local fixtures verify that the two authentication modes receive different fields.

## Compaction

The summary request keeps normal system instructions and tool definitions.
Its final user message explicitly treats prior requests as history and asks for a
factual continuation note. `tool_choice: "none"` prevents new tool calls. The
normal prefix can be reused up to a matching cached boundary. The next inference
starts from the summary and retained cycles, so a lower cache-read fraction is
expected. The existing 80% trigger remains unchanged. Evaluate the summary request
and subsequent work together instead of maximizing cache-read percentage alone.

## Evidence and accounting

The journal's `request` events and `request.json` represent the encoded wire
request. History references still avoid duplicating growing conversation input.
`cache_request` contains equality fingerprints and the first differing request
fields plus the preserved input-item prefix. Fingerprints preserve JSON ordering,
exclude breakpoint annotations for prefix comparison and never include headers.
They diagnose changes to submitted input, not the provider's hidden rendered text.

Each HTTP inference attempt, including retries, cancellation and incomplete
streams, produces `request_attempt`. It retains reported root usage, response ID,
actual model, upstream provider, service tier, cache diagnostics and the whitelisted
`x-request-id` header when supplied. Duration and first text/tool timings use a
monotonic clock. A tool timestamp measures the first streamed tool indication,
not local execution. A response with no text has no first-text measurement.
Full per-item usage attribution remains in the successful `usage` event rather
than being duplicated in the attempt record.

`/context` shows the last request and session cache accounting by connection,
model and purpose. Missing counters remain unknown. The token hit rate divides
summed reported reads by summed inputs from attempts that report both counters.
Unreported attempts remain visible in measurement coverage. Compaction is separate.
Legacy usage records remain readable. New usage events are not counted again
after their attempt event, and nested attribution is never added to root totals.
Reported tokens are not a claim about an unreported or interrupted bill.

`tests/cache_report.py` summarizes journals without network access or assumed
prices. `tests/cache_live.py` is an explicit live evaluation using existing login
or key configuration and an isolated project/state directory:

```sh
python3 tests/cache_live.py /path/to/harn .build/cache-evaluation/run
python3 tests/cache_report.py .build/cache-evaluation/run/state/harn/sessions/*/journal.jsonl
```

Use the same workload and account for before/after runs. Warm identical-request
comparisons isolate reuse, while full agent runs also include model decisions,
instruction updates and effort changes. Do not infer daily savings from either
one short run or API tariffs applied to a subscription. Measured results and
acceptance targets are recorded in [cache evaluation](cache-evaluation.md).

## Official references

- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
- [OpenAI cache diagnostics](https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics)
- [Preserved reasoning](https://developers.openai.com/api/docs/guides/preserved-reasoning)
- [Reasoning configuration updates](https://developers.openai.com/api/docs/guides/reasoning)
- [Sol model capabilities and API prices](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [Subscription inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
