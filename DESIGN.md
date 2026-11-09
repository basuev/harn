# Design

## Scope

The core should remain understandable as a whole. C and configuration in
`config.h` are deliberate choices, not temporary steps toward a framework.
Shell commands, project files, and skill packages are the extension boundary.
A plugin runtime, a model SDK, and a dependency on another coding client are
outside that boundary.

Public defaults and personal configuration have different ownership. The core
has no built-in service connections. Provider implementations are optional source
patches, applied locally under ignored `providers/` and registered in `config.h`.
Release builds export committed sources into a clean directory and use
`config.def.h`. Merely selecting public configuration would not remove applied
adapter sources. Neither personal configuration nor applied modules is packaged.
Fixture connections are isolated from installed and release binaries.

Adding an infrequent connection may require rebuilding. Switching among
configured connections during a conversation must not require another binary
or restart. The same tools, journal, and process accounting serve every
connection.

Validation stays in local Makefile targets. Hosted CI is deliberately omitted.

`README.md` and `DESIGN.md` are the root documentation entry points. Detailed
contracts and integration references belong in `docs/`, using `lower-kebab-case.md`
filenames. Keep conventional `README.md` entry points in self-contained directories
and tool-required names such as `AGENTS.md` and `SKILL.md`.

The version remains 0.1.0 until the first published release. Local rebuilds and
interface changes alone do not require a version increase.

## Terminal and process ownership

Ordinary terminal scrollback was chosen over a full-screen interface to retain
normal terminal habits. Markdown uses the terminal's available width rather
than a fixed reading measure. Presentation must not change provider history,
journal contents, or batch output. Full project paths and model names wrap
rather than being truncated. The prompt keeps provider, model, effort and live
status together to leave more vertical space for the conversation. Session
choices prioritize the first request over technical IDs, including in narrow
terminals, because recognizing the task matters more than its storage identity.
Completion is local and keeps candidates fixed until the draft changes, so
asynchronous output cannot silently change the user's selection. Enter submits
a selected top-level command. Selection commands then open their argument picker,
loading model capabilities when needed, regardless of whether a catalog is cached.
Tab only inserts a suggestion.

Streaming Markdown prioritizes visible progress over perfect retroactive styling.
An unfinished inline construct appears literally while its opener can still be
redrawn. Closing it within that area applies Markdown normally. Once its opener
leaves that area, that construct stays literal through its closing delimiter.
Later constructs still receive normal formatting. Resizing also commits the
current preview because the old layout can no longer be safely revisited. This
presentation-only compromise avoids waiting for an arbitrarily long delimiter
pair without rewriting scrollback, provider history, or batch output.

One event loop keeps local process controls available during network requests,
retry delays, and editor handoff. User commands and model tools share ownership
of the same jobs. Finishing a model turn does not stop a background server.
Exiting harn does, so owned processes do not accumulate between sessions.
Deliberate detachment and SIGKILL remain outside that cleanup guarantee.

Suspension is not cancellation. Foreground work pauses with harn, background
jobs continue, and remote inference may still finish. Returning to the foreground
must consume the pending response without repeating completed actions.

Files and logs supply detailed inspection instead of a parallel interactive
inspection interface. Batch stdout contains the model's response, while
execution receipts and diagnostics go to stderr. Displayed diffs cover direct
file tools, not every change an arbitrary shell command might make.

## Dependencies

libcurl multi was selected after testing HTTPS streaming alongside terminal
input and child output. Asynchronous DNS is required to preserve responsiveness.
Jansson is the single JSON representation for both network traffic and state.
LibJWT with OpenSSL was selected after isolated ID-token validation checks
rather than implementing JWT verification locally. The optional subscription adapter owns LibJWT and its static build. Core and
API-key-only builds do not need that dependency or the OAuth crypto code.

LibYAML preserves compatibility with standard SKILL.md frontmatter, including
multiline descriptions. MD4C avoids maintaining a separate Markdown grammar.
A pinned linenoise with local input and redraw changes avoids an additional
terminal UI library. Vendored revisions, licenses, and patch scope are recorded
in `vendor/README.md`.

Release binaries use the build host's Linux architecture and shared-library ABI,
not a portable static runtime. Native installation needs no language toolchain,
but still needs the runtime libraries. Corresponding sources and third-party
licenses belong in the release archive.

## Provider contract

The core uses Responses-shaped history items and supplies a reusable HTTP/SSE
transport and Responses codec. Adapters translate their wire protocol to these
items and own authentication, catalogs and service-specific error classification.
An "OpenAI-compatible" label alone does not establish correct streaming, function
calls or stateless continuation. A different protocol needs an adapter, not a
second agent loop. Subscription authorization belongs to its optional patch,
separate from journals and from other clients' credentials.

A real subscription response established that
`response.completed.response.output` can be empty after completed tool calls
have already arrived. The adapter therefore collects `response.output_item.done`
items by index and accepts them only after `response.completed`. Complete
provider items, including opaque reasoning, are needed for stateless
continuation within the same connection.

That private representation is not assumed to transfer across connections.
Switching preserves conversation facts and function-call/result pairs without
forwarding the earlier provider's opaque reasoning, item IDs, or namespace
metadata. Original items stay intact in the journal. Equal model IDs do not
justify sharing authorization or context metadata between connections.

Connections select an explicit statically linked adapter. An empty key variable
never selects an authentication protocol implicitly. Optional poll/tick callbacks
serve browser authorization through the same cancellable event loop. Opaque
adapter state is released when leaving its connection. API-key helpers and
Responses codecs are shared, service-specific behavior is not.

