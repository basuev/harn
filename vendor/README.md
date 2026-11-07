# Third-party sources

linenoise comes from antirez/linenoise at
`a473823d74b93eab2ba83480df16ed37617493f2`, under its BSD-style license in the
source headers. The local input parser incorporates incremental escape, UTF-8,
and bracketed-paste handling from antirez/ds4 at
`b3f50180ec2c7324892be1f50bc83555ccb8d5e7`. Local additions provide Ctrl-G editor
handoff, Ctrl-Z suspension handoff without changing the draft,
draft replacement, and the POSIX `strings.h` declaration. Automatic command
suggestions replace upstream Tab previews. Their temporary rows participate in
multiline cleanup and cursor geometry, including the overflow range/count footer
or inline count when only one row fits, with locally frozen candidates, arrow or
Ctrl-N/Ctrl-P selection, Tab insertion, and Escape dismissal. Ctrl-N/Ctrl-P keep
history navigation when suggestions are not visible. Alt-Backspace recognizes
ESC DEL and ESC Ctrl-H, reusing UTF-8 and fold-aware previous-word deletion even
when the escape and backspace arrive in separate reads. Enter accepts the selection and
opens argument suggestions with a trailing space when available, otherwise it
submits the completed command. Tab never executes. Enter also submits an already
matching draft or a draft with no visible suggestion. The multiline renderer
also handles logical newlines, word wrapping, and a separate status line in the
prompt. Short pastes remain visible. Long pastes retain the upstream folding
mechanism. Rendering remains based on linenoise.
Raw mode is retained between submitted messages so local command output cannot
flush already queued keystrokes. Editor handoff and exit still restore it.

LibJWT 3.6.1 comes from benmcollins/libjwt at
`94e3cdf35d120c4d869851714a2cf9f582e9b61e`. Its sources and upstream build files
are included under MPL-2.0, see [libjwt/LICENSE](libjwt/LICENSE). The application
build enables the OpenSSL and Jansson backends and builds only the static
library target.
