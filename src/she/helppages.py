"""Learn tools' `--help` pages (contract §5): resolve, fingerprint, version-probe, crawl, store."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Deque, List, Optional, Self, Sequence, Tuple

from she.sandbox import ProbeResult, Sandbox
from she.store import PageStore, StoredVersion
from she.textdoc import PageDoc, looks_like_help

VERSION_PATTERN = re.compile(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)*(?:[-+~][0-9A-Za-z.]+)?)")
VERSION_PROBES: Tuple[Tuple[str, ...], ...] = (("--version",), ("-V",), ("version",))
VERSION_SCAN_LINES = 3
DEFAULT_DEPTH = 3
DEFAULT_MAX_PAGES = 500
SUBCOMMAND_NAME = re.compile(r"^[a-z][\w.-]*$")


@dataclass(frozen=True, slots=True)
class HelpPage:
    """One stored help page: `tool [subpath...] --help` for an installed version."""

    tool: str
    version: str
    subpath: Tuple[str, ...]
    doc: PageDoc

    def copy(self, **changes: object) -> Self:
        return replace(self, **changes)

    @property
    def source(self) -> str:
        return f"help:{' '.join((self.tool, *self.subpath))}@{self.version}"


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    """A tool name resolved on PATH, with its installed version key."""

    name: str
    path: Path
    version: str


@dataclass(frozen=True, slots=True)
class CrawlReport:
    tool: str
    version: str
    path: Path
    stored: int
    missing: int
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class PagesReport:
    """What `--has-pages TOOL` prints."""

    tool: str
    installed: Optional[ResolvedTool]
    stored: Tuple[StoredVersion, ...] = field(default_factory=tuple)


def help_probes(subpath: Tuple[str, ...]) -> Tuple[Tuple[str, ...], ...]:
    """Argument lists tried, in order, to get help for a (sub)command."""
    match subpath:
        case ():
            return (("--help",), ("help",), ("-h",))
        case words:
            return ((*words, "--help"), ("help", *words), (*words, "-h"))


def parse_version(output: str) -> Optional[str]:
    """The first version-looking token in the first lines of output."""
    for line in output.strip().splitlines()[:VERSION_SCAN_LINES]:
        match VERSION_PATTERN.search(line):
            case None:
                continue
            case found:
                return found.group(1)
    return None


def fingerprint(path: Path) -> str:
    """Cheap binary identity: realpath + size + mtime_ns."""
    real = os.path.realpath(path)
    stat = os.stat(real)
    return f"{real}:{stat.st_size}:{stat.st_mtime_ns}"


def is_script(path: Path) -> bool:
    """True for `#!` scripts (version-manager shims), whose fingerprint can't detect upgrades."""
    with open(os.path.realpath(path), "rb") as handle:
        return handle.read(2) == b"#!"


