# OpenAI API

Core commit: `4aba7b2290b29902c529d678c5cb3b9a46657e7d`

Adapter revision: `1`. Apply independently of other provider patches.

## Install

Keep this directory outside the source checkout when switching revisions.
From a clean checkout of the core commit above:

```sh
git apply --check /path/to/openai/provider.patch
git apply /path/to/openai/provider.patch
```

For a new configuration, copy `providers/openai/config.example.h` to `config.h`.
For an existing configuration, include the header and register the connection
before the final sentinel. Never overwrite personal configuration.

```c
#include "providers/openai/provider.h"
/* Connection entry: */
{"openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-6.1-sol", &openai_provider},
```

Set `OPENAI_API_KEY` in the launching shell, then run `make`, `make check`,
and `make install PREFIX="$HOME/.local"`. No key belongs in source files.

## Dependencies and protocol

No dependencies beyond the core. Adapter code is ISC-licensed. Uses API-key
authorization and stateless streaming Responses, independently of ChatGPT OAuth.

Confirmed `gpt-6.1-sol` capabilities include an initial developer-prefix
breakpoint, implicit history caching, cache diagnostics, and explicit effort
updates through `configuration_update`. Its API context budget and advertised
effort levels come from the official model documentation. Other model entries
retain metadata supplied by the service, without inferred capability flags.

`OPENAI_PROMPT_CACHE_TTL` and `OPENAI_STATIC_CACHE_TTL` in personal `config.h`
default to `"30m"`. Either may be `"12h"`, which has a higher cache-write price.
There is no automatic prewarming. See [prompt caching](../../../docs/prompt-caching.md).

## Verification

```sh
python3 providers/openai/check.py
```

This builds and installs a fixture configuration and exercises local HTTP/PTY
requests, authentication errors, streaming, tools, compaction, continuation,
resume, connection isolation and API cache controls. It does not use real API
credentials or call OpenAI. Live API-key verification has not been performed.
See [the shared contract](../../../docs/provider-patches.md).
