#include "harn.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static char *sessions_path(void)
{
    return fmt("%s/sessions", app.state_dir);
}
static char *latest_session(void)
{
    char *base = sessions_path(), *best = NULL;
    struct timespec latest = {0};
    DIR *d = opendir(base);
    struct dirent *e;
    while (d && (e = readdir(d))) {
        if (e->d_name[0] == '.')
            continue;
        char *p = fmt("%s/%s/meta.json", base, e->d_name);
        json_t *m = load_json(p);
        const char *root = jstr(m, "cwd");
        free(p);
        p = fmt("%s/%s/journal.jsonl", base, e->d_name);
        struct stat st;
        int present = stat(p, &st) == 0;
        free(p);
        if (root && !strcmp(root, app.root) && present &&
            (!best || st.st_mtim.tv_sec > latest.tv_sec ||
             (st.st_mtim.tv_sec == latest.tv_sec && st.st_mtim.tv_nsec > latest.tv_nsec))) {
            free(best);
            best = xstrdup(e->d_name);
            latest = st.st_mtim;
        }
        json_decref(m);
    }
    if (d)
        closedir(d);
    free(base);
    return best;
}
void sessions_list(void)
{
    char *base = sessions_path();
    DIR *d = opendir(base);
    struct dirent *e;
    while (d && (e = readdir(d))) {
        if (e->d_name[0] == '.')
            continue;
        char *p = fmt("%s/%s/meta.json", base, e->d_name);
        json_t *m = load_json(p);
        free(p);
        if (jstr(m, "cwd") && !strcmp(jstr(m, "cwd"), app.root)) {
            struct stat st;
            char *log = fmt("%s/%s/journal.jsonl", base, e->d_name);
            time_t updated = stat(log, &st) == 0 ? st.st_mtime : 0;
            char stamp[40] = "unknown";
            struct tm tm;
            if (updated) {
                localtime_r(&updated, &tm);
                strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", &tm);
            }
            ui_print("%s  started with %s, updated %s\n  %s/%s", e->d_name,
                     jstr(m, "model") ? jstr(m, "model") : "unknown model", stamp, base, e->d_name);
            free(log);
        }
        json_decref(m);
    }
    if (d)
        closedir(d);
    free(base);
}

static int session_recent(const void *a, const void *b)
{
    json_t *left = *(json_t *const *)a, *right = *(json_t *const *)b;
    long l = jint(left, "updated", 0), r = jint(right, "updated", 0);
    if (l == r) {
        l = jint(left, "updated_ns", 0);
        r = jint(right, "updated_ns", 0);
    }
    return l < r ? 1 : l > r ? -1 : strcmp(jstr(right, "id"), jstr(left, "id"));
}
json_t *sessions_catalog(void)
{
    json_t *result = json_array();
    char *base = sessions_path();
    DIR *dir = opendir(base);
    struct dirent *entry;
    while (dir && (entry = readdir(dir))) {
        if (entry->d_name[0] == '.' ||
            (app.session_id && !strcmp(entry->d_name, app.session_id)))
            continue;
        char *path = fmt("%s/%s/meta.json", base, entry->d_name);
        json_t *meta = load_json(path);
        free(path);
        const char *root = jstr(meta, "cwd");
        path = fmt("%s/%s/journal.jsonl", base, entry->d_name);
        struct stat st;
        if (root && !strcmp(root, app.root) && !stat(path, &st)) {
            char *line = NULL, *excerpt = NULL;
            size_t cap = 0;
            FILE *file = fopen(path, "r");
            while (file && getline(&line, &cap, file) > 0) {
                json_t *event = json_loads(line, 0, NULL);
                json_t *data = json_object_get(event, "data");
                const char *type = jstr(event, "type"), *role = jstr(data, "role");
                const char *text = jstr(data, "content");
                if (type && !strcmp(type, "item") && role && !strcmp(role, "user") && text && *text) {
                    size_t len = strcspn(text, "\r\n");
                    if (len > 120) {
                        len = 120;
                        while (len && ((unsigned char)text[len] & 0xc0) == 0x80)
                            len--;
                    }
                    excerpt = fmt("%.*s%s", (int)len, text, text[len] ? "..." : "");
                }
                json_decref(event);
                if (excerpt)
                    break;
            }
            if (file)
                fclose(file);
            free(line);
            json_array_append_new(result, json_pack("{s:s,s:I,s:I,s:s}", "id", entry->d_name,
                                  "updated", (json_int_t)st.st_mtime,
                                  "updated_ns", (json_int_t)st.st_mtim.tv_nsec, "description",
                                  excerpt ? excerpt : "No saved user messages"));
            free(excerpt);
        }
        free(path);
        json_decref(meta);
    }
    if (dir)
        closedir(dir);
    free(base);
    size_t count = json_array_size(result);
    json_t **sorted = xmalloc((count + 1) * sizeof(*sorted));
    for (size_t i = 0; i < count; i++)
        sorted[i] = json_array_get(result, i);
    qsort(sorted, count, sizeof(*sorted), session_recent);
    json_t *catalog = json_array();
    for (size_t i = 0; i < count; i++)
        json_array_append(catalog, sorted[i]);
    free(sorted);
    json_decref(result);
    return catalog;
}

