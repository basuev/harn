#!/usr/bin/env python3
"""Check readable terminal layout without changing the source transcript."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(value):
    return ANSI.sub("", value)


def width(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in plain(value))


class Layout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="harn-render-")
        bridge = Path(cls.tmp.name) / "bridge.c"
        bridge.write_text('''#include "harn.h"
#include <locale.h>
int main(void) {
    setlocale(LC_ALL, "");
    json_error_t error;
    json_t *input = json_loadf(stdin, 0, &error);
    if (!input) return 1;
    const char *text = jstr(input, "text");
    json_t *rows;
    MarkdownStream stream = {0};
    json_t *chunks = json_object_get(input, "chunks");
    int width = jint(input, "width", 42), visible = jint(input, "visible", 24);
    if (chunks) {
        rows = json_array();
        Buf source = {0};
        size_t i, from = 0;
        json_t *chunk;
        json_array_foreach(chunks, i, chunk) {
            if ((long)i == jint(input, "resize", -1)) {
                render_freeze(&stream);
                from = source.len;
                width = jint(input, "new_width", 90);
            }
            buf_puts(&source, json_string_value(chunk));
            json_t *room = json_array_get(json_object_get(input, "windows"), i);
            json_array_append_new(rows, render_stream(&stream, source.data, source.len,
                                                      from, width, room ? json_integer_value(room) : visible,
                                                      i + 1 == json_array_size(chunks)));
        }
        buf_clear(&source);
    } else if (jint(input, "mode", 2) == 1)
        rows = render_stream(&stream, text, json_string_length(json_object_get(input, "text")),
                             0, width, visible, 0);
    else
        rows = render_text(text, json_string_length(json_object_get(input, "text")),
                           width, jint(input, "mode", 2), jint(input, "indent", 0));
    json_decref(stream.literal);
    json_dumpf(rows, stdout, JSON_ENSURE_ASCII);
    json_decref(rows);
    json_decref(input);
    return 0;
}
''')
        cls.binary = str(Path(cls.tmp.name) / "render")
        libs = subprocess.check_output(["pkg-config", "--cflags", "--libs", "libcurl", "jansson", "md4c"], text=True)
        subprocess.run([*shlex.split(os.environ.get("CC", "cc")), "-std=c11", "-D_XOPEN_SOURCE=700",
                        '-DHARN_CONFIG="config.def.h"', "-I" + str(ROOT), str(bridge), str(ROOT / "render.c"), str(ROOT / "util.c"),
                        *shlex.split(libs), "-o", cls.binary], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, text, columns=42, mode=2, indent=0):
        result = subprocess.run([self.binary], input=json.dumps(dict(text=text, width=columns,
                                mode=mode, indent=indent)), text=True, capture_output=True, check=True)
        rows = json.loads(result.stdout)
        self.assertTrue(all(width(row) <= columns for row in rows), rows)
        return rows

    def test_words_and_full_terminal_width(self):
        words = ["Проверил", "сохранение", "сообщений", "и", "запустил", "проверки."] * 12
        for columns in (42, 90, 140):
            with self.subTest(columns=columns):
                rows = self.render(" ".join(words), columns)
                self.assertEqual(" ".join(plain(row) for row in rows).split(), words)
                self.assertGreater(max(map(width, rows)), columns - 15)

    def test_list_continuations_and_nested_items(self):
        rows = list(map(plain, self.render("- " + "long item " * 12 + "\n  - " + "nested item " * 8)))
        self.assertTrue(rows[0].startswith("- long item"), rows)
        nested = next(i for i, row in enumerate(rows) if "- nested" in row)
        self.assertTrue(all(row.startswith("  ") for row in rows[1:nested]), rows)
        self.assertTrue(all(row.startswith("    ") for row in rows[nested + 1:]), rows)

    def test_markdown_and_literal_code(self):
        rows = self.render("## Result\n\n**Passed** with `a_b * c`.\n\n```c\n  x = a * b;\n  puts(\"**literal**\");\n```\n")
        output = "\n".join(rows)
        self.assertIn("\x1b[1mResult", output)
        self.assertIn("\x1b[1mPassed", output)
        self.assertIn("\x1b[7ma_b * c", output)
        self.assertIn('    puts("**literal**");', plain(output))
        self.assertNotIn("```", output)
        self.assertNotIn("##", output)

    def test_numbered_lists_links_and_soft_line_breaks(self):
        rows = list(map(plain, self.render("7. " + "word " * 15 + "\n8. [Manual](https://example.com/docs)\n\nFirst line\nSecond line")))
        self.assertTrue(rows[0].startswith("7. word"), rows)
        self.assertTrue(rows[1].startswith("   "), rows)
        output = "\n".join(rows)
        self.assertIn("8. Manual (https://example.com/docs)", output)
        self.assertIn("First line\nSecond line", output)

    def test_long_words_and_unicode_display_width(self):
        value = "Путь/" + "a" * 300
        self.assertEqual("".join(map(plain, self.render(value))), value)
        rows = self.render("- " + "界面 e\u0301 " * 30)
        self.assertTrue(all(width(row) <= 42 for row in rows))

    def stream(self, chunks, columns=42, visible=24, **options):
        result = subprocess.run([self.binary], input=json.dumps(dict(chunks=chunks,
            width=columns, visible=visible, **options)), text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_pending_inline_text_is_visible_before_its_closer(self):
        for opener, content, closer in (("**", "Привет", "**"), ("`", "a_b", "`"),
                                         ("[", "Manual", "](https://example.com)")):
            with self.subTest(opener=opener):
                frames = self.stream(["Start. " + opener + content, closer])
                self.assertIn(opener + content, "\n".join(map(plain, frames[0])))
                final = "\n".join(frames[1])
                self.assertNotIn(opener + content, plain(final))
                self.assertIn(content, plain(final))
        self.assertIn("**unclosed", "\n".join(map(plain, self.render("**unclosed", mode=1))))

    def test_long_inline_preview_keeps_scrollback_literal_but_next_markdown_works(self):
        for opener, closer in (("**", "**"), ("`", "`"),
                               ("[", "](https://example.com/path_(part))")):
            with self.subTest(opener=opener):
                pending = "Start. " + opener + ("Привет 界 e\u0301 " * 80).rstrip()
                frames = self.stream([pending, closer + "\n\n**Next** with `code`."], visible=8)
                self.assertGreater(len(frames[0]), 8)
                self.assertEqual(frames[0][:-1], frames[1][:len(frames[0]) - 1])
                output = "\n".join(frames[1])
                self.assertIn(opener, plain(output))
                self.assertIn("\x1b[1mNext", output)
                self.assertIn("\x1b[7mcode", output)
                self.assertTrue(all(width(row) <= 42 for row in frames[1]))

    def test_shrinking_available_rows_freezes_an_already_visible_preview(self):
        pending = "**" + ("word " * 30).rstrip()
        frames = self.stream([pending, "**\n\n**After**"], windows=[20, 2])
        self.assertEqual(frames[0][:-1], frames[1][:len(frames[0]) - 1])
        self.assertIn("**word", "\n".join(map(plain, frames[1])))
        self.assertIn("\x1b[1mAfter", "\n".join(frames[1]))

    def test_short_preview_after_a_long_prefix_can_still_be_formatted(self):
        prefix = "Before " * 500 + "\n\n"
        frames = self.stream([prefix + "**Small", "**"], visible=4)
        self.assertEqual(frames[0][:-4], frames[1][:len(frames[0]) - 4])
        self.assertIn("\x1b[1mSmall", "\n".join(frames[1]))

    def test_split_delimiters_and_nested_link_labels(self):
        for chunks in (["*", "*bold", "*", "*"], ["``a`b", "``"],
                       ["[a [nested]", " label]", "(https://example.com/a_(b))"],
                       ["~~strike", "~~"]):
            with self.subTest(chunks=chunks):
                frames = self.stream(chunks)
                self.assertEqual(frames[-1], self.render("".join(chunks)))

    def test_incremental_frames_never_change_rows_released_to_scrollback(self):
        for columns in (42, 90, 140):
            for opener, closer in (("**", "**"), ("``", "``"),
                                   ("[", "](https://example.com/a_(b))")):
                with self.subTest(columns=columns, opener=opener):
                    source = "**Heading**\n\n" + opener + ("Привет 界 e\u0301 " * 100).rstrip()
                    source += closer + "\n\n**After** with `code`."
                    chunks = [source[i:i + 7] for i in range(0, len(source), 7)]
                    frames = self.stream(chunks, columns=columns, visible=4)
                    for old, new in zip(frames, frames[1:]):
                        committed = old[:-4]
                        self.assertEqual(committed, new[:len(committed)])
                    self.assertIn("\x1b[1mAfter", "\n".join(frames[-1]))

    def test_completion_uses_the_parser_for_nested_and_malformed_inline_syntax(self):
        for source in ("***bold italic***", "**bold *italic***", "*outer **inner** end*",
                       "[unclosed **bold**", "`unclosed **bold**"):
            with self.subTest(source=source):
                frames = self.stream([source[:-1], source[-1:]])
                self.assertEqual(frames[-1], self.render(source))

    def test_unclosed_inline_does_not_disable_the_next_paragraph(self):
        for opener in ("**", "`", "[", "[Manual](https://example.com/"):
            with self.subTest(opener=opener):
                frames = self.stream([opener + "word " * 100, "\n \n**After**"], visible=4)
                self.assertEqual(frames[0][:-1], frames[1][:len(frames[0]) - 1])
                self.assertIn("\x1b[1mAfter", "\n".join(frames[1]))

    def test_frozen_code_waits_for_a_matching_backtick_run(self):
        frames = self.stream(["``" + "word " * 100, "``` literal ",
                              "``\n\n**After**"], visible=4)
        self.assertIn("``` literal", "\n".join(map(plain, frames[1])))
        self.assertIn("\x1b[1mAfter", "\n".join(frames[2]))
        self.assertEqual(frames[0][:-1], frames[2][:len(frames[0]) - 1])

    def test_resize_does_not_repeat_an_inline_preview(self):
        frames = self.stream(["Before **keep this", " text**\n\n**After**"],
                             resize=1, new_width=90)
        self.assertIn("Before **keep this", "\n".join(map(plain, frames[0])))
        self.assertNotIn("keep this", "\n".join(map(plain, frames[1])))
        self.assertIn("text**", "\n".join(map(plain, frames[1])))
        self.assertIn("\x1b[1mAfter", "\n".join(frames[1]))

    def test_fenced_code_streams_without_inline_parsing(self):
        rows = self.render("```sh\nprintf '**literal'\n", mode=1)
        self.assertIn("printf '**literal'", "\n".join(map(plain, rows)))

    def test_plain_tool_output_keeps_markdown_syntax(self):
        rows = self.render("**FAILED**: a_b\n--- old\n+++ new", mode=-1, indent=2)
        self.assertEqual(list(map(plain, rows)), ["  **FAILED**: a_b", "  --- old", "  +++ new"])

    def test_code_tabs_keep_their_alignment_inside_display_padding(self):
        rows = self.render("```text\na\tb\n12345678b\n```")
        self.assertEqual(list(map(plain, rows)), ["  a       b", "  12345678b"])


if __name__ == "__main__":
    unittest.main()
