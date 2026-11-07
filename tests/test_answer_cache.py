from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from she.answer_cache import AnswerCache, CacheKeyParts, load_or_create_key
from she.models import LlmAnswer
from she.paths import Paths

DAY = 86400


def parts(**changes: object) -> CacheKeyParts:
    base = CacheKeyParts(
        template_version="1",
        command="rm -rf build",
        model_digest="abc",
        facts_text="facts",
        instructions_text="",
        num_predict=160,
    )
    return base.copy(**changes)


class TestAnswerCache(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.paths = Paths(cache_dir=root / "cache", state_dir=root / "state", config_dir=root / "config")
        self.cache = AnswerCache.from_paths(self.paths, environ={})

    def tearDown(self) -> None:
        self.cache.close()
        self.directory.cleanup()

    def test_round_trip_and_redaction(self) -> None:
        key = self.cache.key(parts())
        self.cache.put(key, LlmAnswer(summary="Uses " + "AKIA" + "ABCDEFGHIJKLMNOP"), "abc")
        self.assertEqual(self.cache.get(key), LlmAnswer(summary="Uses <REDACTED:aws-key>"))

    def test_every_part_changes_the_key(self) -> None:
        base = self.cache.key(parts())
        variants = [
            parts(template_version="2"),
            parts(command="rm -rf build "),
            parts(model_digest="abd"),
            parts(facts_text="facts!"),
            parts(instructions_text="be terse"),
            parts(num_predict=161),
        ]
        self.assertEqual(len({base, *(self.cache.key(variant) for variant in variants)}), 7)

    def test_length_prefix_prevents_boundary_collisions(self) -> None:
        self.assertNotEqual(
            parts(template_version="1a", command="b").message(), parts(template_version="1", command="ab").message()
        )

    def test_command_text_never_stored(self) -> None:
        key = self.cache.key(parts(command="secret-command-xyz"))
        self.cache.put(key, LlmAnswer(summary="ok"), "abc")
        self.cache.close()
        self.assertNotIn(b"secret-command-xyz", self.paths.answers_db.read_bytes())

    def test_files_are_private(self) -> None:
        key = self.cache.key(parts())
        self.cache.put(key, LlmAnswer(summary="ok"), "abc")
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.answers_db).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.key_file).st_mode), 0o600)
        self.assertEqual(len(self.paths.key_file.read_bytes()), 32)

    def test_expired_rows_ignored_and_deleted(self) -> None:
        old_key = self.cache.key(parts(command="old"))
        self.cache.put(old_key, LlmAnswer(summary="old"), "abc", now=1_000_000)
        self.assertIsNone(self.cache.get(old_key, now=1_000_000 + 8 * DAY))
        self.assertEqual(self.cache.get(old_key, now=1_000_000 + 6 * DAY), LlmAnswer(summary="old"))
        self.cache.put(self.cache.key(parts(command="new")), LlmAnswer(summary="new"), "abc", now=1_000_000 + 8 * DAY)
        self.assertEqual(self.cache.entry_count(), 1)

    def test_size_cap_evicts_oldest(self) -> None:
        small = AnswerCache(self.paths.answers_db, self.paths.key_file, ttl_seconds=7 * DAY, max_bytes=500)
        keys = [small.key(parts(command=str(index))) for index in range(5)]
        for index, key in enumerate(keys):
            small.put(key, LlmAnswer(summary="x" * 100), "abc", now=1_000_000 + index)
        survivors = [key for key in keys if small.get(key, now=1_000_010) is not None]
        self.assertEqual(survivors, keys[-len(survivors) :])
        self.assertLess(len(survivors), 5)
        small.close()

    def test_ttl_override_in_days(self) -> None:
        cache = AnswerCache.from_paths(self.paths, environ={"SHE_CACHE_TTL": "1"})
        key = cache.key(parts())
        cache.put(key, LlmAnswer(summary="ok"), "abc", now=1_000_000)
        self.assertIsNone(cache.get(key, now=1_000_000 + 2 * DAY))
        cache.close()

    def test_bad_env_override_raises(self) -> None:
        with self.assertRaises(ValueError) as context:
            AnswerCache.from_paths(self.paths, environ={"SHE_CACHE_MAX_MB": "lots"})
        self.assertEqual(str(context.exception), "expected a number in $SHE_CACHE_MAX_MB, got raw='lots'")

    def test_clear_deletes_file(self) -> None:
        self.cache.put(self.cache.key(parts()), LlmAnswer(summary="ok"), "abc")
        self.assertTrue(self.cache.clear())
        self.assertFalse(self.paths.answers_db.exists())

    def test_key_is_stable_across_instances(self) -> None:
        first = load_or_create_key(self.paths.key_file)
        self.assertEqual(load_or_create_key(self.paths.key_file), first)
