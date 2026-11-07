"""User instructions for the LLM (contract §2.4): exactly one source per call, never merged."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional, Self

from she.models import InstructionsInfo, InstructionsSource
from she.paths import Paths

ENV_VAR = "SHE_INSTRUCTIONS"


@dataclass(frozen=True, slots=True)
class Instructions:
    """The chosen instructions source and its text.

    Attributes:
        source: "ad-hoc", "local", "global" or "none".
        path: The file read, if the text came from a file.
        text: The instructions text ("" for none, or for an empty file).
    """

    source: InstructionsSource
    path: Optional[Path] = None
    text: str = ""

    @classmethod
    def resolve(
        cls, flag_file: Optional[Path], environ: Mapping[str, str], cwd: Path, paths: Paths
    ) -> Self:
        """Pick the first source in order: --instructions FILE, $SHE_INSTRUCTIONS, ./.she/instructions.md,
        ~/.config/she/instructions.md, none.

        Raises:
            FileNotFoundError: If `--instructions FILE` does not exist.
        """
        if flag_file is not None:
            if not flag_file.is_file():
                raise FileNotFoundError(f"--instructions file not found: {flag_file}")
            return cls(source="ad-hoc", path=flag_file, text=flag_file.read_text(encoding="utf-8"))
        env_value = environ.get(ENV_VAR)
        if env_value is not None:
            candidate = Path(env_value).expanduser()
            if env_value and candidate.is_file():
                return cls(source="ad-hoc", path=candidate, text=candidate.read_text(encoding="utf-8"))
            return cls(source="ad-hoc", path=None, text=env_value)
        local = Paths.local_instructions(cwd)
        if local.is_file():
            return cls(source="local", path=local, text=local.read_text(encoding="utf-8"))
        if paths.global_instructions.is_file():
            return cls(source="global", path=paths.global_instructions, text=paths.global_instructions.read_text(encoding="utf-8"))
        return cls(source="none")

    def copy(self, **changes: object) -> Self:
        return replace(self, **changes)

    @property
    def info(self) -> InstructionsInfo:
        """The JSON `instructions` object, with the home directory shortened to `~`."""
        return InstructionsInfo(source=self.source, path=display_path(self.path) if self.path else None)


def display_path(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text


def edit_file(path: Path) -> int:
    """Create `path` (and its directory) if missing, open it in vim, and return vim's exit code.

    Raises:
        FileNotFoundError: If `vim` is not on PATH.
    """
    vim = shutil.which("vim")
    if vim is None:
        raise FileNotFoundError("vim is not on PATH; install vim or edit the file directly: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return subprocess.call([vim, str(path)])
