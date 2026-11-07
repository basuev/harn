# harn

A small terminal coding agent in C.

Normal terminal scrollback, configuration in `config.h`, and shell commands
and files as the extension interface.

## Build

Requires a C11 compiler (GCC or Clang), make, pkg-config, CMake, and development
headers for libcurl with asynchronous DNS, Jansson, OpenSSL 3, libyaml, and MD4C.
LibJWT and linenoise are vendored.

From the source directory:

```sh
make
make install PREFIX="$HOME/.local"
```

Put `~/.local/bin` on `PATH`.

## Run

Run from your project directory. The default connection uses
[ChatGPT sign-in](https://developers.openai.com/siwc/token-sharing-open-source),
which is in preview and requires eligible subscription access.

```sh
harn --login
harn
```

Resume the most recently active session for that directory:

```sh
harn --resume
```

Inside a running instance, `/resume` opens suggestions for previous sessions in
that project, most recently active first, with the first request as a preview.
Select with arrows or Ctrl-N/P and press Enter. Switching stops the previous
session's owned processes, just like exiting it. A failed switch leaves the
current session and its processes intact.

`harn --help` lists startup options. `/help` lists conversation commands.

## Configuration

The first build copies `config.def.h` to your untracked `config.h`. Edit the local
file for connections and defaults, then rebuild. API keys belong in the named
environment variables, never in the source. Only ChatGPT sign-in is configured
in the public defaults. To add a Responses endpoint, add a connection with its
base URL, key environment variable and model ID to the local connection array.

`make release` always builds from `config.def.h`, independently of personal
settings. Release sources do not contain your local `config.h`.

`/effort` loads the selected provider's model catalog and lists its advertised
reasoning levels. Without capability metadata only `default` is offered, which
omits `reasoning.effort`. Explicit choices are remembered per provider and model
within the session, including on resume. Switching to a model without a saved
choice uses `default`. Unsupported explicit choices fail without an inference
request or a silent fallback.

- Personal instructions: `~/.config/harn/AGENTS.md`.
- Directory instructions: `AGENTS.md` in the project, its parents, and visited subdirectories.
- Skills: `~/.agents/skills`, `~/.harn/skills`, `.agents/skills`, `.harn/skills`.
- Sessions: `~/.local/state/harn/sessions`.

Project skills override personal skills. At either level, `.harn` overrides
`.agents`. Configuration and session storage respect `XDG_CONFIG_HOME` and
`XDG_STATE_HOME`, respectively.

See [DESIGN.md](DESIGN.md) for scope and architectural decisions.

## Checks

The local fixture tests require Python 3 and make no model requests to external
services:

```sh
make check
```

`make check-release` requires Python 3.12 or newer and checks a fresh build,
the fixture tests, and installation from the release archive in an isolated
environment.
`make check-terminal` additionally requires Xvfb, xauth, st, Vim, and `computer-use`.

## License

[ISC](LICENSE). Third-party licenses are included in `vendor/`.
