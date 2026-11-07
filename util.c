#include "harn.h"
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

void *xmalloc(size_t n)
{
    void *p = malloc(n ? n : 1);
    if (!p) {
        perror("malloc");
        exit(1);
    }
    return p;
}
char *xstrdup(const char *s)
{
    char *p = strdup(s ? s : "");
    if (!p) {
        perror("strdup");
        exit(1);
    }
    return p;
}
char *fmt(const char *format, ...)
{
    va_list a, b;
    va_start(a, format);
    va_copy(b, a);
    int n = vsnprintf(NULL, 0, format, a);
    va_end(a);
    char *s = xmalloc((size_t)n + 1);
    vsnprintf(s, (size_t)n + 1, format, b);
    va_end(b);
    return s;
}
void buf_add(Buf *b, const void *s, size_t n)
{
    if (!n)
        return;
    if (n > SIZE_MAX - b->len - 1) {
        fputs("Buffer overflow\n", stderr);
        exit(1);
    }
    size_t need = b->len + n + 1;
    if (need > b->cap) {
        size_t c = b->cap ? b->cap : 256;
        while (c < need) {
            if (c > SIZE_MAX / 2) {
                c = need;
                break;
            }
            c *= 2;
        }
        char *p = realloc(b->data, c);
        if (!p) {
            perror("realloc");
            exit(1);
        }
        b->data = p;
        b->cap = c;
    }
    memcpy(b->data + b->len, s, n);
    b->len += n;
    b->data[b->len] = 0;
}
void buf_puts(Buf *b, const char *s)
{
    buf_add(b, s, strlen(s));
}
void buf_clear(Buf *b)
{
    free(b->data);
    memset(b, 0, sizeof(*b));
}
char *utf8_text(const char *s, size_t n)
{
    Buf b = {0};
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        size_t len = c < 0x80                 ? 1
                     : c >= 0xc2 && c <= 0xdf ? 2
                     : c <= 0xef && c >= 0xe0 ? 3
                     : c >= 0xf0 && c <= 0xf4 ? 4
                                              : 0;
        int valid = len && i + len <= n;
        for (size_t j = 1; valid && j < len; j++)
            if (((unsigned char)s[i + j] & 0xc0) != 0x80)
                valid = 0;
        if (valid && len == 3 &&
            ((c == 0xe0 && (unsigned char)s[i + 1] < 0xa0) ||
             (c == 0xed && (unsigned char)s[i + 1] >= 0xa0)))
            valid = 0;
        if (valid && len == 4 &&
            ((c == 0xf0 && (unsigned char)s[i + 1] < 0x90) ||
             (c == 0xf4 && (unsigned char)s[i + 1] >= 0x90)))
            valid = 0;
        if (valid && c) {
            buf_add(&b, s + i, len);
            i += len;
        } else {
            buf_puts(&b, "\xef\xbf\xbd");
            i++;
        }
    }
    return b.data ? b.data : xstrdup("");
}
long long now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
const char *jstr(json_t *o, const char *k)
{
    return json_string_value(json_object_get(o, k));
}
long jint(json_t *o, const char *k, long fallback)
{
    json_t *v = json_object_get(o, k);
    return json_is_integer(v) ? (long)json_integer_value(v) : fallback;
}
int mkdirs(const char *path, mode_t mode)
{
    if (!*path) {
        errno = EINVAL;
        return -1;
    }
    char *p = xstrdup(path);
    for (char *s = p + 1; *s; s++)
        if (*s == '/') {
            *s = 0;
            if (mkdir(p, mode) && errno != EEXIST) {
                free(p);
                return -1;
            }
            *s = '/';
        }
    int r = mkdir(p, mode);
    free(p);
    return r && errno != EEXIST ? -1 : 0;
}
int write_all(int fd, const void *data, size_t n)
{
    const char *p = data;
    while (n) {
        ssize_t r = write(fd, p, n);
        if (r < 0 && errno == EINTR)
            continue;
        if (r <= 0)
            return -1;
        p += r;
        n -= (size_t)r;
    }
    return 0;
}
char *read_file(const char *path, size_t *len)
{
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return NULL;
    Buf b = {0};
    char buf[8192];
    ssize_t n;
    while ((n = read(fd, buf, sizeof(buf))) != 0) {
        if (n < 0) {
            if (errno == EINTR)
                continue;
            buf_clear(&b);
            close(fd);
            return NULL;
        }
        buf_add(&b, buf, (size_t)n);
    }
    close(fd);
    if (len)
        *len = b.len;
    return b.data ? b.data : xstrdup("");
}
int atomic_write(const char *path, const void *data, size_t n, mode_t mode)
{
    char *tmp = fmt("%s.XXXXXX", path);
    int fd = mkstemp(tmp);
    if (fd < 0) {
        free(tmp);
        return -1;
    }
    int bad = fchmod(fd, mode) || write_all(fd, data, n) || fsync(fd);
    if (close(fd))
        bad = 1;
    if (!bad && rename(tmp, path))
        bad = 1;
    if (bad)
        unlink(tmp);
    free(tmp);
    return bad ? -1 : 0;
}
json_t *load_json(const char *path)
{
    json_error_t e;
    return json_load_file(path, JSON_REJECT_DUPLICATES, &e);
}
int save_json(const char *path, json_t *o)
{
    char *s = json_dumps(o, JSON_COMPACT);
    if (!s)
        return -1;
    int r = atomic_write(path, s, strlen(s), 0600);
    free(s);
    return r;
}
