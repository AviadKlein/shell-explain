from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from she import cli
from she.models import LlmAnswer, Summary


class FakeStdin(io.StringIO):
    def __init__(self, text: str, tty: bool = False) -> None:
        super().__init__(text)
        self.__tty = tty

    def isatty(self) -> bool:
        return self.__tty


def fake_llm(answer: LlmAnswer | None = None, error: str | None = None):  # noqa: ANN201
    def run(analysis, instructions, args, paths, deadline, on_chunk):  # noqa: ANN001, ANN202
        if error is not None:
            return cli.LlmOutcome(error=error, llm_ms=5)
        on_chunk(answer.summary)
        return cli.LlmOutcome(
            answer=answer, summary=Summary(text=answer.summary, model="m@abc", cache="miss"), model="m@abc", llm_ms=7
        )

    return run


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "SHE_CACHE_DIR": str(root / "cache"),
                "SHE_STATE_DIR": str(root / "state"),
                "SHE_CONFIG_DIR": str(root / "config"),
            },
        )
        self.environment.start()
        os.environ.pop("SHE_INSTRUCTIONS", None)

    def tearDown(self) -> None:
        self.environment.stop()
        self.directory.cleanup()

    def run_cli(self, argv: list[str], stdin: str | None = None, tty: bool = True) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        fake_stdin = FakeStdin(stdin or "", tty=stdin is None and tty)
        with redirect_stdout(out), redirect_stderr(err), mock.patch("sys.stdin", fake_stdin):
            try:
                code = cli.main(argv)
            except SystemExit as exit_:
                code = int(exit_.code or 0)
        return code, out.getvalue(), err.getvalue()


class TestArguments(CliTestCase):
    def test_double_dash_joins_command(self) -> None:
        args = cli.build_parser().parse_args(["--json", "--", "ls", "-la", "/tmp"])
        self.assertEqual(args.command, ["ls", "-la", "/tmp"])
        self.assertTrue(args.json)

    def test_json_and_json_stream_are_exclusive(self) -> None:
        code, _, err = self.run_cli(["--json", "--json-stream", "--", "ls"])
        self.assertEqual(code, 2)
        self.assertIn("not allowed with argument", err)

    def test_depth_requires_parse_cli(self) -> None:
        code, _, err = self.run_cli(["--depth", "2", "--", "ls"])
        self.assertEqual(code, 2)
        self.assertIn("--depth only applies to --parse-cli", err)

    def test_no_command_on_tty_is_usage_error(self) -> None:
        code, _, err = self.run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("no command given", err)

    def test_two_actions_rejected(self) -> None:
        code, _, err = self.run_cli(["--status", "--warm"])
        self.assertEqual(code, 2)
        self.assertIn("choose one action at a time", err)

    def test_selected_sections(self) -> None:
        namespace = argparse.Namespace(list=True, list_env=False)
        self.assertEqual(cli.selected_sections(namespace), ("files",))


