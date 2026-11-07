from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from she.helppages import HelpLearner, help_probes, parse_version
from she.sandbox import ProbeResult, sandbox_profile
from she.store import PageStore
from she.textdoc import PageDoc

ROOT_HELP = "Tool.\n\nUsage: tool <COMMAND>\n\nCommands:\n  pip   Manage packages\n  run   Run things\n\nOptions:\n  -q, --quiet  Be quiet\n  -v  Verbose\n"
PIP_HELP = "Manage packages\n\nUsage: tool pip <COMMAND>\n\nCommands:\n  install  Install packages\n"
INSTALL_HELP = "Install packages\n\nUsage: tool pip install\n\nOptions:\n  -r, --requirements <FILE>  Read requirements\n  -e  Editable\n"


class FakeSandbox:
    """Answers probes from a table; records every call."""

    def __init__(self, responses: Dict[Tuple[str, ...], ProbeResult]) -> None:
        self.responses = responses
        self.calls: List[Tuple[str, ...]] = []

    def run(self, binary: Path, args: Sequence[str]) -> ProbeResult:
        self.calls.append(tuple(args))
        return self.responses.get(tuple(args), ProbeResult(returncode=1, output="error: unknown"))


class TestHelpLearner(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.tool = self.bin / "tool"
        self.tool.write_bytes(b"\xcf\xfa\xed\xfe binary")
        self.tool.chmod(0o755)
        self.cwd = root / "work"
        self.cwd.mkdir()
        self.store = PageStore.from_path(root / "pages.db")
        self.sandbox = FakeSandbox(
            {
                ("--version",): ProbeResult(returncode=0, output="tool 1.2.3 (abc)\n"),
                ("--help",): ProbeResult(returncode=0, output=ROOT_HELP),
                ("pip", "--help"): ProbeResult(returncode=0, output=PIP_HELP),
                ("pip", "install", "--help"): ProbeResult(returncode=0, output=INSTALL_HELP),
            }
        )
        self.learner = HelpLearner(self.store, self.sandbox, self.cwd, str(self.bin))  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_on_demand_chain_follows_used_subcommands(self) -> None:
        pages = self.learner.chain("tool", ["pip", "install", "requests"], learn=True)
        self.assertEqual([page.source for page in pages], ["help:tool@1.2.3", "help:tool pip@1.2.3", "help:tool pip install@1.2.3"])
        self.assertEqual(self.sandbox.calls, [("--version",), ("--help",), ("pip", "--help"), ("pip", "install", "--help")])

    def test_fingerprint_reuse_runs_nothing(self) -> None:
        self.learner.chain("tool", ["pip"], learn=True)
        fresh_sandbox = FakeSandbox({})
        again = HelpLearner(self.store, fresh_sandbox, self.cwd, str(self.bin))  # type: ignore[arg-type]
        self.assertEqual(len(again.chain("tool", ["pip"], learn=True)), 2)
        self.assertEqual(fresh_sandbox.calls, [])

    def test_no_learn_returns_nothing_for_unknown_tool(self) -> None:
        self.assertEqual(self.learner.chain("tool", [], learn=False), [])
        self.assertEqual(self.sandbox.calls, [("--version",)])

    def test_tools_in_cwd_are_never_learned(self) -> None:
        inside = self.cwd / "local-tool"
        inside.write_bytes(b"x")
        inside.chmod(0o755)
        learner = HelpLearner(self.store, self.sandbox, self.cwd, f"{self.cwd}:{self.bin}")  # type: ignore[arg-type]
        self.assertIsNone(learner.locate("local-tool"))
        self.assertIsNone(learner.locate("./tool"))
        self.assertEqual(learner.locate("tool"), self.tool)

    def test_crawl_and_report(self) -> None:
        report = self.learner.crawl("tool", depth=3)
        self.assertEqual((report.version, report.stored, report.missing), ("1.2.3", 3, 1))
        pages_report = self.learner.report("tool")
        self.assertEqual([(item.version, item.page_count) for item in pages_report.stored], [("1.2.3", 3)])
        self.assertEqual(self.store.forget("tool"), 3)

    def test_chain_follows_cobra_command_groups(self) -> None:
        grouped_root = "Tool CLI\n\nUsage:\n  tool [command]\n\nLakeflow\n  jobs      The Jobs API.\n\nFlags:\n  -h, --help  help\n  -p, --profile string  profile\n"
        jobs_help = "Jobs.\n\nUsage:\n  tool jobs [command]\n\nCreate and manage jobs\n  run-now   Trigger a new job run.\n"
        run_now_help = "Trigger a new job run.\n\nUsage:\n  tool jobs run-now JOB_ID [flags]\n\nFlags:\n      --json JSON   either inline JSON string or @path/to/file.json with request body\n      --no-wait     do not wait\n"
        self.sandbox.responses[("--help",)] = ProbeResult(returncode=0, output=grouped_root)
        self.sandbox.responses[("jobs", "--help")] = ProbeResult(returncode=0, output=jobs_help)
        self.sandbox.responses[("jobs", "run-now", "--help")] = ProbeResult(returncode=0, output=run_now_help)
        pages = self.learner.chain("tool", ["jobs", "run-now"], learn=True)
        self.assertEqual([page.source for page in pages][-1], "help:tool jobs run-now@1.2.3")
        self.assertTrue(pages[-1].doc.lookup("--json").takes_arg)

    def test_pages_parsed_by_older_parser_are_reparsed(self) -> None:
        self.learner.chain("tool", ["pip"], learn=True)
        db = self.store._db()
        with db:
            db.execute("UPDATE pages SET parsed_json = ?", (PageDoc().to_json(),))
            db.execute("PRAGMA user_version = 0")
        self.store.close()
        self.assertEqual(self.store.get_page("tool", "1.2.3", ()).subcommand_names, frozenset({"pip", "run"}))

    def test_store_file_is_private(self) -> None:
        self.store.page_count("tool", "1.2.3")
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_crawl_unknown_tool_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, r"^'nope' is not a command on PATH"):
            self.learner.crawl("nope")

    def test_shim_always_probes_version(self) -> None:
        self.tool.write_text("#!/bin/sh\nexec real \"$@\"\n")
        self.learner.resolve("tool")
        fresh = HelpLearner(self.store, self.sandbox, self.cwd, str(self.bin))  # type: ignore[arg-type]
        fresh.resolve("tool")
        self.assertEqual(self.sandbox.calls.count(("--version",)), 2)


