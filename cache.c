#include "harn.h"
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

int model_supports(const char *field)
{
    return json_is_true(json_object_get(json_object_get(app.model_info, "catalog"), field));
}
static int same(const char *a, const char *b)
{
    return a && b && !strcmp(a, b);
}
static void increment(json_t *o, const char *key, json_int_t amount)
{
    json_object_set_new(o, key, json_integer(json_integer_value(json_object_get(o, key)) + amount));
}
static void reasoning_insert(json_t *input, json_t *item)
{
    size_t n = json_array_size(input);
    const char *role = n ? jstr(json_array_get(input, n - 1), "role") : NULL;
    if (same(role, "user"))
        json_array_insert(input, n - 1, item);
    else
        json_array_append(input, item);
}
void reasoning_prepare(json_t *payload)
{
    json_object_del(payload, "reasoning");
    if (!model_supports("supports_reasoning_effort_updates")) {
        if (strcmp(app.effort, "default"))
            json_object_set_new(payload, "reasoning", json_pack("{s:s}", "effort", app.effort));
        return;
    }
    json_t *old = json_deep_copy(app.reasoning_state), *input = json_object_get(payload, "input");
    int same_connection = same(jstr(app.reasoning_state, "provider"), app.connection->name) &&
                          same(jstr(app.reasoning_state, "model"), app.model) &&
                          jint(app.reasoning_state, "connection_start", -1) ==
                              (long)app.connection_start;
    int reset_default = !same(jstr(app.reasoning_state, "selected"), app.effort) &&
                        !strcmp(app.effort, "default");
    int reset = !same_connection || jint(app.reasoning_state, "cut", -1) != (long)app.cut ||
                reset_default;
    /* Returning to provider-controlled effort must omit explicit overrides. */
    if (reset) {
        const char *base = same_connection && !reset_default ? jstr(old, "base") : app.effort;
        json_decref(app.reasoning_state);
        app.reasoning_state = json_pack("{s:s,s:s,s:s,s:s,s:I,s:I,s:I}",
            "provider", app.connection->name, "model", app.model, "base", base,
            "selected", app.effort, "cut", (json_int_t)app.cut,
            "connection_start", (json_int_t)app.connection_start,
            "start", (json_int_t)json_array_size(app.items));
        for (size_t i = json_array_size(input); i; i--)
            if (same(jstr(json_array_get(input, i - 1), "type"), "configuration_update"))
                json_array_remove(input, i - 1);
    }
    const char *base = jstr(app.reasoning_state, "base");
    if (base && strcmp(base, "default"))
        json_object_set_new(payload, "reasoning", json_pack("{s:s}", "effort", base));
    const char *effective = base;
    size_t i;
    json_t *item;
    json_array_foreach(input, i, item) {
        if (same(jstr(item, "type"), "configuration_update")) {
            const char *effort = jstr(json_object_get(item, "reasoning"), "effort");
            if (effort)
                effective = effort;
        }
    }
    if (!same(effective, app.effort)) {
        item = json_pack("{s:s,s:{s:s}}", "type", "configuration_update",
                         "reasoning", "effort", app.effort);
        reasoning_insert(input, item);
        if (!app.compacting)
            item_add(json_incref(item));
        json_decref(item);
    }
    json_object_set_new(app.reasoning_state, "selected", json_string(app.effort));
    if (!json_equal(old, app.reasoning_state))
        journal("reasoning_context", app.reasoning_state);
    json_decref(old);
}

