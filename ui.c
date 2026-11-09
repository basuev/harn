#include "harn.h"
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <unistd.h>
#include <wchar.h>

static int columns(void);
static const struct {
    const char *name, *description;
} commands[] = {{"/compact", "Compact context"}, {"/context", "Show context"},
                {"/effort", "Show or change reasoning effort"}, {"/help", "Show commands"}, {"/jobs", "Show local jobs"},
                {"/model", "List or change model"}, {"/new", "Create a new session"},
                {"/provider", "List or change provider"},
                {"/quit", "Save and exit"}, {"/resume", "Choose a previous session"}, {"/skill", "List skills"}};
static json_t *resume_choices;
static const char *completion_label(const char *candidate)
{
    if (!strncmp(candidate, "/resume ", 8)) {
        size_t i;
        json_t *session;
        json_array_foreach(resume_choices, i, session)
            if (!strcmp(candidate + 8, jstr(session, "id")))
                return jstr(session, "description");
    }
    return NULL;
}
static const char *completion_description(const char *candidate)
{
    if (!strncmp(candidate, "/resume ", 8))
        return candidate + 8;
    for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); i++)
        if (!strcmp(candidate, commands[i].name))
            return commands[i].description;
    return NULL;
}
static void complete(const char *input, linenoiseCompletions *out)
{
    if (!strncmp(input, "/provider ", 10)) {
        const Connection *c;
        for (size_t i = 0; (c = connection_at(i)); i++) {
            if (!strncmp(c->name, input + 10, strlen(input + 10))) {
                char *candidate = fmt("/provider %s", c->name);
                linenoiseAddCompletion(out, candidate);
                free(candidate);
            }
        }
    } else if (!strncmp(input, "/resume ", 8)) {
        json_decref(resume_choices);
        resume_choices = sessions_catalog();
        size_t i;
        json_t *session;
        json_array_foreach(resume_choices, i, session) {
            const char *id = jstr(session, "id");
            if (!strncmp(id, input + 8, strlen(input + 8)) ||
                strstr(jstr(session, "description"), input + 8)) {
                char *candidate = fmt("/resume %s", id);
                linenoiseAddCompletion(out, candidate);
                free(candidate);
            }
        }
    } else if (!strncmp(input, "/effort ", 8)) {
        if (!app.models && !json_is_object(json_object_get(app.model_info, "catalog")))
            return;
        const char *level;
        for (size_t i = 0; (level = effort_at(i)); i++) {
            if (!strncmp(level, input + 8, strlen(input + 8))) {
                char *candidate = fmt("/effort %s", level);
                linenoiseAddCompletion(out, candidate);
                free(candidate);
            }
        }
    } else if (!strncmp(input, "/model ", 7)) {
        size_t i;
        json_t *model;
        json_array_foreach(app.models, i, model) {
            const char *name = jstr(model, "slug");
            if (name && !strncmp(name, input + 7, strlen(input + 7))) {
                char *candidate = fmt("/model %s", name);
                linenoiseAddCompletion(out, candidate);
                free(candidate);
            }
        }
    } else if (!strncmp(input, "/jobs ", 6)) {
        if (strlen(input + 6) < 5 && !strncmp("stop ", input + 6, strlen(input + 6)))
            linenoiseAddCompletion(out, "/jobs stop ");
        const char *prefix = !strncmp(input, "/jobs stop ", 11) ? "/jobs stop " : "/jobs ";
        for (Job *j = app.jobs; j; j = j->next) {
            char *candidate = fmt("%s%d", prefix, j->id);
            if (!strncmp(candidate, input, strlen(input)))
                linenoiseAddCompletion(out, candidate);
            free(candidate);
        }
    } else if (input[0] == '/' && !strchr(input, ' ')) {
        for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); i++)
            if (!strncmp(commands[i].name, input, strlen(input)))
                linenoiseAddCompletion(out, commands[i].name);
    }
}
void ui_status(void)
{
    if (!app.tty || app.editor_pid || app.quit)
        return;
    long input = context_input();
    char context[32], jobs[32] = "", queued[32] = "", next[256];
    if (input < 0 || !app.context_known)
        snprintf(context, sizeof(context), "?");
    else if (input > 0 && 100.0 * input < app.context_limit)
        snprintf(context, sizeof(context), "<1%%");
    else
        snprintf(context, sizeof(context), "~%.0f%%", 100.0 * input / app.context_limit);
    int count = 0;
    for (Job *j = app.jobs; j; j = j->next)
        if (j->background && job_running(j))
            count++;
    if (count)
        snprintf(jobs, sizeof(jobs), " bg:%d", count);
    if (app.queued_messages)
        snprintf(queued, sizeof(queued), " q:%d", app.queued_messages);
    const char *activity = app.cancel && app.busy ? "Stopping..." :
                           app.busy && strcmp(app.activity, "idle") ? app.activity : "";
    snprintf(next, sizeof(next), "%s%sctx:%s%s%s", activity, *activity ? " " : "",
             context, jobs, queued);
    char *prompt = fmt("%s\n%s/%s %s %s\n> ", app.root, app.connection->name,
                       app.model, app.effort, next);
    if (app.prompt && !strcmp(prompt, app.prompt)) {
        free(prompt);
        return;
    }
    if (app.input_active)
        linenoiseHide(&app.input);
    free(app.prompt);
    app.prompt = prompt;
    if (app.input_active) {
        app.input.prompt = app.prompt;
        app.input.plen = strlen(app.prompt);
        linenoiseShow(&app.input);
    }
}
void ui_activity(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    vsnprintf(app.activity, sizeof(app.activity), format, args);
    va_end(args);
    ui_status();
}
static void input_stop(void)
{
    if (app.input_active) {
        app.input_buf = app.input.buf;
        app.input_size = app.input.buflen + 1;
        linenoiseEditStop(&app.input);
        app.input_active = 0;
    }
}
static void input_start(const char *draft)
{
    if (!app.tty || app.quit)
        return;
    ui_status();
    if (linenoiseEditStart(&app.input, 0, 1, app.input_buf, app.input_size, app.prompt)) {
        app.quit = 1;
        return;
    }
    app.input.buflen_max = (size_t)-1 / 2;
    app.input_active = 1;
    fcntl(0, F_SETFL, app.input_flags | O_NONBLOCK);
    if (draft) {
        linenoiseEditReplace(&app.input, draft, strlen(draft));
        app.input_buf = app.input.buf;
        app.input_size = app.input.buflen + 1;
    }
}
static int columns(void)
{
    struct winsize ws;
    if (!ioctl(1, TIOCGWINSZ, &ws) && ws.ws_col)
        return ws.ws_col;
    return 80;
}
static void raw_output(const char *text, size_t len)
{
    Buf b = {0};
    for (size_t i = 0; i < len; i++) {
        if (text[i] == '\n' && (!i || text[i - 1] != '\r'))
            buf_puts(&b, "\r");
        buf_add(&b, text + i, 1);
    }
    if (b.len)
        write_all(1, b.data, b.len);
    buf_clear(&b);
}
enum { OUTPUT_TEXT, OUTPUT_LOG, OUTPUT_META, OUTPUT_BEGIN, OUTPUT_FINAL, OUTPUT_END, OUTPUT_USER,
       OUTPUT_EVENT = 8, OUTPUT_RECORD = 16 };