class TestHelpers(unittest.TestCase):
    def test_parse_version(self) -> None:
        self.assertEqual(parse_version("uv 0.11.23 (3cdf50e09 2026-06-19)"), "0.11.23")
        self.assertEqual(parse_version("git version 2.47.1\n"), "2.47.1")
        self.assertEqual(parse_version("v1.2.3-beta.1"), "1.2.3-beta.1")
        self.assertIsNone(parse_version("no numbers here"))

    def test_help_probes_order(self) -> None:
        self.assertEqual(help_probes(()), (("--help",), ("help",), ("-h",)))
        self.assertEqual(help_probes(("pip",)), (("pip", "--help"), ("help", "pip"), ("pip", "-h")))

    def test_sandbox_profile_denies_network_and_writes(self) -> None:
        profile = sandbox_profile(Path("/private/tmp/scratch"))
        self.assertIn("(deny network*)", profile)
        self.assertIn("(deny file-write*)", profile)
        self.assertIn('(subpath "/private/tmp/scratch")', profile)
        self.assertIn('(literal "/dev/null")', profile)


@unittest.skipUnless(os.access("/usr/bin/sandbox-exec", os.X_OK), "sandbox-exec not available")
class TestRealSandbox(unittest.TestCase):
    def test_writes_outside_scratch_fail(self) -> None:
        from she.sandbox import Sandbox

        target = Path(tempfile.gettempdir()) / "she-sandbox-test-should-not-exist"
        result = Sandbox().run(Path("/usr/bin/touch"), [str(target)])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())
