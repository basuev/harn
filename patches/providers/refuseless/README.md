# Refuseless

Core commit: `4aba7b2290b29902c529d678c5cb3b9a46657e7d`

Adapter revision: `1`. Apply independently of other provider patches.

## Install

Keep this directory outside the source checkout when switching revisions.
From a clean checkout of the core commit above:

```sh
git apply --check /path/to/refuseless/provider.patch
git apply /path/to/refuseless/provider.patch
```

For a new configuration, copy `providers/refuseless/config.example.h` to `config.h`.
For an existing configuration, include the adapter header and add this entry
before the final `{0}` sentinel. Do not overwrite personal settings.

```c
#include "providers/refuseless/provider.h"
/* Connection entry: */
{"refuseless", "https://api.refuseless.com/v1", "REFUSELESS_API_KEY",
 "deepseek-v4.1-flash-abliterated", &refuseless_provider},
```

Then run `make`, `make check`, and `make install PREFIX="$HOME/.local"`.
The patch adds only `providers/refuseless/`, including fixtures and build additions.

## Dependencies

No dependencies beyond the core. Adapter code is ISC-licensed.

## Authentication and protocol

Set `REFUSELESS_API_KEY` in the launching shell, then run:

```sh
harn --provider refuseless
```

Uses stateless streaming Responses and function calls. Compatibility is not
claimed for every model in the service catalog. Choose a model that supports
the required tool and continuation contract.

Normalizes native `reasoning_efforts` metadata. No universal effort list is imposed.

## Verification

```sh
python3 providers/refuseless/check.py
```

This builds and installs a fixture configuration in `.build/`, then tests the
installed binary against local HTTP/PTY fixtures. It does not use your account
or call the real service. Live service verification has not been performed for
this extraction. See [the shared contract](../../../docs/provider-patches.md).
