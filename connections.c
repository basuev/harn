#define CONFIG_CONNECTIONS
#include "harn.h"
#include <stdlib.h>
#include <string.h>

const Connection *connection_at(size_t index)
{
    return index < sizeof(connections) / sizeof(connections[0]) ? &connections[index] : NULL;
}
const Connection *connection_find(const char *name)
{
    const Connection *c;
    for (size_t i = 0; (c = connection_at(i)); i++)
        if (name && !strcmp(c->name, name))
            return c;
    return NULL;
}
int connection_select(const char *name)
{
    const Connection *next = connection_find(name);
    if (!next) {
        message("Provider %s is not configured. Add it to config.h and rebuild.", name);
        return 0;
    }
    if (next == app.connection)
        return 1;
    if (!app.connection_models)
        app.connection_models = json_object();
    if (app.connection && app.model)
        json_object_set_new(app.connection_models, app.connection->name, json_string(app.model));
    app.connection = next;
    app.connection_start = json_array_size(app.items);
    const char *model = jstr(app.connection_models, next->name);
    free(app.model);
    app.model = xstrdup(model ? model : next->model);
    free(app.api_base);
    const char *base = getenv("HARN_API_BASE");
    app.api_base = xstrdup(base ? base : next->api_base);
    json_decref(app.credentials);
    app.credentials = NULL;
    json_decref(app.registration);
    app.registration = NULL;
    json_decref(app.discovery);
    app.discovery = NULL;
    json_decref(app.models);
    app.models = NULL;
    json_decref(app.last_usage);
    app.last_usage = NULL;
    app.usage_valid = 0;
    app.context_exceeded = 0;
    context_budget(NULL, NULL);
    return 1;
}
int connection_restore(json_t *data)
{
    const char *name = jstr(data, "provider");
    if (!name) {
        /* Journals before named connections saved either endpoint identity or only a model. */
        const char *base = jstr(data, "api_base"), *key = jstr(data, "api_key_env");
        const Connection *c;
        for (size_t i = 0; (c = connection_at(i)); i++) {
            const char *override = getenv("HARN_API_BASE");
            if ((!base && !*c->key_env) ||
                (base && key && !strcmp(key, c->key_env) &&
                 (!strcmp(base, c->api_base) || (override && !strcmp(base, override))))) {
                name = c->name;
                break;
            }
        }
    }
    if (!name) {
        message("Session endpoint is not configured. Add its connection to config.h and rebuild.");
        return 0;
    }
    if (!connection_select(name))
        return 0;
    const char *model = jstr(data, "model");
    if (model) {
        free(app.model);
        app.model = xstrdup(model);
    }
    return 1;
}
void connection_command(const char *name)
{
    while (*name == ' ')
        name++;
    if (!*name) {
        ui_print("Provider: %s", app.connection->name);
        const Connection *c;
        for (size_t i = 0; (c = connection_at(i)); i++) {
            if (*c->key_env) {
                const char *key = getenv(c->key_env);
                ui_print("  %s (%s %s)", c->name, c->key_env, key && *key ? "set" : "missing");
            } else
                ui_print("  %s (ChatGPT sign-in)", c->name);
        }
        return;
    }
    if (app.busy) {
        message("Interrupt the current turn before changing the provider");
        return;
    }
    const Connection *next = connection_find(name);
    if (next && *next->key_env) {
        const char *key = getenv(next->key_env);
        if (!key || !*key) {
            message("Set %s before selecting %s", next->key_env, name);
            return;
        }
    }
    if (next == app.connection)
        return;
    if (!connection_select(name))
        return;
    effort_activate();
    json_t *selection = json_pack("{s:s,s:s}", "provider", app.connection->name, "model", app.model);
    journal("provider", selection);
    json_decref(selection);
    message("Provider: %s", app.connection->name);
    message("Model: %s", app.model);
    ui_status();
}