static void breakpoint(json_t *item, const char *ttl)
{
    json_t *content = json_object_get(item, "content");
    if (json_is_string(content)) {
        json_t *blocks = json_pack("[{s:s,s:O}]", "type", "input_text", "text", content);
        json_object_set_new(item, "content", blocks);
        content = blocks;
    }
    json_t *block = json_array_get(content, json_array_size(content) - 1);
    if (json_is_object(block))
        json_object_set_new(block, "prompt_cache_breakpoint",
                            json_pack("{s:s,s:s}", "mode", "explicit", "ttl", ttl));
}
json_t *responses_cached_encode(json_t *payload)
{
    json_t *wire = responses_encode(payload);
    json_t *policy = json_object_get(json_object_get(app.model_info, "catalog"), "prompt_cache");
    if (!json_is_object(policy))
        return wire;
    if (json_is_true(json_object_get(policy, "key"))) {
        char *key = fmt("harn:%s", app.session_id);
        json_object_set_new(wire, "prompt_cache_key", json_string(key));
        free(key);
    }
    const char *ttl = jstr(policy, "ttl"), *static_ttl = jstr(policy, "static_ttl");
    if (!ttl)
        return wire;
    json_t *options = json_pack("{s:s,s:s}", "mode", "implicit", "ttl", ttl);
    if (json_is_true(json_object_get(policy, "diagnostics")) &&
        same(jstr(app.cache_last, "provider"), app.connection->name) &&
        same(jstr(app.cache_last, "model"), app.model) &&
        same(jstr(app.cache_last, "outcome"), "completed") && jstr(app.cache_last, "response_id"))
        json_object_set(options, "comparison_response_id",
                        json_object_get(app.cache_last, "response_id"));
    json_object_set_new(wire, "prompt_cache_options", options);
    json_t *instructions = json_object_get(wire, "instructions");
    json_t *input = json_object_get(wire, "input");
    if (json_is_string(instructions)) {
        json_t *developer = json_pack("{s:s,s:O}", "role", "developer", "content", instructions);
        json_array_insert_new(input, 0, developer);
        json_object_del(wire, "instructions");
    }
    /* Keep a boundary after the initial policy even when later input changes. */
    size_t n = 0;
    while (n < json_array_size(input) && same(jstr(json_array_get(input, n), "role"), "developer"))
        n++;
    if (n)
        breakpoint(json_array_get(input, n - 1), static_ttl ? static_ttl : ttl);
    return wire;
}

