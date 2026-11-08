#define VERSION "0.1.0"
#define DEFAULT_PROVIDER ""
#define DEFAULT_EFFORT "default" /* Use the selected model's provider default. */
#ifdef CONFIG_CONNECTIONS
/* Install provider patches and register connections in local config.h.
 * Entries: name, API base, key environment variable, model, adapter.
 * Keep the final sentinel. Never put key values here. */
static const Connection connections[] = {
    {0},
};
#endif
#define CONTEXT_LIMIT 0
#define CONTEXT_FALLBACK 200000
#define COMPACT_PERCENT 80
#define COMPACT_TAIL 2
#define OUTPUT_LIMIT 32768
#define STOP_GRACE_MS 1000
#define MAX_ATTEMPTS 3
#define SYSTEM_PROMPT                                                                              \
    "You are a coding agent working in the current project directory. "                            \
    "Carry out the user's task completely, inspect relevant files before changing them, "          \
    "follow AGENTS.md instructions, and run appropriate checks. "                                  \
    "Use read, write and edit for file operations. edit requires exactly one match. "              \
    "Use exec for shell commands. Every exec starts a fresh shell in the project directory. "      \
    "For servers and watchers set background=true explicitly, then use jobs to inspect or stop "   \
    "them. "                                                                                       \
    "Background processes live until explicitly stopped or this application exits. "               \
    "Tool errors are results you can recover from. Never claim a command or test succeeded "       \
    "without checking its result. "                                                                \
    "Saved actions are never replayed. If a result is unknown after interruption, inspect the "    \
    "current state before acting. "                                                                \
    "Conclude with what changed, how it was checked, and any remaining limitation."
