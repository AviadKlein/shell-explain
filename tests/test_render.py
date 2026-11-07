from __future__ import annotations

import io
import unittest

from she.models import BreakdownItem, EnvEffect, Explanation, FileEffect, InstructionsInfo
from she.render import (
    SummaryPrinter,
    breakdown_lines,
    explanation_json,
    footer,
    format_source,
    inferred_text,
    item_label,
)

COMMAND = "rm -rf x && ls"
ITEMS = (
    BreakdownItem(span=(0, 2), text="rm", kind="command", help="remove directory entries", source="man:rm(1)"),
    BreakdownItem(span=(3, 6), text="-rf", kind="option", help="-r: recurse; -f: force", source="man:rm(1)"),
    BreakdownItem(span=(9, 11), text="&&", kind="operator", help="run the right side only if the left side succeeds", source="syntax"),
    BreakdownItem(span=(12, 14), text="ls", kind="command", help=None, source=None),
)


class TestBreakdown(unittest.TestCase):
    def test_explainshell_layout(self) -> None:
        self.assertEqual(
            breakdown_lines(COMMAND, ITEMS, 100),
            [
                "  rm -rf x && ls",
                "  ── ───   ── ──",
                "  │   │    │  └─ ls: (no description found)",
                "  │   │    └─ run the right side only if the left side succeeds",
                "  │   └─ -rf: -r: recurse; -f: force  [man rm(1)]",
                "  └─ rm: remove directory entries  [man rm(1)]",
            ],
        )

    def test_truncation_keeps_source(self) -> None:
        lines = breakdown_lines(COMMAND, ITEMS, 45)
        self.assertTrue(all(len(line) <= 45 for line in lines))
        self.assertTrue(lines[-1].endswith("[man rm(1)]"))
        self.assertIn("…", lines[-1])

    def test_multiline_falls_back_to_list(self) -> None:
        lines = breakdown_lines("rm -rf x \\\n && ls", ITEMS[:1], 100)
        self.assertEqual(lines, ["  rm  rm: remove directory entries  [man rm(1)]"])

    def test_builtin_label(self) -> None:
        item = BreakdownItem(span=(0, 2), text="cd", kind="command", help="change the current directory", source="builtin")
        self.assertEqual(item_label(item), "cd (builtin): change the current directory")

    def test_format_source(self) -> None:
        self.assertEqual(format_source("help:uv pip@0.9.4"), "help uv pip@0.9.4")
        self.assertIsNone(format_source("syntax"))


class TestSections(unittest.TestCase):
    def test_inferred_and_footer(self) -> None:
        files = (FileEffect(path="out.tgz", op="write", basis="inferred"),)
        env = (EnvEffect(name="X", op="read", basis="inferred"),)
        self.assertEqual(
            inferred_text(files, env, ("files", "env")),
            "INFERRED (LLM guess)\n  written   out.tgz                    inferred\n  read      X                          inferred\n",
        )
        self.assertEqual(inferred_text(files, env, ("env",)).count("\n"), 2)
        self.assertEqual(
            footer(["man:find(1)", "syntax"], "qwen3.5:4b@0123456789abcdef", "global"),
            "sources: man find(1) · model qwen3.5:4b@0123456789ab · instructions: global\n",
        )

    def test_json_section_selection(self) -> None:
        explanation = Explanation(
            command="rm x",
            instructions=InstructionsInfo(source="none"),
            files=(FileEffect(path="x", op="delete", basis="visible"),),
        )
        full = explanation_json(explanation, ("breakdown", "files", "env", "unknowns", "summary"))
        self.assertEqual(full["schema"], "she/v1")
        self.assertIn("summary", full)
        self.assertEqual(
            explanation_json(explanation, ("files",)),
            {"schema": "she/v1", "command": "rm x", "files": [{"path": "x", "op": "delete", "basis": "visible", "detail": None}]},
        )


class TestSummaryPrinter(unittest.TestCase):
    def test_wraps_streamed_words(self) -> None:
        out = io.StringIO()
        printer = SummaryPrinter(out, width=20)
        printer.start()
        for chunk in ["Dele", "tes every", " file ending", " in .tmp under", " here."]:
            printer.write(chunk)
        printer.finish()
        self.assertEqual(out.getvalue(), "\nSUMMARY (LLM guess)\n  Deletes every file\n  ending in .tmp\n  under here.\n")
