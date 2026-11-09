#include "harn.h"
#include <string.h>

static int same(const char *a, const char *b)
{
    return a && b && !strcmp(a, b);
}
static void increment(json_t *o, const char *key, json_int_t amount)
{
    json_object_set_new(o, key, json_integer(json_integer_value(json_object_get(o, key)) + amount));
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
    json_t *usage = json_object_get(data, "usage");
    json_t *details = json_object_get(usage, "input_tokens_details");
    long input = jint(usage, "input_tokens", -1), cached = jint(details, "cached_tokens", -1);
    if (input >= 0 && cached >= 0) {
        increment(total, "cache_measured", 1);
        increment(total, "cache_input_tokens", input);
        increment(total, "cached_tokens", cached);
    }

}
void cache_restore(const char *type, json_t *data)
{
    if (!strcmp(type, "request_attempt")) {
        account(data);
        json_decref(app.cache_last);
        app.cache_last = json_incref(data);
    } else if (json_is_object(data) &&
               !json_is_true(json_object_get(data, "cache_accounted"))) {
        json_t *legacy = json_copy(data);
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
        json_t *usage = json_copy(json_object_get(data, "usage"));
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
