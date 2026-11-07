from __future__ import annotations

import unittest

from she.textdoc import PageDoc, clean_text, looks_like_help, summarize

BSD_MAN = """FIND(1)                General Commands Manual                FIND(1)

NAME
     find - walk a file hierarchy

DESCRIPTION
     The options are as follows:

     -E      Interpret regular expressions as extended (modern) regular expressions.  More text.

     -f path
             Add path to the list of paths that will be recursed into.

PRIMARIES
     -delete
             Delete found files and/or directories.  Always returns true.

             This second paragraph is ignored.

     -name pattern
             True if the last component of the pathname matches pattern.
"""

GNU_MAN = """GIT-LOG(1)                Git Manual                GIT-LOG(1)

NAME
       git-log - Show commit logs

OPTIONS
       -<number>, -n <number>, --max-count=<number>
           Limit the number of commits to output.

   Commit Formatting
       --pretty[=<format>], --format=<format>
           Pretty-print the contents of the commit logs in a given format. This
           mentions --pretty, --format, or --oneline in the body.

       --[no-]color
           Turn colour on or off.
"""

CLAP_HELP = """An extremely fast Python package manager.

Usage: uv [OPTIONS] <COMMAND>

Commands:
  run        Run a command or script
  pip        Manage Python packages with a pip-compatible interface
  help       Display documentation for a command

Cache options:
  -n, --no-cache               Avoid reading from or writing to the cache, instead using a temporary
                               directory for the duration of the operation [env: UV_NO_CACHE=]
      --cache-dir <CACHE_DIR>  Path to the cache directory [env: UV_CACHE_DIR=]

Global options:
  -q, --quiet...
          Use quiet output
"""

SSH_LIKE = """NAME
     ssh - OpenSSH remote login client

     -L [bind_address:]port:host:hostport
     -L local_socket:host:hostport
             Specifies that connections to the given TCP port are forwarded.

     -i identity_file
             Selects a file from which the identity is read.
"""

COBRA_GROUPED_HELP = """Databricks CLI

Usage:
  databricks [command]

Databricks Workspace
  fs                                     Filesystem related commands
  workspace                              The Workspace API allows you to list workspace objects.

Lakeflow
  jobs                                   The Jobs API allows you to create, edit, and delete jobs.
  pipelines                              The Pipelines API allows you to create pipelines.

AI/BI
  genie                                  Genie provides a no-code experience.

Additional Commands:
  auth                                   Authentication related commands
  completion                             Generate the autocompletion script for the specified shell

Flags:
      --debug            enable debug logging
  -h, --help             help for databricks
  -p, --profile string   ~/.databrickscfg profile

Use "databricks [command] --help" for more information about a command.
"""

JQ_MAN = """JQ(1)                                                                  JQ(1)

NAME
       jq - Command-line JSON processor

INVOKING JQ
       --compact-output / -c:

              By default, jq pretty-prints JSON output.

       --raw-output / -r:

\t      With this option, if the filter's result is a string then it will be written directly.

       --raw-output0:

              Like -r but jq will print NUL instead of newline.

       --arg name value:

              This option passes a value to the jq program as a predefined variable.
"""

MAN_WITH_PROSE_SECTIONS = """FOO(1)

NAME
     foo - do foo

DESCRIPTION
     foo reads the input and writes the output.
     It never does anything else.

EXIT STATUS
     0  success
"""


