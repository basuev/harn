#include "harn.h"
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static json_t *error(const char *s)
{
    return json_pack("{s:s}", "error", s);
}
typedef struct {
    const char *text;
    size_t len;
} Line;
static Line *lines(const char *text, size_t *count)
{
    *count = 0;
    for (const char *p = text; *p; p++)
        if (*p == '\n')
            (*count)++;
    size_t len = strlen(text);
    if (len && text[len - 1] != '\n')
        (*count)++;
    Line *result = xmalloc((*count + 1) * sizeof(*result));
    const char *p = text;
    for (size_t i = 0; i < *count; i++) {
        const char *end = strchr(p, '\n');
        result[i] = (Line){p, end ? (size_t)(end + 1 - p) : strlen(p)};
        p += result[i].len;
    }
    return result;
}
static int same_line(Line a, Line b)
{
    return a.len == b.len && !memcmp(a.text, b.text, a.len);
}
static void diff_line(Buf *out, char prefix, Line line)
{
    buf_add(out, &prefix, 1);
    buf_add(out, line.text, line.len);
    if (line.len && line.text[line.len - 1] != '\n')
        buf_puts(out, "\n\\ No newline at end of file\n");
}
static void change_show(const char *operation, const char *path, const char *before,
                        const char *after, int created)
{
    json_t *valid = before ? json_string(before) : NULL;
    if (!valid) {
        message("%s %s: %zu bytes written, prior contents unavailable as UTF-8 text", operation,
                path, strlen(after));
        return;
    }
    json_decref(valid);
    size_t old_count, new_count;
    Line *old = lines(before, &old_count), *next = lines(after, &new_count);
    size_t prefix = 0, suffix = 0;
    while (prefix < old_count && prefix < new_count && same_line(old[prefix], next[prefix]))
        prefix++;
    while (suffix < old_count - prefix && suffix < new_count - prefix &&
           same_line(old[old_count - suffix - 1], next[new_count - suffix - 1]))
        suffix++;
    if (prefix == old_count && prefix == new_count && !created) {
        message("%s %s: unchanged (%zu bytes)", operation, path, strlen(after));
        free(old);
        free(next);
        return;
    }
    ui_meta("%s %s: %zu bytes%s", operation, path, strlen(after), created ? ", created" : "");
    if (!old_count && !new_count) {
        json_t *event = json_pack("{s:s,s:s,s:s,s:b}", "tool", operation, "path", path, "diff", "",
                                  "created", created);
        journal("change", event);
        json_decref(event);
        free(old);
        free(next);
        return;
    }
    size_t start = prefix > 3 ? prefix - 3 : 0, context = suffix > 3 ? 3 : suffix;
    size_t old_end = old_count - suffix + context, new_end = new_count - suffix + context;
    char *header = fmt("--- %s\n+++ %s\n@@ -%zu,%zu +%zu,%zu @@\n", created ? "/dev/null" : path,
                       path, old_end > start ? start + 1 : start, old_end - start,
                       new_end > start ? start + 1 : start, new_end - start);
    Buf out = {0};
    buf_puts(&out, header);
    free(header);
    for (size_t i = start; i < prefix; i++)
        diff_line(&out, ' ', old[i]);
    for (size_t i = prefix; i < old_count - suffix; i++)
        diff_line(&out, '-', old[i]);
    for (size_t i = prefix; i < new_count - suffix; i++)
        diff_line(&out, '+', next[i]);
    for (size_t i = old_count - suffix; i < old_end; i++)
        diff_line(&out, ' ', old[i]);
    ui_notice(out.data, out.len);
    json_t *event = json_pack("{s:s,s:s,s:s}", "tool", operation, "path", path, "diff", out.data);
    journal("change", event);
    json_decref(event);
    buf_clear(&out);
    free(old);
    free(next);
}
static json_t *file_read(json_t *args)
{
    const char *path = jstr(args, "path");
    if (!path)
        return error("path is required");
    json_t *skill = skills_read(args);
    if (skill)
        return skill;
    FILE *f = fopen(path, "r");
    if (!f)
        return error(strerror(errno));
    long start = jint(args, "start_line", 1), end = jint(args, "end_line", 0);
    long offset = jint(args, "offset", 0);
    if (offset < 0 ||
        (offset && (json_object_get(args, "start_line") || json_object_get(args, "end_line"))) ||
        (offset && fseeko(f, offset, SEEK_SET))) {
        fclose(f);
        return error("Choose a valid line range or byte offset");
    }
    if (start < 1 || (end && end < start)) {
        fclose(f);
        return error("Invalid line range");
    }
    Buf b = {0};
    char *line = NULL;
    size_t cap = 0;
    ssize_t n;
    long number = 0, next = 0;
    off_t next_offset = offset;
    while ((n = getline(&line, &cap, f)) >= 0) {
        number++;
        if (number < start)
            continue;
        if (end && number > end) {
            next_offset = ftello(f) - n;
            break;
        }
        if (memchr(line, 0, (size_t)n)) {
            free(line);
            fclose(f);
            buf_clear(&b);
            return error("Binary file. Use exec for binary inspection.");
        }
        if (b.len + (size_t)n > OUTPUT_LIMIT) {
            size_t room = OUTPUT_LIMIT - b.len;
            while (room && ((unsigned char)line[room] & 0xc0) == 0x80)
                room--;
            buf_add(&b, line, room);
            next = number;
            next_offset = ftello(f) - n + (off_t)room;
            break;
        }
        buf_add(&b, line, (size_t)n);
        next_offset = ftello(f);
    }
    int bad = ferror(f);
    free(line);
    fclose(f);
    if (bad) {
        buf_clear(&b);
        return error("File read failed");
    }
    char *text = utf8_text(b.data ? b.data : "", b.len);
    json_t *r = json_pack("{s:s,s:s,s:i,s:i,s:b}", "path", path, "content", text, "start_line",
                          (int)start, "next_line", (int)next, "truncated", next != 0);
    json_object_set_new(r, "next_offset", json_integer(next_offset));
    if (offset)
        ui_meta("read %s: offset %ld, %zu bytes", path, offset, b.len);
    else if (end)
        ui_meta("read %s: lines %ld-%ld, %zu bytes", path, start, end, b.len);
    else
        ui_meta("read %s: from line %ld, %zu bytes", path, start, b.len);
    if (next)
        message("read truncated. Continue at byte offset %lld.", (long long)next_offset);
    free(text);
    buf_clear(&b);
    return r;
}
static char *write_path(const char *path)
{
    char *resolved = realpath(path, NULL);
    return resolved ? resolved : xstrdup(path);
}
static int save_file(const char *path, const char *data, size_t len)
{
    char *target = write_path(path);
    struct stat st;
    mode_t mode = stat(target, &st) ? 0644 : st.st_mode & 0777;
    char *parent = xstrdup(target), *slash = strrchr(parent, '/');
    if (slash) {
        *slash = 0;
        if (*parent && mkdirs(parent, 0755)) {
            free(parent);
            free(target);
            return -1;
        }
    }
    free(parent);
    int r = atomic_write(target, data, len, mode);
    free(target);
    return r;
}
static json_t *file_write(json_t *args)
{
    const char *path = jstr(args, "path"), *content = jstr(args, "content");
    if (!path || !content)
        return error("path and content are required");
    size_t before_len = 0;
    char *before = read_file(path, &before_len);
    int created = !before && errno == ENOENT;
    if (before && memchr(before, 0, before_len)) {
        free(before);
        before = NULL;
    }
    if (save_file(path, content, strlen(content))) {
        free(before);
        return error(strerror(errno));
    }
    change_show("write", path, created ? "" : before, content, created);
    free(before);
    return json_pack("{s:s,s:I}", "path", path, "bytes", (json_int_t)strlen(content));
}
static json_t *file_edit(json_t *args)
{
    const char *path = jstr(args, "path"), *old = jstr(args, "old"),
               *replacement = jstr(args, "new");
    if (!path || !old || !*old || !replacement)
        return error("path, non-empty old, and new are required");
    size_t len;
    char *data = read_file(path, &len);
    if (!data)
        return error(strerror(errno));
    if (memchr(data, 0, len)) {
        free(data);
        return error("Cannot edit a binary file");
    }
    char *match = strstr(data, old);
    if (!match) {
        free(data);
        return error("No exact match");
    }
    if (strstr(match + 1, old)) {
        free(data);
        return error("Ambiguous match. Include more surrounding text.");
    }
    Buf next = {0};
    buf_add(&next, data, (size_t)(match - data));
    buf_puts(&next, replacement);
    buf_puts(&next, match + strlen(old));
    if (save_file(path, next.data, next.len)) {
        free(data);
        buf_clear(&next);
        return error(strerror(errno));
    }
    change_show("edit", path, data, next.data ? next.data : "", 0);
    free(data);
    json_t *r = json_pack("{s:s,s:I}", "path", path, "bytes", (json_int_t)next.len);
    buf_clear(&next);
    return r;
}
static json_t *job_result(int id, int output, long offset, long limit)
{
    json_t *result = job_info(id, output, offset, limit);
    if (json_is_true(json_object_get(result, "truncated")))
        message("Model receives output bytes %ld-%ld of %ld for #%d. Full log: %s",
                jint(result, "offset", 0), jint(result, "next_offset", 0), jint(result, "bytes", 0),
                id, jstr(result, "log_path"));
    return result;
}
json_t *tool_run(const char *name, json_t *args)
{
    if (!json_is_object(args))
        return error("Arguments must be a JSON object");
    if ((!strcmp(name, "read") || !strcmp(name, "write") || !strcmp(name, "edit")) &&
        jstr(args, "path")) {
        json_t *rules = instructions_require(jstr(args, "path"), strcmp(name, "read") != 0);
        if (rules)
            return rules;
    }
    if (!strcmp(name, "read"))
        return file_read(args);
    if (!strcmp(name, "write"))
        return file_write(args);
    if (!strcmp(name, "edit"))
        return file_edit(args);
    if (!strcmp(name, "exec")) {
        const char *command = jstr(args, "command");
        if (!command)
            return error("command is required");
        int background = json_is_true(json_object_get(args, "background"));
        Job *j = job_start(command, background);
        if (!j)
            return error(strerror(errno));
        if (!background) {
            ui_activity("exec #%d", j->id);
            long timeout = jint(args, "timeout_ms", 0);
            long long deadline = timeout > 0 ? now_ms() + timeout : 0;
            while ((job_running(j) || j->fd >= 0) && !app.cancel && !app.quit) {
                if (deadline && now_ms() >= deadline) {
                    job_stop(j, 0);
                    deadline = 0;
                }
                pump(100);
            }
            if (app.cancel || app.quit)
                job_stop(j, 0);
        }
        return job_result(j->id, 1, -1, OUTPUT_LIMIT);
    }
    if (!strcmp(name, "jobs")) {
        const char *action = jstr(args, "action");
        if (!action || !strcmp(action, "list")) {
            json_t *list = json_array();
            const char *key;
            json_t *value;
            json_object_foreach(app.past_jobs, key, value)
                json_array_append_new(list, job_info(atoi(key), 0, 0, 0));
            message("jobs list: %zu jobs", json_array_size(list));
            return list;
        }
        int id = (int)jint(args, "id", 0);
        Job *j = job_find(id);
        message("jobs %s #%d", action, id);
        if (!strcmp(action, "stop")) {
            if (j)
                job_stop(j, 0);
            return job_result(id, 1, -1, OUTPUT_LIMIT);
        }
        if (!strcmp(action, "wait")) {
            ui_activity("wait #%d", id);
            long timeout = jint(args, "timeout_ms", 1000);
            long long deadline = timeout > 0 ? now_ms() + timeout : 0;
            while (j && job_running(j) && !app.cancel && !app.quit &&
                   (!deadline || now_ms() < deadline))
                pump(100);
        } else if (strcmp(action, "output") && strcmp(action, "status"))
            return error("Unknown jobs action");
        json_t *result = job_result(id, strcmp(action, "status") != 0, jint(args, "offset", -1),
                                    jint(args, "limit", OUTPUT_LIMIT));
        if (jstr(result, "state"))
            message("jobs #%d: %s, exit code %ld", id, jstr(result, "state"),
                    jint(result, "exit_code", -1));
        return result;
    }
    return error("Unknown tool");
}
static json_t *function(const char *name, const char *description, const char *properties,
                        const char *required)
{
    json_error_t e;
    json_t *p = json_loads(properties, 0, &e), *r = json_loads(required, 0, &e);
    return json_pack("{s:s,s:s,s:s,s:b,s:{s:s,s:o,s:o,s:b}}", "type", "function", "name", name,
                     "description", description, "strict", 0, "parameters", "type", "object",
                     "properties", p, "required", r, "additionalProperties", 0);
}
json_t *tool_schema(void)
{
    json_t *tools = json_array();
    json_array_append_new(
        tools,
        function("read",
                 "Read a UTF-8 file, line range, or byte offset. Use next_offset when truncated.",
                 "{\"path\":{\"type\":\"string\"},\"start_line\":{\"type\":\"integer\"},\"end_"
                 "line\":{\"type\":\"integer\"},\"offset\":{\"type\":\"integer\"}}",
                 "[\"path\"]"));
    json_array_append_new(
        tools, function("write", "Create or replace a text file. Parent directories are created.",
                        "{\"path\":{\"type\":\"string\"},\"content\":{\"type\":\"string\"}}",
                        "[\"path\",\"content\"]"));
    json_array_append_new(tools, function("edit",
                                          "Replace exactly one occurrence of old with new. Missing "
                                          "or ambiguous matches are errors.",
                                          "{\"path\":{\"type\":\"string\"},\"old\":{\"type\":"
                                          "\"string\"},\"new\":{\"type\":\"string\"}}",
                                          "[\"path\",\"old\",\"new\"]"));
    json_array_append_new(tools,
                          function("exec",
                                   "Run a shell command in the project. Set background=true for "
                                   "servers or watchers. Returns a job id and output.",
                                   "{\"command\":{\"type\":\"string\"},\"background\":{\"type\":"
                                   "\"boolean\"},\"timeout_ms\":{\"type\":\"integer\"}}",
                                   "[\"command\"]"));
    json_array_append_new(
        tools,
        function("jobs",
                 "List jobs, inspect status/output, wait, or stop a job and its children. wait "
                 "timeout_ms defaults to 1000. Output offsets are bytes.",
                 "{\"action\":{\"type\":\"string\",\"enum\":[\"list\",\"status\",\"output\","
                 "\"wait\",\"stop\"]},\"id\":{\"type\":\"integer\"},\"timeout_ms\":{\"type\":"
                 "\"integer\"},\"offset\":{\"type\":\"integer\"},\"limit\":{\"type\":\"integer\"}}",
                 "[\"action\"]"));
    return tools;
}
