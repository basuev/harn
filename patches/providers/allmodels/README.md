# AllModels

Core commit: `3b89ab329f7e09b25d12f9b769ff1a09ac781bb1`

Adapter revision: `1`. Apply independently of other provider patches.

## Install

Keep this directory outside the source checkout when switching revisions.
From a clean checkout of the core commit above:

```sh
git apply --check /path/to/allmodels/provider.patch
git apply /path/to/allmodels/provider.patch
```

For a new configuration, copy `providers/allmodels/config.example.h` to `config.h`.
For an existing configuration, include the adapter header and add this entry
before the final `{0}` sentinel. Do not overwrite personal settings.

```c
#include "providers/allmodels/provider.h"
/* Connection entry: */
{"allmodels", "https://api.allmodels.io/oai", "ALLMODELS_API_KEY",
 "deepseek/deepseek-v4.1-flash-abliterated", &allmodels_provider},
```

Then run `make`, `make check`, and `make install PREFIX="$HOME/.local"`.
The patch adds only `providers/allmodels/`, including fixtures and build additions.

## Dependencies

No dependencies beyond the core. Adapter code is ISC-licensed.

## Authentication and protocol

Set `ALLMODELS_API_KEY` in the launching shell, then run:

```sh
harn --provider allmodels
```

Uses stateless streaming Responses and function calls. Compatibility is not
claimed for every model in the service catalog. Choose a model that supports
the required tool and continuation contract.

## Verification

```sh
python3 providers/allmodels/check.py
```

This builds and installs a fixture configuration in `.build/`, then tests the
installed binary against local HTTP/PTY fixtures. It does not use your account
or call the real service. Live service verification has not been performed for
this extraction. See [the shared contract](../../../docs/PROVIDER_PATCHES.md).