class TestExplain(CliTestCase):
    def test_parse_only_human_output_no_llm(self) -> None:
        with mock.patch("she.cli.run_llm") as run_llm:
            code, out, _ = self.run_cli(["--parse-only", "--no-learn", "--", "frobnicate > out.txt"])
        run_llm.assert_not_called()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("BREAKDOWN\n  frobnicate > out.txt\n"))
        self.assertIn("written   out.txt", out)
        self.assertIn("frobnicate — no man page or help found", out)
        self.assertIn("no LLM (--parse-only)", out)

    def test_parse_error_exit_code(self) -> None:
        code, _, _ = self.run_cli(["--parse-only", "--no-learn", "--", 'echo "oops'])
        self.assertEqual(code, 3)

    def test_llm_failure_exit_code_wins(self) -> None:
        with mock.patch("she.cli.run_llm", side_effect=fake_llm(error="cannot reach Ollama")):
            code, out, err = self.run_cli(["--no-learn", "--", 'echo "oops'])
        self.assertEqual(code, 4)
        self.assertIn("(LLM unavailable: cannot reach Ollama)", out)
        self.assertIn("she: cannot reach Ollama", err)

    def test_summary_and_inferred_printed_after_deterministic(self) -> None:
        answer = LlmAnswer(summary="Removes x.", files=())
        with mock.patch("she.cli.run_llm", side_effect=fake_llm(answer)):
            code, out, _ = self.run_cli(["--no-learn", "--", "frobnicate x"])
        self.assertEqual(code, 0)
        self.assertLess(out.index("UNKNOWNS"), out.index("SUMMARY (LLM guess)\n  Removes x."))
        self.assertTrue(out.endswith("· model m@abc · instructions: none\n"))

    def test_json_single_object(self) -> None:
        answer = LlmAnswer(summary="Does it.")
        with mock.patch("she.cli.run_llm", side_effect=fake_llm(answer)):
            code, out, _ = self.run_cli(["--json", "--no-learn", "--", "frobnicate"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["schema"], "she/v1")
        self.assertEqual(data["summary"]["text"], "Does it.")
        self.assertEqual(data["timing_ms"]["llm"], 7)

    def test_stdin_json_is_a_list(self) -> None:
        code, out, _ = self.run_cli(["--json", "--parse-only", "--no-learn"], stdin="frob a\ncat <<EOF\nhi\nEOF\n")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([item["command"] for item in data], ["frob a", "cat <<EOF\nhi\nEOF"])
        self.assertIsNone(data[0]["summary"])

    def test_list_selects_files_in_json(self) -> None:
        code, out, _ = self.run_cli(["--json", "--list", "--parse-only", "--no-learn", "--", "frob > o"])
        self.assertEqual(json.loads(out), {
            "schema": "she/v1",
            "command": "frob > o",
            "files": [{"path": "o", "op": "write", "basis": "visible", "detail": "from > redirect"}],
        })

    def test_json_stream_events(self) -> None:
        answer = LlmAnswer(summary="Hi.")
        with mock.patch("she.cli.run_llm", side_effect=fake_llm(answer)):
            code, out, _ = self.run_cli(["--json-stream", "--no-learn", "--", "frob"])
        events = [json.loads(line)["event"] for line in out.splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(events, ["deterministic", "summary_chunk", "inferred", "done"])

    def test_missing_instructions_file_is_usage_error(self) -> None:
        code, _, err = self.run_cli(["--parse-only", "--instructions", "/nope/x.md", "--", "ls"])
        self.assertEqual(code, 2)
        self.assertIn("--instructions file not found: /nope/x.md", err)


class TestActions(CliTestCase):
    def test_show_instructions_none(self) -> None:
        code, out, _ = self.run_cli(["--show-instructions"])
        self.assertEqual((code, out), (0, "source: none\npath:   -\n"))

    def test_edit_global_returns_vim_code(self) -> None:
        with mock.patch("she.cli.edit_file", return_value=1) as edit:
            code, _, _ = self.run_cli(["--edit-global-instructions"])
        self.assertEqual(code, 1)
        self.assertEqual(edit.call_args.args[0].name, "instructions.md")

    def test_list_tools_empty_and_forget(self) -> None:
        self.assertEqual(self.run_cli(["--list-tools"])[1], "no learned tools\n")
        self.assertEqual(self.run_cli(["--forget", "uv@1.0"])[1], "forgot uv@1.0: 0 pages deleted\n")

    def test_clear_cache(self) -> None:
        code, out, _ = self.run_cli(["--clear-cache"])
        self.assertEqual(code, 0)
        self.assertIn("answer cache was already empty", out)

    def test_action_with_command_rejected(self) -> None:
        code, _, err = self.run_cli(["--status", "--", "ls"])
        self.assertEqual(code, 2)
        self.assertIn("--status does not take a command to explain", err)