class HelpLearner:
    """Finds stored help pages for a tool and learns missing ones through the sandbox."""

    def __init__(self, store: PageStore, sandbox: Optional[Sandbox], cwd: Path, search_path: Optional[str]) -> None:
        self.__store = store
        self.__sandbox = sandbox
        self.__cwd = Path(os.path.realpath(cwd))
        self.__search_path = search_path
        self.__resolved: dict[str, Optional[ResolvedTool]] = {}

    @classmethod
    def from_env(cls, store: PageStore, sandbox: Optional[Sandbox]) -> Self:
        return cls(store, sandbox, Path.cwd(), os.environ.get("PATH"))

    @property
    def can_probe(self) -> bool:
        return self.__sandbox is not None

    # -- resolution -----------------------------------------------------------------

    def locate(self, name: str) -> Optional[Path]:
        """Absolute PATH location of `name`, refusing relative paths and anything inside cwd."""
        if not name or "/" in name or name in {".", ".."}:
            return None
        found = shutil.which(name, path=self.__search_path)
        if found is None or not os.path.isabs(found):
            return None
        for candidate in (Path(found), Path(os.path.realpath(found))):
            if candidate.is_relative_to(self.__cwd):
                return None
        return Path(found)

    def resolve(self, name: str) -> Optional[ResolvedTool]:
        """Locate `name` and determine its installed version key (probing only when needed)."""
        if name in self.__resolved:
            return self.__resolved[name]
        path = self.locate(name)
        resolved = None if path is None else ResolvedTool(name=name, path=path, version=self._version(name, path))
        self.__resolved[name] = resolved
        return resolved

    def _version(self, name: str, path: Path) -> str:
        identity = fingerprint(path)
        real = os.path.realpath(path)
        shim = is_script(path)
        known = None if shim else self.__store.version_for_fingerprint(real, identity)
        if known is not None:
            return known
        version = self._probe_version(path) or f"fp-{hashlib.sha256(identity.encode()).hexdigest()[:12]}"
        if not shim:
            self.__store.record_fingerprint(real, identity, name, version)
        return version

    def _probe_version(self, path: Path) -> Optional[str]:
        if self.__sandbox is None:
            return None
        for args in VERSION_PROBES:
            result = self.__sandbox.run(path, args)
            version = parse_version(result.output) if result.ok else None
            if version is not None:
                return version
        return None

    # -- pages ----------------------------------------------------------------------

    def page(self, tool: ResolvedTool, subpath: Tuple[str, ...], learn: bool) -> Optional[HelpPage]:
        """The stored page for `tool subpath...`, learning it first if allowed and not known missing."""
        stored = self.__store.get_page(tool.name, tool.version, subpath)
        if stored is not None:
            return HelpPage(tool=tool.name, version=tool.version, subpath=subpath, doc=stored)
        if not learn or self.__sandbox is None or self.__store.is_missing(tool.name, tool.version, subpath):
            return None
        learned = self._learn_page(tool, subpath)
        if learned is None:
            self.__store.mark_missing(tool.name, tool.version, subpath)
            return None
        text, doc = learned
        self.__store.add_tool(tool.name, tool.version, str(tool.path))
        self.__store.put_page(tool.name, tool.version, subpath, text, doc)
        return HelpPage(tool=tool.name, version=tool.version, subpath=subpath, doc=doc)

    def _learn_page(self, tool: ResolvedTool, subpath: Tuple[str, ...]) -> Optional[Tuple[str, PageDoc]]:
        assert self.__sandbox is not None
        for args in help_probes(subpath):
            result: ProbeResult = self.__sandbox.run(tool.path, args)
            if result.timed_out or not result.output.strip() or not looks_like_help(result.output):
                continue
            return result.output, PageDoc.from_text(result.output)
        return None

    def chain(self, name: str, words: Sequence[str], learn: bool) -> List[HelpPage]:
        """Pages along the subcommand path actually used: `uv pip install` → uv, uv pip, uv pip install.

        Only words listed as subcommands of the previous page are followed.
        """
        tool = self.resolve(name)
        if tool is None:
            return []
        root = self.page(tool, (), learn)
        if root is None:
            return []
        pages = [root]
        for word in words[:DEFAULT_DEPTH]:
            if word not in pages[-1].doc.subcommand_names:
                break
            nested = self.page(tool, (*pages[-1].subpath, word), learn)
            if nested is None:
                break
            pages.append(nested)
        return pages

    def crawl(self, name: str, depth: int = DEFAULT_DEPTH, max_pages: int = DEFAULT_MAX_PAGES) -> CrawlReport:
        """Learn a tool's full subcommand tree (`--parse-cli`), breadth first.

        Raises:
            ValueError: If depth/max_pages are out of range, the tool isn't on PATH, or probes can't run.
        """
        if depth < 0:
            raise ValueError(f"expected depth >= 0, got {depth=}")
        if max_pages < 1:
            raise ValueError(f"expected max_pages >= 1, got {max_pages=}")
        if self.__sandbox is None:
            raise ValueError("cannot learn tools: /usr/bin/sandbox-exec is not available")
        tool = self.resolve(name)
        if tool is None:
            raise ValueError(f"{name!r} is not a command on PATH (relative paths and files in the current directory are never learned)")
        queue: Deque[Tuple[str, ...]] = deque([()])
        stored = missing = 0
        while queue and stored < max_pages:
            subpath = queue.popleft()
            page = self.page(tool, subpath, learn=True)
            if page is None:
                missing += 1
                continue
            stored += 1
            if len(subpath) < depth:
                queue.extend(
                    (*subpath, sub)
                    for sub, _ in page.doc.subcommands
                    if sub != "help" and SUBCOMMAND_NAME.match(sub)
                )
        return CrawlReport(
            tool=name, version=tool.version, path=tool.path, stored=stored, missing=missing, truncated=bool(queue)
        )

    def report(self, name: str) -> PagesReport:
        return PagesReport(tool=name, installed=self.resolve(name), stored=tuple(self.__store.stored_versions(name)))
