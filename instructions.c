#include "harn.h"
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static void parent(char *path)
{
    char *slash = strrchr(path, '/');
    if (slash)
        slash[slash == path ? 1 : 0] = 0;
}
static char *directory(const char *path)
{
    char *probe = path[0] == '/' ? xstrdup(path) : fmt("%s/%s", app.root, path);
    char *resolved = realpath(probe, NULL);
    struct stat st;
    int is_dir = resolved && !stat(resolved, &st) && S_ISDIR(st.st_mode);
    json_t *tail = json_array();
    while (!resolved && strcmp(probe, "/")) {
        char *slash = strrchr(probe, '/');
        json_array_append_new(tail, json_string(slash + 1));
        parent(probe);
        resolved = realpath(probe, NULL);
    }
    free(probe);
    if (resolved) {
        for (size_t i = json_array_size(tail); i; i--) {
            const char *part = json_string_value(json_array_get(tail, i - 1));
            if (!strcmp(part, ".."))
                parent(resolved);
            else if (*part && strcmp(part, ".")) {
                char *next = fmt("%s%s%s", resolved, strcmp(resolved, "/") ? "/" : "", part);
                free(resolved);
                resolved = next;
            }
        }
        if (!is_dir)
            parent(resolved);
    }
    json_decref(tail);
    return resolved;
}
static int source(json_t *sources, const char *path, const char *scope, int strict)
{
    if (json_object_get(sources, scope))
        return 1;
    /* The personal source already applies everywhere, including its own directory
     * and aliases reached through symlinks or hard links. */
    const char *global = jstr(json_object_get(sources, "global"), "path");
    struct stat personal, candidate;
    if (global && !stat(global, &personal) && !stat(path, &candidate) &&
        personal.st_dev == candidate.st_dev && personal.st_ino == candidate.st_ino)
        return 1;
    size_t len = 0;
    char *text = read_file(path, &len);
    if (!text && (errno == ENOENT || errno == ENOTDIR))
        return 1;
    const char *error = text ? NULL : strerror(errno);
    json_t *content = text && !memchr(text, 0, len) ? json_stringn(text, len) : NULL;
    if (text && !content)
        error = "expected UTF-8 text";
    free(text);
    if (error && strict) {
        message("Cannot read AGENTS.md: %s (%s)", path, error);
        return 0;
    }
    json_t *entry = json_pack("{s:s,s:s}", "path", path, "scope", scope);
    if (error)
        json_object_set_new(entry, "error", json_string(error));
    else
        json_object_set_new(entry, "content", content);
    json_object_set_new(sources, scope, entry);
    return 1;
}
static int chain(json_t *sources, const char *dir, int strict)
{
    if (strcmp(dir, "/")) {
        char *above = xstrdup(dir);
        parent(above);
        int ok = chain(sources, above, strict);
        free(above);
        if (!ok)
            return 0;
    }
    char *path = fmt("%s%sAGENTS.md", dir, strcmp(dir, "/") ? "/" : "");
    int ok = source(sources, path, dir, strict);
    free(path);
    return ok;
}
int instructions_load(void)
{
    json_t *sources = json_object();
    char *path = fmt("%s/AGENTS.md", app.config_dir);
    int ok = source(sources, path, "global", 1);
    free(path);
    if (!ok) {
        json_decref(sources);
        return 0;
    }
    if (!chain(sources, app.root, 1)) {
        json_decref(sources);
        return 0;
    }
    const char *dir;
    json_t *unused;
    json_object_foreach(app.instruction_dirs, dir, unused) chain(sources, dir, 0);
    if (!json_equal(sources, app.instruction_sources)) {
        const char *scope;
        json_t *entry;
        json_object_foreach(sources, scope, entry)
        {
            if (json_equal(entry, json_object_get(app.instruction_sources, scope)))
                continue;
            if (jstr(entry, "error"))
                message("Cannot read AGENTS.md: %s (%s)", jstr(entry, "path"),
                        jstr(entry, "error"));
            else
                message("read instructions: %s (scope %s)", jstr(entry, "path"), scope);
        }
        json_object_foreach(app.instruction_sources, scope,
                            entry) if (!json_object_get(sources, scope))
            message("Instructions removed: %s", jstr(entry, "path"));
        journal("instructions", sources);
    }
    Buf instructions = {0};
    buf_puts(&instructions, SYSTEM_PROMPT);
    buf_puts(
        &instructions,
        "\n\nThe latest Current harn policy snapshot in the conversation replaces all earlier "
        "snapshots, including removed sources and skills. Historical AGENTS.md reads do not "
        "override that snapshot. Files combine by directory scope. Global rules apply everywhere. "
        "Closer directories override conflicting ancestor rules while other rules remain. "
        "Explicit user requests take precedence. Sources listed in the snapshot are loaded and do "
        "not need another read. Before exec affects a new directory, inspect "
        "its AGENTS.md chain with read. If write/edit reports changed instructions, reconsider "
        "the change using the updated rules before retrying.\n");
    if (!app.instructions || strcmp(app.instructions, instructions.data)) {
        if (app.instructions)
            app.usage_valid = 0;
        free(app.instructions);
        app.instructions = xstrdup(instructions.data);
        json_t *event = json_string(app.instructions);
        journal("system_instructions", event);
        json_decref(event);
    }
    buf_clear(&instructions);
    buf_puts(&instructions, "Current harn policy snapshot:\n");
    const char *scope;
    json_t *entry;
    json_object_foreach(sources, scope, entry)
    {
        char *header = fmt("\nAGENTS.md source: %s\nScope: %s\n", jstr(entry, "path"), scope);
        buf_puts(&instructions, header);
        free(header);
        buf_puts(&instructions, jstr(entry, "content")
                                    ? jstr(entry, "content")
                                    : "Instructions unavailable. File operations "
                                      "in this scope must wait until readable.");
        buf_puts(&instructions, "\n");
    }
    skills_refresh(&instructions);
    int changed = !json_equal(sources, json_object_get(app.policy_snapshot, "sources")) ||
                  !json_equal(app.skills, json_object_get(app.policy_snapshot, "skills"));
    json_decref(app.instruction_sources);
    app.instruction_sources = sources;
    if (changed) {
        size_t index = json_array_size(app.items);
        item_add(json_pack("{s:s,s:s}", "role", "developer", "content", instructions.data));
        json_decref(app.policy_snapshot);
        app.policy_snapshot = json_pack("{s:O,s:O,s:I}", "sources", sources, "skills", app.skills,
                                        "item", (json_int_t)index);
        journal("policy", app.policy_snapshot);
        app.usage_valid = 0;
    }
    buf_clear(&instructions);
    instructions_preserve();
    ui_status();
    return 1;
}
void instructions_preserve(void)
{
    long index = jint(app.policy_snapshot, "item", -1);
    if (index < 0 || (size_t)index >= app.cut)
        return;
    json_t *item = json_array_get(app.items, (size_t)index);
    if (!item)
        return;
    size_t next = json_array_size(app.items);
    item_add(json_incref(item));
    json_object_set_new(app.policy_snapshot, "item", json_integer((json_int_t)next));
    journal("policy", app.policy_snapshot);
}
static int contains(const char *dir, const char *scope)
{
    size_t n = strlen(scope);
    return !strcmp(scope, "global") || !strcmp(scope, "/") ||
           (!strncmp(dir, scope, n) && (!dir[n] || dir[n] == '/'));
}
static json_t *applicable(json_t *sources, const char *dir)
{
    json_t *selected = json_object();
    const char *scope;
    json_t *entry;
    json_object_foreach(sources, scope, entry) if (contains(dir, scope))
        json_object_set(selected, scope, entry);
    return selected;
}
json_t *instructions_require(const char *path, int writing)
{
    char *dir = directory(path);
    if (!dir)
        return json_pack("{s:s}", "error", strerror(errno));
    if (!json_object_get(app.instruction_dirs, dir)) {
        json_object_set_new(app.instruction_dirs, dir, json_true());
        json_t *entry = json_string(dir);
        journal("instruction_dir", entry);
        json_decref(entry);
    }
    if (!instructions_load()) {
        free(dir);
        return json_pack("{s:s}", "error", "Could not load applicable AGENTS.md instructions");
    }
    json_t *current = applicable(app.instruction_sources, dir), *result = NULL;
    const char *scope;
    json_t *entry;
    json_object_foreach(current, scope, entry)
    {
        if (jstr(entry, "error")) {
            result =
                json_pack("{s:s,s:s}", "error", "Cannot read applicable AGENTS.md instructions",
                          "path", jstr(entry, "path"));
            break;
        }
    }
    if (!result && writing) {
        json_t *delivered = applicable(app.delivered_instructions, dir);
        if (!json_equal(current, delivered))
            result = json_pack("{s:s,s:s,s:b}", "error",
                               "AGENTS.md instructions changed for this path. No file was changed. "
                               "Review the updated instructions and prepare the change again.",
                               "path", path, "instructions_changed", 1);
        json_decref(delivered);
    }
    json_decref(current);
    free(dir);
    return result;
}
