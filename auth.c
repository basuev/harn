#include "harn.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <jwt.h>
#include <netinet/in.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/rand.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static int listener = -1, client = -1, declined;
static Buf browser;
static char *authorization_url, *oauth_state, *oauth_nonce, *pkce, *callback, *code;
static char *config_path(const char *name)
{
    if (!strcmp(app.connection->name, "chatgpt"))
        return fmt("%s/%s.json", app.config_dir, name);
    return fmt("%s/%s-%s.json", app.config_dir, app.connection->name, name);
}
static char *encode(const unsigned char *bytes, size_t n)
{
    char *out = xmalloc(4 * ((n + 2) / 3) + 1);
    int len = EVP_EncodeBlock((unsigned char *)out, bytes, (int)n);
    while (len && out[len - 1] == '=')
        out[--len] = 0;
    for (int i = 0; i < len; i++) {
        if (out[i] == '+')
            out[i] = '-';
        if (out[i] == '/')
            out[i] = '_';
    }
    return out;
}
static char *random_string(void)
{
    unsigned char bytes[32];
    if (RAND_bytes(bytes, sizeof(bytes)) != 1)
        return NULL;
    return encode(bytes, sizeof(bytes));
}
static int granted(json_t *o)
{
    const char *scope = jstr(o, "scope"), *word = "chatgpt.tokens.use.direct";
    if (!scope)
        return 0;
    while (*scope) {
        const char *end = strchr(scope, ' ');
        if (!end)
            end = scope + strlen(scope);
        if ((size_t)(end - scope) == strlen(word) && !memcmp(scope, word, strlen(word)))
            return 1;
        scope = *end ? end + 1 : end;
    }
    return 0;
}
static char *form(json_t *o)
{
    Buf b = {0};
    const char *key;
    json_t *value;
    json_object_foreach(o, key, value)
    {
        const char *s = json_string_value(value);
        if (!s)
            continue;
        char *k = curl_easy_escape(NULL, key, 0), *v = curl_easy_escape(NULL, s, 0);
        if (b.len)
            buf_puts(&b, "&");
        buf_puts(&b, k);
        buf_puts(&b, "=");
        buf_puts(&b, v);
        curl_free(k);
        curl_free(v);
    }
    json_decref(o);
    return b.data ? b.data : xstrdup("");
}
static int discover(void)
{
    if (app.discovery)
        return 1;
    char *url = fmt("%s/.well-known/openid-configuration", app.issuer);
    long status;
    app.discovery = http_json(url, NULL, NULL, NULL, &status);
    free(url);
    if (status != 200 || !jstr(app.discovery, "issuer") ||
        strcmp(jstr(app.discovery, "issuer"), app.issuer) ||
        !jstr(app.discovery, "token_endpoint") || !jstr(app.discovery, "authorization_endpoint") ||
        !jstr(app.discovery, "jwks_uri")) {
        message("Could not load sign-in configuration");
        json_decref(app.discovery);
        app.discovery = NULL;
        return 0;
    }
    return 1;
}
static json_t *exchange(json_t *fields, long *status)
{
    char *body = form(fields);
    json_t *r = http_json(jstr(app.discovery, "token_endpoint"), body,
                          "application/x-www-form-urlencoded", NULL, status);
    free(body);
    return r;
}
static const char *oauth_error(json_t *o)
{
    json_t *e = json_object_get(o, "error");
    const char *s = json_string_value(e);
    if (!s)
        s = jstr(e, "code");
    return s ? s : "connection or authorization error";
}
struct Identity {
    jwk_set_t *keys;
    const char *nonce, *subject;
    char *sub, *email;
};
static int check_identity(jwt_t *jwt, jwt_config_t *config)
{
    struct Identity *id = config->ctx;
    jwt_value_t value;
    value = (jwt_value_t){.type = JWT_VALUE_STR, .name = "kid"};
    if (jwt_header_get(jwt, &value) || !value.str_val)
        return 1;
    config->key = jwks_find_bykid(id->keys, value.str_val);
    config->alg = JWT_ALG_RS256;
    if (!config->key)
        return 1;
    if (id->nonce) {
        value = (jwt_value_t){.type = JWT_VALUE_STR, .name = "nonce"};
        if (jwt_claim_get(jwt, &value) || !value.str_val || strcmp(value.str_val, id->nonce))
            return 1;
    }
    value = (jwt_value_t){.type = JWT_VALUE_STR, .name = "sub"};
    if (jwt_claim_get(jwt, &value) || !value.str_val ||
        (id->subject && strcmp(value.str_val, id->subject)))
        return 1;
    id->sub = xstrdup(value.str_val);
    value = (jwt_value_t){.type = JWT_VALUE_STR, .name = "email"};
    if (!jwt_claim_get(jwt, &value) && value.str_val)
        id->email = xstrdup(value.str_val);
    return 0;
}
static int verify(json_t *tokens, const char *nonce, const char *subject)
{
    long status;
    json_t *published = http_json(jstr(app.discovery, "jwks_uri"), NULL, NULL, NULL, &status);
    if (!published || status != 200) {
        json_decref(published);
        return 0;
    }
    char *text = json_dumps(published, JSON_COMPACT);
    json_decref(published);
    jwk_set_t *keys = jwks_create(text);
    free(text);
    jwt_checker_t *checker = jwt_checker_new();
    struct Identity id = {.keys = keys, .nonce = nonce, .subject = subject};
    const jwt_alg_t algorithm = JWT_ALG_RS256;
    const char *required[] = {"iss", "aud", "exp", "sub"};
    int bad = !keys || jwks_error_any(keys) || !checker || !jstr(tokens, "id_token");
    if (!bad)
        bad = jwt_checker_setalgs(checker, &algorithm, 1) ||
              jwt_checker_require(checker, required, 4) ||
              jwt_checker_claim_set(checker, JWT_CLAIM_ISS, jstr(app.discovery, "issuer")) ||
              jwt_checker_claim_set(checker, JWT_CLAIM_AUD, jstr(app.registration, "client_id")) ||
              jwt_checker_setcb(checker, check_identity, &id) ||
              jwt_checker_verify(checker, jstr(tokens, "id_token"));
    if (!bad) {
        json_object_set_new(tokens, "subject", json_string(id.sub));
        if (id.email)
            json_object_set_new(tokens, "email", json_string(id.email));
    }
    free(id.sub);
    free(id.email);
    jwt_checker_free(checker);
    jwks_free(keys);
    return !bad;
}
static char *query(const char *p, const char *key)
{
    size_t k = strlen(key);
    while (*p) {
        const char *end = strchr(p, '&');
        if (!end)
            end = p + strlen(p);
        if ((size_t)(end - p) > k && !memcmp(p, key, k) && p[k] == '=') {
            int len;
            char *raw = curl_easy_unescape(NULL, p + k + 1, (int)(end - p - k - 1), &len);
            if (!raw)
                return NULL;
            char *out = strlen(raw) == (size_t)len ? xstrdup(raw) : NULL;
            curl_free(raw);
            return out;
        }
        p = *end ? end + 1 : end;
    }
    return NULL;
}
static void reply(int status, const char *location, const char *body)
{
    char *text =
        fmt("HTTP/1.1 %d Response\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: "
            "no-store\r\nConnection: close\r\nContent-Length: %zu\r\n%s%s%s\r\n%s",
            status, strlen(body), location ? "Location: " : "", location ? location : "",
            location ? "\r\n" : "", body);
    (void)write(client, text, strlen(text));
    free(text);
}
int auth_pollfds(struct curl_waitfd *fds)
{
    int n = 0;
    if (listener >= 0)
        fds[n++] = (struct curl_waitfd){.fd = listener, .events = CURL_WAIT_POLLIN};
    if (client >= 0)
        fds[n++] = (struct curl_waitfd){.fd = client, .events = CURL_WAIT_POLLIN};
    return n;
}
void auth_tick(void)
{
    if (listener < 0)
        return;
    if (client < 0) {
        client = accept(listener, NULL, NULL);
        if (client >= 0) {
            fcntl(client, F_SETFL, O_NONBLOCK);
            fcntl(client, F_SETFD, FD_CLOEXEC);
        }
    }
    if (client < 0)
        return;
    char bytes[4096];
    ssize_t n = read(client, bytes, sizeof(bytes));
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
        return;
    if (n > 0)
        buf_add(&browser, bytes, (size_t)n);
    if (n <= 0)
        goto finish;
    if (browser.len > 32768) {
        reply(400, NULL, "Invalid request");
        goto finish;
    }
    if (!strstr(browser.data, "\r\n\r\n"))
        return;
    char *p = browser.data;
    if (strncmp(p, "GET ", 4)) {
        reply(405, NULL, "GET required");
        goto finish;
    }
    p += 4;
    char *end = strchr(p, ' ');
    if (!end)
        goto finish;
    *end = 0;
    if (!strcmp(p, "/start"))
        reply(302, authorization_url, "");
    else if (!strncmp(p, "/auth/callback?", 15)) {
        char *state = query(p + 15, "state"), *issued = query(p + 15, "client_id"),
             *failure = query(p + 15, "error");
        const char *old = jstr(app.registration, "client_id");
        int valid = state && strlen(state) == strlen(oauth_state) &&
                    !CRYPTO_memcmp(state, oauth_state, strlen(state));
        if (issued && old && strcmp(issued, old))
            valid = 0;
        if (!valid || code) {
            reply(400, NULL, "Invalid sign-in state");
        } else if (failure) {
            declined = 1;
            reply(303, "/done", "");
        } else if (!old && !issued) {
            declined = 1;
            reply(400, NULL, "Incomplete registration");
        } else {
            if (issued) {
                json_object_set_new(app.registration, "client_id", json_string(issued));
                char *path = config_path("registration");
                if (save_json(path, app.registration))
                    declined = 1;
                free(path);
            }
            code = query(p + 15, "code");
            reply(code ? 303 : 400, code ? "/done" : NULL, code ? "" : "Missing code");
        }
        free(state);
        free(issued);
        free(failure);
    } else
        reply(200, NULL, "Sign-in received. You can return to harn.");
finish:
    close(client);
    client = -1;
    buf_clear(&browser);
}
static void browser_close(void)
{
    if (listener >= 0)
        close(listener);
    if (client >= 0)
        close(client);
    listener = client = -1;
    buf_clear(&browser);
    free(authorization_url);
    authorization_url = NULL;
    free(oauth_state);
    oauth_state = NULL;
    free(oauth_nonce);
    oauth_nonce = NULL;
    free(pkce);
    pkce = NULL;
    free(callback);
    callback = NULL;
    free(code);
    code = NULL;
}
static int prepare_registration(void)
{
    if (mkdirs(app.config_dir, 0700))
        return 0;
    if (!app.registration) {
        char *path = config_path("registration");
        app.registration = load_json(path);
        free(path);
        if (!app.registration)
            app.registration = json_object();
    }
    if (!jstr(app.registration, "ext_agent_host_id")) {
        unsigned char bytes[16];
        if (RAND_bytes(bytes, sizeof(bytes)) != 1)
            return 0;
        bytes[6] = (bytes[6] & 15) | 64;
        bytes[8] = (bytes[8] & 63) | 128;
        char *host = fmt(
            "urn:uuid:%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
            bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
            bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]);
        json_object_set_new(app.registration, "ext_agent_host_id", json_string(host));
        free(host);
        char *path = config_path("registration");
        int r = save_json(path, app.registration);
        free(path);
        if (r)
            return 0;
    }
    return 1;
}
int auth_login(void)
{
    if (!prepare_registration() || !discover())
        return 0;
    browser_close();
    declined = 0;
    oauth_state = random_string();
    oauth_nonce = random_string();
    pkce = random_string();
    if (!oauth_state || !oauth_nonce || !pkce)
        return 0;
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int len;
    if (!EVP_Digest(pkce, strlen(pkce), digest, &len, EVP_sha256(), NULL))
        return 0;
    char *challenge = encode(digest, len);
    listener = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    if (listener < 0 || bind(listener, (struct sockaddr *)&address, sizeof(address)) ||
        listen(listener, 8)) {
        free(challenge);
        browser_close();
        message("Could not start sign-in listener");
        return 0;
    }
    fcntl(listener, F_SETFL, O_NONBLOCK);
    fcntl(listener, F_SETFD, FD_CLOEXEC);
    socklen_t size = sizeof(address);
    getsockname(listener, (struct sockaddr *)&address, &size);
    callback = fmt("http://127.0.0.1:%u/auth/callback", ntohs(address.sin_port));
    char *start = fmt("http://127.0.0.1:%u/start", ntohs(address.sin_port));
    const char *client_id = jstr(app.registration, "client_id");
    json_t *fields =
        json_pack("{s:s,s:s,s:s,s:s,s:s,s:s,s:s,s:s,s:s,s:s}", "client_id",
                  client_id ? client_id : "dynamic_agent_client", "ext_agent_host_id",
                  jstr(app.registration, "ext_agent_host_id"), "response_type", "code",
                  "redirect_uri", callback, "scope",
                  "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
                  "resource", app.api_base, "state", oauth_state, "nonce", oauth_nonce,
                  "code_challenge_method", "S256", "code_challenge", challenge);
    free(challenge);
    if (!client_id)
        json_object_set_new(fields, "agent_name_hint", json_string("harn"));
    if (app.credentials && jstr(app.credentials, "id_token"))
        json_object_set_new(fields, "id_token_hint",
                            json_string(jstr(app.credentials, "id_token")));
    char *params = form(fields);
    authorization_url = fmt("%s?%s", jstr(app.discovery, "authorization_endpoint"), params);
    free(params);
    message("Continue with ChatGPT: %s", start);
    char *link = fmt("%s/login-url", app.config_dir);
    atomic_write(link, start, strlen(start), 0600);
    free(link);
    if (!getenv("HARN_NO_BROWSER")) {
        pid_t pid = fork();
        if (!pid) {
            int null = open("/dev/null", O_RDWR);
            dup2(null, 0);
            dup2(null, 1);
            dup2(null, 2);
            for (int fd = 3; fd < 65536; fd++)
                close(fd);
            execlp("xdg-open", "xdg-open", start, (char *)NULL);
            _exit(127);
        }
    }
    free(start);
    while (!code && !declined && !app.cancel && !app.quit)
        pump(100);
    if (!code || declined || app.cancel || app.quit) {
        message("Sign-in cancelled");
        browser_close();
        return 0;
    }
    long status;
    json_t *tokens = exchange(
        json_pack("{s:s,s:s,s:s,s:s,s:s,s:s}", "grant_type", "authorization_code", "client_id",
                  jstr(app.registration, "client_id"), "code", code, "code_verifier", pkce,
                  "redirect_uri", callback, "resource", app.api_base),
        &status);
    int valid = tokens && status == 200 && jstr(tokens, "access_token") &&
                jstr(tokens, "refresh_token") && granted(tokens) &&
                verify(tokens, oauth_nonce, jstr(app.registration, "subject"));
    if (valid) {
        json_object_set(tokens, "client_id", json_object_get(app.registration, "client_id"));
        json_object_set_new(tokens, "saved_at", json_integer(time(NULL)));
        char *path = config_path("credentials");
        valid = save_json(path, tokens) == 0;
        free(path);
        if (valid) {
            json_decref(app.credentials);
            app.credentials = json_incref(tokens);
            json_object_set(app.registration, "subject", json_object_get(tokens, "subject"));
            path = config_path("registration");
            save_json(path, app.registration);
            free(path);
            message("Signed in with ChatGPT");
        }
    } else
        message("Sign-in failed: %s",
                status == 200 ? "identity or plan permission not accepted" : oauth_error(tokens));
    json_decref(tokens);
    browser_close();
    return valid;
}
static int fresh(json_t *tokens)
{
    return jstr(tokens, "access_token") &&
           jint(tokens, "saved_at", 0) + jint(tokens, "expires_in", 0) > time(NULL) + 60;
}
int auth_refresh(int force)
{
    if (!prepare_registration() || !discover())
        return 0;
    char *lock_path = !strcmp(app.connection->name, "chatgpt")
                          ? fmt("%s/refresh.lock", app.config_dir)
                          : fmt("%s/%s-refresh.lock", app.config_dir, app.connection->name);
    int lockfd = open(lock_path, O_CREAT | O_RDWR, 0600);
    free(lock_path);
    if (lockfd < 0)
        return 0;
    struct flock lock = {.l_type = F_WRLCK, .l_whence = SEEK_SET};
    while (fcntl(lockfd, F_SETLK, &lock) < 0) {
        if (errno != EACCES && errno != EAGAIN) {
            close(lockfd);
            return 0;
        }
        pump(100);
        if (app.cancel || app.quit) {
            close(lockfd);
            return 0;
        }
    }
    char *path = config_path("credentials");
    json_t *old = load_json(path);
    int changed = jstr(old, "access_token") && jstr(app.credentials, "access_token") &&
                  strcmp(jstr(old, "access_token"), jstr(app.credentials, "access_token"));
    if (old && ((!force && fresh(old)) || changed)) {
        json_decref(app.credentials);
        app.credentials = old;
        free(path);
        close(lockfd);
        return 1;
    }
    if (!jstr(old, "refresh_token")) {
        json_decref(old);
        free(path);
        close(lockfd);
        return 0;
    }
    long status;
    json_t *next =
        exchange(json_pack("{s:s,s:s,s:s,s:s}", "grant_type", "refresh_token", "client_id",
                           jstr(app.registration, "client_id"), "refresh_token",
                           jstr(old, "refresh_token"), "resource", app.api_base),
                 &status);
    int ok = status == 200 && jstr(next, "access_token") && jstr(next, "refresh_token");
    if (ok && jstr(next, "id_token"))
        ok = verify(next, NULL, jstr(old, "subject"));
    if (ok) {
        json_object_update(old, next);
        json_object_set_new(old, "saved_at", json_integer(time(NULL)));
        ok = granted(old) && save_json(path, old) == 0;
    }
    if (ok) {
        json_decref(app.credentials);
        app.credentials = json_incref(old);
        message("ChatGPT authorization renewed");
    } else {
        const char *reason = oauth_error(next);
        message("Authorization renewal failed: %s", reason);
        if (!strcmp(reason, "invalid_grant") || !strcmp(reason, "refresh_token_reused") ||
            !strcmp(reason, "refresh_token_expired") ||
            !strcmp(reason, "refresh_token_invalidated")) {
            unlink(path);
            json_decref(app.credentials);
            app.credentials = NULL;
            message("Run harn --login to sign in again");
        }
    }
    json_decref(next);
    json_decref(old);
    free(path);
    close(lockfd);
    return ok;
}
int auth_ready(int force)
{
    if (*app.connection->key_env) {
        const char *key = getenv(app.connection->key_env);
        if (!key || !*key) {
            message("Set %s to the API key for %s", app.connection->key_env, app.api_base);
            return 0;
        }
        json_decref(app.credentials);
        app.credentials = json_pack("{s:s}", "access_token", key);
        return 1;
    }
    if (!prepare_registration())
        return 0;
    if (!app.credentials) {
        char *path = config_path("credentials");
        app.credentials = load_json(path);
        free(path);
    }
    if (app.credentials) {
        if (force || !fresh(app.credentials))
            return auth_refresh(force);
        return granted(app.credentials);
    }
    return auth_login();
}
int auth_logout(void)
{
    if (*app.connection->key_env) {
        message("API key authorization uses %s. Unset it in the launching shell to sign out.",
                app.connection->key_env);
        return 1;
    }
    if (!prepare_registration() || !discover())
        return 0;
    if (!app.credentials) {
        char *path = config_path("credentials");
        app.credentials = load_json(path);
        free(path);
    }
    if (!app.credentials) {
        message("Already signed out");
        return 1;
    }
    const char *url = jstr(app.discovery, "revocation_endpoint");
    if (!url) {
        message("Revocation endpoint unavailable");
        return 0;
    }
    char *body = form(json_pack("{s:s,s:s,s:s}", "token", jstr(app.credentials, "refresh_token"),
                                "token_type_hint", "refresh_token", "client_id",
                                jstr(app.registration, "client_id")));
    long status;
    json_t *response = http_json(url, body, "application/x-www-form-urlencoded", NULL, &status);
    free(body);
    json_decref(response);
    if (status != 200) {
        message("Could not confirm remote sign-out");
        return 0;
    }
    char *path = config_path("credentials");
    unlink(path);
    free(path);
    json_decref(app.credentials);
    app.credentials = NULL;
    message("Signed out");
    return 1;
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
    json_array_foreach(app.models, i, model)
    {
        const char *slug = jstr(model, "slug"), *visibility = jstr(model, "visibility");
        if (slug && visibility && !strcmp(visibility, "list"))
            ui_print("  %s", slug);
    }
}
static json_t *current_model(void)
{
    size_t i;
    json_t *model;
    json_array_foreach(app.models, i, model)
    {
        const char *slug = jstr(model, "slug");
        if (slug && !strcmp(slug, app.model))
            return model;
    }
    return NULL;
}
static json_t *model_catalog(json_t *catalog)
{
    json_t *entries = json_object_get(catalog, *app.connection->key_env ? "data" : "models");
    if (!json_is_array(entries))
        return NULL;
    if (!*app.connection->key_env)
        return json_incref(entries);
    json_t *models = json_array(), *entry;
    size_t i;
    json_array_foreach(entries, i, entry)
    {
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
int models_list(int show)
{
    if (!auth_ready(0))
        return 0;
    char *url = fmt("%s/models", app.api_base);
    long status;
    json_t *catalog = http_json(url, NULL, NULL, jstr(app.credentials, "access_token"), &status);
    free(url);
    json_t *models = status == 200 ? model_catalog(catalog) : NULL;
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