static void session_excerpt(const char *label, const char *text)
{
    size_t len = strcspn(text, "\r\n");
    if (len > 160) {
        len = 160;
        while (len && ((unsigned char)text[len] & 0xc0) == 0x80)
            len--;
    }
    message("%s: %.*s%s", label, (int)len, text, text[len] ? "..." : "");
}
void session_show(int resumed)
{
    message("%s session: %s", resumed ? "Resumed" : "New", app.session_id);
    if (!app.tty)
        message("Project: %s", app.root);
    if (!resumed)
        return;
    const char *first = NULL, *last = NULL;
    size_t i;
    json_t *item;
    json_array_foreach(app.items, i, item)
    {
        const char *role = jstr(item, "role"), *content = jstr(item, "content");
        if (role && !strcmp(role, "user") && content && *content) {
            if (!first)
                first = content;
            last = content;
        }
    }
    if (first)
        session_excerpt("Started with", first);
    if (last && last != first)
        session_excerpt("Last request", last);
    if (!first)
        message("No saved user messages");
}

void journal(const char *type, json_t *data)
{
    if (!app.journal)
        return;
    json_t *e = json_pack("{s:s,s:I,s:O}", "type", type, "time", (json_int_t)time(NULL), "data",
                          data ? data : json_null());
    char *s = json_dumps(e, JSON_COMPACT | JSON_ENSURE_ASCII);
    json_decref(e);
    int bad = !s || fprintf(app.journal, "%s\n", s) < 0 || fflush(app.journal) ||
              fsync(fileno(app.journal));
    free(s);
    if (bad) {
        fputs("Cannot save the session journal\n", stderr);
        app.quit = 1;
        app.cancel = 1;
        app.exit_status = 1;
    }
}
void item_add(json_t *item)
{
    journal("item", item);
    json_array_append_new(app.items, item);
    skills_track(item, 1);
}
void user_add(const char *text)
{
    item_add(json_pack("{s:s,s:s}", "role", "user", "content", text));
}
const char *effort_at(size_t index)
{
    if (index == 0)
        return "default";
    json_t *model;
    size_t i;
    json_array_foreach(app.models, i, model) {
        const char *slug = jstr(model, "slug");
        if (slug && app.model && !strcmp(slug, app.model)) {
            json_t *level = json_array_get(json_object_get(model, "supported_reasoning_levels"),
                                           index - 1);
            return json_is_string(level) ? json_string_value(level) : jstr(level, "effort");
        }
    }
    const char *saved_model = jstr(app.model_info, "model");
    if (saved_model && app.model && !strcmp(saved_model, app.model)) {
        json_t *level = json_array_get(json_object_get(json_object_get(app.model_info, "catalog"),
                                                     "supported_reasoning_levels"), index - 1);
        return json_is_string(level) ? json_string_value(level) : jstr(level, "effort");
    }
    return NULL;
}
int effort_supported(const char *value)
{
    const char *level;
    for (size_t i = 0; (level = effort_at(i)); i++)
        if (!strcmp(level, value))
            return 1;
    return 0;
}
static char *effort_key(void)
{
    return fmt("%s\n%s", app.connection->name, app.model);
}
void effort_activate(void)
{
    char *key = effort_key();
    const char *saved = jstr(app.efforts, key);
    free(app.effort);
    app.effort = xstrdup(saved ? saved : "default");
    app.usage_valid = 0;
    free(key);
    json_t *selection = json_pack("{s:s,s:s,s:s}", "provider", app.connection->name,
                                 "model", app.model, "value", app.effort);
    journal("effort", selection);
    json_decref(selection);
}
int effort_select(const char *value)
{
    if (app.models && !effort_supported(value)) {
        message("Unsupported effort for %s/%s: %s. Use /effort to list available levels.",
                app.connection->name, app.model, value);
        return 0;
    }
    int changed = !app.effort || strcmp(app.effort, value);
    char *next = xstrdup(value);
    free(app.effort);
    app.effort = next;
    json_t *selection = json_string(app.effort);
    if (app.connection && app.model) {
        if (!app.efforts)
            app.efforts = json_object();
        char *key = effort_key();
        json_object_set(app.efforts, key, selection);
        free(key);
        json_decref(selection);
        selection = json_pack("{s:s,s:s,s:s}", "provider", app.connection->name,
                              "model", app.model, "value", app.effort);
    }
    journal("effort", selection);
    json_decref(selection);
    if (changed)
        app.usage_valid = 0;
    ui_status();
    return 1;
}
static void usage_restore(json_t *data)
{
    json_decref(app.last_usage);
    app.last_usage = json_incref(data);
    json_t *usage = json_object_get(data, "usage");
    app.usage_valid = json_is_integer(json_object_get(usage, "input_tokens"));
}
void usage_record(json_t *response)
{
    json_t *usage = json_object_get(response, "usage");
    json_t *data =
        json_pack("{s:O,s:I,s:s,s:I,s:s}", "usage", usage ? usage : json_null(), "request",
                  (json_int_t)json_array_size(app.requests), "model", app.model, "context_limit",
                  (json_int_t)app.context_limit, "limit_source", app.limit_source);
    json_object_set_new(data, "provider", json_string(app.connection->name));
    json_object_set(data, "model_info", app.model_info);
    json_object_set_new(data, "requested_effort", json_string(app.effort));
    json_t *reasoning = json_object_get(response, "reasoning");
    if (reasoning)
        json_object_set(data, "response_reasoning", reasoning);
    journal(app.compacting ? "compaction_usage" : "usage", data);
    if (!app.compacting)
        usage_restore(data);
    json_decref(data);
    ui_status();
}
long context_input(void)
{
    return app.usage_valid ? jint(json_object_get(app.last_usage, "usage"), "input_tokens", -1)
                           : -1;
}

