# harn

A small terminal coding agent in C.

Normal terminal scrollback, configuration in `config.h`, and shell commands
and files as the extension interface.

## Build

Requires a C11 compiler, make, pkg-config, and development headers for libcurl
with asynchronous DNS, Jansson, libyaml, and MD4C. Provider patches may add
explicit dependencies.

```sh
make
make install PREFIX="$HOME/.local"
```

The first build copies `config.def.h` to untracked `config.h`. The core has no
built-in service connections. Install a [provider patch](docs/PROVIDER_PATCHES.md)
and add its connection to `config.h` before starting a conversation. API key
values belong in environment variables, not source files.

## Run

Put `~/.local/bin` on `PATH`. Select a connection registered in your local build:

```sh
harn --provider my-provider
```

Subscription adapters document their sign-in command. `harn --help` lists
startup options. `/help` lists conversation commands. `harn --version` identifies
the core revision and configured adapter revisions.

Resume the most recently active session for the current directory:

```sh
harn --resume
```

Inside a running instance, `/resume` offers previous sessions in that project,
most recently active first, with the first request as a preview. Select with
arrows or Ctrl-N/P and press Enter. Switching stops the previous session's
owned processes. A failed switch leaves the current session and processes intact.

`/provider` switches configured connections without restarting. `/model` selects
a model and `/effort` offers its advertised reasoning levels. `default` leaves
effort to the service. Explicit choices are remembered per connection and model,
including on resume. Unsupported choices fail without silent substitution.

## Local configuration

`config.h` and applied adapters under `providers/` are local and ignored by Git.
The public patch catalog lives in `patches/providers/`. Standard releases export
committed, unpatched sources rather than the personal working tree.

- Personal instructions: `~/.config/harn/AGENTS.md`.
- Directory instructions: `AGENTS.md` in the project, its parents, and visited subdirectories.
- Skills: `~/.agents/skills`, `~/.harn/skills`, `.agents/skills`, `.harn/skills`.
- Sessions: `~/.local/state/harn/sessions`.

Project skills override personal skills. At either level, `.harn` overrides
`.agents`. Configuration and session storage respect `XDG_CONFIG_HOME` and
`XDG_STATE_HOME`, respectively.

See [DESIGN.md](DESIGN.md) for architectural decisions and
[provider patches](docs/PROVIDER_PATCHES.md) for the adapter contract and installation.

## Checks

Local fixture checks use no external services or real credentials:

```sh
make check
```

This checks the core and every installed adapter. `make check-patches` additionally
applies the published patches to their pinned core revision and checks isolated
and combined installations. `make check-release` verifies a clean release archive,
source build and installation. Release checks require Python 3.12 or newer.
`make check-terminal` additionally requires Xvfb, xauth, st, Vim and `computer-use`.

## License

[ISC](LICENSE). Installed adapters carry their own additional dependency licenses.
