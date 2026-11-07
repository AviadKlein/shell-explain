from __future__ import annotations

import unittest
from typing import List, Tuple

from she.effects import Invocation, collect, command_file_effects, positionals
from she.parser import parse


def files_of(name: str, *args: str) -> List[Tuple[str, str]]:
    return [(effect.op, effect.path) for effect in command_file_effects(Invocation(name=name, args=args))]


def collect_for(command: str) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str, object]]]:
    parsed = parse(command)
    invocations = [
        Invocation(name=simple.name.text, args=tuple(word.text for word in simple.args))
        for simple in parsed.commands
        if simple.kind == "command"
    ]
    files, env = collect(parsed, invocations)
    return [(item.op, item.path) for item in files], [(item.op, item.name, item.value) for item in env]


class TestCommandRules(unittest.TestCase):
    def test_rm_deletes_positionals(self) -> None:
        self.assertEqual(files_of("rm", "-rf", "build", "--", "-weird"), [("delete", "build"), ("delete", "-weird")])

    def test_mkdir_touch_ln_create(self) -> None:
        self.assertEqual(files_of("mkdir", "-p", "a/b"), [("create", "a/b")])
        self.assertEqual(files_of("touch", "x"), [("create", "x")])
        self.assertEqual(files_of("ln", "-s", "target", "link"), [("create", "link")])

    def test_head_skips_count_value(self) -> None:
        self.assertEqual(files_of("head", "-n", "5", "f.txt"), [("read", "f.txt")])

    def test_cp_and_mv(self) -> None:
        self.assertEqual(files_of("cp", "-r", "a", "b", "dest"), [("read", "a"), ("read", "b"), ("write", "dest")])
        self.assertEqual(files_of("mv", "old", "new"), [("delete", "old"), ("write", "new")])

    def test_chmod_skips_mode(self) -> None:
        self.assertEqual(files_of("chmod", "+x", "run.sh"), [("chmod", "run.sh")])

    def test_tee_append(self) -> None:
        self.assertEqual(files_of("tee", "-a", "log"), [("append", "log")])
        self.assertEqual(files_of("tee", "log"), [("write", "log")])

    def test_sed_in_place_bsd_and_gnu(self) -> None:
        self.assertEqual(files_of("sed", "-i", "", "s/a/b/", "f"), [("write", "f")])
        self.assertEqual(files_of("sed", "-i.bak", "s/a/b/", "f"), [("write", "f")])
        self.assertEqual(files_of("sed", "-e", "s/a/b/", "f"), [("read", "f")])

    def test_grep_reads_files_after_pattern(self) -> None:
        self.assertEqual(files_of("grep", "-rn", "TODO", "src"), [("read", "src")])
        self.assertEqual(files_of("grep", "-e", "x", "a", "b"), [("read", "a"), ("read", "b")])

    def test_find_delete_glob(self) -> None:
        self.assertEqual(
            files_of("find", ".", "-name", "*.tmp", "-delete"), [("read", "."), ("delete", "./**/*.tmp")]
        )
        self.assertEqual(files_of("find", "/tmp/", "-delete"), [("read", "/tmp/"), ("delete", "/tmp/**")])

    def test_download_outputs(self) -> None:
        self.assertEqual(files_of("curl", "-o", "out.html", "https://x"), [("write", "out.html")])
        self.assertEqual(files_of("wget", "-O", "page", "https://x"), [("write", "page")])

    def test_ssh_identity_and_config(self) -> None:
        self.assertEqual(files_of("ssh", "-i", "key", "-F", "cfg", "host"), [("read", "key"), ("read", "cfg")])

    def test_dd(self) -> None:
        self.assertEqual(files_of("dd", "if=/dev/zero", "of=disk.img"), [("read", "/dev/zero"), ("write", "disk.img")])

    def test_unknown_command_has_no_rule(self) -> None:
        self.assertEqual(files_of("frobnicate", "a"), [])


class TestCollect(unittest.TestCase):
    def test_redirects(self) -> None:
        files, _ = collect_for("cmd < in > out 2>&1 >> log 2>/dev/null")
        self.assertEqual(files, [("read", "in"), ("write", "out"), ("append", "log")])

    def test_export_assignment_and_reads(self) -> None:
        _, env = collect_for("export A=1; B=2; echo $HOME $A")
        self.assertEqual(env, [("set", "B", "2"), ("set", "A", "1"), ("read", "HOME", None), ("read", "A", None)])

    def test_unset_and_cd(self) -> None:
        _, env = collect_for("unset FOO; cd /tmp")
        self.assertEqual(env, [("unset", "FOO", None), ("set", "PWD", "/tmp")])

    def test_loop_variable_is_not_an_env_read(self) -> None:
        _, env = collect_for('for f in *; do echo "$f"; done')
        self.assertEqual(env, [])

    def test_prefix_assignment(self) -> None:
        _, env = collect_for("X=1 make")
        self.assertEqual(env, [("set", "X", "1")])


class TestPositionals(unittest.TestCase):
    def test_double_dash_ends_options(self) -> None:
        self.assertEqual(positionals(["-a", "x", "--", "-b"]), ["x", "-b"])

    def test_value_options_swallow_next(self) -> None:
        self.assertEqual(positionals(["-n", "5", "f"], frozenset({"-n"})), ["f"])
