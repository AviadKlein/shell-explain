"""SQLite help-page cache (`pages.db`): learned tools, fingerprints, pages and man renders."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Self, Tuple

from she.paths import ensure_private_file
from she.textdoc import PARSER_VERSION, PageDoc

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    path TEXT NOT NULL,
    learned_at INTEGER NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS fingerprints (
    path TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
    tool TEXT NOT NULL,
    version TEXT NOT NULL,
    subpath TEXT NOT NULL,
    text TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    PRIMARY KEY (tool, version, subpath)
);
CREATE TABLE IF NOT EXISTS missing (
    tool TEXT NOT NULL,
    version TEXT NOT NULL,
    subpath TEXT NOT NULL,
    PRIMARY KEY (tool, version, subpath)
);
CREATE TABLE IF NOT EXISTS man_pages (
    path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    text TEXT NOT NULL,
    parsed_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS man_missing (
    name TEXT PRIMARY KEY,
    checked_at INTEGER NOT NULL
);
"""
MAN_MISSING_TTL_SECONDS = 24 * 3600


@dataclass(frozen=True, slots=True)
class StoredVersion:
    """One learned version of a tool, as listed by `--has-pages` / `--list-tools`."""

    name: str
    version: str
    path: str
    page_count: int
    learned_at: int


def subpath_key(subpath: Tuple[str, ...]) -> str:
    return " ".join(subpath)


