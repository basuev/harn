# Provider patches

Providers are optional source patches, compiled into a personal harn build.
The core has no service connections. See [DESIGN.md](../DESIGN.md) for rationale.

## Adapter boundary

A provider adapter handles authentication, model metadata, request encoding,
stream decoding and service-specific errors. It uses the core HTTP transport
and event loop, including cancellation and bounded retries.

Responses adapters can reuse the core codec. Other protocols must produce the
same internal messages, `function_call` / `function_call_output` pairs, `call_id`,
usage and completion status. Partial responses must not execute tools.

The core owns tool execution, jobs, UI, history, sessions and compaction.
Adapters must not replace these mechanisms. Credentials and private protocol
state belong to one connection and must not leak into another connection or
conversation logs. Login, refresh and logout are required only for adapters
whose authentication needs them.

## C interface

Declare a `const Provider` from [harn.h](../harn.h) and reference it in the last
field of a `Connection` in `config.h`. See [tests/fixture.c](../tests/fixture.c)
for a minimal adapter. Keep the final `{0}` connection sentinel.

Required operations are `ready(force)`, `models(status)`, `catalog(body)`,
`headers()`, `encode(request)` and `event(net)`. `models` sets the HTTP status
and returns the raw catalog. `catalog` returns an array with `slug`, `visibility`
and available context/effort metadata. `encode` converts canonical history to
wire JSON. `event` consumes `Net.data` at an SSE event boundary, clears that
buffer and updates canonical `response`, `output`, `text` and `malformed` fields.
Only `response.status == "completed"` with a valid stream is accepted as success.

JSON inputs are borrowed, returned JSON values are owned by the caller. The
transport takes ownership of the list returned by `headers`. Common helpers
include `api_key_ready`, `api_key_logout`, `bearer_headers`, `responses_models`,
`catalog_data`, `catalog_models`, `responses_encode` and `responses_event`.

Optional operations are `logout`, `refresh`, `classify`, `legacy`, `pollfds`,
`tick` and `close`. Error classification returns -1 for terminal, 0 for common
policy, or 1 for retryable. `pollfds(NULL)` returns the descriptor count.
Store opaque connection state in `app.provider_state`, release it in `close`,
and leave the shared credentials object for core cleanup. `legacy` matches
historical connection metadata, never supplies credentials from history.

## Patch contents

Publish `patches/providers/<id>/README.md` and `provider.patch`. The README must
state the exact supported core commit, dependencies and licenses, configuration
snippet, test command and any live verification performed.

The patch adds its code, declarations, example configuration and fixture tests
under `providers/<id>/`. Optional build flags and dependencies go in `build.mk`.
Core build hooks discover these files. The user registers connections explicitly
in local `config.h`. Patches must not modify that file, public defaults, other
adapters or shared core files. Changes to the core interface belong upstream.

Adapters compile into the binary. There is no runtime loader or stable binary
ABI. Source compatibility is tied to the stated core revision. Independently
installable adapters must not depend on another provider being installed.

## Required verification

Run the installed binary against a local HTTP fixture through CLI and PTY.
Function-level mocks alone are insufficient. Tests must demonstrate:

- Patch application, build and installation with no unrelated provider required.
- Correct authentication, rejection and cancellation without credential leakage.
  OAuth adapters also cover identity validation, refresh and logout.
- Model selection and advertised effort levels. `default` leaves effort to the
  service. Unsupported choices fail without silent substitution. Context limits
  and usage distinguish reported values from unknown or fallback values.
- Incremental text and a complete tool round-trip, including multiple calls,
  fragmented input and rejection of incomplete or malformed responses.
- Cancellation, bounded retries and terminal errors without replaying tools or
  stopping independent jobs.
- Conversation continuation, compaction and session resume with model and effort
  preserved. Switching A -> B -> A isolates credentials and metadata and does
  not forward another connection's opaque reasoning or protocol identifiers.

`make check` runs core tests and installed adapters' fixture tests without
external services or real credentials. A live-support claim additionally needs
an explicitly run tool round-trip and continuation, recording date, model and
authentication method. Fixture success alone is not evidence of live support.

## Installation

Obtain the patch and supported core commit from its README. Keep the patch
outside the checkout when switching revisions. In a clean source checkout:

```sh
git checkout --detach <core-commit>
git apply --check /path/to/provider.patch
git apply /path/to/provider.patch
```

Create `config.h` from `config.def.h` if it does not exist, then add the README's
connection snippet. Keep key values outside source files. Build and install:

```sh
make
make check
make install PREFIX="$HOME/.local"
```

Additional adapters follow the same steps on the same core revision. Updates
use a fresh checkout and reapply the selected patches. Standard releases are
built from unpatched core sources, never from a personal patched checkout.
