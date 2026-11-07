"""Filesystem locations used by `she`, resolved once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self


@dataclass(frozen=True, slots=True)
class Paths:
    """Where `she` keeps its caches, state and configuration.

    Attributes:
        cache_dir: Re-creatable data (help pages, answer cache).
        state_dir: Small machine-local state (HMAC key, Ollama PID, watchdog state).
        config_dir: User-edited configuration (global instructions).
    """

    cache_dir: Path
    state_dir: Path
    config_dir: Path

    @classmethod
    def from_env(cls) -> Self:
        """Build paths from macOS defaults, honouring XDG and `SHE_*_DIR` overrides."""
        home = Path.home()
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        default_cache = Path(xdg_cache) / "she" if xdg_cache else home / "Library" / "Caches" / "she"
        return cls(
            cache_dir=Path(os.environ.get("SHE_CACHE_DIR") or default_cache),
            state_dir=Path(os.environ.get("SHE_STATE_DIR") or home / "Library" / "Application Support" / "she"),
            config_dir=Path(os.environ.get("SHE_CONFIG_DIR") or home / ".config" / "she"),
        )

    def copy(self, **changes: Path) -> Self:
        """Return a new instance with selected fields replaced."""
        return replace(self, **changes)

    @property
    def pages_db(self) -> Path:
        return self.cache_dir / "pages.db"

    @property
    def answers_db(self) -> Path:
        return self.cache_dir / "answers.db"

    @property
    def key_file(self) -> Path:
        return self.state_dir / "key"

    @property
    def ollama_pid_file(self) -> Path:
        return self.state_dir / "ollama.pid"

    @property
    def watchdog_state_file(self) -> Path:
        return self.state_dir / "watchdog-idle-since"

    @property
    def global_instructions(self) -> Path:
        return self.config_dir / "instructions.md"

    @staticmethod
    def local_instructions(cwd: Path) -> Path:
        """The local instructions file for a working directory (no parent walk)."""
        return cwd / ".she" / "instructions.md"


def ensure_private_dir(path: Path) -> None:
    """Create a directory readable only by the current user (mode 700)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def ensure_private_file(path: Path) -> None:
    """Create (if missing) a file readable only by the current user (mode 600)."""
    ensure_private_dir(path.parent)
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
    os.chmod(path, 0o600)
