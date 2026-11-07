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
    json_t *rows = render_text(text, json_string_length(json_object_get(input, "text")),
                              jint(input, "width", 42), jint(input, "mode", 2),
                              jint(input, "indent", 0));
    json_dumpf(rows, stdout, JSON_ENSURE_ASCII);
    json_decref(rows);
    json_decref(input);
    return 0;
}
''')
        cls.binary = str(Path(cls.tmp.name) / "render")
        libs = subprocess.check_output(["pkg-config", "--cflags", "--libs", "libcurl", "jansson", "md4c"], text=True)
        subprocess.run([*shlex.split(os.environ.get("CC", "cc")), "-std=c11", "-D_XOPEN_SOURCE=700",
                        "-I" + str(ROOT), str(bridge), str(ROOT / "render.c"), str(ROOT / "util.c"),
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

    def test_pending_inline_spans_do_not_rewrite_scrollback(self):
        prefix = "Start. "
        pending = "**" + ("long highlighted text " * 80).rstrip()
        self.assertEqual(list(map(plain, self.render(prefix + pending, mode=1))), ["Start."])
        rows = self.render(prefix + pending + "**", mode=1)
        self.assertGreater(len(rows), 30)
        self.assertEqual(" ".join(map(plain, rows)).split(), (prefix + pending[2:]).split())
        self.assertIn("**unclosed", "\n".join(map(plain, self.render("**unclosed"))))

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