/* Diagnostic fingerprints are local equality checks, never cache keys or secrets. */
static json_t *fingerprint(json_t *value)
{
    char *text = json_dumps(value ? value : json_null(), JSON_COMPACT | JSON_ENCODE_ANY);
    uint64_t hash = UINT64_C(14695981039346656037);
    for (const unsigned char *p = (const unsigned char *)text; p && *p; p++) {
        hash ^= *p;
        hash *= UINT64_C(1099511628211);
    }
    free(text);
    char hex[17];
    snprintf(hex, sizeof(hex), "%016llx", (unsigned long long)hash);
    return json_string(hex);
}
static void strip_markers(json_t *value)
{
    if (json_is_object(value)) {
        json_object_del(value, "prompt_cache_breakpoint");
        const char *key;
        json_t *child;
        json_object_foreach(value, key, child) strip_markers(child);
    } else if (json_is_array(value)) {
        size_t i;
        json_t *child;
        json_array_foreach(value, i, child) strip_markers(child);
    }
}
void cache_request(json_t *wire)
{
    json_t *fields = json_object(), *input = json_array(), *changes = json_array();
    json_t *normalized = json_deep_copy(wire);
    strip_markers(normalized);
    json_object_del(json_object_get(normalized, "prompt_cache_options"), "comparison_response_id");
    const char *key;
    json_t *value;
    json_object_foreach(normalized, key, value) {
        if (!strcmp(key, "input")) {
            size_t i;
            json_t *item;
            json_array_foreach(value, i, item) json_array_append_new(input, fingerprint(item));
        } else
            json_object_set_new(fields, key, fingerprint(value));
    }
    int related = same(jstr(app.cache_previous, "provider"), app.connection->name);
    json_t *previous_fields = json_object_get(app.cache_previous, "fields");
    if (related) {
        json_object_foreach(fields, key, value)
            if (!json_equal(value, json_object_get(previous_fields, key)))
                json_array_append_new(changes, json_string(key));
        json_object_foreach(previous_fields, key, value)
            if (!json_object_get(fields, key))
                json_array_append_new(changes, json_string(key));
    }
    json_t *previous_input = json_object_get(app.cache_previous, "input");
    size_t shared = 0;
    if (related)
        while (shared < json_array_size(input) && shared < json_array_size(previous_input) &&
               json_equal(json_array_get(input, shared), json_array_get(previous_input, shared)))
            shared++;
    json_t *data = json_pack("{s:s,s:s,s:s,s:I,s:o,s:o,s:o,s:I,s:b}",
        "provider", app.connection->name, "model", app.model,
        "kind", app.compacting ? "compaction" : "inference",
        "request", (json_int_t)json_array_size(app.requests), "fields", fields, "input", input,
        "changes", changes, "shared_items", (json_int_t)shared,
        "history_appended", related && shared == json_array_size(previous_input));
    json_object_set_new(data, "wire_fingerprint", fingerprint(wire));
    json_object_set_new(data, "previous_request", related
        ? json_incref(json_object_get(app.cache_previous, "request")) : json_null());
    journal("cache_request", data);
    cache_restore("cache_request", data);
    json_decref(data);
    json_decref(normalized);
}
static void account(json_t *data)
{
    if (!app.cache_totals)
        app.cache_totals = json_array();
    const char *provider = jstr(data, "provider"), *model = jstr(data, "model");
    const char *kind = jstr(data, "kind");
    json_t *total = NULL, *entry;
    size_t i;
    json_array_foreach(app.cache_totals, i, entry) {
        if (same(jstr(entry, "provider"), provider) && same(jstr(entry, "model"), model) &&
            same(jstr(entry, "kind"), kind)) {
            total = entry;
            break;
        }
    }
    if (!total) {
        total = json_pack("{s:s,s:s,s:s}", "provider", provider ? provider : "unknown",
                          "model", model ? model : "unknown", "kind", kind ? kind : "inference");
        json_array_append_new(app.cache_totals, total);
    }
    increment(total, "attempts", 1);
    if (!same(jstr(data, "outcome"), "completed"))
        increment(total, "failed_attempts", 1);
    json_t *usage = json_object_get(data, "usage");
    json_t *details = json_object_get(usage, "input_tokens_details");
    long input = jint(usage, "input_tokens", -1), cached = jint(details, "cached_tokens", -1);
    if (input >= 0) {
        increment(total, "input_measured", 1);
        increment(total, "input_tokens", input);
    }
    if (input >= 0 && cached >= 0) {
        increment(total, "cache_measured", 1);
        increment(total, "cache_input_tokens", input);
        increment(total, "cached_tokens", cached);
    }
    long writes = jint(details, "cache_write_tokens", -1);
    if (writes >= 0) {
        increment(total, "writes_measured", 1);
        increment(total, "cache_write_tokens", writes);
    }
    increment(total, "output_tokens", jint(usage, "output_tokens", 0));
}
void cache_restore(const char *type, json_t *data)
{
    if (!strcmp(type, "cache_request")) {
        json_decref(app.cache_previous);
        app.cache_previous = json_incref(data);
    } else if (!strcmp(type, "reasoning_context")) {
        json_decref(app.reasoning_state);
        app.reasoning_state = json_deep_copy(data);
    } else if (!strcmp(type, "request_attempt")) {
        account(data);
        json_decref(app.cache_last);
        app.cache_last = json_incref(data);
    } else if (json_is_object(data) &&
               !json_is_true(json_object_get(data, "cache_accounted"))) {
        json_t *legacy = json_deep_copy(data);
        json_object_set_new(legacy, "kind", json_string(!strcmp(type, "compaction_usage")
                                                           ? "compaction" : "inference"));
        json_object_set_new(legacy, "outcome", json_string("completed"));
        account(legacy);
        json_decref(app.cache_last);
        app.cache_last = legacy;
    }
}
void cache_attempt(Net *net, int attempt)
{
    const char *status = jstr(net->response, "status");
    const char *outcome = app.cancel || app.quit ? "cancelled" : net->result ? "transport_error"
                        : net->malformed ? "invalid_stream" : net->status != 200 ? "http_error"
                        : status ? status : "incomplete_stream";
    json_t *data = json_pack("{s:s,s:s,s:s,s:I,s:i,s:i,s:i,s:s}",
        "provider", app.connection->name, "model", app.model,
        "kind", app.compacting ? "compaction" : "inference",
        "request", (json_int_t)json_array_size(app.requests), "attempt", attempt,
        "http_status", (int)net->status, "curl_code", (int)net->result, "outcome", outcome);
    const char *from[] = {"id", "model", "provider", "service_tier", "reasoning",
                          "prompt_cache_diagnostics", "usage"};
    const char *to[] = {"response_id", "response_model", "upstream_provider", "service_tier",
                        "response_reasoning", "prompt_cache_diagnostics", "usage"};
    for (size_t i = 0; i < sizeof(from) / sizeof(*from); i++)
        if (json_object_get(net->response, from[i]))
            json_object_set(data, to[i], json_object_get(net->response, from[i]));
    if (same(outcome, "completed") && json_is_object(json_object_get(data, "usage"))) {
        /* The successful usage event retains full per-item attribution. Do not
         * duplicate its growing tree in every attempt record. */
        json_t *usage = json_deep_copy(json_object_get(data, "usage"));
        json_object_del(usage, "attribution");
        json_object_set_new(data, "usage", usage);
    }
    if (net->request_id)
        json_object_set_new(data, "request_id", json_string(net->request_id));
    json_t *timing = json_pack("{s:I}", "duration_ms", (json_int_t)net->duration_ms);
    json_object_set_new(timing, "first_text_ms", net->first_text_ms >= 0
        ? json_integer(net->first_text_ms) : json_null());
    json_object_set_new(timing, "first_tool_ms", net->first_tool_ms >= 0
        ? json_integer(net->first_tool_ms) : json_null());
    json_object_set_new(data, "timing", timing);
    const char *comparison[] = {"changes", "shared_items", "history_appended", "previous_request"};
    for (size_t i = 0; i < sizeof(comparison) / sizeof(*comparison); i++)
        json_object_set(data, comparison[i], json_object_get(app.cache_previous, comparison[i]));
    journal("request_attempt", data);
    cache_restore("request_attempt", data);
    json_decref(data);
}
void cache_show(void)
{
    if (same(jstr(app.cache_last, "provider"), app.connection->name) &&
        same(jstr(app.cache_last, "model"), app.model)) {
        json_t *usage = json_object_get(app.cache_last, "usage");
        json_t *details = json_object_get(usage, "input_tokens_details");
        long input = jint(usage, "input_tokens", -1), cached = jint(details, "cached_tokens", -1);
        if (input > 0 && cached >= 0)
            ui_print("Cache last request: %.1f%% (%ld/%ld input tokens)",
                     100.0 * cached / input, cached, input);
        else
            ui_print("Cache last request: unknown");
        long writes = jint(details, "cache_write_tokens", -1);
        if (writes >= 0)
            ui_print("Cache writes: %ld tokens", writes);
        else
            ui_print("Cache writes: unknown");
        json_t *diagnostics = json_object_get(app.cache_last, "prompt_cache_diagnostics");
        const char *type = jstr(diagnostics, "type"), *reason = jstr(diagnostics, "reason");
        if (type)
            ui_print("Cache diagnosis: %s%s%s%s", type, reason ? " (" : "", reason ? reason : "",
                     reason ? ")" : "");
        else if (input >= 0 && cached == 0)
            ui_print("Cache miss reason: unknown");
        json_t *changes = json_object_get(app.cache_last, "changes");
        if (json_array_size(changes)) {
            Buf text = {0};
            size_t i;
            json_t *change;
            json_array_foreach(changes, i, change) {
                if (text.len)
                    buf_puts(&text, ", ");
                buf_puts(&text, json_string_value(change));
            }
            ui_print("Request fields changed: %s", text.data);
            buf_clear(&text);
        }
        if (json_is_true(json_object_get(app.cache_last, "history_appended")))
            ui_print("Sent history: previous prefix preserved");
        else if (json_is_integer(json_object_get(app.cache_last, "previous_request")))
            ui_print("Sent history: prefix changed after %ld items",
                     jint(app.cache_last, "shared_items", 0));
        const char *outcome = jstr(app.cache_last, "outcome");
        if (outcome && strcmp(outcome, "completed"))
            ui_print("Last request: %s", outcome);
        if (same(jstr(app.cache_last, "kind"), "compaction"))
            ui_print("Last request purpose: compaction");
        json_t *timing = json_object_get(app.cache_last, "timing");
        if (json_is_object(timing)) {
            ui_print("Request duration: %ld ms", jint(timing, "duration_ms", 0));
            if (jint(timing, "first_text_ms", -1) >= 0)
                ui_print("First text: %ld ms", jint(timing, "first_text_ms", -1));
            if (jint(timing, "first_tool_ms", -1) >= 0)
                ui_print("First tool call: %ld ms", jint(timing, "first_tool_ms", -1));
        }
    } else
        ui_print("Cache last request: unmeasured");
    size_t i;
    json_t *total;
    json_array_foreach(app.cache_totals, i, total) {
        long measured = jint(total, "cache_measured", 0);
        long input = jint(total, "cache_input_tokens", 0);
        if (measured && input > 0)
            ui_print("Cache session %s/%s (%s): %.1f%%, %ld/%ld attempts measured",
                jstr(total, "provider"), jstr(total, "model"), jstr(total, "kind"),
                100.0 * jint(total, "cached_tokens", 0) / input,
                measured, jint(total, "attempts", 0));
        else
            ui_print("Cache session %s/%s (%s): unknown (%ld attempts)",
                jstr(total, "provider"), jstr(total, "model"), jstr(total, "kind"),
                jint(total, "attempts", 0));
    }
}
