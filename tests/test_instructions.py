from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from she.instructions import Instructions, edit_file
from she.paths import Paths


class TestInstructionsResolve(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.paths = Paths(cache_dir=self.root / "c", state_dir=self.root / "s", config_dir=self.root / "config")
        self.paths.config_dir.mkdir()
        self.paths.global_instructions.write_text("global text")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write_local(self, text: str) -> Path:
        local = Paths.local_instructions(self.cwd)
        local.parent.mkdir(exist_ok=True)
        local.write_text(text)
        return local

    def test_global_when_nothing_else(self) -> None:
        chosen = Instructions.resolve(None, {}, self.cwd, self.paths)
        self.assertEqual((chosen.source, chosen.text), ("global", "global text"))

    def test_local_beats_global(self) -> None:
        self.write_local("local text")
        self.assertEqual(Instructions.resolve(None, {}, self.cwd, self.paths).text, "local text")

    def test_empty_local_still_wins(self) -> None:
        self.write_local("")
        chosen = Instructions.resolve(None, {}, self.cwd, self.paths)
        self.assertEqual((chosen.source, chosen.text), ("local", ""))

    def test_local_not_found_in_parent(self) -> None:
        self.write_local("local text")
        child = self.cwd / "sub"
        child.mkdir()
        self.assertEqual(Instructions.resolve(None, {}, child, self.paths).source, "global")

    def test_env_text_and_env_file(self) -> None:
        self.write_local("local text")
        self.assertEqual(Instructions.resolve(None, {"SHE_INSTRUCTIONS": "be terse"}, self.cwd, self.paths).text, "be terse")
        env_file = self.root / "env.md"
        env_file.write_text("from env file")
        chosen = Instructions.resolve(None, {"SHE_INSTRUCTIONS": str(env_file)}, self.cwd, self.paths)
        self.assertEqual((chosen.source, chosen.path, chosen.text), ("ad-hoc", env_file, "from env file"))

    def test_flag_beats_env(self) -> None:
        flag_file = self.root / "flag.md"
        flag_file.write_text("from flag")
        chosen = Instructions.resolve(flag_file, {"SHE_INSTRUCTIONS": "env"}, self.cwd, self.paths)
        self.assertEqual((chosen.source, chosen.text), ("ad-hoc", "from flag"))

    def test_missing_flag_file_raises(self) -> None:
        missing = self.root / "nope.md"
        with self.assertRaises(FileNotFoundError) as context:
            Instructions.resolve(missing, {}, self.cwd, self.paths)
        self.assertEqual(str(context.exception), f"--instructions file not found: {missing}")

    def test_none(self) -> None:
        self.paths.global_instructions.unlink()
        chosen = Instructions.resolve(None, {}, self.cwd, self.paths)
        self.assertEqual((chosen.source, chosen.path, chosen.text), ("none", None, ""))
        self.assertEqual(chosen.info.model_dump(), {"source": "none", "path": None})


class TestEditFile(unittest.TestCase):
    def test_creates_file_and_returns_vim_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new" / "instructions.md"
            with mock.patch("she.instructions.shutil.which", return_value="/usr/bin/vim"), mock.patch(
                "she.instructions.subprocess.call", return_value=3
            ) as call:
                self.assertEqual(edit_file(target), 3)
            self.assertTrue(target.is_file())
            call.assert_called_once_with(["/usr/bin/vim", str(target)])

    def test_missing_vim_raises(self) -> None:
        with mock.patch("she.instructions.shutil.which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, r"^vim is not on PATH"):
                edit_file(Path("/tmp/x.md"))
