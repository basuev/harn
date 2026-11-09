#include "harn.h"
#include <ctype.h>
#include <md4c.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

enum { BOLD = 1, ITALIC = 2, CODE = 4, DIM = 8, STRIKE = 16 };
typedef struct {
    char text[8];
    int len, width, style, indent, literal;
} Cell;
typedef struct List List;
struct List {
    unsigned number;
    int ordered, indent;
    List *parent;
};
typedef struct {
    Cell *cells;
    size_t len, cap;
    int indent, style[5], literal, item;
    List *list;
} Render;

static void add_text(Render *r, const char *text, size_t len)
{
    mbstate_t state = {0};
    for (size_t p = 0; p < len;) {
        wchar_t c;
        size_t n = mbrtowc(&c, text + p, len - p, &state);
        if (n == (size_t)-1 || n == (size_t)-2 || !n) {
            memset(&state, 0, sizeof(state));
            n = 1;
            c = (unsigned char)text[p];
        }
        if (r->len == r->cap) {
            r->cap = r->cap ? r->cap * 2 : 256;
            Cell *cells = realloc(r->cells, r->cap * sizeof(*cells));
            if (!cells) {
                perror("realloc");
                exit(1);
            }
            r->cells = cells;
        }
        Cell *cell = &r->cells[r->len++];
        *cell = (Cell){.len = (int)n, .width = wcwidth(c), .indent = r->indent,
                       .literal = r->literal};
        memcpy(cell->text, text + p, n);
        for (int i = 0; i < 5; i++)
            if (r->style[i])
                cell->style |= 1 << i;
        p += n;
    }
}
static void separation(Render *r, int blank)
{
    if (!r->len)
        return;
    if (r->cells[r->len - 1].text[0] != '\n')
        add_text(r, "\n", 1);
    if (blank && (r->len < 2 || r->cells[r->len - 2].text[0] != '\n'))
        add_text(r, "\n", 1);
}
static int enter_block(MD_BLOCKTYPE type, void *detail, void *data)
{
    Render *r = data;
    switch (type) {
    case MD_BLOCK_UL:
    case MD_BLOCK_OL: {
        List *list = xmalloc(sizeof(*list));
        *list = (List){.ordered = type == MD_BLOCK_OL, .indent = r->indent,
                       .parent = r->list};
        if (list->ordered)
            list->number = ((MD_BLOCK_OL_DETAIL *)detail)->start;
        separation(r, !r->list);
        r->list = list;
        break;
    }
    case MD_BLOCK_LI: {
        separation(r, 0);
        r->indent = 0;
        for (int i = 0; i < r->list->indent; i++)
            add_text(r, " ", 1);
        char marker[32];
        int n = r->list->ordered
                    ? snprintf(marker, sizeof(marker), "%u. ", r->list->number++)
                    : snprintf(marker, sizeof(marker), "- ");
        add_text(r, marker, (size_t)n);
        r->indent = r->list->indent + n;
        MD_BLOCK_LI_DETAIL *item = detail;
        if (item->is_task) {
            char task[] = "[ ] ";
            task[1] = item->task_mark;
            r->indent += 4;
            add_text(r, task, 4);
        }
        r->item = 1;
        break;
    }
    case MD_BLOCK_P:
    case MD_BLOCK_H:
        if (!r->item)
            separation(r, 1);
        r->item = 0;
        if (type == MD_BLOCK_H)
            r->style[0]++;
        break;
    case MD_BLOCK_CODE:
        separation(r, !r->list);
        r->literal++;
        r->indent += 2;
        break;
    case MD_BLOCK_QUOTE:
        separation(r, 1);
        r->indent += 2;
        break;
    case MD_BLOCK_HR:
        separation(r, 1);
        break;
    default:
        break;
    }
    return 0;
}
static int leave_block(MD_BLOCKTYPE type, void *detail, void *data)
{
    (void)detail;
    Render *r = data;
    switch (type) {
    case MD_BLOCK_H:
        r->style[0]--;
        separation(r, 0);
        break;
    case MD_BLOCK_P:
        separation(r, 0);
        break;
    case MD_BLOCK_LI:
        separation(r, 0);
        r->item = 0;
        break;
    case MD_BLOCK_UL:
    case MD_BLOCK_OL: {
        List *list = r->list;
        r->indent = list->indent;
        r->list = list->parent;
        free(list);
        break;
    }
    case MD_BLOCK_CODE:
        separation(r, 0);
        r->indent -= 2;
        r->literal--;
        break;
    case MD_BLOCK_QUOTE:
        separation(r, 0);
        r->indent -= 2;
        break;
    default:
        break;
    }
    return 0;
}
static int span(MD_SPANTYPE type, void *detail, void *data, int entering)
{
    Render *r = data;
    int i = type == MD_SPAN_STRONG ? 0 : type == MD_SPAN_EM ? 1
                : type == MD_SPAN_CODE ? 2 : type == MD_SPAN_DEL ? 4 : -1;
    if (i >= 0)
        r->style[i] += entering ? 1 : -1;
    if (type == MD_SPAN_A && !entering) {
        if (((MD_SPAN_A_DETAIL *)detail)->is_autolink)
            return 0;
        MD_ATTRIBUTE href = ((MD_SPAN_A_DETAIL *)detail)->href;
        add_text(r, " (", 2);
        r->style[3]++;
        add_text(r, href.text, href.size);
        r->style[3]--;
        add_text(r, ")", 1);
    }
    return 0;
}
static int enter_span(MD_SPANTYPE type, void *detail, void *data)
{
    return span(type, detail, data, 1);
}
static int leave_span(MD_SPANTYPE type, void *detail, void *data)
{
    return span(type, detail, data, 0);
}
static int text_part(MD_TEXTTYPE type, const MD_CHAR *text, MD_SIZE len, void *data)
{
    Render *r = data;
    if (type == MD_TEXT_BR || type == MD_TEXT_SOFTBR)
        add_text(r, "\n", 1);
    else if (type == MD_TEXT_NULLCHAR)
        add_text(r, "\xef\xbf\xbd", 3);
    else
        add_text(r, text, len);
    return 0;
}
static void style(Buf *row, int next, int *current)
{
    if (next == *current)
        return;
    buf_puts(row, "\x1b[0m");
    if (next & BOLD)
        buf_puts(row, "\x1b[1m");
    if (next & ITALIC)
        buf_puts(row, "\x1b[3m");
    if (next & CODE)
        buf_puts(row, "\x1b[7m");
    if (next & DIM)
        buf_puts(row, "\x1b[2m");
    if (next & STRIKE)
        buf_puts(row, "\x1b[9m");
    *current = next;
}
static void row_end(json_t *rows, Buf *row, int *current)
{
    style(row, 0, current);
    json_array_append_new(rows, json_string(row->data ? row->data : ""));
    buf_clear(row);
}
static int blank_line(const char *text, size_t len, size_t at)
{
    if (text[at] != '\n')
        return 0;
    size_t end = at + 1;
    while (end < len && (text[end] == ' ' || text[end] == '\t' || text[end] == '\r'))
        end++;
    return end < len && text[end] == '\n';
}
/* Zero means that an inline opener is still incomplete. Non-openers advance
 * by one byte. The Markdown parser remains responsible for actual styling. */