class PageStore:
    """Thin wrapper around `pages.db`. The connection is opened lazily on first use."""

    def __init__(self, db_path: Path) -> None:
        self.__db_path = db_path
        self.__connection: Optional[sqlite3.Connection] = None

    @classmethod
    def from_path(cls, db_path: Path) -> Self:
        """Open (lazily) the store at `db_path`.

        Raises:
            TypeError: If `db_path` is not a Path.
        """
        if not isinstance(db_path, Path):
            raise TypeError(f"expected Path for db_path, got {type(db_path).__name__}")
        return cls(db_path)

    @property
    def path(self) -> Path:
        return self.__db_path

    def _db(self) -> sqlite3.Connection:
        if self.__connection is None:
            ensure_private_file(self.__db_path)
            connection = sqlite3.connect(self.__db_path)
            connection.executescript(SCHEMA)
            self.__connection = connection
            self._reparse_if_stale(connection)
        return self.__connection

    @staticmethod
    def _reparse_if_stale(connection: sqlite3.Connection) -> None:
        """Re-parse stored pages from their raw text when they were parsed by an older parser."""
        if connection.execute("PRAGMA user_version").fetchone()[0] == PARSER_VERSION:
            return
        with connection as db:
            for table, key in (("pages", "rowid"), ("man_pages", "rowid")):
                rows = db.execute(f"SELECT {key}, text FROM {table}").fetchall()
                db.executemany(
                    f"UPDATE {table} SET parsed_json = ? WHERE {key} = ?",
                    [(PageDoc.from_text(text).to_json(), row_id) for row_id, text in rows],
                )
            db.execute("DELETE FROM missing")
            db.execute(f"PRAGMA user_version = {PARSER_VERSION}")

    def close(self) -> None:
        if self.__connection is not None:
            self.__connection.close()
            self.__connection = None

    # -- fingerprints and versions ------------------------------------------------

    def version_for_fingerprint(self, path: str, fingerprint: str) -> Optional[str]:
        row = self._db().execute(
            "SELECT version FROM fingerprints WHERE path = ? AND fingerprint = ?", (path, fingerprint)
        ).fetchone()
        return row[0] if row else None

    def record_fingerprint(self, path: str, fingerprint: str, name: str, version: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO fingerprints (path, fingerprint, name, version) VALUES (?, ?, ?, ?)",
                (path, fingerprint, name, version),
            )

    def add_tool(self, name: str, version: str, path: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO tools (name, version, path, learned_at) VALUES (?, ?, ?, ?)",
                (name, version, path, int(time.time())),
            )

    # -- pages ----------------------------------------------------------------------

    def get_page(self, tool: str, version: str, subpath: Tuple[str, ...]) -> Optional[PageDoc]:
        row = self._db().execute(
            "SELECT parsed_json FROM pages WHERE tool = ? AND version = ? AND subpath = ?",
            (tool, version, subpath_key(subpath)),
        ).fetchone()
        return PageDoc.from_json(row[0]) if row else None

    def put_page(self, tool: str, version: str, subpath: Tuple[str, ...], text: str, doc: PageDoc) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO pages (tool, version, subpath, text, parsed_json) VALUES (?, ?, ?, ?, ?)",
                (tool, version, subpath_key(subpath), text, doc.to_json()),
            )

    def is_missing(self, tool: str, version: str, subpath: Tuple[str, ...]) -> bool:
        row = self._db().execute(
            "SELECT 1 FROM missing WHERE tool = ? AND version = ? AND subpath = ?",
            (tool, version, subpath_key(subpath)),
        ).fetchone()
        return row is not None

    def mark_missing(self, tool: str, version: str, subpath: Tuple[str, ...]) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO missing (tool, version, subpath) VALUES (?, ?, ?)",
                (tool, version, subpath_key(subpath)),
            )

    def page_count(self, tool: str, version: str) -> int:
        row = self._db().execute(
            "SELECT COUNT(*) FROM pages WHERE tool = ? AND version = ?", (tool, version)
        ).fetchone()
        return int(row[0])

    def stored_versions(self, name: Optional[str] = None) -> List[StoredVersion]:
        """Learned tool versions (all tools, or one), newest first within each tool."""
        query = (
            "SELECT t.name, t.version, t.path, t.learned_at,"
            " (SELECT COUNT(*) FROM pages p WHERE p.tool = t.name AND p.version = t.version)"
            " FROM tools t {where} ORDER BY t.name, t.learned_at DESC"
        )
        rows = (
            self._db().execute(query.format(where="WHERE t.name = ?"), (name,)).fetchall()
            if name is not None
            else self._db().execute(query.format(where="")).fetchall()
        )
        return [
            StoredVersion(name=row[0], version=row[1], path=row[2], learned_at=int(row[3]), page_count=int(row[4]))
            for row in rows
        ]

    def forget(self, name: str, version: Optional[str] = None) -> int:
        """Delete stored pages of a tool (one version, or all). Returns the number of pages deleted."""
        where, params = ("tool = ? AND version = ?", (name, version)) if version else ("tool = ?", (name,))
        tool_where = where.replace("tool =", "name =")
        with self._db() as db:
            deleted = db.execute(f"DELETE FROM pages WHERE {where}", params).rowcount
            db.execute(f"DELETE FROM missing WHERE {where}", params)
            db.execute(f"DELETE FROM tools WHERE {tool_where}", params)
            db.execute(f"DELETE FROM fingerprints WHERE {tool_where}", params)
        return int(deleted)

    # -- man pages ------------------------------------------------------------------

    def get_man(self, path: str, mtime_ns: int, size: int) -> Optional[Tuple[str, PageDoc]]:
        row = self._db().execute(
            "SELECT text, parsed_json FROM man_pages WHERE path = ? AND mtime_ns = ? AND size = ?",
            (path, mtime_ns, size),
        ).fetchone()
        return (row[0], PageDoc.from_json(row[1])) if row else None

    def put_man(self, path: str, mtime_ns: int, size: int, text: str, doc: PageDoc) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO man_pages (path, mtime_ns, size, text, parsed_json) VALUES (?, ?, ?, ?, ?)",
                (path, mtime_ns, size, text, doc.to_json()),
            )

    def is_man_missing(self, name: str) -> bool:
        row = self._db().execute("SELECT checked_at FROM man_missing WHERE name = ?", (name,)).fetchone()
        return row is not None and time.time() - int(row[0]) < MAN_MISSING_TTL_SECONDS

    def mark_man_missing(self, name: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO man_missing (name, checked_at) VALUES (?, ?)", (name, int(time.time()))
            )