int session_open(const char *resume)
{
    char *model_option = app.model_override ? xstrdup(app.model) : NULL;
    json_t *selected = NULL;
    json_decref(app.model_info);
    app.model_info = NULL;
    app.items = json_array();
    app.requests = json_array();
    app.cycles = json_array();
    app.past_jobs = json_object();
    app.active_skills = json_object();
    app.instruction_dirs = json_object();
    json_object_set_new(app.instruction_dirs, app.root, json_true());
    app.next_job = 1;
    char *base = sessions_path();
    if (mkdirs(base, 0700)) {
        free(base);
        return -1;
    }
    if (resume) {
        app.session_id = *resume ? xstrdup(resume) : latest_session();
        if (!app.session_id || strchr(app.session_id, '/') || app.session_id[0] == '.') {
            message("No matching session");
            free(base);
            return -1;
        }
        app.session_dir = fmt("%s/%s", base, app.session_id);
        char *p = fmt("%s/meta.json", app.session_dir);
        json_t *m = load_json(p);
        free(p);
        if (!jstr(m, "cwd") || strcmp(jstr(m, "cwd"), app.root)) {
            message("Session belongs to a different project");
            json_decref(m);
            free(base);
            return -1;
        }
        selected = json_incref(m);
        free(app.effort);
        app.effort = xstrdup(jstr(m, "effort") ? jstr(m, "effort") : "default");
        if (jstr(m, "model")) {
            free(app.model);
            app.model = xstrdup(jstr(m, "model"));
        }
        json_decref(m);
    } else {
        time_t t = time(NULL);
        struct tm tm;
        localtime_r(&t, &tm);
        char stamp[40];
        strftime(stamp, sizeof(stamp), "%Y%m%d-%H%M%S", &tm);
        app.session_dir = fmt("%s/%s-XXXXXX", base, stamp);
        if (!mkdtemp(app.session_dir)) {
            free(base);
            return -1;
        }
        app.session_id = xstrdup(strrchr(app.session_dir, '/') + 1);
        char *p = fmt("%s/meta.json", app.session_dir);
        json_t *m = json_pack("{s:s,s:s,s:s,s:s,s:s}", "cwd", app.root, "model", app.model,
                              "version", VERSION, "provider", app.connection->name,
                              "effort", app.effort);
        int r = save_json(p, m);
        json_decref(m);
        free(p);
        if (r) {
            free(base);
            return -1;
        }
    }
    free(base);
    char *path = fmt("%s/journal.jsonl", app.session_dir);
    int fd = open(path, O_RDWR | O_CREAT, 0600);
    free(path);
    if (fd < 0)
        return -1;
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    struct flock lock = {.l_type = F_WRLCK, .l_whence = SEEK_SET};
    if (fcntl(fd, F_SETLK, &lock)) {
        close(fd);
        message("Session is already open");
        return -1;
    }
    app.journal = fdopen(fd, "a+");
    if (!app.journal) {
        close(fd);
        return -1;
    }
    rewind(app.journal);
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    int partial = 0;
    off_t broken_offset = 0;
    while ((n = getline(&line, &cap, app.journal)) > 0) {
        json_error_t error;
        json_t *e = json_loadb(line, (size_t)n, 0, &error);
        if (!e) {
            broken_offset = ftello(app.journal) - n;
            if (line[n - 1] != '\n') {
                partial = 1;
                break;
            }
            ssize_t recovered = getline(&line, &cap, app.journal);
            json_t *note = recovered > 0 ? json_loadb(line, (size_t)recovered, 0, &error) : NULL;
            const char *kind = jstr(note, "type");
            int known = kind && !strcmp(kind, "recovery") &&
                        jint(json_object_get(note, "data"), "offset", -1) == broken_offset;
            json_decref(note);
            if (known)
                continue;
            message("Invalid journal record at line %d", error.line);
            free(line);
            return -1;
        }
        const char *type = jstr(e, "type");
        json_t *data = json_object_get(e, "data");
        if (type && !strcmp(type, "item")) {
            json_array_append(app.items, data);
            skills_track(data, 0);
        } else if (type && !strcmp(type, "cycle"))
            json_array_append(app.cycles, json_object_get(data, "start"));
        else if (type && !strcmp(type, "request"))
            json_array_append(app.requests, data);
        else if (type && !strcmp(type, "provider")) {
            json_decref(selected);
            selected = json_incref(data);
            app.connection_start = json_array_size(app.items);
            json_decref(app.model_info);
            app.model_info = NULL;
            json_decref(app.last_usage);
            app.last_usage = NULL;
            app.usage_valid = 0;
            if (jstr(data, "model")) {
                free(app.model);
                app.model = xstrdup(jstr(data, "model"));
            }
        } else if (type && !strcmp(type, "model") && json_is_string(data)) {
            free(app.model);
            app.model = xstrdup(json_string_value(data));
        } else if (type && !strcmp(type, "effort") && json_is_string(data)) {
            free(app.effort);
            app.effort = xstrdup(json_string_value(data));
        } else if (type && !strcmp(type, "effort") && jstr(data, "value") &&
                   jstr(data, "provider") && jstr(data, "model")) {
            if (!app.efforts)
                app.efforts = json_object();
            char *key = fmt("%s\n%s", jstr(data, "provider"), jstr(data, "model"));
            json_object_set(app.efforts, key, json_object_get(data, "value"));
            free(key);
            free(app.effort);
            app.effort = xstrdup(jstr(data, "value"));
        } else if (type && !strcmp(type, "model_info")) {
            json_decref(app.model_info);
            app.model_info = json_incref(data);
        } else if (type && !strcmp(type, "instruction_dir") && json_is_string(data))
            json_object_set_new(app.instruction_dirs, json_string_value(data), json_true());
        else if (type && !strcmp(type, "skill"))
            skills_restore(data);
        else if (type && !strcmp(type, "usage"))
            usage_restore(data);
        else if (type && !strcmp(type, "compact")) {
            app.usage_valid = 0;
            app.cut = (size_t)jint(data, "cut", 0);
            free(app.summary);
            app.summary = xstrdup(jstr(data, "summary"));
        } else if (type && !strcmp(type, "job")) {
            int id = (int)jint(data, "id", 0);
            char key[32];
            snprintf(key, sizeof(key), "%d", id);
            json_object_set(app.past_jobs, key, data);
            if (id >= app.next_job)
                app.next_job = id + 1;
        }
        json_decref(e);
    }
    free(line);
    clearerr(app.journal);
    fseek(app.journal, 0, SEEK_END);
    if (partial) {
        fputc('\n', app.journal);
        fflush(app.journal);
        json_t *note = json_pack("{s:I,s:s}", "offset", (json_int_t)broken_offset, "detail",
                                 "Unfinished final record preserved. Its outcome is unknown.");
        journal("recovery", note);
        json_decref(note);
        message("Unfinished journal record preserved");
    }
    if (selected) {
        /* Only the final connection must still be configured. Earlier connections are history. */
        char *model = app.model;
        app.model = xstrdup(app.connection->model);
        size_t start = app.connection_start;
        json_t *info = app.model_info, *usage = app.last_usage;
        int valid = app.usage_valid;
        app.model_info = app.last_usage = NULL;
        int restored = connection_restore(selected);
        json_decref(selected);
        json_decref(app.model_info);
        json_decref(app.last_usage);
        app.model_info = info;
        app.last_usage = usage;
        app.usage_valid = valid;
        app.connection_start = start;
        free(app.model);
        app.model = model;
        if (!restored)
            return -1;
    }
    if (resume)
        session_recover();
    int changed = app.connection_override &&
                  strcmp(app.connection_override, app.connection->name);
    if (changed && !connection_select(app.connection_override))
        return -1;
    if (model_option) {
        free(app.model);
        app.model = model_option;
        if (resume) {
            json_t *selection = json_string(app.model);
            journal("model", selection);
            json_decref(selection);
        }
    }
    if (changed || (resume && model_option))
        effort_activate();
    if (!effort_select(app.effort_override ? app.effort_override : app.effort))
        return -1;
    if (changed) {
        json_t *selection = json_pack("{s:s,s:s}", "provider", app.connection->name, "model",
                                      app.model);
        journal("provider", selection);
        json_decref(selection);
    }
    skills_preserve(app.cut);
    /* Source refresh appends events and must follow journal-tail recovery. */
    if (!instructions_load())
        return -1;
    if (app.summary) {
        char *summary_path = fmt("%s/summary.txt", app.session_dir);
        if (access(summary_path, F_OK) &&
            atomic_write(summary_path, app.summary, strlen(app.summary), 0600))
            message("Could not save summary view");
        free(summary_path);
    }
    const char *measured_model = jstr(app.last_usage, "model");
    if (!measured_model || strcmp(measured_model, app.model))
        app.usage_valid = 0;
    json_t *saved = app.model_info ? app.model_info : json_object_get(app.last_usage, "model_info");
    const char *saved_model = jstr(saved, "model");
    if (saved_model && !strcmp(saved_model, app.model))
        context_budget(json_object_get(saved, "catalog"), "catalog (saved)");
    else {
        const char *source = jstr(app.last_usage, "limit_source");
        json_t *legacy = json_pack("{s:I}", "context_window",
                                   (json_int_t)jint(app.last_usage, "context_limit", 0));
        context_budget(measured_model && !strcmp(measured_model, app.model) && source &&
                               !strncmp(source, "catalog", 7)
                           ? legacy
                           : NULL,
                       "catalog (saved)");
        json_decref(legacy);
    }
    if (json_array_size(app.requests)) {
        json_t *last = json_array_get(app.requests, json_array_size(app.requests) - 1);
        const char *instructions = jstr(json_object_get(last, "request"), "instructions");
        if (instructions && strcmp(instructions, app.instructions))
            app.usage_valid = 0;
    }
    return 0;
}
void session_recover(void)
{
    json_t *results = json_object();
    size_t i;
    json_t *it;
    json_array_foreach(app.items, i, it)
    {
        const char *t = jstr(it, "type"), *id = jstr(it, "call_id");
        if (t && id && !strcmp(t, "function_call_output"))
            json_object_set_new(results, id, json_true());
    }
    size_t n = json_array_size(app.items);
    for (i = 0; i < n; i++) {
        it = json_array_get(app.items, i);
        const char *t = jstr(it, "type"), *id = jstr(it, "call_id");
        if (t && id && !strcmp(t, "function_call") && !json_object_get(results, id)) {
            item_add(
                json_pack("{s:s,s:s,s:s}", "type", "function_call_output", "call_id", id, "output",
                          "Outcome unknown after interruption. This action has not been replayed. "
                          "Inspect current files and processes before deciding what to do next."));
            message("Unconfirmed tool result: %s", jstr(it, "name") ? jstr(it, "name") : id);
        }
    }
    json_decref(results);
    if (json_object_size(app.past_jobs))
        item_add(json_pack("{s:s,s:s}", "role", "developer", "content",
                           "Application restarted. No previous jobs were restarted or attached. "
                           "Their saved logs remain available. No actions have been replayed."));
}
static void history_append(json_t *input, size_t index)
{
    json_t *item = json_array_get(app.items, index);
    if (index >= app.connection_start) {
        json_array_append(input, item);
        return;
    }
    const char *type = jstr(item, "type"), *role = jstr(item, "role");
    if (type && !strcmp(type, "reasoning"))
        return;
    if (type && (!strcmp(type, "function_call") || !strcmp(type, "function_call_output"))) {
        json_t *portable = json_object();
        const char *fields[] = {"type", "name", "arguments", "call_id", "output"};
        for (size_t i = 0; i < sizeof(fields) / sizeof(fields[0]); i++) {
            json_t *value = json_object_get(item, fields[i]);
            if (value)
                json_object_set(portable, fields[i], value);
        }
        json_array_append_new(input, portable);
    } else if (role && !strcmp(role, "assistant")) {
        json_t *content = json_object_get(item, "content");
        if (json_is_string(content)) {
            json_array_append_new(input, json_pack("{s:s,s:O}", "role", role, "content", content));
            return;
        }
        Buf text = {0};
        size_t i;
        json_t *part;
        json_array_foreach(content, i, part)
        {
            const char *value = jstr(part, "text");
            if (!value)
                value = jstr(part, "refusal");
            if (value)
                buf_puts(&text, value);
        }
        if (text.len)
            json_array_append_new(input, json_pack("{s:s,s:s}", "role", role, "content", text.data));
        buf_clear(&text);
    } else
        json_array_append(input, item);
}
json_t *context_build(size_t cut, size_t end, const char *summary, size_t keep_user)
{
    json_t *input = json_array();
    if (summary && *summary) {
        char *text = fmt("Earlier conversation summary:\n%s", summary);
        json_array_append_new(input, json_pack("{s:s,s:s}", "role", "developer", "content", text));
        free(text);
    }
    if (keep_user < cut && keep_user < json_array_size(app.items))
        history_append(input, keep_user);
    if (end > json_array_size(app.items))
        end = json_array_size(app.items);
    for (size_t i = cut; i < end; i++)
        history_append(input, i);
    return input;
}
static size_t last_user(void)
{
    size_t i = json_array_size(app.items);
    while (i) {
        json_t *o = json_array_get(app.items, --i);
        const char *r = jstr(o, "role");
        if (r && !strcmp(r, "user"))
            return i;
    }
    return (size_t)-1;
}
json_t *request_build(void)
{
    json_decref(app.delivered_instructions);
    app.delivered_instructions = json_incref(app.instruction_sources);
    json_t *input = context_build(app.cut, json_array_size(app.items), app.summary, last_user());
    json_t *r = json_pack("{s:s,s:s,s:o,s:b,s:b}", "model", app.model, "instructions",
                          app.instructions, "input", input, "store", 0, "stream", 1);
    json_object_set_new(r, "tools", tool_schema());
    return r;
}
static long fraction(long tokens, long percent)
{
    return tokens / 100 * percent + tokens % 100 * percent / 100;
}
void context_budget(json_t *model, const char *source)
{
    long window = jint(model, "context_window", 0);
    long percent = jint(model, "effective_context_window_percent", 100);
    long recommended = jint(model, "auto_compact_token_limit", 0);
    json_t *catalog = json_is_object(model) ? json_object() : json_null();
    const char *fields[] = {"context_window", "effective_context_window_percent",
                            "auto_compact_token_limit", "supported_reasoning_levels",
                            "default_reasoning_level"};
    for (size_t i = 0; i < sizeof(fields) / sizeof(fields[0]); i++) {
        json_t *value = json_object_get(model, fields[i]);
        if (value)
            json_object_set(catalog, fields[i], value);
    }
    app.context_known = window > 0 || CONTEXT_LIMIT > 0;
    app.context_limit = window > 0 ? window : CONTEXT_FALLBACK;
    app.limit_source = window > 0 ? source : "config.h fallback";
    if (window > 0 && percent > 0 && percent <= 100 && fraction(window, percent) > 0)
        app.context_limit = fraction(window, percent);
    if (CONTEXT_LIMIT > 0 && (window <= 0 || CONTEXT_LIMIT < app.context_limit)) {
        app.context_limit = CONTEXT_LIMIT;
        app.limit_source = "config.h";
    }
    app.compact_limit = fraction(app.context_limit, COMPACT_PERCENT);
    app.compact_source = "config.h";
    if (recommended > 0 && recommended < app.context_limit) {
        app.compact_limit = recommended;
        app.compact_source = source;
    }
    json_t *info = json_pack("{s:s,s:o,s:I,s:s,s:I,s:s}", "model", app.model, "catalog", catalog,
                             "context_limit", (json_int_t)app.context_limit, "limit_source",
                             app.limit_source, "compact_limit", (json_int_t)app.compact_limit,
                             "compact_source", app.compact_source);
    json_object_set_new(info, "provider",
                        json_string(app.connection ? app.connection->name : DEFAULT_PROVIDER));
    json_decref(app.model_info);
    app.model_info = info;
    ui_status();
}
void context_show(void)
{
    long input = context_input();
    const char *status = strstr(app.limit_source, "saved") ? " saved" : "";
    ui_print("Model: %s (%s)", app.model, app.connection->name);
    if (!app.context_known && !json_is_object(json_object_get(app.model_info, "catalog")))
        ui_print("Context: unknown (catalog not loaded)");
    else if (!app.context_known)
        ui_print("Context: unknown (fallback %ld, compact %ld)", app.context_limit,
                 app.compact_limit);
    else if (input >= 0)
        ui_print("Context: ~%.1f%% (%ld/%ld%s), compact %ld", 100.0 * input / app.context_limit,
                 input, app.context_limit, status, app.compact_limit);
    else
        ui_print("Context: unmeasured (%ld%s budget, compact %ld)", app.context_limit, status,
                 app.compact_limit);
    ui_print("Session: %s", app.session_dir);
}
void request_record(json_t *request)
{
    json_t *template = json_deep_copy(request);
    json_object_del(template, "input");
    json_t *selection = json_array();
    size_t i;
    json_t *item;
    json_array_foreach(json_object_get(request, "input"), i, item)
    {
        size_t k = 0;
        while (k < json_array_size(app.items) && json_array_get(app.items, k) != item)
            k++;
        if (k < json_array_size(app.items))
            json_array_append_new(selection, json_integer((json_int_t)k));
        else
            json_array_append(selection, item);
    }
    json_t *r = json_pack("{s:s,s:o,s:o}", "provider", app.connection->name,
                          "request", template, "selection", selection);
    journal("request", r);
    json_array_append_new(app.requests, r);
    char *path = fmt("%s/request.json", app.session_dir);
    if (save_json(path, request))
        message("Could not save request view");
    free(path);
}
/* Exchange only conversation ownership. Terminal and transport state stay live. */
static void session_exchange(App *other)
{
#define EXCHANGE(field)                                      \
    do {                                                     \
        unsigned char value[sizeof(app.field)];               \
        memcpy(value, &app.field, sizeof(value));              \
        memcpy(&app.field, &other->field, sizeof(value));      \
        memcpy(&other->field, value, sizeof(value));           \
    } while (0)
    EXCHANGE(session_id);
    EXCHANGE(session_dir);
    EXCHANGE(journal);
    EXCHANGE(model);
    EXCHANGE(effort);
    EXCHANGE(connection);
    EXCHANGE(api_base);
    EXCHANGE(connection_models);
    EXCHANGE(efforts);
    EXCHANGE(credentials);
    EXCHANGE(provider_state);
    EXCHANGE(models);
    EXCHANGE(model_info);
    EXCHANGE(items);
    EXCHANGE(requests);
    EXCHANGE(cycles);
    EXCHANGE(past_jobs);
    EXCHANGE(active_skills);
    EXCHANGE(skills);
    EXCHANGE(instruction_dirs);
    EXCHANGE(instruction_sources);
    EXCHANGE(delivered_instructions);
    EXCHANGE(instructions);
    EXCHANGE(last_usage);
    EXCHANGE(summary);
    EXCHANGE(cut);
    EXCHANGE(connection_start);
    EXCHANGE(next_job);
    EXCHANGE(jobs);
    EXCHANGE(context_limit);
    EXCHANGE(compact_limit);
    EXCHANGE(limit_source);
    EXCHANGE(compact_source);
    EXCHANGE(context_known);
    EXCHANGE(usage_valid);
    EXCHANGE(context_exceeded);
#undef EXCHANGE
}
static void session_dispose(void)
{
    session_close();
    free(app.session_id);
    free(app.session_dir);
    free(app.model);
    free(app.effort);
    free(app.api_base);
    free(app.instructions);
    free(app.summary);
    json_decref(app.connection_models);
    json_decref(app.efforts);
    auth_release();
    json_decref(app.models);
    json_decref(app.model_info);
    json_decref(app.items);
    json_decref(app.requests);
    json_decref(app.cycles);
    json_decref(app.past_jobs);
    json_decref(app.active_skills);
    json_decref(app.skills);
    json_decref(app.instruction_dirs);
    json_decref(app.instruction_sources);
    json_decref(app.delivered_instructions);
    json_decref(app.last_usage);
}
int session_switch(const char *id)
{
    if (id && app.session_id && !strcmp(id, app.session_id)) {
        message("Session is already active");
        return 0;
    }
    App previous = {0};
    session_exchange(&previous);
    app.connection = previous.connection;
    app.model = xstrdup(previous.model);
    app.effort = xstrdup(id ? "default" : previous.effort);
    if (!id)
        app.models = json_incref(previous.models);
    app.api_base = xstrdup(previous.api_base);
    app.credentials = json_incref(previous.credentials);
    const char *provider_override = app.connection_override, *effort_override = app.effort_override;
    int model_override = app.model_override;
    app.connection_override = app.effort_override = NULL;
    app.model_override = 0;
    context_budget(NULL, NULL);
    int ok = !session_open(id);
    app.connection_override = provider_override;
    app.effort_override = effort_override;
    app.model_override = model_override;
    if (!ok) {
        if (!id)
            message("Could not create a new session");
        session_dispose();
        session_exchange(&previous);
        ui_status();
        return 0;
    }
    session_exchange(&previous);
    jobs_shutdown();
    session_dispose();
    session_exchange(&previous);
    app.compact_requested = 0;
    app.exit_status = 0;
    session_show(id != NULL);
    if (!app.tty) {
        message("Provider: %s", app.connection->name);
        message("Model: %s", app.model);
        message("Effort: %s", app.effort);
    }
    ui_status();
    return 1;
}
void session_close(void)
{
    if (app.journal) {
        fflush(app.journal);
        fclose(app.journal);
        app.journal = NULL;
    }
}
