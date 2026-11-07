#include "harn.h"
#include <errno.h>
#include <fcntl.h>
#include <locale.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif

App app;
static volatile sig_atomic_t suspend_pending, suspend_sender;
static void signal_event(int sig)
{
    int save = errno;
    if (sig == SIGTSTP) {
        if (suspend_pending)
            return;
        suspend_pending = 1;
    }
    unsigned char b = (unsigned char)sig;
    (void)write(app.signal_pipe[1], &b, 1);
    errno = save;
}
static void stop_event(int sig, siginfo_t *info, void *context)
{
    (void)context;
    if (!suspend_pending)
        suspend_sender = info->si_pid;
    signal_event(sig);
}
void pump(int timeout)
{
    jobs_tick();
    auth_tick();
    ui_status();
    if (app.net) {
        int running;
        if (curl_multi_perform(app.multi, &running) != CURLM_OK) {
            app.net->result = CURLE_RECV_ERROR;
            app.net->done = 1;
        }
        CURLMsg *m;
        int remaining;
        while ((m = curl_multi_info_read(app.multi, &remaining)))
            if (m->msg == CURLMSG_DONE && m->easy_handle == app.net->easy) {
                app.net->result = m->data.result;
                app.net->done = 1;
            }
    }
    if (app.net && app.net->done)
        return;
    size_t count = 5;
    for (Job *j = app.jobs; j; j = j->next)
        if (j->fd >= 0)
            count++;
    struct curl_waitfd *fds = xmalloc(count * sizeof(*fds));
    unsigned int n = 0;
    fds[n++] = (struct curl_waitfd){.fd = app.signal_pipe[0], .events = CURL_WAIT_POLLIN};
    int input = -1;
    if (app.stdin_open && !app.editor_pid) {
        input = (int)n;
        fds[n++] = (struct curl_waitfd){.fd = 0, .events = CURL_WAIT_POLLIN};
    }
    n += (unsigned int)auth_pollfds(fds + n);
    for (Job *j = app.jobs; j; j = j->next)
        if (j->fd >= 0)
            fds[n++] = (struct curl_waitfd){.fd = j->fd, .events = CURL_WAIT_POLLIN};
    if (app.editor_pid && timeout > 50)
        timeout = 50;
    if (curl_multi_poll(app.multi, fds, n, timeout, NULL) != CURLM_OK) {
        app.quit = 1;
        app.cancel = 1;
    }
    if (fds[0].revents) {
        unsigned char signals[64];
        ssize_t len = read(app.signal_pipe[0], signals, sizeof(signals));
        for (ssize_t i = 0; i < len; i++) {
            if (signals[i] == SIGINT) {
                if (app.busy) {
                    app.cancel = 1;
                    ui_status();
                } else if (app.input_active) {
                    linenoiseEditReplace(&app.input, "", 0);
                }
            } else if (signals[i] == SIGTSTP) {
                ui_suspend(!suspend_sender || suspend_sender == app.editor_pid);
                suspend_pending = 0;
            } else if (signals[i] == SIGTERM || signals[i] == SIGHUP) {
                app.quit = 1;
                app.cancel = 1;
            } else if (signals[i] == SIGWINCH && app.input_active)
                ui_input();
        }
    }
    if (input >= 0 && fds[input].revents && !app.quit)
        ui_input();
    free(fds);
    jobs_tick();
    auth_tick();
    ui_status();
}
static char *response_text(json_t *response)
{
    Buf b = {0};
    size_t i, j;
    json_t *it, *part;
    json_array_foreach(json_object_get(response, "output"), i, it)
    {
        json_array_foreach(json_object_get(it, "content"), j, part)
        {
            const char *t = jstr(part, "text");
            if (t)
                buf_puts(&b, t);
        }
    }
    return b.data ? b.data : xstrdup("");
}
int compact_context(void)
{
    size_t cycles = json_array_size(app.cycles);
    if (cycles <= (size_t)COMPACT_TAIL) {
        message("Not enough history to compact");
        return 0;
    }
    size_t cut =
        (size_t)json_integer_value(json_array_get(app.cycles, cycles - (size_t)COMPACT_TAIL));
    if (cut <= app.cut) {
        message("Recent context is already retained");
        return 0;
    }
    message("Compacting context");
    json_t *input = context_build(app.cut, cut, app.summary, (size_t)-1);
    json_array_append_new(
        input,
        json_pack(
            "{s:s,s:s}", "role", "user", "content",
            "The preceding entries are historical conversation being compacted. Earlier requests "
            "and reply-only instructions apply only to their original turns. Do not execute them "
            "now. Write the factual continuation note now, preserving the task, constraints, "
            "completed changes, exact check outcomes, errors, unresolved work and job states."));
    const char *instructions =
        "Summarize the conversation for a coding agent continuing the same task. Preserve the "
        "user's goal and all constraints, project instructions, completed changes and checks, "
        "important paths, tool errors, unresolved work, and current job states including "
        "user-requested stops. Distinguish confirmed results from unknown outcomes. Do not repeat "
        "actions or invent results. Return a concise factual continuation note.";
    json_t *payload = json_pack("{s:s,s:s,s:o,s:b,s:b}", "model", app.model, "instructions",
                                instructions, "input", input, "store", 0, "stream", 1);
    app.compacting = 1;
    json_t *response = model_request(payload);
    if (response)
        usage_record(response);
    app.compacting = 0;
    json_decref(payload);
    if (!response)
        return 0;
    char *text = response_text(response);
    json_decref(response);
    if (!*text) {
        free(text);
        message("Compaction returned no summary");
        return 0;
    }
    size_t replaced = cut - app.cut;
    json_t *event = json_pack("{s:I,s:s,s:i}", "cut", (json_int_t)cut, "summary", text,
                              "tail_cycles", COMPACT_TAIL);
    journal("compact", event);
    json_decref(event);
    free(app.summary);
    app.summary = text;
    app.cut = cut;
    skills_preserve(cut);
    app.usage_valid = 0;
    char *path = fmt("%s/summary.txt", app.session_dir);
    if (atomic_write(path, text, strlen(text), 0600))
        message("Could not save summary view");
    message("Context compacted: %zu items summarized, %d complete cycles retained. "
            "Input size is unmeasured until the next response. Full journal preserved.",
            replaced, COMPACT_TAIL);
    message("Summary: %s", path);
    free(path);
    ui_status();
    return 1;
}
void run_turn(void)
{
    app.busy = 1;
    app.cancel = 0;
    app.exit_status = 0;
    if (!instructions_load() || !models_list(0)) {
        app.exit_status = 1;
        goto done;
    }
    if (context_input() >= app.compact_limit)
        compact_context();
    int compacted = 0;
    while (!app.cancel && !app.quit) {
        json_t *payload = request_build(), *response = model_request(payload);
        json_decref(payload);
        if (!response) {
            if (app.context_exceeded && !compacted && !app.cancel && !app.quit) {
                compacted = 1;
                if (compact_context())
                    continue;
            }
            app.exit_status = 1;
            break;
        }
        json_t *output = json_object_get(response, "output");
        if (!json_is_array(output) || !json_array_size(output)) {
            json_decref(response);
            message("Model returned no output items");
            app.exit_status = 1;
            break;
        }
        usage_record(response);
        json_t *cycle = json_pack("{s:I}", "start", (json_int_t)json_array_size(app.items));
        journal("cycle", cycle);
        json_array_append(app.cycles, json_object_get(cycle, "start"));
        json_decref(cycle);
        size_t i;
        json_t *item;
        json_array_foreach(output, i, item) item_add(json_incref(item));
        int calls = 0;
        json_array_foreach(output, i, item)
        {
            const char *type = jstr(item, "type");
            if (!type || strcmp(type, "function_call"))
                continue;
            calls++;
            const char *id = jstr(item, "call_id"), *name = jstr(item, "name"),
                       *args = jstr(item, "arguments");
            if (!id || !name) {
                message("Malformed tool call");
                app.cancel = 1;
                continue;
            }
            const char *dot = strrchr(name, '.');
            if (dot)
                name = dot + 1;
            json_t *result;
            if (app.cancel || app.quit)
                result = json_pack("{s:s}", "error",
                                   "Cancelled before execution. This action was not run.");
            else {
                json_error_t error;
                json_t *arguments = args ? json_loads(args, JSON_REJECT_DUPLICATES, &error) : NULL;
                ui_activity("%s", name);
                result = tool_run(name, arguments);
                json_decref(arguments);
                if (jstr(result, "error"))
                    message("%s: %s", name, jstr(result, "error"));
            }
            char *text = json_dumps(result, JSON_COMPACT);
            json_decref(result);
            item_add(json_pack("{s:s,s:s,s:s}", "type", "function_call_output", "call_id", id,
                               "output", text ? text : "{\"error\":\"Invalid tool output\"}"));
            free(text);
        }
        json_decref(response);
        if (!calls)
            break;
        if (app.queued.len)
            ui_pending();
        if (!instructions_load()) {
            app.exit_status = 1;
            break;
        }
        if (context_input() >= app.compact_limit && !app.cancel && !app.quit)
            compact_context();
    }
done:
    if (app.cancel) {
        json_t *note =
            json_pack("{s:s,s:s}", "role", "developer", "content",
                      "The current turn was interrupted. Completed changes remain. Existing "
                      "background jobs were left running. Do not replay actions.");
        item_add(note);
        if (!app.quit)
            ui_stopped();
    }
    app.busy = 0;
    app.cancel = 0;
    ui_activity("idle");
}
static void usage(void)
{
    puts("harn " VERSION "\nUsage: harn [-C directory] [-m model] [-p prompt] [-f "
         "file] [--provider name] [--effort level] [--resume [session]]\n"
         "       harn --login\n       harn --logout\n       harn --models\n"
         "       harn --sessions\nOptions: --help, --version\nInteractive commands: /help");
}
static const char *option_value(int *i, int argc, char **argv)
{
    if (*i + 1 >= argc) {
        usage();
        exit(2);
    }
    return argv[++*i];
}
int main(int argc, char **argv)
{
    setlocale(LC_CTYPE, "");
    memset(&app, 0, sizeof(app));
    app.model = xstrdup("");
    context_budget(NULL, NULL);
    const char *resume = NULL, *prompt = NULL, *file = NULL;
    int action = 0;
    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];
        if (!strcmp(arg, "--help") || !strcmp(arg, "-h")) {
            usage();
            return 0;
        }
        if (!strcmp(arg, "--version")) {
            puts("harn " VERSION);
            return 0;
        }
        if (!strcmp(arg, "-C")) {
            if (chdir(option_value(&i, argc, argv))) {
                perror("chdir");
                return 1;
            }
        } else if (!strcmp(arg, "--provider")) {
            app.connection_override = option_value(&i, argc, argv);
        } else if (!strcmp(arg, "--effort")) {
            app.effort_override = option_value(&i, argc, argv);
        } else if (!strcmp(arg, "-m")) {
            app.model_override = 1;
            free(app.model);
            app.model = xstrdup(option_value(&i, argc, argv));
        } else if (!strcmp(arg, "-p")) {
            prompt = option_value(&i, argc, argv);
            app.batch = 1;
        } else if (!strcmp(arg, "-f"))
            file = option_value(&i, argc, argv);
        else if (!strcmp(arg, "--resume")) {
            resume = "";
            if (i + 1 < argc && argv[i + 1][0] != '-')
                resume = argv[++i];
        } else if (!strcmp(arg, "--login"))
            action = 1;
        else if (!strcmp(arg, "--logout"))
            action = 2;
        else if (!strcmp(arg, "--models"))
            action = 3;
        else if (!strcmp(arg, "--sessions"))
            action = 4;
        else {
            usage();
            return 2;
        }
    }
    if (app.context_limit < 128 || COMPACT_TAIL < 1) {
        fputs("Invalid context settings\n", stderr);
        return 2;
    }
    if (!effort_select(app.effort_override ? app.effort_override : DEFAULT_EFFORT))
        return 2;
    app.root = getcwd(NULL, 0);
    if (!app.root) {
        perror("getcwd");
        return 1;
    }
    const char *home = getenv("HOME");
    if (!home)
        home = ".";
    const char *cfg = getenv("XDG_CONFIG_HOME"), *state = getenv("XDG_STATE_HOME");
    app.config_dir = cfg && *cfg ? fmt("%s/harn", cfg) : fmt("%s/.config/harn", home);
    app.state_dir =
        state && *state ? fmt("%s/harn", state) : fmt("%s/.local/state/harn", home);
    if (mkdirs(app.state_dir, 0700) || mkdirs(app.config_dir, 0700)) {
        perror("state directory");
        return 1;
    }
    char *model_option = app.model_override ? xstrdup(app.model) : NULL;
    if (!connection_select(app.connection_override ? app.connection_override : DEFAULT_PROVIDER))
        return 2;
    if (model_option) {
        free(app.model);
        app.model = model_option;
    }
    app.issuer = xstrdup(getenv("HARN_AUTH_ISSUER") ? getenv("HARN_AUTH_ISSUER")
                                                   : "https://auth.openai.com");
    if (pipe(app.signal_pipe)) {
        perror("pipe");
        return 1;
    }
    for (int i = 0; i < 2; i++) {
        fcntl(app.signal_pipe[i], F_SETFL, O_NONBLOCK);
        fcntl(app.signal_pipe[i], F_SETFD, FD_CLOEXEC);
    }
    signal(SIGINT, signal_event);
    signal(SIGTERM, signal_event);
    signal(SIGHUP, signal_event);
    signal(SIGWINCH, signal_event);
    signal(SIGPIPE, SIG_IGN);
