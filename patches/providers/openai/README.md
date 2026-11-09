# OpenAI API

Core commit: `de667c98782cb004598763935ab1c967e837371c`

Adapter revision: `2`. Apply independently of other provider patches.

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

Uses the ordinary Responses codec without generated cache keys, explicit TTL,
breakpoints or configuration-update items. Selected effort is sent on each
request. The documented `gpt-6.1-sol` context budget and effort choices supplement
the API's minimal model catalog. Other model entries use service metadata.
See [prompt caching](../../../docs/prompt-caching.md) for the retained core scope.

## Verification

```sh
python3 providers/openai/check.py
```

This builds and installs a fixture configuration and exercises local HTTP/PTY
requests, authentication errors, streaming, tools, compaction, continuation,
resume, connection isolation and request-level effort changes. It does not use real API
credentials or call OpenAI. Live API-key verification has not been performed.
See [the shared contract](../../../docs/provider-patches.md).