static size_t inline_end(const char *text, size_t len, size_t start)
{
    size_t i = start;
    if (text[i] == '\\')
        return i + 1 < len ? i + 2 : 0;
    if (text[i] == '!' && i + 1 < len && text[i + 1] == '[')
        i++;
    if (text[i] == '[') {
        size_t end = i + 1;
        int depth = 1;
        for (; end < len; end++) {
            if (text[end] == '\\' && end + 1 < len)
                end++;
            else if (text[end] == '[')
                depth++;
            else if (text[end] == ']' && !--depth)
                break;
            else if (blank_line(text, len, end))
                return end;
        }
        if (end == len || end + 1 == len)
            return 0;
        if (text[end + 1] != '(')
            return end + 1;
        depth = 1;
        end += 2;
        for (; end < len && depth; end++) {
            if (blank_line(text, len, end))
                return end;
            if (text[end] == '\\' && end + 1 < len)
                end++;
            else if (text[end] == '(')
                depth++;
            else if (text[end] == ')')
                depth--;
        }
        return depth ? 0 : end;
    }
    if (text[i] == '`' || text[i] == '*' || text[i] == '_' || text[i] == '~') {
        size_t n = 1;
        while (i + n < len && text[i + n] == text[i])
            n++;
        if (text[i] == '~' && n < 2)
            return i + n;
        if (i + n == len)
            return 0;
        if (text[i] != '`' &&
            (isspace((unsigned char)text[i + n]) ||
             (i && isalnum((unsigned char)text[i - 1]))))
            return i + n;
        for (size_t end = i + n; end < len;) {
            if (blank_line(text, len, end))
                return end;
            if (text[end] == '\\' && text[i] != '`' && end + 1 < len) {
                end += 2;
                continue;
            }
            if (text[end] != text[i]) {
                end++;
                continue;
            }
            size_t run = 1;
            while (end + run < len && text[end + run] == text[i])
                run++;
            if ((text[i] == '`' && run == n) ||
                (text[i] != '`' && run >= n && !isspace((unsigned char)text[end - 1])))
                return end + n;
            end += run;
        }
        return 0;
    }
    return start + 1;
}
static size_t pending_inline(const char *text, size_t len)
{
    int fence = 0;
    for (size_t i = 0; i < len;) {
        if ((!i || text[i - 1] == '\n') && i + 2 < len &&
            (!strncmp(text + i, "```", 3) || !strncmp(text + i, "~~~", 3))) {
            fence = !fence;
            while (i < len && text[i] != '\n')
                i++;
        }
        if (i == len)
            break;
        if (fence) {
            i++;
            continue;
        }
        size_t end = inline_end(text, len, i);
        if (!end)
            return i;
        i = end;
    }
    return len;
}
json_t *render_text(const char *text, size_t len, int width, int markdown, int indent)
{
    Render r = {.indent = indent};
    if (markdown > 0) {
        MD_PARSER parser = {.flags = MD_FLAG_NOHTML | MD_FLAG_STRIKETHROUGH | MD_FLAG_TASKLISTS,
                            .enter_block = enter_block, .leave_block = leave_block,
                            .enter_span = enter_span, .leave_span = leave_span,
                            .text = text_part};
        md_parse(text, (MD_SIZE)len, &parser, &r);
    } else {
        r.literal = markdown < 0;
        add_text(&r, text, len);
    }
    json_t *rows = json_array();
    Buf row = {0};
    int col = 0, current = 0;
    for (size_t p = 0; p < r.len;) {
        Cell cell = r.cells[p];
        if (cell.text[0] == '\n') {
            row_end(rows, &row, &current);
            col = 0;
            p++;
            continue;
        }
        int hanging = cell.indent < width ? cell.indent : 0;
        if (!col) {
            for (int i = 0; i < hanging; i++)
                buf_puts(&row, " ");
            col = hanging;
        }
        int space = cell.text[0] == ' ' || cell.text[0] == '\t';
        size_t end = p + 1;
        int word = cell.width > 0 ? cell.width : 0;
        if (!space && !cell.literal) {
            while (end < r.len && r.cells[end].text[0] != ' ' &&
                   r.cells[end].text[0] != '\t' && r.cells[end].text[0] != '\n') {
                if (r.cells[end].width > 0)
                    word += r.cells[end].width;
                end++;
            }
            if (col > hanging && word > width - col) {
                while (row.len && row.data[row.len - 1] == ' ')
                    row.data[--row.len] = 0;
                row_end(rows, &row, &current);
                for (int i = 0; i < hanging; i++)
                    buf_puts(&row, " ");
                col = hanging;
            }
        }
        for (; p < end; p++) {
            cell = r.cells[p];
            int w = cell.text[0] == '\t' ? 8 - (col - hanging) % 8 : cell.width;
            if (w < 0)
                w = 0;
            if (w && col + w > width) {
                row_end(rows, &row, &current);
                for (int i = 0; i < hanging; i++)
                    buf_puts(&row, " ");
                col = hanging;
                if (space && !cell.literal)
                    continue;
            }
            style(&row, cell.style, &current);
            if (cell.text[0] == '\t') {
                for (int i = 0; i < w; i++)
                    buf_puts(&row, " ");
            } else
                buf_add(&row, cell.text, (size_t)cell.len);
            col += w;
        }
    }
    if (row.len)
        row_end(rows, &row, &current);
    free(r.cells);
    return rows;
}

