"""Resolve and render local man pages.

Resolution first scans the man directories directly (microseconds), because `man -w`
costs ~150 ms per call on macOS. `man -w NAME` is the fallback, and its "not found"
answers are cached for a day so unknown tools don't pay for it on every explain.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Self, Tuple

from she.store import PageStore
from she.textdoc import PageDoc, clean_text

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][\w.+-]*$")
SECTION_PATTERN = re.compile(r"\.(?P<section>[1-9][a-z]*)(?:\.gz)?$")
SECTIONS: Tuple[str, ...] = ("1", "8", "6")
EXTRA_MAN_DIRS: Tuple[str, ...] = (
    "/usr/share/man",
    "/usr/local/share/man",
    "/opt/homebrew/share/man",
    "/Library/Developer/CommandLineTools/usr/share/man",
    "/Applications/Xcode.app/Contents/Developer/usr/share/man",
)
COMMAND_TIMEOUT_SECONDS = 5
RENDER_WIDTH = 200


@dataclass(frozen=True, slots=True)
class ManPage:
    """A rendered and parsed man page.

    Attributes:
        name: The page name, e.g. "find" or "git-log".
        section: The manual section, e.g. "1".
        path: The page source file.
        doc: The parsed options, title and subcommands.
    """

    name: str
    section: str
    path: Path
    doc: PageDoc

    def copy(self, **changes: object) -> Self:
        return replace(self, **changes)

    @property
    def source(self) -> str:
        return f"man:{self.name}({self.section})"


def man_dirs(environ: Optional[dict] = None) -> Tuple[Path, ...]:
    """Directories searched for man pages: `MANPATH`, then dirs next to `PATH` entries, then defaults."""
    env = os.environ if environ is None else environ
    return _man_dirs(env.get("MANPATH", ""), env.get("PATH", ""))


@lru_cache(maxsize=4)
def _man_dirs(manpath: str, path: str) -> Tuple[Path, ...]:
    candidates: List[str] = [entry for entry in manpath.split(":") if entry]
    for entry in path.split(":"):
        if entry and os.path.isabs(entry):
            parent = os.path.dirname(entry.rstrip("/"))
            candidates.extend([os.path.join(parent, "share", "man"), os.path.join(parent, "man")])
    candidates.extend(EXTRA_MAN_DIRS)
    unique = dict.fromkeys(os.path.normpath(candidate) for candidate in candidates)
    return tuple(Path(candidate) for candidate in unique if os.path.isdir(candidate))


def find_page_file(name: str, directories: Tuple[Path, ...]) -> Optional[Path]:
    """Find `NAME.<section>` under `man<section>/` in the given directories (sections 1, 8, 6)."""
    for section in SECTIONS:
        for directory in directories:
            section_dir = directory / f"man{section}"
            for suffix in (f".{section}", f".{section}.gz"):
                candidate = section_dir / f"{name}{suffix}"
                if candidate.is_file():
                    return candidate
    return None


class ManPages:
    """Looks up man pages, rendering each file once and caching the result in `pages.db`."""

    def __init__(self, store: Optional[PageStore], directories: Tuple[Path, ...]) -> None:
        self.__store = store
        self.__directories = directories

    @classmethod
    def from_env(cls, store: Optional[PageStore]) -> Self:
        return cls(store, man_dirs())

    def resolve(self, name: str, fallback: bool = True) -> Optional[Path]:
        """The man page file for `name`, or None. Names with slashes or odd characters are refused.

        Args:
            name: Page name, e.g. "find" or "git-log".
            fallback: Also ask `man -w` (slow) when the directory scan finds nothing.
        """
        if not NAME_PATTERN.match(name):
            return None
        found = find_page_file(name, self.__directories)
        if found is not None or not fallback:
            return found
        if self.__store is not None and self.__store.is_man_missing(name):
            return None
        resolved = _man_w(name)
        if resolved is None and self.__store is not None:
            self.__store.mark_man_missing(name)
        return resolved

    def load(self, name: str, fallback: bool = True) -> Optional[ManPage]:
        """Resolve, render and parse the page for `name` (see `resolve` for `fallback`)."""
        path = self.resolve(name, fallback)
        if path is None:
            return None
        stat = path.stat()
        cached = self.__store.get_man(str(path), stat.st_mtime_ns, stat.st_size) if self.__store else None
        if cached is not None:
            doc = cached[1]
        else:
            text = render(path, name)
            if text is None:
                return None
            doc = PageDoc.from_text(text)
            if self.__store is not None:
                self.__store.put_man(str(path), stat.st_mtime_ns, stat.st_size, text, doc)
        match SECTION_PATTERN.search(path.name):
            case None:
                section = "1"
            case found:
                section = found.group("section")
        return ManPage(name=name, section=section, path=path, doc=doc)


def _man_w(name: str) -> Optional[Path]:
    try:
        completed = subprocess.run(
            ["man", "-w", name],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    lines = completed.stdout.strip().splitlines()
    if completed.returncode != 0 or not lines:
        return None
    path = Path(lines[0].strip())
    return path if path.is_file() else None


def render(path: Path, name: str) -> Optional[str]:
    """Render a man page to plain text with mandoc, falling back to `man NAME` with a cat pager."""
    mandoc = Path("/usr/bin/mandoc")
    environment = {**os.environ, "MANPAGER": "cat", "PAGER": "cat", "MANWIDTH": str(RENDER_WIDTH), "TERM": "dumb"}
    commands = (
        [[str(mandoc), "-T", "ascii", "-O", f"width={RENDER_WIDTH}", str(path)]] if mandoc.exists() else []
    ) + [["man", name]]
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
                stdin=subprocess.DEVNULL,
                env=environment,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        text = clean_text(completed.stdout.decode("utf-8", errors="replace"))
        if completed.returncode == 0 and text.strip():
            return text
    return None