#ifdef __linux__
    prctl(PR_SET_CHILD_SUBREAPER, 1);
#endif
    provider_init();
    if (action)
        app.batch = 1;
    ui_init();
    if (app.tty) {
        struct sigaction stop_action = {0};
        stop_action.sa_sigaction = stop_event;
        stop_action.sa_flags = SA_SIGINFO;
        sigemptyset(&stop_action.sa_mask);
        sigaction(SIGTSTP, &stop_action, NULL);
    }
    if (action || prompt)
        app.stdin_open = 0;
    if (action) {
        app.busy = 1;
        int ok = 1;
        if (action == 1)
            ok = auth_ready(1) && models_list(0);
        if (action == 2)
            ok = auth_logout();
        if (action == 3)
            ok = models_list(1);
        if (action == 4)
            sessions_list();
        app.exit_status = !ok;
        goto finish;
    }
    if (session_open(resume)) {
        app.exit_status = 1;
        goto finish;
    }
    session_show(resume != NULL);
    message("Provider: %s", app.connection->name);
    message("Model: %s", app.model);
    message("Effort: %s", app.effort);
    if (file) {
        char *text = read_file(file, NULL);
        if (!text) {
            message("Cannot read task file: %s", file);
            app.exit_status = 1;
            goto finish;
        }
        buf_puts(&app.queued, text);
        free(text);
    }
    if (prompt) {
        if (app.queued.len)
            buf_puts(&app.queued, "\n");
        buf_puts(&app.queued, prompt);
    }
    while (!app.quit) {
        if (app.queued.len) {
            ui_pending();
            run_turn();
            if (app.batch)
                break;
        } else if (app.compact_requested) {
            app.compact_requested = 0;
            app.busy = 1;
            app.cancel = 0;
            compact_context();
            app.busy = 0;
            app.cancel = 0;
            ui_activity("idle");
        } else if (!app.stdin_open && !app.editor_pid)
            break;
        else
            pump(100);
    }
finish:
    jobs_shutdown();
    ui_close();
    session_close();
    provider_close();
    close(app.signal_pipe[0]);
    close(app.signal_pipe[1]);
    return app.exit_status;
}
