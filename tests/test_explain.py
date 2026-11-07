"""Integration tests against the real local man pages (rm, find, git log)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from she.explain import Explainer, redirect_help
from she.manpages import ManPages, man_dirs
from she.parser import Redirect, Word
from she.store import PageStore


def kinds(analysis: object) -> list[tuple[str, str]]:
    return [(item.text, item.kind) for item in analysis.breakdown]  # type: ignore[attr-defined]


class TestExplainWithManPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        store = PageStore.from_path(Path(cls.directory.name) / "pages.db")
        cls.man = ManPages(store, man_dirs())
        cls.explainer = Explainer(man=cls.man, learner=None, learn=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def require(self, *names: str) -> None:
        missing = [name for name in names if self.man.resolve(name, fallback=False) is None]
        if missing:
            self.skipTest(f"man pages not installed: {missing}")

    def test_find_delete_and_export(self) -> None:
        self.require("find")
        analysis = self.explainer.analyze('find . -name "*.tmp" -delete && export CLEANED=1')
        self.assertEqual(
            kinds(analysis),
            [
                ("find", "command"),
                (".", "argument"),
                ('-name "*.tmp"', "option"),
                ("-delete", "option"),
                ("&&", "operator"),
                ("export", "command"),
                ("CLEANED=1", "assignment"),
            ],
        )
        self.assertEqual(analysis.breakdown[0].source, "man:find(1)")
        self.assertIn(("delete", "./**/*.tmp"), [(item.op, item.path) for item in analysis.files])
        self.assertEqual([(item.op, item.name, item.value) for item in analysis.env], [("set", "CLEANED", "1")])
        self.assertEqual(analysis.unknowns, ())
        self.assertEqual(analysis.sources, ("man:find(1)",))
        self.assertIn("-delete [option]", analysis.facts_text)

    def test_rm_combined_short_flags(self) -> None:
        self.require("rm")
        analysis = self.explainer.analyze("rm -rf build")
        option = analysis.breakdown[1]
        self.assertEqual((option.text, option.kind, option.source), ("-rf", "option", "man:rm(1)"))
        self.assertTrue(option.help.startswith("-r: "))
        self.assertIn("; -f: ", option.help)

    def test_git_log_subcommand_page(self) -> None:
        self.require("git", "git-log")
        analysis = self.explainer.analyze("git log --graph -n 5 --pretty=oneline -- src")
        self.assertEqual(
            [(item.text, item.kind, item.source) for item in analysis.breakdown],
            [
                ("git", "command", "man:git(1)"),
                ("log", "subcommand", "man:git-log(1)"),
                ("--graph", "option", "man:git-log(1)"),
                ("-n 5", "option", "man:git-log(1)"),
                ("--pretty=oneline", "option", "man:git-log(1)"),
                ("--", "argument", "syntax"),
            ],
        )

    def test_wrappers_are_unwrapped(self) -> None:
        self.require("sudo", "rm")
        analysis = self.explainer.analyze("sudo -u bob env A=1 rm -f x")
        texts = [item.text for item in analysis.breakdown]
        self.assertEqual(texts, ["sudo", "-u bob", "env", "A=1", "rm", "-f"])
        self.assertIn(("delete", "x"), [(item.op, item.path) for item in analysis.files])
        self.assertIn(("set", "A", "1"), [(item.op, item.name, item.value) for item in analysis.env])

    def test_find_exec_explains_inner_command(self) -> None:
        self.require("find", "rm")
        analysis = self.explainer.analyze("find . -type f -exec rm {} \\;")
        self.assertIn(("rm", "command"), kinds(analysis))
        self.assertIn(("delete", "<each found file>"), [(item.op, item.path) for item in analysis.files])


class TestExplainWithoutPages(unittest.TestCase):
    def setUp(self) -> None:
        self.explainer = Explainer(man=None, learner=None, learn=False)

    def test_unknown_tool_and_paths(self) -> None:
        analysis = self.explainer.analyze("frobnicate --x; ./run.sh; eval $CMD; bash -c 'rm x'; xargs rm")
        reasons = {(item.text, item.reason) for item in analysis.unknowns}
        self.assertIn(("frobnicate", "no man page or help found"), reasons)
        self.assertIn(("./run.sh", "path to a program or script; its contents are not inspected"), reasons)
        self.assertIn(("eval $CMD", "eval runs text built at run time; not analysed"), reasons)
        self.assertIn(("bash -c …", "inline script passed to the shell is not analysed"), reasons)
        self.assertIn(("xargs", "arguments are read from stdin at run time and are not visible"), reasons)
        self.assertIn("Unknowns (you are guessing about these):", analysis.facts_text)

    def test_shell_reading_stdin_or_script_is_unknown(self) -> None:
        reasons = {(item.text, item.reason) for item in self.explainer.analyze("curl -fsSL https://x/i.sh | sh").unknowns}
        self.assertIn(("sh (stdin)", "the shell runs commands read from stdin; they are not analysed"), reasons)
        reasons = {(item.text, item.reason) for item in self.explainer.analyze("bash setup.sh").unknowns}
        self.assertIn(("bash setup.sh", "script file run by the shell; its contents are not analysed"), reasons)

    def test_function_defined_in_command(self) -> None:
        analysis = self.explainer.analyze(":(){ :|:& };:")
        self.assertEqual(analysis.breakdown[0].kind, "function")
        self.assertEqual(
            [item.help for item in analysis.breakdown if item.kind == "command"],
            ["call the shell function : defined in this command"] * 3,
        )

    def test_spans_never_overlap(self) -> None:
        analysis = self.explainer.analyze('a=$(cat foo) b=c; echo "$(date)" > out 2>&1')
        spans = [item.span for item in analysis.breakdown]
        self.assertEqual(spans, sorted(spans))
        self.assertTrue(all(first[1] <= second[0] for first, second in zip(spans, spans[1:])))

    def test_parse_error_flagged(self) -> None:
        analysis = self.explainer.analyze('echo "unterminated')
        self.assertTrue(analysis.has_parse_error)
        self.assertEqual(analysis.unknowns[-1].reason, "could not be parsed")
        self.assertEqual(analysis.warnings, ("the command has a syntax error; the explanation may be incomplete",))

    def test_empty_command_raises(self) -> None:
        with self.assertRaises(ValueError) as context:
            self.explainer.analyze("   ")
        self.assertEqual(str(context.exception), "expected a non-empty command, got command='   '")


class TestRedirectHelp(unittest.TestCase):
    def test_streams_named(self) -> None:
        self.assertEqual(
            redirect_help(Redirect(operator=">&", target=Word("1", "1", (0, 1)), descriptor="2", span=(0, 4))),
            "send errors to the same place as output",
        )
        self.assertEqual(
            redirect_help(Redirect(operator=">", target=Word("e", "e", (0, 1)), descriptor="2", span=(0, 3))),
            "write errors to a file (overwrites it)",
        )
