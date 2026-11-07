"""The answer cache (contract §7): HMAC-keyed, redacted LLM answers in `answers.db` (mode 600)."""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional, Self

from she.models import LlmAnswer
from she.paths import Paths, ensure_private_dir, ensure_private_file
from she.redact import redact_answer

KEY_BYTES = 32
DEFAULT_TTL_DAYS = 7.0
DEFAULT_MAX_MB = 50.0
SECONDS_PER_DAY = 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS answers (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    model_digest TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS answers_created_at ON answers (created_at);
"""


@dataclass(frozen=True, slots=True)
class CacheKeyParts:
    """Everything an answer depends on (contract §7.1). Hashed, never stored."""

    template_version: str
    command: str
    model_digest: str
    facts_text: str
    instructions_text: str
    num_predict: int

    def copy(self, **changes: object) -> Self:
        return replace(self, **changes)

    def message(self) -> bytes:
        """Length-prefixed concatenation, so no two different part tuples share a message."""
        parts = (
            self.template_version.encode("utf-8"),
            self.command.encode("utf-8"),
            self.model_digest.encode("utf-8"),
            hashlib.sha256(self.facts_text.encode("utf-8")).digest(),
            hashlib.sha256(self.instructions_text.encode("utf-8")).digest(),
            str(self.num_predict).encode("ascii"),
        )
        return b"".join(len(part).to_bytes(8, "big") + part for part in parts)


def load_or_create_key(path: Path) -> bytes:
    """The machine key: 32 random bytes at `path` (mode 600), created on first use."""
    if path.exists() and path.stat().st_size == KEY_BYTES:
        return path.read_bytes()
    ensure_private_dir(path.parent)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(os.urandom(KEY_BYTES))
    os.chmod(path, 0o600)
    return path.read_bytes()


def _env_number(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"expected a number in ${name}, got {raw=}") from error
    if value <= 0:
        raise ValueError(f"expected a positive number in ${name}, got {value=}")
    return value


class AnswerCache:
    """Stores redacted `LlmAnswer`s under HMAC keys, with TTL expiry and a size cap."""

    def __init__(self, db_path: Path, key_path: Path, ttl_seconds: float, max_bytes: int) -> None:
        self.__db_path = db_path
        self.__key_path = key_path
        self.__ttl_seconds = ttl_seconds
        self.__max_bytes = max_bytes
        self.__connection: Optional[sqlite3.Connection] = None
        self.__machine_key: Optional[bytes] = None

    @classmethod
    def from_paths(cls, paths: Paths, environ: Optional[Mapping[str, str]] = None) -> Self:
        """Cache under `paths`, with `SHE_CACHE_TTL` (days) and `SHE_CACHE_MAX_MB` overrides.

        Raises:
            ValueError: If an override is not a positive number.
        """
        env = os.environ if environ is None else environ
        ttl_days = _env_number(env, "SHE_CACHE_TTL", DEFAULT_TTL_DAYS)
        max_mb = _env_number(env, "SHE_CACHE_MAX_MB", DEFAULT_MAX_MB)
        return cls(paths.answers_db, paths.key_file, ttl_days * SECONDS_PER_DAY, int(max_mb * 1024 * 1024))

    def _db(self) -> sqlite3.Connection:
        if self.__connection is None:
            ensure_private_file(self.__db_path)
            connection = sqlite3.connect(self.__db_path)
            connection.executescript(SCHEMA)
            self.__connection = connection
        return self.__connection

    def close(self) -> None:
        if self.__connection is not None:
            self.__connection.close()
            self.__connection = None

    def key(self, parts: CacheKeyParts) -> str:
        if self.__machine_key is None:
            self.__machine_key = load_or_create_key(self.__key_path)
        return hmac.new(self.__machine_key, parts.message(), hashlib.sha256).hexdigest()

    def get(self, key: str, now: Optional[float] = None) -> Optional[LlmAnswer]:
        """The cached answer for `key`, ignoring rows older than the TTL."""
        current = time.time() if now is None else now
        row = self._db().execute(
            "SELECT value FROM answers WHERE key = ? AND created_at >= ?", (key, int(current - self.__ttl_seconds))
        ).fetchone()
        return LlmAnswer.model_validate_json(row[0]) if row else None

    def put(self, key: str, answer: LlmAnswer, model_digest: str, now: Optional[float] = None) -> None:
        """Store a redacted copy; in the same transaction delete expired rows and evict to the size cap."""
        current = int(time.time() if now is None else now)
        value = redact_answer(answer).model_dump_json()
        with self._db() as db:
            db.execute("DELETE FROM answers WHERE created_at < ?", (int(current - self.__ttl_seconds),))
            db.execute(
                "INSERT OR REPLACE INTO answers (key, value, model_digest, created_at) VALUES (?, ?, ?, ?)",
                (key, value, model_digest, current),
            )
            rows = db.execute(
                "SELECT key, length(key) + length(value) + length(model_digest) FROM answers"
                " ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
            total = 0
            evict = []
            for row_key, size in rows:
                total += int(size)
                if total > self.__max_bytes:
                    evict.append((row_key,))
            db.executemany("DELETE FROM answers WHERE key = ?", evict)

    def entry_count(self) -> int:
        return int(self._db().execute("SELECT COUNT(*) FROM answers").fetchone()[0])

    @property
    def path(self) -> Path:
        return self.__db_path

    def clear(self) -> bool:
        """Delete the cache file. Returns True if there was one."""
        self.close()
        existed = self.__db_path.exists()
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{self.__db_path}{suffix}").unlink(missing_ok=True)
        return existed