void render_freeze(MarkdownStream *stream)
{
    if (!stream->pending)
        return;
    if (!stream->literal)
        stream->literal = json_array();
    json_t *last = json_array_get(stream->literal, json_array_size(stream->literal) - 1);
    if (!last || json_integer_value(json_array_get(last, 1)))
        json_array_append_new(stream->literal, json_pack("[I,i]",
                              (json_int_t)(stream->pending - 1), 0));
}
static void stream_source(Buf *out, MarkdownStream *stream, const char *text,
                          size_t from, size_t end, size_t pending)
{
    size_t range = 0;
    for (size_t i = from; i < end; i++) {
        json_t *span = json_array_get(stream->literal, range);
        while (span && json_integer_value(json_array_get(span, 1)) &&
               (size_t)json_integer_value(json_array_get(span, 1)) <= i)
            span = json_array_get(stream->literal, ++range);
        int literal = i >= pending || (span &&
                      i >= (size_t)json_integer_value(json_array_get(span, 0)));
        if (literal && (unsigned char)text[i] < 128 && ispunct((unsigned char)text[i]))
            buf_puts(out, "\\");
        buf_add(out, text + i, 1);
    }
}
json_t *render_stream(MarkdownStream *stream, const char *text, size_t len,
                       size_t from, int width, int visible, int complete)
{
    if (stream->pending && stream->rows > (size_t)visible &&
        stream->pending_row < stream->rows - (size_t)visible)
        render_freeze(stream);
    size_t i;
    json_t *span;
    json_array_foreach(stream->literal, i, span) {
        if (!json_integer_value(json_array_get(span, 1))) {
            size_t end = inline_end(text, len,
                                   (size_t)json_integer_value(json_array_get(span, 0)));
            if (end)
                json_array_set_new(span, 1, json_integer((json_int_t)end));
        }
    }
    size_t pending = pending_inline(text, len);
    stream->pending = !complete && pending < len ? pending + 1 : 0;
    Buf source = {0};
    stream_source(&source, stream, text, from, len, complete ? len : pending);
    json_t *frame = render_text(source.data ? source.data : "", source.len, width, 2, 0);
    buf_clear(&source);
    stream->rows = json_array_size(frame);
    stream->pending_row = 0;
    if (stream->pending && pending >= from) {
        stream_source(&source, stream, text, from, pending, pending);
        json_t *prefix = render_text(source.data ? source.data : "", source.len, width, 2, 0);
        size_t rows = json_array_size(prefix);
        stream->pending_row = rows ? rows - 1 : 0;
        json_decref(prefix);
        buf_clear(&source);
    }
    if (stream->pending && stream->rows > (size_t)visible &&
        stream->pending_row < stream->rows - (size_t)visible)
        render_freeze(stream);
    return frame;
}
