# ChatGPT subscription

Core commit: `802b58dc2b9e24ede5725db8f5bf630f071b6f8e`

Adapter revision: `2`. Apply independently of other provider patches.

## Install

Keep this directory outside the source checkout when switching revisions.
From a clean checkout of the core commit above:

```sh
git apply --check /path/to/chatgpt/provider.patch
git apply /path/to/chatgpt/provider.patch
```

For a new configuration, copy `providers/chatgpt/config.example.h` to `config.h`.
For an existing configuration, include the adapter header and add this entry
before the final `{0}` sentinel. Do not overwrite personal settings.

```c
#include "providers/chatgpt/provider.h"
/* Connection entry: */
{"chatgpt", "https://api.openai.com/v1", "",
 "gpt-6.1-sol", &chatgpt_provider},
```

Then run `make`, `make check`, and `make install PREFIX="$HOME/.local"`.
The patch adds only `providers/chatgpt/`, including fixtures and build additions.

## Dependencies

CMake, OpenSSL 3 development headers, and the normal core dependencies.
The patch includes LibJWT 3.6.1 at
`94e3cdf35d120c4d869851714a2cf9f582e9b61e`, built statically with OpenSSL and
Jansson. Its MPL-2.0 license is included and installed as `LICENSE.libjwt`.

## Authentication and protocol

Uses subscription OAuth/OIDC, not an API key. Service eligibility is required.
Existing `credentials.json`, `registration.json` and `refresh.lock` are retained
for the connection named `chatgpt`. Additional connection names use separate files.

```sh
harn --provider chatgpt --login
harn --provider chatgpt
harn --provider chatgpt --logout
```

## Verification

```sh
python3 providers/chatgpt/check.py
```

This builds and installs a fixture configuration in `.build/`, then tests the
installed binary against local HTTP/PTY fixtures. It does not use your account
or call the real service. Live verification on 2026-11-09 used subscription OAuth with `gpt-6.1-sol`: read-tool round-trips, stateless continuation, session resume and compaction followed by continuation. See [the shared contract](../../../docs/provider-patches.md).

## Prompt caching

A stable key is preserved across a session and its resumes. Subscription inference
currently rejects API TTL, breakpoint, diagnostics and configuration-update
fields, even when the generic catalog advertises effort updates. The adapter
therefore uses subscription-confirmed capabilities. See [prompt caching](../../../docs/prompt-caching.md)
and [measured results](../../../docs/cache-evaluation.md).