class TestPageDocFromText(unittest.TestCase):
    def test_bsd_man_page_inline_and_following_descriptions(self) -> None:
        doc = PageDoc.from_text(BSD_MAN)
        self.assertEqual(doc.title, "find - walk a file hierarchy")
        self.assertEqual(doc.description, "walk a file hierarchy")
        self.assertEqual(doc.lookup("-E").summary, "Interpret regular expressions as extended (modern) regular expressions")
        self.assertTrue(doc.lookup("-f").takes_arg)
        self.assertTrue(doc.lookup("-name").takes_arg)
        self.assertFalse(doc.lookup("-delete").takes_arg)
        self.assertEqual(doc.lookup("-delete").description, "Delete found files and/or directories.  Always returns true.")

    def test_gnu_man_page_flag_forms(self) -> None:
        doc = PageDoc.from_text(GNU_MAN)
        self.assertEqual(doc.lookup("-n").flags, ("-n", "--max-count"))
        self.assertTrue(doc.lookup("--max-count").takes_arg)
        self.assertIs(doc.lookup("--format"), doc.lookup("--pretty"))
        self.assertTrue(doc.lookup("--pretty").takes_arg)
        self.assertEqual(doc.lookup("--no-color").summary, "Turn colour on or off")
        self.assertIsNone(doc.lookup("--oneline"))

    def test_clap_help_options_and_subcommands(self) -> None:
        doc = PageDoc.from_text(CLAP_HELP)
        self.assertEqual(doc.title, "An extremely fast Python package manager.")
        self.assertEqual(doc.lookup("--no-cache").flags, ("-n", "--no-cache"))
        self.assertTrue(doc.lookup("--cache-dir").takes_arg)
        self.assertEqual(doc.lookup("-q").flags, ("-q", "--quiet"))
        self.assertEqual(
            doc.subcommands,
            (
                ("run", "Run a command or script"),
                ("pip", "Manage Python packages with a pip-compatible interface"),
                ("help", "Display documentation for a command"),
            ),
        )
        self.assertEqual(doc.subcommand_help("pip"), "Manage Python packages with a pip-compatible interface")

    def test_cobra_command_groups_without_colon(self) -> None:
        doc = PageDoc.from_text(COBRA_GROUPED_HELP)
        self.assertEqual(
            [name for name, _ in doc.subcommands],
            ["fs", "workspace", "jobs", "pipelines", "genie", "auth", "completion"],
        )
        self.assertEqual(doc.subcommand_help("jobs"), "The Jobs API allows you to create, edit, and delete jobs.")
        self.assertTrue(doc.lookup("--profile").takes_arg)

    def test_free_form_headings_need_command_rows(self) -> None:
        self.assertEqual(PageDoc.from_text(MAN_WITH_PROSE_SECTIONS).subcommands, ())
        self.assertEqual(PageDoc.from_text(CLAP_HELP).subcommand_names, frozenset({"run", "pip", "help"}))

    def test_slash_separated_flags_with_trailing_colon(self) -> None:
        doc = PageDoc.from_text(JQ_MAN)
        self.assertEqual(doc.lookup("-r").flags, ("--raw-output", "-r"))
        self.assertFalse(doc.lookup("-r").takes_arg)
        self.assertTrue(doc.lookup("-r").summary.startswith("With this option"))
        self.assertFalse(doc.lookup("-c").takes_arg)
        self.assertFalse(doc.lookup("--raw-output0").takes_arg)
        self.assertTrue(doc.lookup("--arg").takes_arg)

    def test_consecutive_headers_share_one_description(self) -> None:
        doc = PageDoc.from_text(SSH_LIKE)
        self.assertEqual(doc.lookup("-L").flags, ("-L",))
        self.assertTrue(doc.lookup("-L").takes_arg)
        self.assertEqual(doc.lookup("-L").summary, "Specifies that connections to the given TCP port are forwarded")
        self.assertTrue(doc.lookup("-i").takes_arg)

    def test_json_round_trip_keeps_index(self) -> None:
        doc = PageDoc.from_text(CLAP_HELP)
        restored = PageDoc.from_json(doc.to_json())
        self.assertEqual(restored, doc)
        self.assertEqual(restored.lookup("-n"), doc.lookup("-n"))

    def test_copy_rebuilds_index(self) -> None:
        doc = PageDoc.from_text(CLAP_HELP).copy(options=())
        self.assertIsNone(doc.lookup("-n"))

    def test_non_string_raises(self) -> None:
        with self.assertRaises(TypeError) as context:
            PageDoc.from_text(b"bytes")  # type: ignore[arg-type]
        self.assertEqual(str(context.exception), "expected str page text, got bytes")


class TestHelpers(unittest.TestCase):
    def test_clean_text_strips_overstrike_and_ansi(self) -> None:
        self.assertEqual(clean_text("N\x08NA\x08AM\x08ME\x08E \x1b[1mbold\x1b[0m\r\n"), "NAME bold\n")

    def test_looks_like_help(self) -> None:
        self.assertTrue(looks_like_help("Usage: tool [options]"))
        self.assertTrue(looks_like_help("  -a  all\n  -b  bee\n"))
        self.assertFalse(looks_like_help("tool 1.2.3\n"))

    def test_summarize_first_sentence_and_limit(self) -> None:
        self.assertEqual(summarize("Do a thing.  Then more."), "Do a thing")
        self.assertEqual(len(summarize("word " * 100)), 160)
        self.assertTrue(summarize("word " * 100).endswith("…"))
