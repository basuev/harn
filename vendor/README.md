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
history navigation when suggestions are not visible. Word movement recognizes
Alt-B/F and modified Left/Right sequences for Alt, Ctrl and Meta, using UTF-8
and fold-aware character steps. Alt-Backspace recognizes
ESC DEL and ESC Ctrl-H, reusing UTF-8 and fold-aware previous-word deletion even
when the escape and backspace arrive in separate reads. Enter submits selected
top-level commands so the application can consistently load and open argument
pickers. An argument candidate ending in a space opens its next suggestions,
otherwise Enter submits the completed command. Separate display labels let
session choices show request text while retaining executable IDs. Labels and
descriptions clip by terminal-cell width without splitting UTF-8. Tab never
executes. Enter also submits an already matching draft or a draft with no visible
suggestion. The multiline renderer also handles logical newlines, word wrapping,
and a multiline prompt with connection and live status together. Short pastes remain visible. Long pastes retain the upstream folding
mechanism. Rendering remains based on linenoise.
Raw mode is retained between submitted messages so local command output cannot
flush already queued keystrokes. Editor handoff and exit still restore it.
