#include "harn.h"
#include "fixture.h"

static json_t *fixture_catalog(json_t *catalog)
{
    return json_object_get(catalog, "models") ? catalog_models(catalog) : catalog_data(catalog);
}
static int fixture_legacy(json_t *data, const Connection *connection)
{
    return !jstr(data, "api_base") || api_key_legacy(data, connection);
}
const Provider fixture_provider = {
    .id = "fixture", .revision = "1", .auth_label = "fixture key",
    .request_path = "/responses", .ready = api_key_ready, .logout = api_key_logout,
    .models = responses_models, .catalog = fixture_catalog, .headers = bearer_headers,
    .encode = responses_cached_encode, .event = responses_event,
    .legacy = fixture_legacy,
};