Endpoint settings come from the current build rather than stale journal URLs.
A saved conversation does not provide credentials. A public model catalog also
does not prove that an API key is valid.

Streaming inference has no total wall-clock deadline. A real long-context,
high-effort request received HTTP 200 but was cut off by the former five-minute
curl deadline twice. Connection establishment and metadata/auth requests still
have finite deadlines. Inference remains interruptible through the event loop.
The journal records curl codes, diagnostic messages and stream status so HTTP
success cannot mask an incomplete response.

## Instructions and skills

Personal rules belong to harn's configuration directory. Shared skill packages
under `.agents` must not implicitly supply global instructions for harn.
Directory rules do not require a Git boundary, so a parent AGENTS.md can serve
several projects. Filesystem aliases of the personal file retain its single
global scope rather than appearing again as directory rules.

AGENTS.md sources are live policy outside history compaction. Changes must reach
the model before a prepared direct write or edit can proceed. Symlinks use the
actual destination's scope. For arbitrary shell commands, the model remains
responsible for inspecting applicable rules. This is instruction delivery, not
a sandbox for shell execution.

Skill instructions instead represent what the model actually read. Their exact
snapshots survive compaction and resume, even if the package changes or vanishes.
A later full read supersedes the snapshot. The catalog is not a mode selector,
and applying a skill does not grant another agent's private tools. Shared and
harn-specific packages coexist so local choices do not require editing shared
packages.

## History and context

Prompt-cache reuse is optimized at the adapter boundary and measured per HTTP
attempt. Live instruction changes append authoritative snapshots instead of
rewriting the prefix. See [prompt caching](docs/prompt-caching.md) for policy
precedence, capability boundaries, subscription findings and accounting.

Ctrl-C prioritizes discarding a nonempty input draft over cancelling active
work. Only an empty input allows Ctrl-C to interrupt the turn. This priority
also applies to SIGINT received through the signal pipe.

In-instance creation keeps the current provider, model and selected effort,
not the original command-line overrides or other models' effort preferences.
It starts fresh conversation, instruction, skill and usage state. Creation and
resume use the same ownership exchange so failed creation preserves the old
conversation and its jobs.

In-instance resume keeps the current session open until the target has loaded
and acquired its journal lock. Failure must preserve the current conversation
and owned jobs. Success ends the old session's process ownership before exposing
the resumed conversation. Startup provider, model and effort overrides apply only
to startup, not to later session selections.

Resume reconstructs the conversation, not the execution of saved actions.
Unknown outcomes remain unknown until inspected. Interrupting work does not
roll back completed changes. Compaction replaces part of the model's active
input without rewriting the original journal. Request selections refer back to
journal items instead of duplicating the growing input on every turn.

Compaction tests exposed a failure where an old request such as "reply exactly
ACK" was followed instead of summarized. An explicit final instruction to
summarize the historical conversation fixed this. Increasing the retained tail
alone did not. Two retained cycles then passed the three agreed continuation
comparisons, including after restart.

The local compaction trigger remains 80%. A paired subscription experiment
compared 80% and 90% from the same long conversation with mostly synthetic logs.
Both passed two coding continuations, but 90% showed no correctness advantage
and used substantially more measured input. There was one paired run per task,
and cache conditions differed. This informed the default, not a universal
optimum or a published reproducible benchmark.

The displayed context estimate and compaction decision must refer to the same
last measured input. They are not measurements of the next request. Output
usage is recorded separately. Changed instructions and compaction invalidate
the previous input measurement. A local fallback budget must not be presented
as a provider-confirmed window.

A temporary catalog outage may reuse explicitly labeled saved metadata for the
selected model. This prevents a metadata outage from unnecessarily blocking
inference, without masking authorization failures, invalid catalogs, or an
authoritative model removal.

## Reasoning effort

Effort is an explicit request setting, not a measure of how much thinking
occurred. `default` omits the API field and is not the same as `none`. Historical
sessions that omitted it must keep that behavior unless explicitly overridden.
New sessions default to provider-controlled effort. Available explicit values
come from the selected model's `supported_reasoning_levels` catalog metadata,
not a universal list. Without that metadata only `default` is exposed. Before a
catalog or saved capabilities are loaded, completion must not offer a misleading
default-only picker. Selecting `/effort` loads capabilities first,
then opens its argument suggestions without making an inference request. New
catalog levels need no client rebuild. Saved catalog capabilities remain usable
during the same temporary outages as saved context metadata.

Explicit choices belong to a provider/model pair within a session. Switching
restores that pair's choice or uses `default`, rather than carrying an unrelated
model's effort across endpoints. Historical string-valued effort events remain
readable. Unsupported settings remain errors rather than silently falling back
to another level. No automatic routing or speculative model capability registry
is implied.

Earlier requests omitted effort. The [Sol model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
and [reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
specified a medium default when explicit control was added. That explains the
historical interpretation, but is not per-response confirmation of server effort.

Optional decision helpers belong outside the core, using the shell and file
extension boundary. They are not a shipped routing layer or a runtime dependency.

## Provider patch contract

The [provider patch contract](docs/provider-patches.md) defines the adapter boundary,
source-level interfaces, acceptance tests and installation. Patches are pinned to
an exact core commit rather than a stable plugin ABI. Keeping ordinary adapters
in disjoint directories lets them compose without editing shared source files.
Changes to the common boundary belong in the core, not in adapter dependencies.
