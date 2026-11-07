#include "harn.h"
#include <dirent.h>
#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <yaml.h>

static int delimiter(const char *line)
{
    if (strncmp(line, "---", 3))
        return 0;
    line += 3;
    while (*line == ' ' || *line == '\t' || *line == '\r')
        line++;
    return !*line || *line == '\n' || *line == '#';
}
static json_t *metadata(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) {
        if (errno != ENOENT)
            message("Cannot load skill %s: %s", path, strerror(errno));
        return NULL;
    }
    char *line = NULL;
    size_t cap = 0;
    ssize_t n = getline(&line, &cap, f);
    const char *first = line;
    if (n >= 3 && !memcmp(first, "\xef\xbb\xbf", 3))
        first += 3;
    int closed = 0;
    Buf header = {0};
    if (n > 0 && delimiter(first)) {
        while ((n = getline(&line, &cap, f)) > 0) {
            if (delimiter(line)) {
                closed = 1;
                break;
            }
            buf_add(&header, line, (size_t)n);
        }
    }
    free(line);
    fclose(f);
    if (!closed) {
        message("Cannot load skill %s: expected YAML frontmatter", path);
        buf_clear(&header);
        return NULL;
    }
    yaml_parser_t parser;
    if (!yaml_parser_initialize(&parser)) {
        message("Cannot initialize YAML parser");
        buf_clear(&header);
        return NULL;
    }
    yaml_parser_set_input_string(&parser, (unsigned char *)(header.data ? header.data : ""),
                                 header.len);
    yaml_document_t doc;
    json_t *info = json_object();
    if (yaml_parser_load(&parser, &doc)) {
        yaml_node_t *root = yaml_document_get_root_node(&doc);
        if (root && root->type == YAML_MAPPING_NODE) {
            for (yaml_node_pair_t *p = root->data.mapping.pairs.start;
                 p < root->data.mapping.pairs.top; p++) {
                yaml_node_t *key = yaml_document_get_node(&doc, p->key);
                yaml_node_t *value = yaml_document_get_node(&doc, p->value);
                if (key->type != YAML_SCALAR_NODE || value->type != YAML_SCALAR_NODE)
                    continue;
                const char *name = (char *)key->data.scalar.value;
                if (!strcmp(name, "name") || !strcmp(name, "description"))
                    json_object_set_new(
                        info, name,
                        json_stringn((char *)value->data.scalar.value, value->data.scalar.length));
            }
        }
        yaml_document_delete(&doc);
    } else
        message("Cannot load skill %s: %s at line %zu", path, parser.problem,
                parser.problem_mark.line + 2);
    yaml_parser_delete(&parser);
    buf_clear(&header);
    const char *name = jstr(info, "name"), *description = jstr(info, "description");
    if (!name || !*name || !description || !*description) {
        message("Cannot load skill %s: name and description are required", path);
        json_decref(info);
        return NULL;
    }
    return info;
}
static void discover(json_t *catalog, const char *base, const char *origin)
{
    struct dirent **entries;
    int n = scandir(base, &entries, NULL, alphasort);
    if (n < 0) {
        if (errno != ENOENT)
            message("Cannot scan skills %s: %s", base, strerror(errno));
        return;
    }
    for (int i = 0; i < n; i++) {
        if (entries[i]->d_name[0] != '.') {
            char *candidate = fmt("%s/%s/SKILL.md", base, entries[i]->d_name);
            char *path = realpath(candidate, NULL);
            if (path) {
                json_t *info = metadata(path);
                if (info) {
                    json_object_set_new(info, "path", json_string(path));
                    json_object_set_new(info, "source", json_string(origin));
                    json_object_set(catalog, jstr(info, "name"), info);
                    json_decref(info);
                }
            }
            free(path);
            free(candidate);
        }
        free(entries[i]);
    }
    free(entries);
}
void skills_refresh(Buf *instructions)
{
    json_t *catalog = json_object();
    const char *roots[] = {getenv("HOME"), app.root};
    const char *directories[] = {".agents", ".harn"};
    for (size_t i = 0; i < sizeof(roots) / sizeof(*roots); i++) {
        if (!roots[i] || !*roots[i])
            continue;
        for (size_t j = 0; j < sizeof(directories) / sizeof(*directories); j++) {
            char *base = fmt("%s/%s/skills", roots[i], directories[j]);
            discover(catalog, base, i ? "project" : "user");
            free(base);
        }
    }
    json_decref(app.skills);
    app.skills = catalog;
    if (!json_object_size(catalog))
        return;
    char *text = json_dumps(catalog, JSON_COMPACT | JSON_SORT_KEYS);
    buf_puts(instructions,
             "\n\nAvailable skills (name, description, path):\n"
             "For a matching task or an explicit user request, apply an already loaded skill "
             "or read the entire SKILL.md with read using only path before applying it. "
             "Resolve referenced paths relative to the skill's directory. "
             "Run scripts through exec with absolute paths. Apply skills only "
             "to tasks they cover. User instructions take precedence. Skill instructions remain "
             "available after context compaction. Newer whole-file reads replace older skill "
             "instructions.\n");
    buf_puts(instructions, text);
    free(text);
}
json_t *skills_read(json_t *args)
{
    if (jint(args, "start_line", 1) != 1 || jint(args, "end_line", 0) || jint(args, "offset", 0))
        return NULL;
    char *path = realpath(jstr(args, "path"), NULL);
    if (!path)
        return NULL;
    const char *key;
    json_t *info, *found = NULL;
    json_object_foreach(app.skills, key, info)
    {
        if (!strcmp(jstr(info, "path"), path)) {
            found = info;
            break;
        }
    }
    json_t *result = NULL;
    if (found) {
        size_t len;
        char *text = read_file(path, &len);
        if (!text)
            result = json_pack("{s:s}", "error", strerror(errno));
        else {
            json_t *content = memchr(text, 0, len) ? NULL : json_stringn(text, len);
            if (content) {
                result = json_pack("{s:s,s:o,s:s,s:i,s:i,s:b,s:I}", "path", path, "content",
                                   content, "skill", jstr(found, "name"), "start_line", 1,
                                   "next_line", 0, "truncated", 0, "next_offset", (json_int_t)len);
            } else
                result = json_pack("{s:s}", "error", "Skill instructions must be UTF-8 text");
            free(text);
        }
    }
    free(path);
    return result;
}
static json_t *skill_result(json_t *item)
{
    const char *type = jstr(item, "type"), *role = jstr(item, "role");
    json_error_t error;
    if (type && !strcmp(type, "function_call_output")) {
        const char *output = jstr(item, "output");
        return output ? json_loads(output, 0, &error) : NULL;
    }
    /* Resume still preserves snapshots saved by the former /skill NAME UI. */
    json_t *parts = json_object_get(item, "content");
    if (!role || strcmp(role, "developer") || !json_is_array(parts))
        return NULL;
    const char *metadata = jstr(json_array_get(parts, 0), "text");
    json_t *result = metadata ? json_loads(metadata, 0, &error) : NULL;
    json_t *content = json_object_get(json_array_get(parts, 1), "text");
    if (!json_is_true(json_object_get(result, "user_selected")) || !json_is_string(content)) {
        json_decref(result);
        return NULL;
    }
    json_object_set(result, "content", content);
    return result;
}
static json_t *snapshot(json_t *event)
{
    long index = jint(event, "item", -1);
    json_t *item = index < 0 ? NULL : json_array_get(app.items, (size_t)index);
    json_t *result = skill_result(item);
    const char *name = jstr(result, "skill"), *path = jstr(result, "path");
    if (!name || !path || !jstr(result, "content") || !jstr(event, "name") ||
        !jstr(event, "path") || strcmp(name, jstr(event, "name")) ||
        strcmp(path, jstr(event, "path"))) {
        json_decref(result);
        return NULL;
    }
    return result;
}
void skills_restore(json_t *event)
{
    json_t *result = snapshot(event);
    if (result) {
        json_object_set(app.active_skills, jstr(event, "path"), event);
        json_decref(result);
    }
}
void skills_track(json_t *item, int live)
{
    json_t *result = skill_result(item);
    const char *name = jstr(result, "skill"), *path = jstr(result, "path");
    if (name && path) {
        json_t *event = json_pack("{s:s,s:s,s:I}", "name", name, "path", path, "item",
                                  (json_int_t)json_array_size(app.items) - 1);
        skills_restore(event);
        if (live) {
            journal("skill", event);
            ui_meta("Using skill: %s", name);
        }
        json_decref(event);
    }
    json_decref(result);
}
void skills_command(void)
{
    if (!app.busy) {
        Buf catalog = {0};
        skills_refresh(&catalog);
        buf_clear(&catalog);
    }
    if (!json_object_size(app.skills))
        ui_print("No skills found in ~/.agents/skills, ~/.harn/skills, "
                 ".agents/skills or .harn/skills");
    const char *name;
    json_t *info;
    json_object_foreach(app.skills, name, info) {
        char *description = xstrdup(jstr(info, "description"));
        for (char *p = description; *p; p++)
            if (*p == '\n' || *p == '\r' || *p == '\t')
                *p = ' ';
        ui_print("%s (%s)\n  %s", name, jstr(info, "source"), description);
        free(description);
    }
}
void skills_preserve(size_t cut)
{
    const char *path;
    json_t *event;
    json_object_foreach(app.active_skills, path, event)
    {
        if (jint(event, "pin", jint(event, "item", -1)) >= (long)cut)
            continue;
        json_t *result = snapshot(event);
        if (!result)
            continue;
        char *text = fmt("Active skill %s from %s:\n"
                         "Apply only to matching tasks. User instructions take precedence. "
                         "Later whole-file reads of this skill supersede this snapshot.\n%s",
                         jstr(event, "name"), path, jstr(result, "content"));
        size_t index = json_array_size(app.items);
        item_add(json_pack("{s:s,s:s}", "role", "developer", "content", text));
        free(text);
        json_decref(result);
        json_t *pinned = json_deep_copy(event);
        json_object_set_new(pinned, "pin", json_integer((json_int_t)index));
        journal("skill", pinned);
        json_object_set(app.active_skills, path, pinned);
        json_decref(pinned);
    }
}
