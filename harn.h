#ifndef HARN_H
#define HARN_H
#define _POSIX_C_SOURCE 200809L
#include "vendor/linenoise/linenoise.h"
#include <curl/curl.h>
#include <jansson.h>
#include <signal.h>
#include <stdio.h>
#include <sys/types.h>
#include <termios.h>

typedef struct Net Net;
typedef struct Connection Connection;
typedef struct Provider Provider;
struct Provider {
    const char *id, *revision, *auth_label, *request_path;
    int (*ready)(int);
    int (*logout)(void);
    int (*refresh)(int);
    json_t *(*models)(long *);
    json_t *(*catalog)(json_t *);
    struct curl_slist *(*headers)(void);
    json_t *(*encode)(json_t *);
    void (*event)(Net *);
    /* -1 is terminal, 0 uses the common policy, 1 is retryable. */
    int (*classify)(long, const char *);
    int (*legacy)(json_t *, const Connection *);
    int (*pollfds)(struct curl_waitfd *);
    void (*tick)(void);
    void (*close)(void);
};
struct Connection {
    const char *name, *api_base, *key_env, *model;
    const Provider *provider;
};
#ifndef HARN_CONFIG
#define HARN_CONFIG "config.h"
#endif
#include HARN_CONFIG
#ifndef HARN_REVISION
#define HARN_REVISION "unknown"
#endif

typedef struct {
    char *data;
    size_t len, cap;
} Buf;
typedef struct Job Job;
struct Job {
    int id, fd, logfd, background, reaped, status, stopped, announced;
    pid_t pid, pgid;
    long long kill_at;
    char *command, *path;
    Buf display;
    Job *next;
};
struct Net {
    CURL *easy;
    struct curl_slist *headers;
    Buf body, line, data, text;
    json_t *response, *output;
    char *payload;
    CURLcode result;
    long status;
    int done, stream, malformed, message_started, message_phase;
    char error[CURL_ERROR_SIZE];
};
typedef struct {
    char *root, *config_dir, *state_dir, *session_dir, *session_id, *model, *effort;
    const char *effort_override;
    const Connection *connection;
    char *api_base, *instructions;
    void *provider_state;
    json_t *connection_models, *efforts;
    size_t connection_start;
    json_t *items, *requests, *cycles, *credentials, *past_jobs;
    json_t *last_usage, *models, *model_info;
    json_t *skills, *active_skills;
    json_t *instruction_dirs, *instruction_sources, *delivered_instructions;
    FILE *journal;
    size_t cut;
    char *summary;
    long context_limit, compact_limit;
    const char *limit_source, *compact_source;
    int context_known, usage_valid, compacting, model_override;
    int context_exceeded;
    const char *connection_override;
    int next_job, busy, cancel, quit, batch, exit_status;
    Buf queued;
    int queued_messages, queued_during_turn, pending_delivery;
    char activity[48], *prompt;
    CURLM *multi;
    Net *net;
    Job *jobs;
    int signal_pipe[2], tty, stdin_open, input_active, input_flags;
    struct termios original_termios;
    struct linenoiseState input;
    char *input_buf;
    size_t input_size;
    Buf stdin_buf;
    json_t *deferred_output, *output_frame;
    Buf output_line;
    int output_rows, output_skip, output_columns, output_kind, response_commentary, compact_requested;
    pid_t editor_pid;
    char *editor_path, *editor_draft;
} App;
extern App app;

void *xmalloc(size_t);
char *xstrdup(const char *);
char *fmt(const char *, ...);
void buf_add(Buf *, const void *, size_t);
void buf_puts(Buf *, const char *);
void buf_clear(Buf *);
char *utf8_text(const char *, size_t);
long long now_ms(void);
const char *jstr(json_t *, const char *);
long jint(json_t *, const char *, long);
int mkdirs(const char *, mode_t);
int write_all(int, const void *, size_t);
char *read_file(const char *, size_t *);
int atomic_write(const char *, const void *, size_t, mode_t);
json_t *load_json(const char *);
int save_json(const char *, json_t *);
void pump(int);
void message(const char *, ...);
void ui_print(const char *, ...);
void ui_meta(const char *, ...);
void ui_notice(const char *, size_t);
void ui_status(void);
void ui_activity(const char *, ...);
void ui_stopped(void);

int session_open(const char *);
void session_close(void);
void journal(const char *, json_t *);
void item_add(json_t *);
void user_add(const char *);
json_t *context_build(size_t, size_t, const char *, size_t);
json_t *request_build(void);
void request_record(json_t *);
void context_show(void);
void context_budget(json_t *, const char *);
void usage_record(json_t *);
long context_input(void);
void sessions_list(void);
json_t *sessions_catalog(void);
int session_switch(const char *);
void session_show(int);
const char *effort_at(size_t);
int effort_select(const char *);
void effort_activate(void);
int effort_supported(const char *);
void session_recover(void);

Job *job_start(const char *, int);
Job *job_find(int);
void jobs_tick(void);
void job_stop(Job *, int);
int job_running(Job *);
json_t *job_info(int, int, long, long);
void jobs_shutdown(void);
void jobs_local(const char *);
json_t *tool_schema(void);
json_t *tool_run(const char *, json_t *);

int instructions_load(void);
json_t *instructions_require(const char *, int);

void skills_refresh(Buf *);
json_t *skills_read(json_t *);
void skills_track(json_t *, int);
void skills_restore(json_t *);
void skills_preserve(size_t);
void skills_command(void);

json_t *http_json(const char *, const char *, const char *, const char *, long *);
json_t *http_json_headers(const char *, const char *, const char *, struct curl_slist *, long *);
struct curl_slist *bearer_headers(void);
json_t *responses_models(long *);
json_t *model_request(json_t *);
int auth_ready(int);
int auth_refresh(int);
int auth_logout(void);
void auth_release(void);
int api_key_ready(int);
int api_key_logout(void);
int api_key_legacy(json_t *, const Connection *);
json_t *catalog_models(json_t *);
json_t *catalog_data(json_t *);
json_t *responses_encode(json_t *);
void responses_event(Net *);
void provider_version(void);
int models_list(int);
void models_show(void);
const Connection *connection_at(size_t);
const Connection *connection_find(const char *);
int connection_select(const char *);
int connection_restore(json_t *);
void connection_command(const char *);
void provider_init(void);
void provider_close(void);
int auth_pollfds(struct curl_waitfd *);
void auth_tick(void);

void ui_init(void);
void ui_close(void);
void ui_suspend(int);
void ui_input(void);
void ui_write(const char *, size_t);
void ui_response_begin(int);
void ui_response_end(void);
void ui_exec_start(Job *);
void ui_exec_done(Job *);
json_t *render_text(const char *, size_t, int, int, int);
size_t render_prefix(const char *, size_t);
void ui_editor_done(int);
void ui_command(char *);
void ui_pending(void);
void run_turn(void);
int compact_context(void);
#endif
