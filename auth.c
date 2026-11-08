#include "harn.h"
#include <stdlib.h>
#include <string.h>

int api_key_ready(int force)
{
    (void)force;
    const char *key = app.connection->key_env && *app.connection->key_env
                          ? getenv(app.connection->key_env) : NULL;
    if (!key || !*key) {
        message("Set %s to the API key for %s", app.connection->key_env, app.api_base);
        return 0;
    }
    json_decref(app.credentials);
    app.credentials = json_pack("{s:s}", "access_token", key);
    return 1;
}
int api_key_logout(void)
{
    message("API key authorization uses %s. Unset it in the launching shell to sign out.",
            app.connection->key_env);
    return 1;
}
int api_key_legacy(json_t *data, const Connection *c)
{
    const char *base = jstr(data, "api_base"), *key = jstr(data, "api_key_env");
    const char *override = getenv("HARN_API_BASE");
    return base && key && !strcmp(key, c->key_env) &&
           (!strcmp(base, c->api_base) || (override && !strcmp(base, override)));
}
int auth_ready(int force)
{
    return app.connection->provider->ready(force);
}
int auth_refresh(int force)
{
    return app.connection->provider->refresh && app.connection->provider->refresh(force);
}
int auth_logout(void)
{
    if (!app.connection->provider->logout) {
        message("This provider does not support sign-out");
        return 0;
    }
    return app.connection->provider->logout();
}
int auth_pollfds(struct curl_waitfd *fds)
{
    return app.connection && app.connection->provider->pollfds
               ? app.connection->provider->pollfds(fds) : 0;
}
void auth_tick(void)
{
    if (app.connection && app.connection->provider->tick)
        app.connection->provider->tick();
}
void auth_release(void)
{
    if (app.connection && app.connection->provider->close)
        app.connection->provider->close();
    app.provider_state = NULL;
    json_decref(app.credentials);
    app.credentials = NULL;
}
json_t *catalog_models(json_t *catalog)
{
    json_t *entries = json_object_get(catalog, "models");
    if (!json_is_array(entries))
        return NULL;
    size_t i;
    json_t *entry;
    json_array_foreach(entries, i, entry) {
        const char *slug = jstr(entry, "slug");
        if (!slug || !*slug)
            return NULL;
    }
    return json_incref(entries);
}
json_t *catalog_data(json_t *catalog)
{
    json_t *entries = json_object_get(catalog, "data");
    if (!json_is_array(entries))
        return NULL;
    json_t *models = json_array(), *entry;
    size_t i;
    json_array_foreach(entries, i, entry) {
        const char *id = jstr(entry, "id");
        json_t *window = json_object_get(entry, "context_length");
        if (!id || !*id || (window && !json_is_null(window) &&
                            (!json_is_integer(window) || json_integer_value(window) <= 0))) {
            json_decref(models);
            return NULL;
        }
        json_t *model = json_pack("{s:s,s:s}", "slug", id, "visibility", "list");
        if (json_is_integer(window))
            json_object_set(model, "context_window", window);
        json_object_set(model, "supported_reasoning_levels",
                        json_object_get(entry, "supported_reasoning_levels"));
        json_array_append_new(models, model);
    }
    return models;
}
void models_show(void)
{
    ui_print("Model: %s", app.model);
    if (!app.models) {
        message("Model list is not loaded yet");
        return;
    }
    size_t i;
    json_t *model;
    json_array_foreach(app.models, i, model) {
        const char *slug = jstr(model, "slug"), *visibility = jstr(model, "visibility");
        if (slug && visibility && !strcmp(visibility, "list"))
            ui_print("  %s", slug);
    }
}
static json_t *current_model(void)
{
    size_t i;
    json_t *model;
    json_array_foreach(app.models, i, model) {
        const char *slug = jstr(model, "slug");
        if (slug && !strcmp(slug, app.model))
            return model;
    }
    return NULL;
}
int models_list(int show)
{
    if (!auth_ready(0))
        return 0;
    long status;
    json_t *catalog = app.connection->provider->models(&status);
    json_t *models = status == 200 ? app.connection->provider->catalog(catalog) : NULL;
    int fresh = models != NULL;
    json_t *model;
    if (fresh) {
        json_decref(app.models);
        app.models = models;
        model = current_model();
        context_budget(NULL, NULL);
    } else {
        model = current_model();
        const char *saved_model = jstr(app.model_info, "model");
        if (!model && saved_model && !strcmp(saved_model, app.model) &&
            json_is_object(json_object_get(app.model_info, "catalog")))
            model = json_object_get(app.model_info, "catalog");
        int temporary = status == 0 || status == 408 || status == 429 || status >= 500;
        int saved = temporary && !app.cancel && !app.quit && (model || (show && app.models));
        json_t *event = json_pack("{s:i,s:b}", "http_status", (int)status, "saved", saved);
        journal("model_catalog_error", event);
        json_decref(event);
        if (!saved) {
            json_decref(catalog);
            message("Could not load models");
            return 0;
        }
        message("Could not refresh models. Using saved model data.");
    }
    int found = model != NULL;
    if (model)
        context_budget(model, fresh ? "catalog" : "catalog (saved)");
    if (fresh || found)
        journal("model_info", app.model_info);
    json_decref(catalog);
    if (show)
        models_show();
    if (!found && fresh)
        message("Model %s is unavailable. Use /model to select an available model.", app.model);
    ui_status();
    return show ? 1 : found;
}
