"""Run version/help probes under macOS `sandbox-exec`: no network, no writes outside a scratch dir."""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Self, Sequence

from she.textdoc import clean_text

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
TIMEOUT_SECONDS = 5.0
OUTPUT_CAP_BYTES = 256 * 1024
READ_CHUNK_BYTES = 65536


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Outcome of one sandboxed probe (stdout and stderr combined, ANSI stripped)."""

    returncode: Optional[int]
    output: str
    timed_out: bool = False
    truncated: bool = False

    def copy(self, **changes: object) -> Self:
        return replace(self, **changes)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def sandbox_profile(scratch: Path) -> str:
    """The Seatbelt profile: allow everything except network and writes outside `scratch` and devices."""
    scratch_literal = str(scratch).replace("\\", "\\\\").replace('"', '\\"')
    return (
        "(version 1)"
        "(allow default)"
        "(deny network*)"
        "(deny file-write*)"
        "(allow file-write*"
        f' (subpath "{scratch_literal}")'
        ' (literal "/dev/null")'
        ' (literal "/dev/zero")'
        ' (regex #"^/dev/tty")'
        ' (regex #"^/dev/fd/"))'
    )


def probe_environment(scratch: Path) -> dict[str, str]:
    """Environment for probes: the user's env with pagers, colour and terminal neutralised."""
    return {
        **os.environ,
        "PAGER": "cat",
        "MANPAGER": "cat",
        "GIT_PAGER": "cat",
        "TERM": "dumb",
        "NO_COLOR": "1",
        "TMPDIR": str(scratch),
    }


@dataclass(frozen=True, slots=True)
class Sandbox:
    """Runs a binary with arguments under `sandbox-exec`.

    Attributes:
        executable: Path to `sandbox-exec`.
        timeout: Seconds before the probe is killed.
        output_cap: Bytes of output kept; the probe is killed once it prints more.
    """

    executable: Path = SANDBOX_EXEC
    timeout: float = TIMEOUT_SECONDS
    output_cap: int = OUTPUT_CAP_BYTES

    @classmethod
    def from_system(cls) -> Optional[Self]:
        """The sandbox if `sandbox-exec` exists on this machine, else None (then nothing is probed)."""
        return cls() if os.access(SANDBOX_EXEC, os.X_OK) else None

    def copy(self, **changes: object) -> Self:
        return replace(self, **changes)

    def run(self, binary: Path, args: Sequence[str]) -> ProbeResult:
        """Run `binary args...` sandboxed, with stdin=/dev/null and cwd set to a fresh scratch dir.

        Raises:
            ValueError: If `binary` is not an absolute path.
        """
        if not binary.is_absolute():
            raise ValueError(f"probes only run absolute paths, got {binary=}")
        scratch = Path(os.path.realpath(tempfile.mkdtemp(prefix="she-probe-")))
        try:
            return self._run(binary, tuple(args), scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _run(self, binary: Path, args: tuple[str, ...], scratch: Path) -> ProbeResult:
        command = [str(self.executable), "-p", sandbox_profile(scratch), str(binary), *args]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=scratch,
                env=probe_environment(scratch),
                start_new_session=True,
            )
        except OSError as error:
            return ProbeResult(returncode=None, output=f"could not start probe: {error}")
        chunks: list[bytes] = []
        received = 0
        timed_out = truncated = False
        deadline = time.monotonic() + self.timeout
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if not selector.select(timeout=remaining):
                    continue
                chunk = os.read(process.stdout.fileno(), READ_CHUNK_BYTES)
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if received >= self.output_cap:
                    truncated = True
                    break
        if timed_out or truncated:
            _kill_group(process)
        process.stdout.close()
        try:
            returncode = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            returncode = process.wait()
        output = b"".join(chunks)[: self.output_cap].decode("utf-8", errors="replace")
        return ProbeResult(
            returncode=None if timed_out else returncode,
            output=clean_text(output),
            timed_out=timed_out,
            truncated=truncated,
        )


def _kill_group(process: subprocess.Popen) -> None:
    """Kill the probe and anything it spawned (it runs in its own session)."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
