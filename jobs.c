#include "harn.h"
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

Job *job_find(int id)
{
    for (Job *j = app.jobs; j; j = j->next)
        if (j->id == id)
            return j;
    return NULL;
}
int job_running(Job *j)
{
    if (!j || j->pgid <= 0)
        return 0;
    return kill(-j->pgid, 0) == 0 || errno == EPERM;
}
static void job_record(Job *j, const char *state)
{
    json_t *data =
        json_pack("{s:i,s:s,s:s,s:b,s:i}", "id", j->id, "command", j->command, "state", state,
                  "background", j->background, "exit_code", j->reaped ? j->status : -1);
    journal("job", data);
    char key[32];
    snprintf(key, sizeof(key), "%d", j->id);
    json_object_set(app.past_jobs, key, data);
    json_decref(data);
}
static void display_output(Job *j, int eof)
{
    size_t end = j->display.len;
    if (!eof && end) {
        size_t start = end - 1;
        while (start && end - start < 4 && ((unsigned char)j->display.data[start] & 0xc0) == 0x80)
            start--;
        unsigned char c = (unsigned char)j->display.data[start];
        size_t need = c >= 0xf0 && c <= 0xf4   ? 4
                      : c >= 0xe0 && c <= 0xef ? 3
                      : c >= 0xc2 && c <= 0xdf ? 2
                                               : 1;
        if (need > end - start)
            end = start;
    }
    if (end) {
        char *text = utf8_text(j->display.data, end);
        ui_notice(text, strlen(text));
        free(text);
        memmove(j->display.data, j->display.data + end, j->display.len - end);
        j->display.len -= end;
        j->display.data[j->display.len] = 0;
    }
}
Job *job_start(const char *command, int background)
{
    int pipefd[2];
    if (pipe(pipefd))
        return NULL;
    Job *j = xmalloc(sizeof(*j));
    memset(j, 0, sizeof(*j));
    j->id = app.next_job++;
    j->fd = pipefd[0];
    j->command = xstrdup(command);
    j->background = background;
    char *dir = fmt("%s/jobs", app.session_dir);
    if (mkdirs(dir, 0700)) {
        free(dir);
        goto bad;
    }
    j->path = fmt("%s/%d.log", dir, j->id);
    free(dir);
    j->logfd = open(j->path, O_CREAT | O_WRONLY | O_APPEND, 0600);
    if (j->logfd < 0)
        goto bad;
    j->pid = fork();
    if (j->pid < 0) {
        close(j->logfd);
        goto bad;
    }
    if (!j->pid) {
        setpgid(0, 0);
        signal(SIGINT, SIG_DFL);
        signal(SIGTERM, SIG_DFL);
        signal(SIGHUP, SIG_DFL);
        signal(SIGPIPE, SIG_DFL);
        int null = open("/dev/null", O_RDONLY);
        dup2(null, 0);
        dup2(pipefd[1], 1);
        dup2(pipefd[1], 2);
        long maxfd = sysconf(_SC_OPEN_MAX);
        if (maxfd < 0 || maxfd > 65536)
            maxfd = 65536;
        for (int fd = 3; fd < maxfd; fd++)
            close(fd);
        execl("/bin/sh", "sh", "-c", command, (char *)NULL);
        _exit(127);
    }
    close(pipefd[1]);
    setpgid(j->pid, j->pid);
    j->pgid = j->pid;
    fcntl(j->fd, F_SETFL, fcntl(j->fd, F_GETFL) | O_NONBLOCK);
    fcntl(j->fd, F_SETFD, FD_CLOEXEC);
    fcntl(j->logfd, F_SETFD, FD_CLOEXEC);
    j->next = app.jobs;
    app.jobs = j;
    job_record(j, "running");
    ui_exec_start(j);
    return j;
bad:
    close(pipefd[0]);
    close(pipefd[1]);
    free(j->path);
    free(j->command);
    free(j);
    return NULL;
}
void job_stop(Job *j, int by_user)
{
    if (!job_running(j))
        return;
    j->stopped = 1;
    kill(-j->pgid, SIGTERM);
    j->kill_at = now_ms() + STOP_GRACE_MS;
    job_record(j, "stopping");
    if (by_user) {
        char *text =
            fmt("User stopped job #%d: %s. Do not restart it unless asked.", j->id, j->command);
        item_add(json_pack("{s:s,s:s}", "role", "developer", "content", text));
        free(text);
        message("Stopping #%d", j->id);
    }
}
void jobs_tick(void)
{
    int status;
    pid_t pid;
    while ((pid = waitpid(-1, &status, WNOHANG)) > 0) {
        if (pid == app.editor_pid) {
            app.editor_pid = 0;
            ui_editor_done(status);
            continue;
        }
        for (Job *j = app.jobs; j; j = j->next)
            if (j->pid == pid) {
                j->reaped = 1;
                j->status = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
            }
    }
    for (Job *j = app.jobs; j; j = j->next) {
        if (j->fd >= 0) {
            char data[8192];
            ssize_t n;
            while ((n = read(j->fd, data, sizeof(data))) > 0) {
                if (write_all(j->logfd, data, (size_t)n))
                    message("Could not save output for #%d", j->id);
                if (!j->background && !app.tty) {
                    buf_add(&j->display, data, (size_t)n);
                    display_output(j, 0);
                }
            }
            if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
                close(j->fd);
                j->fd = -1;
                display_output(j, 1);
            }
        }
        if (j->kill_at && now_ms() >= j->kill_at) {
            kill(-j->pgid, SIGKILL);
            j->kill_at = 0;
        }
        if (j->reaped && !job_running(j) && j->fd < 0 && !j->announced) {
            j->pgid = 0;
            j->kill_at = 0;
            j->announced = 1;
            job_record(j, j->stopped ? "stopped" : "exited");
            ui_exec_done(j);
        }
    }
}
json_t *job_info(int id, int output, long offset, long limit)
{
    Job *j = job_find(id);
    char key[32];
    snprintf(key, sizeof(key), "%d", id);
    json_t *past = json_object_get(app.past_jobs, key);
    if (!j && !past)
        return json_pack("{s:s}", "error", "Unknown job");
    const char *past_state = jstr(past, "state");
    const char *state = j ? (job_running(j) ? (j->stopped ? "stopping" : "running")
                                            : (j->stopped ? "stopped" : "exited"))
                        : past_state && !strcmp(past_state, "stopped")
                            ? "stopped (previous invocation)"
                        : past_state && !strcmp(past_state, "exited")
                            ? "exited (previous invocation)"
                            : "unknown (previous invocation, not attached)";
    json_t *r = json_pack("{s:i,s:s,s:s,s:i}", "id", id, "state", state, "command",
                          j ? j->command : jstr(past, "command"), "exit_code",
                          j ? (j->reaped ? j->status : -1) : (int)jint(past, "exit_code", -1));
    if (output) {
        char *path = fmt("%s/jobs/%d.log", app.session_dir, id);
        int fd = open(path, O_RDONLY);
        free(path);
        struct stat st;
        if (fd >= 0 && !fstat(fd, &st)) {
            if (limit <= 0 || limit > OUTPUT_LIMIT)
                limit = OUTPUT_LIMIT;
            if (offset < 0)
                offset = st.st_size > limit ? st.st_size - limit : 0;
            if (offset > st.st_size)
                offset = st.st_size;
            char *data = xmalloc((size_t)limit);
            ssize_t n = pread(fd, data, (size_t)limit, offset);
            if (n < 0)
                n = 0;
            char *text = utf8_text(data, (size_t)n);
            free(data);
            json_object_set_new(r, "output", json_string(text));
            free(text);
            json_object_set_new(r, "offset", json_integer(offset));
            json_object_set_new(r, "next_offset", json_integer(offset + n));
            json_object_set_new(r, "bytes", json_integer(st.st_size));
            json_object_set_new(r, "truncated",
                                json_boolean(offset > 0 || offset + n < st.st_size));
            char *log_path = fmt("%s/jobs/%d.log", app.session_dir, id);
            json_object_set_new(r, "log_path", json_string(log_path));
            free(log_path);
        }
        if (fd >= 0)
            close(fd);
    }
    return r;
}
void jobs_local(const char *args)
{
    while (*args == ' ')
        args++;
    if (!*args) {
        const char *key;
        json_t *past;
        int count = 0;
        json_object_foreach(app.past_jobs, key, past)
        {
            int id = atoi(key);
            json_t *info = job_info(id, 0, 0, 0);
            ui_print("#%d %s: %s", id, jstr(info, "state"), jstr(past, "command"));
            json_decref(info);
            count++;
        }
        if (!count)
            ui_print("No jobs");
        return;
    }
    if (!strncmp(args, "stop ", 5)) {
        Job *j = job_find(atoi(args + 5));
        if (j && job_running(j))
            job_stop(j, 1);
        else
            message("Job is not running");
        return;
    }
    json_t *r = job_info(atoi(args), 1, -1, OUTPUT_LIMIT);
    const char *out = jstr(r, "output");
    ui_print("jobs #%ld: %s", jint(r, "id", 0),
             jstr(r, "state") ? jstr(r, "state") : jstr(r, "error"));
    if (jstr(r, "command"))
        ui_print("Command: %s\nExit code: %ld", jstr(r, "command"), jint(r, "exit_code", -1));
    if (jstr(r, "log_path"))
        ui_print("Log: %s", jstr(r, "log_path"));
    if (json_is_true(json_object_get(r, "truncated")))
        ui_print("Showing bytes %ld-%ld of %ld. Full output is in the log.", jint(r, "offset", 0),
                 jint(r, "next_offset", 0), jint(r, "bytes", 0));
    while (out && *out) {
        const char *end = strchr(out, '\n');
        ui_print("jobs #%ld: %.*s", jint(r, "id", 0), end ? (int)(end - out) : (int)strlen(out),
                 out);
        out = end ? end + 1 : NULL;
    }
    json_decref(r);
}
void jobs_shutdown(void)
{
    for (Job *j = app.jobs; j; j = j->next)
        job_stop(j, 0);
    long long deadline = now_ms() + STOP_GRACE_MS + 500;
    while (now_ms() < deadline) {
        int alive = 0;
        jobs_tick();
        for (Job *j = app.jobs; j; j = j->next)
            if (job_running(j))
                alive = 1;
        if (!alive)
            break;
        struct timespec ts = {0, 10000000};
        nanosleep(&ts, NULL);
    }
    for (Job *j = app.jobs; j; j = j->next)
        if (job_running(j))
            kill(-j->pgid, SIGKILL);
    jobs_tick();
    while (app.jobs) {
        Job *j = app.jobs;
        app.jobs = j->next;
        if (j->fd >= 0)
            close(j->fd);
        close(j->logfd);
        free(j->command);
        free(j->path);
        buf_clear(&j->display);
        free(j);
    }
}
