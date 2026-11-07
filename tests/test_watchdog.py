from __future__ import annotations

import plistlib
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from she import watchdog
from she.paths import Paths


class TestTick(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.paths = Paths(cache_dir=root / "c", state_dir=root / "s", config_dir=root / "g")
        self.paths.state_dir.mkdir()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_no_pid_file_means_not_ours(self) -> None:
        self.assertEqual(watchdog.tick(self.paths, {}).action, "no-pid-file")

    def test_idle_then_stopped_after_threshold(self) -> None:
        self.paths.ollama_pid_file.write_text("4242\n")
        with mock.patch("she.watchdog.is_she_ollama", return_value=True), mock.patch(
            "she.watchdog.loaded_model_count", return_value=0
        ), mock.patch("she.watchdog.os.kill") as kill:
            self.assertEqual(watchdog.tick(self.paths, {}, now=1000.0).action, "idle-started")
            self.assertEqual(watchdog.tick(self.paths, {}, now=1000.0 + 29 * 60).action, "idle")
            self.assertEqual(watchdog.tick(self.paths, {}, now=1000.0 + 31 * 60).action, "stopped")
        kill.assert_called_once_with(4242, signal.SIGTERM)
        self.assertFalse(self.paths.ollama_pid_file.exists())

    def test_loaded_model_resets_idle_clock(self) -> None:
        self.paths.ollama_pid_file.write_text("4242\n")
        self.paths.watchdog_state_file.write_text("1.0\n")
        with mock.patch("she.watchdog.is_she_ollama", return_value=True), mock.patch(
            "she.watchdog.loaded_model_count", return_value=1
        ):
            self.assertEqual(watchdog.tick(self.paths, {}, now=99999.0).action, "busy")
        self.assertFalse(self.paths.watchdog_state_file.exists())

    def test_foreign_process_is_left_alone(self) -> None:
        self.paths.ollama_pid_file.write_text("4242\n")
        with mock.patch("she.watchdog.is_she_ollama", return_value=False), mock.patch("she.watchdog.os.kill") as kill:
            self.assertEqual(watchdog.tick(self.paths, {}).action, "not-running")
        kill.assert_not_called()

    def test_bad_idle_env_raises(self) -> None:
        with self.assertRaises(ValueError) as context:
            watchdog.idle_minutes({"SHE_SERVER_IDLE": "-5"})
        self.assertEqual(str(context.exception), "expected positive minutes in $SHE_SERVER_IDLE, got value=-5.0")


class TestPlist(unittest.TestCase):
    def test_content(self) -> None:
        data = plistlib.loads(watchdog.plist_content("/usr/local/bin/she", {"SHE_SERVER_IDLE": "10"}))
        self.assertEqual(data["Label"], "com.she.watchdog")
        self.assertEqual(data["ProgramArguments"], ["/usr/local/bin/she", "--watchdog-tick"])
        self.assertEqual(data["StartInterval"], 300)
        self.assertEqual(data["EnvironmentVariables"], {"SHE_SERVER_IDLE": "10"})
