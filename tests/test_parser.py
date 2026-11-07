from __future__ import annotations

import unittest

from she.parser import parse, split_statements


class TestParse(unittest.TestCase):
    def test_find_and_export(self) -> None:
        parsed = parse('find . -name "*.tmp" -delete && export CLEANED=1')
        find, export = parsed.commands
        self.assertEqual(find.name.text, "find")
        self.assertEqual([word.text for word in find.args], [".", "-name", "*.tmp", "-delete"])
        self.assertEqual(find.args[2].raw, '"*.tmp"')
        self.assertEqual(export.kind, "declaration")
        self.assertEqual([(item.name, item.value) for item in export.assignments], [("CLEANED", "1")])
        self.assertEqual([(token.text, token.span) for token in parsed.operators], [("&&", (29, 31))])
        self.assertFalse(parsed.has_errors)

    def test_fork_bomb_defines_function_and_calls_it(self) -> None:
        parsed = parse(":(){ :|:& };:")
        self.assertEqual(parsed.function_names, frozenset({":"}))
        self.assertEqual([command.name.text for command in parsed.commands], [":", ":", ":"])
        self.assertEqual([token.text for token in parsed.operators], ["|", "&", ";"])

    def test_git_log_arguments(self) -> None:
        parsed = parse("git log --graph --abbrev-commit --pretty=oneline origin..mybranch")
        (git,) = parsed.commands
        self.assertEqual(
            [word.text for word in git.args],
            ["log", "--graph", "--abbrev-commit", "--pretty=oneline", "origin..mybranch"],
        )

    def test_prefix_assignment_and_redirects(self) -> None:
        parsed = parse("X=1 cmd > out 2>&1")
        (command,) = parsed.commands
        self.assertEqual([(item.name, item.value) for item in command.assignments], [("X", "1")])
        self.assertEqual(parsed.shell_assignments, ())
        self.assertEqual(
            [(item.operator, item.target.text if item.target else None, item.descriptor) for item in command.redirects],
            [(">", "out", None), (">&", "1", "2")],
        )

    def test_heredoc_with_nested_file_redirect(self) -> None:
        parsed = parse("cat <<EOF > out\nhello $X\nEOF")
        (command,) = parsed.commands
        self.assertEqual([item.operator for item in command.redirects], ["<<", ">"])
        self.assertEqual(command.redirects[0].span, (4, 9))
        self.assertEqual([token.text for token in parsed.env_reads], ["X"])

    def test_substitution_and_expansions(self) -> None:
        parsed = parse('echo $(date) "${HOME}/x"')
        self.assertEqual([(token.text, token.span) for token in parsed.substitutions], [("$(", (5, 7))])
        self.assertEqual([token.text for token in parsed.env_reads], ["HOME"])
        self.assertEqual(sorted(command.name.text for command in parsed.commands), ["date", "echo"])

    def test_loop_variables_are_recorded(self) -> None:
        parsed = parse('for f in *.txt; do wc -l "$f"; done')
        self.assertEqual(parsed.loop_variables, ("f",))

    def test_unset_command(self) -> None:
        (command,) = parse("unset FOO BAR").commands
        self.assertEqual(command.kind, "unset")
        self.assertEqual([word.text for word in command.args], ["FOO", "BAR"])

    def test_parse_error_is_reported_not_raised(self) -> None:
        parsed = parse('echo "unterminated')
        self.assertTrue(parsed.has_errors)

    def test_non_string_raises(self) -> None:
        with self.assertRaises(TypeError) as context:
            parse(123)  # type: ignore[arg-type]
        self.assertEqual(str(context.exception), "expected str command, got int")


class TestSplitStatements(unittest.TestCase):
    def test_heredoc_and_continuation_stay_whole(self) -> None:
        script = "ls -la\ncat <<EOF\nhi\nEOF\necho a \\\n  b\n# comment\nfoo && bar"
        self.assertEqual(split_statements(script), ["ls -la", "cat <<EOF\nhi\nEOF", "echo a \\\n  b", "foo && bar"])

    def test_empty_input(self) -> None:
        self.assertEqual(split_statements("\n# only a comment\n"), [])
