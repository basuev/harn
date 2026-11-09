#include "harn.h"
#include <ctype.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>

void provider_init(void)
{
    if (curl_global_init(CURL_GLOBAL_DEFAULT)) {
        fputs("Cannot initialize libcurl\n", stderr);
        exit(1);
    }
    app.multi = curl_multi_init();
    if (!(curl_version_info(CURLVERSION_NOW)->features & CURL_VERSION_ASYNCHDNS)) {
        fputs("libcurl needs asynchronous DNS support\n", stderr);
        exit(1);
    }
}
void provider_close(void)
{
    if (app.multi)
        curl_multi_cleanup(app.multi);
    curl_global_cleanup();
}
void responses_event(Net *n)
{
    if (!n->data.len)
        return;
    if (!strcmp(n->data.data, "[DONE]")) {
        buf_clear(&n->data);
        return;
    }
    json_error_t error;
    json_t *event = json_loadb(n->data.data, n->data.len, JSON_REJECT_DUPLICATES, &error);
    buf_clear(&n->data);
    if (!event) {
        n->malformed = 1;
        return;
    }
    const char *type = jstr(event, "type");
    if (type && !strcmp(type, "response.output_item.added")) {
        json_t *item = json_object_get(event, "item");
        const char *kind = jstr(item, "type"), *phase = jstr(item, "phase");
        if (kind && !strcmp(kind, "message")) {
            if (n->message_started)
                ui_response_end();
            n->message_started = 0;
            n->message_phase = phase && !strcmp(phase, "commentary") ? -1
                                   : phase && !strcmp(phase, "final_answer") ? 1 : 0;
        }
        if (kind && !strcmp(kind, "function_call") && n->first_tool_ms < 0)
            n->first_tool_ms = now_ms() - n->started_at;
    }
    if (type && !strcmp(type, "response.output_item.done")) {
        json_t *item = json_object_get(event, "item");
        const char *kind = jstr(item, "type");
        if (kind && !strcmp(kind, "function_call") && n->first_tool_ms < 0)
            n->first_tool_ms = now_ms() - n->started_at;
        long index = jint(event, "output_index", (long)json_array_size(n->output));
        if (!json_is_object(item) || index < 0) {
            n->malformed = 1;
        } else {
            while (json_array_size(n->output) <= (size_t)index)
                json_array_append_new(n->output, json_null());
            json_array_set(n->output, (size_t)index, item);
        }
    }
    if (type && !strcmp(type, "response.output_text.delta")) {
        const char *delta = jstr(event, "delta");
        if (delta) {
            if (*delta && n->first_text_ms < 0)
                n->first_text_ms = now_ms() - n->started_at;
            buf_puts(&n->text, delta);
            if (!app.compacting && *delta) {
                if (!n->message_started) {
                    ui_response_begin(n->message_phase);
                    n->message_started = 1;
                }
                ui_write(delta, strlen(delta));
            }
        }
    }
    if (type && !strcmp(type, "response.function_call_arguments.delta") && n->first_tool_ms < 0)
        n->first_tool_ms = now_ms() - n->started_at;
    if (type && (!strcmp(type, "response.completed") || !strcmp(type, "response.failed") ||
                 !strcmp(type, "response.incomplete"))) {
        json_decref(n->response);
        n->response = json_incref(json_object_get(event, "response"));
    }
    if (type && !strcmp(type, "error")) {
        json_decref(n->response);
        n->response =
            json_pack("{s:s,s:O}", "status", "failed", "error",
                      json_object_get(event, "error") ? json_object_get(event, "error") : event);
    }
    json_decref(event);
}
static void stream_line(Net *n)
{
    if (n->line.len && n->line.data[n->line.len - 1] == '\r')
        n->line.data[--n->line.len] = 0;
    if (!n->line.len)
        app.connection->provider->event(n);
    else if (n->line.len >= 5 && !memcmp(n->line.data, "data:", 5)) {
        size_t start = 5;
        if (start < n->line.len && n->line.data[start] == ' ')
            start++;
        if (n->data.len)
            buf_puts(&n->data, "\n");
        buf_add(&n->data, n->line.data + start, n->line.len - start);
    }
    buf_clear(&n->line);
}
static size_t received(char *data, size_t size, size_t count, void *opaque)
{
    Net *n = opaque;
    size_t len = size * count;
    if (!n->stream)
        buf_add(&n->body, data, len);
    else {
        if (n->body.len < OUTPUT_LIMIT) {
            size_t take = len;
            if (take > OUTPUT_LIMIT - n->body.len)
                take = OUTPUT_LIMIT - n->body.len;
            buf_add(&n->body, data, take);
        }
        for (size_t i = 0; i < len; i++) {
            if (data[i] == '\n')
                stream_line(n);
            else
                buf_add(&n->line, data + i, 1);
        }
    }
    return len;
}
static size_t received_header(char *data, size_t size, size_t count, void *opaque)
{
    Net *n = opaque;
    size_t len = size * count;
    const char *name = "x-request-id:";
    size_t start = strlen(name), end = len;
    if (len >= start && !strncasecmp(data, name, start)) {
        while (start < end && isspace((unsigned char)data[start]))
            start++;
        while (end > start && isspace((unsigned char)data[end - 1]))
            end--;
        free(n->request_id);
        n->request_id = utf8_text(data + start, end - start);
    }
    return len;
}
static void net_run(Net *n, const char *url, const char *body, const char *content_type,
                    struct curl_slist *headers)
{
    n->started_at = now_ms();
    n->first_text_ms = n->first_tool_ms = -1;
    n->headers = headers;
    if (n->stream)
        n->output = json_array();
    n->easy = curl_easy_init();
    if (!n->easy) {
        n->result = CURLE_FAILED_INIT;
        curl_slist_free_all(n->headers);
        n->headers = NULL;
        return;
    }
    curl_easy_setopt(n->easy, CURLOPT_URL, url);
    curl_easy_setopt(n->easy, CURLOPT_WRITEFUNCTION, received);
    curl_easy_setopt(n->easy, CURLOPT_WRITEDATA, n);
    curl_easy_setopt(n->easy, CURLOPT_HEADERFUNCTION, received_header);
    curl_easy_setopt(n->easy, CURLOPT_HEADERDATA, n);
    curl_easy_setopt(n->easy, CURLOPT_ERRORBUFFER, n->error);
    curl_easy_setopt(n->easy, CURLOPT_CONNECTTIMEOUT_MS, 20000L);
    /* Streaming inference may legitimately take longer than five minutes.
     * Keep finite deadlines for metadata/auth, while inference stays cancellable. */
    curl_easy_setopt(n->easy, CURLOPT_TIMEOUT_MS, n->stream ? 0L : 300000L);
    curl_easy_setopt(n->easy, CURLOPT_NOSIGNAL, 1L);
    const char *ca = getenv("HARN_CA_FILE");
    if (ca)
        curl_easy_setopt(n->easy, CURLOPT_CAINFO, ca);
    if (body) {
        n->payload = xstrdup(body);
        curl_easy_setopt(n->easy, CURLOPT_POSTFIELDS, n->payload);
    }
    if (content_type) {
        char *h = fmt("Content-Type: %s", content_type);
        n->headers = curl_slist_append(n->headers, h);
        free(h);
    }
    curl_easy_setopt(n->easy, CURLOPT_HTTPHEADER, n->headers);
    app.net = n;
    if (curl_multi_add_handle(app.multi, n->easy) != CURLM_OK) {
        n->result = CURLE_FAILED_INIT;
        n->done = 1;
    }
    while (!n->done && !app.cancel && !app.quit)
        pump(1000);
    if (app.cancel || app.quit)
        n->result = CURLE_ABORTED_BY_CALLBACK;
    n->duration_ms = now_ms() - n->started_at;
    curl_easy_getinfo(n->easy, CURLINFO_RESPONSE_CODE, &n->status);
    curl_multi_remove_handle(app.multi, n->easy);
    curl_easy_cleanup(n->easy);
    n->easy = NULL;
    app.net = NULL;
    if (n->response && json_array_size(n->output)) {
        size_t i;
        json_t *item;
        json_array_foreach(n->output, i, item)
        {
            if (!json_is_object(item))
                n->malformed = 1;
        }
        json_object_set(n->response, "output", n->output);
    }
    curl_slist_free_all(n->headers);
    free(n->payload);
}
static void net_free(Net *n)
{
    buf_clear(&n->body);
    buf_clear(&n->line);
    buf_clear(&n->data);
    buf_clear(&n->text);
    json_decref(n->response);
    json_decref(n->output);
    free(n->request_id);
}
json_t *http_json_headers(const char *url, const char *body, const char *content_type,
                          struct curl_slist *headers, long *status)
{
    char previous[sizeof(app.activity)];
    memcpy(previous, app.activity, sizeof(previous));
    ui_activity("network");
    Net n = {0};
    net_run(&n, url, body, content_type, headers);
    if (status)
        *status = n.status;
    json_error_t error;
    json_t *o =
        n.body.data ? json_loadb(n.body.data, n.body.len, JSON_REJECT_DUPLICATES, &error) : NULL;
    if (n.result && !app.cancel && !app.quit)
        message("Connection failed: %s", n.error[0] ? n.error : curl_easy_strerror(n.result));
    if (n.result) {
        json_decref(o);
        o = NULL;
    }
    net_free(&n);
    ui_activity("%s", previous);
    return o;
}
static struct curl_slist *bearer_header(const char *token)
{
    if (!token)
        return NULL;
    char *header = fmt("Authorization: Bearer %s", token);
    struct curl_slist *headers = curl_slist_append(NULL, header);
    free(header);
    return headers;
}
struct curl_slist *bearer_headers(void)
{
    return bearer_header(jstr(app.credentials, "access_token"));
}
json_t *http_json(const char *url, const char *body, const char *content_type,
                  const char *bearer, long *status)
{
    return http_json_headers(url, body, content_type, bearer_header(bearer), status);
}
json_t *responses_models(long *status)
{
    char *url = fmt("%s/models", app.api_base);
    json_t *catalog = http_json_headers(url, NULL, NULL, app.connection->provider->headers(), status);
    free(url);
    return catalog;
}
static const char *api_error(json_t *o)
{
    json_t *e = json_object_get(o, "error");
    const char *s = jstr(e, "message");
    if (!s)
        s = jstr(e, "code");
    if (!s)
        s = jstr(o, "detail");
    return s ? s : "Request did not complete";
}
static char *redacted_error(const char *text)
{
    char *result = xstrdup(text);
    const char *fields[] = {"access_token", "refresh_token", "id_token", "api_key"};
    for (size_t i = 0; i < sizeof(fields) / sizeof(fields[0]); i++) {
        const char *secret = jstr(app.credentials, fields[i]);
        if (!secret || !*secret)
            continue;
        Buf safe = {0};
        const char *p = result, *match;
        size_t len = strlen(secret);
        while ((match = strstr(p, secret))) {
            buf_add(&safe, p, (size_t)(match - p));
            buf_puts(&safe, "[redacted]");
            p = match + len;
        }
        buf_puts(&safe, p);
        free(result);
        result = safe.data ? safe.data : xstrdup("");
    }
    return result;
}
json_t *responses_encode(json_t *payload)
{
    return json_deep_copy(payload);
}
json_t *model_request(json_t *payload)
{
    if (!effort_supported(app.effort)) {
        message("Unsupported effort for %s/%s: %s. Use /effort to select a supported level.",
                app.connection->name, app.model, app.effort);
        return NULL;
    }
    app.context_exceeded = 0;
    ui_activity("%s", app.compacting ? "compact" : "model");
    if (!auth_ready(0))
        return NULL;
    if (!strcmp(app.effort, "default"))
        json_object_del(payload, "reasoning");
    else
        json_object_set_new(payload, "reasoning", json_pack("{s:s}", "effort", app.effort));
    json_t *wire = app.connection->provider->encode(payload);
    if (!wire)
        return NULL;
    request_record(wire);
    if (app.quit) {
        json_decref(wire);
        return NULL;
    }
    if (app.pending_delivery && !app.compacting) {
        message("Queued messages included in request #%zu: %d", json_array_size(app.requests),
                app.pending_delivery);
        json_t *delivery =
            json_pack("{s:I,s:i}", "request", (json_int_t)json_array_size(app.requests), "messages",
                      app.pending_delivery);
        journal("delivery", delivery);
        json_decref(delivery);
        app.pending_delivery = 0;
    }
    char *body = json_dumps(wire, JSON_COMPACT);
    json_decref(wire);
    char *url = fmt("%s%s", app.api_base, app.connection->provider->request_path);
    json_t *result = NULL;
    for (int attempt = 1; attempt <= MAX_ATTEMPTS && !app.cancel && !app.quit; attempt++) {
        ui_activity("%s", app.compacting ? "compact" : "model");
        Net n = {.stream = 1};
        net_run(&n, url, body, "application/json", app.connection->provider->headers());
        cache_attempt(&n, attempt);
        if (n.text.len && !app.compacting) {
            if (!app.tty)
                ui_write("\n", 1);
            ui_response_end();
        }
        const char *status = jstr(n.response, "status");
        if (!n.result && !n.malformed && n.status == 200 && status &&
            !strcmp(status, "completed")) {
            result = json_incref(n.response);
            net_free(&n);
            break;
        }
        if (app.cancel || app.quit) {
            net_free(&n);
            break;
        }
        json_t *failure = n.response;
        if (!failure && n.body.data) {
            json_error_t e;
            failure = json_loadb(n.body.data, n.body.len, 0, &e);
        } else
            json_incref(failure);
        const char *code = jstr(json_object_get(failure, "error"), "code");
        if (code &&
            (!strcmp(code, "context_length_exceeded") || !strcmp(code, "context_window_exceeded")))
            app.context_exceeded = 1;
        int refresh = app.connection->provider->refresh && n.status == 401 && attempt < MAX_ATTEMPTS;
        int temporary = n.result == CURLE_COULDNT_CONNECT ||
                        n.result == CURLE_COULDNT_RESOLVE_HOST ||
                        n.result == CURLE_OPERATION_TIMEDOUT || n.result == CURLE_RECV_ERROR ||
                        n.result == CURLE_PARTIAL_FILE || n.status >= 500 || n.status == 429 ||
                        (!n.response && !n.malformed && n.status == 200);
        int classification = app.connection->provider->classify
                                 ? app.connection->provider->classify(n.status, code) : 0;
        if (classification)
            temporary = classification > 0;
        if (classification < 0)
            refresh = 0;
        char *diagnostic = redacted_error(n.result ? (n.error[0] ? n.error : curl_easy_strerror(n.result))
                                                   : api_error(failure));
        message("%s%s%s", n.result ? "Connection failed: " : "Model: ", diagnostic,
                n.malformed ? " (invalid stream)" : "");
        json_t *event = json_pack("{s:i,s:i,s:s}", "attempt", attempt, "http_status", (int)n.status,
                                  "code", code ? code : "transport_or_stream");
        json_object_set_new(event, "request",
                            json_integer((json_int_t)json_array_size(app.requests)));
        json_object_set_new(event, "curl_code", json_integer(n.result));
        json_object_set_new(event, "message", json_string(diagnostic));
        free(diagnostic);
        json_object_set_new(event, "invalid_stream", json_boolean(n.malformed));
        json_object_set_new(event, "response_status", status ? json_string(status) : json_null());
        journal("request_error", event);
        json_decref(event);
        json_decref(failure);
        net_free(&n);
        if (refresh) {
            if (auth_refresh(1))
                continue;
            break;
        }
        if (!temporary || attempt == MAX_ATTEMPTS)
            break;
        message("Retrying request (%d/%d)", attempt + 1, MAX_ATTEMPTS);
        ui_activity("retry %d/%d", attempt + 1, MAX_ATTEMPTS);
        long long until = now_ms() + attempt * 1000;
        while (now_ms() < until && !app.cancel && !app.quit)
            pump(100);
    }
    free(body);
    free(url);
    return result;
}