static void output_reset(void)
{
    buf_clear(&app.output_line);
    json_decref(app.output_frame);
    app.output_frame = NULL;
    json_decref(app.output_markdown.literal);
    memset(&app.output_markdown, 0, sizeof(app.output_markdown));
    app.output_offset = 0;
    app.output_rows = app.output_skip = app.output_columns = 0;
}
static void output_resize(void)
{
    if (!app.output_columns || app.output_columns == columns())
        return;
    /* Already printed text keeps its old layout. Retain source only to find
     * the end of an inline preview whose opener cannot be redrawn now. */
    if (app.output_kind == OUTPUT_TEXT) {
        render_freeze(&app.output_markdown);
        app.output_offset = app.output_line.len;
        json_decref(app.output_frame);
        app.output_frame = NULL;
        app.output_rows = app.output_skip = app.output_columns = 0;
    } else
        output_reset();
}
static int visible_rows(void)
{
    struct winsize ws;
    int rows = !ioctl(1, TIOCGWINSZ, &ws) && ws.ws_row ? ws.ws_row : 24;
    int input = app.input_active ? (int)app.input.oldrows : 0;
    return rows > input + 1 ? rows - input - 1 : 1;
}
static void output_draw(int complete)
{
    /* A resize can arrive between output_resize and this draw. Keep the
     * current frame's width until its scrollback has been released. */
    int width = app.output_columns ? app.output_columns : columns();
    int kind = app.output_kind;
    const char *text = app.output_line.data ? app.output_line.data : "";
    json_t *frame = kind == OUTPUT_TEXT
                        ? render_stream(&app.output_markdown, text, app.output_line.len,
                                        app.output_offset, width, visible_rows(), complete)
                        : render_text(text, app.output_line.len, width,
                                      kind == OUTPUT_LOG ? -1 : 0, 2);
    size_t count = json_array_size(frame), old = json_array_size(app.output_frame);
    size_t same = (size_t)app.output_skip;
    while (same < old && same < count &&
           json_equal(json_array_get(frame, same), json_array_get(app.output_frame, same)))
        same++;
    if (same == old && same == count) {
        json_decref(frame);
        return;
    }
    int input = app.input_active;
    if (input)
        linenoiseHide(&app.input);
    int clear = old ? (int)(old - same) : app.output_rows;
    if (clear) {
        char *seq = fmt("\x1b[%dA\r\x1b[0J", clear);
        write_all(1, seq, strlen(seq));
        free(seq);
    }
    for (size_t i = same; i < count; i++) {
        const char *row = json_string_value(json_array_get(frame, i));
        int dim = kind == OUTPUT_META || (kind == OUTPUT_TEXT && app.response_commentary);
        if (dim)
            write_all(1, "\x1b[2m", 4);
        int bold = kind == OUTPUT_USER || (kind == OUTPUT_EVENT && strstr(row, "Stopped by you"));
        if (bold)
            write_all(1, "\x1b[1m", 4);
        if (kind == OUTPUT_USER && i == 0) {
            write_all(1, "> ", 2);
            if (!strncmp(row, "  ", 2))
                row += 2;
        }
        raw_output(row, strlen(row));
        if (dim || bold)
            write_all(1, "\x1b[22m", 5);
        write_all(1, "\r\n", 2);
    }
    json_decref(app.output_frame);
    app.output_frame = frame;
    app.output_columns = width;
    int visible = visible_rows();
    if ((int)count - app.output_skip > visible)
        app.output_skip = (int)count - visible;
    app.output_rows = (int)count - app.output_skip;
    if (input)
        linenoiseShow(&app.input);
}
static void output_finish(void)
{
    if (app.output_kind == OUTPUT_TEXT && app.output_line.len)
        output_draw(1);
    output_reset();
}
static void ui_output(const char *text, size_t len, int kind)
{
    if (app.editor_pid) {
        if (!app.deferred_output)
            app.deferred_output = json_array();
        json_array_append_new(app.deferred_output,
                              json_pack("{s:i,s:o}", "kind", kind,
                                        "text", json_stringn(text ? text : "", len)));
        return;
    }
    if (!app.tty) {
        if (kind == OUTPUT_TEXT)
            write_all(1, text, len);
        return;
    }
    output_resize();
    if (kind & OUTPUT_RECORD) {
        output_finish();
        kind &= ~OUTPUT_RECORD;
    }
    if (kind == OUTPUT_END) {
        output_finish();
        return;
    }
    if (kind == OUTPUT_BEGIN || kind == OUTPUT_FINAL) {
        output_finish();
        app.response_commentary = kind == OUTPUT_BEGIN;
        int input = app.input_active;
        if (input)
            linenoiseHide(&app.input);
        write_all(1, "\r\n", 2);
        if (input)
            linenoiseShow(&app.input);
        return;
    }
    if (kind != app.output_kind)
        output_finish();
    app.output_kind = kind;
    buf_add(&app.output_line, text, len);
    output_draw(0);
    if (kind != OUTPUT_TEXT && len && text[len - 1] == '\n')
        output_reset();
}
void ui_stopped(void)
{
    int count = 0;
    for (Job *j = app.jobs; j; j = j->next)
        if (j->background && job_running(j))
            count++;
    char *text = count ? fmt("\nStopped by you\n%d background job%s still running\n\n",
                             count, count == 1 ? "" : "s")
                       : xstrdup("\nStopped by you\n\n");
    if (app.tty)
        ui_output(text, strlen(text), OUTPUT_EVENT | OUTPUT_RECORD);
    else
        write_all(2, text, strlen(text));
    free(text);
}
void ui_write(const char *text, size_t len)
{
    ui_output(text, len, OUTPUT_TEXT);
}
void ui_response_begin(int phase)
{
    ui_output(NULL, 0, phase < 0 ? OUTPUT_BEGIN : OUTPUT_FINAL);
}
void ui_response_end(void)
{
    ui_output(NULL, 0, OUTPUT_END);
}
void ui_notice(const char *text, size_t len)
{
    if (app.tty)
        ui_output(text, len, OUTPUT_LOG);
    else
        write_all(2, text, len);
}
static void formatted(int diagnostic, const char *format, va_list args)
{
    va_list copy;
    va_copy(copy, args);
    int n = vsnprintf(NULL, 0, format, args);
    char *s = xmalloc((size_t)n + 2);
    vsnprintf(s, (size_t)n + 1, format, copy);
    va_end(copy);
    s[n] = '\n';
    s[n + 1] = 0;
    if (app.tty) {
        ui_output(s, (size_t)n + 1,
                  (diagnostic == 2 ? OUTPUT_META : OUTPUT_LOG) | OUTPUT_RECORD);
    } else if (diagnostic)
        ui_notice(s, (size_t)n + 1);
    else
        ui_write(s, (size_t)n + 1);
    free(s);
}
void message(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    formatted(1, format, args);
    va_end(args);
}
void ui_meta(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    formatted(2, format, args);
    va_end(args);
}
void ui_print(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    formatted(0, format, args);
    va_end(args);
}
void ui_exec_start(Job *job)
{
    char *header = fmt("exec #%d%s: %s", job->id,
                       job->background ? " (background)" : "", job->command);
    if (!app.tty) {
        ui_meta("%s", header);
    } else {
        size_t len = strcspn(header, "\r\n");
        json_t *rows = render_text(header, len, columns() > 5 ? columns() - 3 : columns(), 0, 2);
        const char *first = json_string_value(json_array_get(rows, 0));
        ui_meta("%s%s", first && !strncmp(first, "  ", 2) ? first + 2 : first ? first : "",
                header[len] || json_array_size(rows) > 1 ? "..." : "");
        json_decref(rows);
    }
    free(header);
}
void ui_exec_done(Job *job)
{
    if (app.tty && job->status && !job->stopped && !job->background) {
        json_t *info = job_info(job->id, 1, -1, columns() * 6);
        const char *output = jstr(info, "output");
        if (output && *output) {
            ui_meta("jobs #%d output tail:", job->id);
            const char *tail = output + strlen(output);
            int lines = 0;
            while (tail > output) {
                tail--;
                if (*tail == '\n' && ++lines > 6) {
                    tail++;
                    break;
                }
            }
            ui_notice(tail, strlen(tail));
        }
        json_decref(info);
    }
    if (!job->status && !job->stopped) {
        ui_meta("jobs #%d exited (0)", job->id);
    } else {
        if (app.tty)
            message("jobs #%d %s (%d), /jobs %d", job->id,
                    job->stopped ? "stopped" : "exited", job->status, job->id);
        else
            message("jobs #%d %s (%d)", job->id,
                    job->stopped ? "stopped" : "exited", job->status);
    }
}
static void editor_start(void)
{
    if (!app.tty || app.editor_pid)
        return;
    const char *editor = getenv("EDITOR");
    if (!editor || !*editor) {
        message("Set EDITOR to open an external editor");
        return;
    }
    output_finish();
    app.editor_draft = xstrdup(app.input.buf);
    linenoiseHide(&app.input);
    input_stop();
    fcntl(0, F_SETFL, app.input_flags & ~O_NONBLOCK);
    output_reset();
    const char *tmp = getenv("TMPDIR");
    app.editor_path = fmt("%s/harn-draft-XXXXXX", tmp && *tmp ? tmp : "/tmp");
    int fd = mkstemp(app.editor_path);
    if (fd < 0 || write_all(fd, app.editor_draft, strlen(app.editor_draft))) {
        if (fd >= 0)
            close(fd);
        ui_editor_done(1 << 8);
        return;
    }
    close(fd);
    app.editor_pid = fork();
    if (app.editor_pid < 0) {
        app.editor_pid = 0;
        ui_editor_done(1 << 8);
        return;
    }
    if (!app.editor_pid) {
        signal(SIGINT, SIG_DFL);
        signal(SIGTERM, SIG_DFL);
        signal(SIGHUP, SIG_DFL);
        signal(SIGPIPE, SIG_DFL);
        for (int i = 3; i < 65536; i++)
            close(i);
        char *cmd = fmt("exec %s \"$1\"", editor);
        execl("/bin/sh", "sh", "-c", cmd, "editor", app.editor_path, (char *)NULL);
        _exit(127);
    }
}
void ui_editor_done(int status)
{
    char *next = NULL;
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0 && app.editor_path)
        next = read_file(app.editor_path, NULL);
    int failed = !next;
    if (!next)
        next = xstrdup(app.editor_draft);
    if (app.editor_path)
        unlink(app.editor_path);
    tcsetattr(0, TCSANOW, &app.original_termios);
    input_start(next);
    free(next);
    free(app.editor_path);
    app.editor_path = NULL;
    free(app.editor_draft);
    app.editor_draft = NULL;
    if (failed)
        message("Editor failed. Draft preserved.");
    json_t *pending = app.deferred_output;
    app.deferred_output = NULL;
    size_t i;
    json_t *event;
    json_array_foreach(pending, i, event)
    {
        json_t *text = json_object_get(event, "text");
        ui_output(json_string_value(text), json_string_length(text), (int)jint(event, "kind", 0));
    }
    json_decref(pending);
}
void ui_suspend(int editor_signalled)
{
    if (!app.tty)
        return;
    if (app.editor_pid) {
        int status;
        pid_t pid;
        /* The editor's own Ctrl-Z already signals the foreground group.
         * Sending it another TSTP can leave a second stop pending after fg. */
        if (!editor_signalled)
            kill(app.editor_pid, SIGTSTP);
        do {
            pid = waitpid(app.editor_pid, &status, WUNTRACED);
        } while (pid < 0 && errno == EINTR);
        if (pid == app.editor_pid && !WIFSTOPPED(status)) {
            app.editor_pid = 0;
            ui_editor_done(status);
        }
    }
    struct termios mode;
    if (tcgetattr(0, &mode)) {
        message("Cannot suspend: %s", strerror(errno));
        return;
    }
    int flags = fcntl(0, F_GETFL);
    int input = app.input_active;
    if (input) {
        linenoiseHide(&app.input);
        app.input_active = 0;
        write_all(1, "\x1b[?2004l", 8);
    }
    if (app.output_rows) {
        char *seq = fmt("\x1b[%dA\r\x1b[0J", app.output_rows);
        write_all(1, seq, strlen(seq));
        free(seq);
        app.output_rows = 0;
        json_decref(app.output_frame);
        app.output_frame = NULL;
    }
    tcsetattr(0, TCSANOW, &app.original_termios);
    fcntl(0, F_SETFL, app.input_flags);
    for (Job *j = app.jobs; j; j = j->next)
        if (!j->background && job_running(j))
            kill(-j->pgid, SIGSTOP);
    struct sigaction stop_action = {0}, previous;
    stop_action.sa_handler = SIG_DFL;
    sigemptyset(&stop_action.sa_mask);
    sigaction(SIGTSTP, &stop_action, &previous);
    raise(SIGTSTP);
    sigaction(SIGTSTP, &previous, NULL);
    /* A shell's bg resumes execution without giving us the terminal. Wait
     * for fg before changing its modes or redrawing anything. */
    pid_t foreground;
    while ((foreground = tcgetpgrp(0)) >= 0 && foreground != getpgrp())
        kill(0, SIGTTIN);
    /* A resumed full-screen editor restores its own modes. Do not race its
     * SIGCONT handler by putting the terminal back into its stopped mode. */
    if (!app.editor_pid) {
        tcsetattr(0, TCSANOW, &mode);
        fcntl(0, F_SETFL, flags);
    }
    for (Job *j = app.jobs; j; j = j->next)
        if (!j->background && job_running(j))
            kill(-j->pgid, SIGCONT);
    /* The editor shares our foreground group and already received the
     * shell's SIGCONT. A duplicate can interrupt its terminal reinitialization. */
    if (input) {
        write_all(1, "\x1b[?2004h", 8);
        app.input.cols = columns();
        if (app.output_line.len)
            ui_output("", 0, app.output_kind);
        app.input_active = 1;
        linenoiseShow(&app.input);
    }
}
void ui_init(void)
{
    app.stdin_open = 1;
    app.input_flags = fcntl(0, F_GETFL);
    app.tty = !app.batch && isatty(0) && isatty(1);
    if (app.tty) {
        tcgetattr(0, &app.original_termios);
        app.input_size = 4096;
        app.input_buf = xmalloc(app.input_size);
        linenoiseSetMultiLine(1);
        linenoiseSetCompletionCallback(complete);
        linenoiseSetCompletionDescriptionCallback(completion_description);
        linenoiseSetCompletionLabelCallback(completion_label);
        char *p = fmt("%s/input-history", app.state_dir);
        linenoiseHistoryLoad(p);
        free(p);
        input_start(NULL);
    } else if (app.input_flags >= 0)
        fcntl(0, F_SETFL, app.input_flags | O_NONBLOCK);
}
void ui_close(void)
{
    if (app.editor_pid) {
        kill(app.editor_pid, SIGTERM);
        kill(app.editor_pid, SIGKILL);
        waitpid(app.editor_pid, NULL, 0);
        app.editor_pid = 0;
    }
    input_stop();
    if (app.tty)
        tcsetattr(0, TCSANOW, &app.original_termios);
    if (app.input_flags >= 0)
        fcntl(0, F_SETFL, app.input_flags);
    if (app.editor_path)
        unlink(app.editor_path);
    free(app.input_buf);
    app.input_buf = NULL;
    free(app.prompt);
    app.prompt = NULL;
    json_decref(app.deferred_output);
    app.deferred_output = NULL;
    output_reset();
}
static void queue(const char *s)
{
    if (app.queued.len)
        buf_puts(&app.queued, "\n");
    buf_puts(&app.queued, s);
    app.queued_messages++;
    if (app.busy) {
        app.queued_during_turn = 1;
        message("Message queued (%d waiting)", app.queued_messages);
    }
    ui_status();
}
void ui_command(char *line)
{
    if (!line || !*line)
        return;
    if (!strcmp(line, "/quit")) {
        app.quit = 1;
        app.cancel = 1;
        return;
    }
    if (!strncmp(line, "/jobs", 5) && (line[5] == 0 || line[5] == ' ')) {
        jobs_local(line + 5);
        return;
    }
    if (!strcmp(line, "/context")) {
        context_show();
        return;
    }
    if (!strcmp(line, "/help")) {
        ui_print("/jobs [id]        List jobs or read their output\n"
                 "/jobs stop id     Stop a job\n"
                 "/context          Context and session directory\n"
                 "/provider [name]  List providers or select one\n"
                 "/model [name]     List models or select one\n"
                 "/effort [level]   Show or change reasoning effort\n"
                 "/new              Create a new session\n"
                 "/resume [id]      Choose a previous session\n"
                 "/skill            List skills\n"
                 "/compact          Summarize earlier history\n"
                 "/quit             Save and exit\n"
                 "/help             Show commands\n"
                 "Ctrl-C: clear draft, or interrupt if empty.\n"
                 "Ctrl-D: exit. Ctrl-G: editor.\n"
                 "Alt-Backspace/Ctrl-W: delete word. Ctrl-N/P: suggestions or history.\n"
                 "Tab: insert suggestion. Enter: next argument or submit. Escape: dismiss.\n"
                 "Ctrl-Z: suspend. Use fg in the shell to resume.");
        return;
    }
    if (!strcmp(line, "/compact")) {
        if (app.busy)
            message("Interrupt the current turn before compacting");
        else
            app.compact_requested = 1;
        return;
    }
    if (!strcmp(line, "/skill")) {
        skills_command();
        return;
    }
    if (!strncmp(line, "/provider", 9) && (line[9] == 0 || line[9] == ' ')) {
        connection_command(line + 9);
        return;
    }
    if (!strcmp(line, "/new")) {
        if (app.busy || app.queued.len)
            message("Interrupt the current turn before creating a session");
        else
            session_switch(NULL);
        return;
    }
    if (!strncmp(line, "/resume", 7) && (line[7] == 0 || line[7] == ' ')) {
        const char *id = line + 7;
        while (*id == ' ')
            id++;
        if (app.busy || app.queued.len) {
            message("Interrupt the current turn before switching sessions");
            return;
        }
        if (!*id) {
            json_t *catalog = sessions_catalog();
            if (!json_array_size(catalog))
                message("No previous sessions for this project");
            else if (app.tty)
                linenoiseEditReplace(&app.input, "/resume ", 8);
            else {
                size_t i;
                json_t *session;
                json_array_foreach(catalog, i, session)
                    ui_print("%s  %s", jstr(session, "id"), jstr(session, "description"));
            }
            json_decref(catalog);
        } else
            session_switch(id);
        return;
    }
    if (!strncmp(line, "/effort", 7) && (line[7] == 0 || line[7] == ' ')) {
        const char *value = line + 7;
        while (*value == ' ')
            value++;
        if (!app.busy && !app.models) {
            app.busy = 1;
            app.cancel = 0;
            int loaded = models_list(0);
            app.busy = 0;
            app.cancel = 0;
            if (!loaded)
                return;
        }
        if (!*value) {
            if (app.tty) {
                if (!app.input.len)
                    linenoiseEditReplace(&app.input, "/effort ", 8);
            } else {
                ui_print("Effort: %s", app.effort);
                const char *level;
                for (size_t i = 0; (level = effort_at(i)); i++)
                    ui_print("  %s", level);
                ui_print("Supported levels depend on the model. default leaves effort to the provider.");
            }
        } else if (app.busy)
            message("Interrupt the current turn before changing the effort");
        else if (!effort_supported(value))
            message("Unsupported effort for %s/%s: %s. Use /effort to list available levels.",
                    app.connection->name, app.model, value);
        else if (effort_select(value))
            message("Effort: %s", app.effort);
        return;
    }
    if (!strncmp(line, "/model", 6) && (line[6] == 0 || line[6] == ' ')) {
        const char *s = line + 6;
        while (*s == ' ')
            s++;
        if (!*s) {
            if (!app.busy && (!app.tty || !app.models)) {
                app.busy = 1;
                app.cancel = 0;
                models_list(!app.tty);
                app.busy = 0;
                app.cancel = 0;
            } else if (!app.tty)
                models_show();
            if (app.tty && app.models) {
                if (!app.input.len)
                    linenoiseEditReplace(&app.input, "/model ", 7);
            } else if (app.tty && app.busy)
                message("Model list is not loaded yet");
        } else if (app.busy)
            message("Interrupt the current turn before changing the model");
        else {
            if (*s) {
                free(app.model);
                app.model = xstrdup(s);
                effort_activate();
                app.usage_valid = 0;
                context_budget(NULL, NULL);
                json_t *model = json_string(s);
                journal("model", model);
                json_decref(model);
                message("Model: %s", app.model);
                ui_status();
            }
        }
        return;
    }
    if (line[0] == '/') {
        message("Unknown command. Use /help");
        return;
    }
    queue(line);
}
void ui_input(void)
{
    if (app.editor_pid)
        return;
    if (app.tty) {
        struct winsize ws;
        if (!ioctl(1, TIOCGWINSZ, &ws) && ws.ws_col && app.input.cols != ws.ws_col) {
            app.input.cols = ws.ws_col;
            linenoiseHide(&app.input);
            linenoiseShow(&app.input);
        }
        char *line = linenoiseEditFeed(&app.input);
        int reason = errno;
        app.input_buf = app.input.buf;
        app.input_size = app.input.buflen + 1;
        if (line == linenoiseEditMore)
            return;
        if (!line && reason == EINPROGRESS) {
            editor_start();
            return;
        }
        if (!line && reason == EINTR) {
            raise(SIGTSTP);
            return;
        }
        if (!line && reason == EAGAIN && app.input.len) {
            linenoiseEditReplace(&app.input, "", 0);
            return;
        }
        linenoiseHide(&app.input);
        /* Keep raw mode between submissions. Restoring and flushing the
         * terminal here can discard input typed during local command output. */
        app.input_active = 0;
        output_finish();
        if (!line && reason == ENOENT) {
            app.quit = 1;
            app.cancel = 1;
            return;
        }
        if (!line && reason == EAGAIN) {
            if (app.busy) {
                app.cancel = 1;
                ui_status();
            }
        } else if (line) {
            if (*line) {
                raw_output("\n", 1);
                char *shown = fmt("%s\n", line);
                ui_output(shown, strlen(shown), OUTPUT_USER);
                free(shown);
                linenoiseHistoryAdd(line);
                char *path = fmt("%s/input-history", app.state_dir);
                linenoiseHistorySave(path);
                free(path);
            }
            input_start(NULL);
            ui_command(line);
            free(line);
            return;
        }
        input_start(NULL);
    } else {
        char data[4096];
        ssize_t n = read(0, data, sizeof(data));
        if (n == 0) {
            app.stdin_open = 0;
            if (app.stdin_buf.len) {
                ui_command(app.stdin_buf.data);
                buf_clear(&app.stdin_buf);
            }
            return;
        }
        if (n < 0)
            return;
        for (ssize_t i = 0; i < n; i++) {
            if (data[i] == '\n') {
                if (app.stdin_buf.len && app.stdin_buf.data[app.stdin_buf.len - 1] == '\r')
                    app.stdin_buf.data[--app.stdin_buf.len] = 0;
                if (app.stdin_buf.data)
                    ui_command(app.stdin_buf.data);
                buf_clear(&app.stdin_buf);
            } else
                buf_add(&app.stdin_buf, data + i, 1);
        }
    }
}
void ui_pending(void)
{
    if (!app.queued.len)
        return;
    user_add(app.queued.data);
    if (app.queued_during_turn)
        app.pending_delivery += app.queued_messages;
    app.queued_messages = 0;
    app.queued_during_turn = 0;
    buf_clear(&app.queued);
    ui_status();
}
