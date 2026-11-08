"""launchd watchdog that stops an idle Ollama server that `she` started (contract §6)."""

from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional, Self

from she.llm import server_url
from she.paths import Paths, ensure_private_dir

LABEL = "com.she.watchdog"
START_INTERVAL_SECONDS = 300
DEFAULT_IDLE_MINUTES = 30.0
PASSED_THROUGH_ENV = (
    "SHE_SERVER_IDLE",
    "SHE_OLLAMA_URL",
    "SHE_OLLAMA_PORT",
    "SHE_STATE_DIR",
    "SHE_CACHE_DIR",
    "SHE_CONFIG_DIR",
)


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plist_content(executable: str, environ: Mapping[str, str]) -> bytes:
    """The launch agent: run `<she> --watchdog-tick` every 5 minutes."""
    content: dict = {
        "Label": LABEL,
        "ProgramArguments": [executable, "--watchdog-tick"],
        "StartInterval": START_INTERVAL_SECONDS,
        "RunAtLoad": False,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    passed = {name: environ[name] for name in PASSED_THROUGH_ENV if environ.get(name)}
    if passed:
        content["EnvironmentVariables"] = passed
    return plistlib.dumps(content)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(executable: str, environ: Mapping[str, str]) -> Path:
    """Write the plist and load it with `launchctl bootstrap` (re-loading if already present).

    Raises:
        RuntimeError: If launchctl refuses the agent.
    """
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plist_content(executable, environ))
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    completed = subprocess.run(["launchctl", "bootstrap", _domain(), str(path)], capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed ({completed.returncode}): {completed.stderr.strip()}")
    return path


def uninstall() -> bool:
    """Boot the agent out and delete the plist. Returns True if a plist existed."""
    path = plist_path()
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def idle_minutes(environ: Mapping[str, str]) -> float:
    """`SHE_SERVER_IDLE` in minutes (default 30).

    Raises:
        ValueError: If it is not a positive number.
    """
    raw = environ.get("SHE_SERVER_IDLE")
    if not raw:
        return DEFAULT_IDLE_MINUTES
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"expected minutes in $SHE_SERVER_IDLE, got {raw=}") from error
    if value <= 0:
        raise ValueError(f"expected positive minutes in $SHE_SERVER_IDLE, got {value=}")
    return value


@dataclass(frozen=True, slots=True)
class TickResult:
    """What one watchdog tick decided (for tests and debugging)."""

    action: str
    pid: Optional[int] = None

    def copy(self, **changes: object) -> Self:
        return replace(self, **changes)


def read_pid(pid_file: Path) -> Optional[int]:
    """The pid of the server she started (the pid file is `<pid> <url>`, or just `<pid>` from older versions)."""
    if not pid_file.is_file():
        return None
    first = next(iter(pid_file.read_text().split()), "")
    return int(first) if first.isdigit() else None


def read_server_url(pid_file: Path) -> Optional[str]:
    """The URL the she-started server listens on, or None if the pid file doesn't record one."""
    if not pid_file.is_file():
        return None
    fields = pid_file.read_text().split()
    return fields[1] if len(fields) > 1 else None


def is_she_ollama(pid: int) -> bool:
    """True if `pid` is alive and its command name contains "ollama"."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    completed = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True)
    return completed.returncode == 0 and "ollama" in completed.stdout.lower()


def loaded_model_count(base_url: str) -> Optional[int]:
    """Number of models in `/api/ps`, or None if the server is unreachable."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/ps", timeout=2) as response:
            return len(json.loads(response.read().decode("utf-8")).get("models", []))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def tick(paths: Paths, environ: Mapping[str, str], now: Optional[float] = None) -> TickResult:
    """One watchdog check: stop the she-started server after `SHE_SERVER_IDLE` minutes with no model loaded."""
    current = time.time() if now is None else now
    pid = read_pid(paths.ollama_pid_file)
    if pid is None:
        return TickResult(action="no-pid-file")
    if not is_she_ollama(pid):
        paths.ollama_pid_file.unlink(missing_ok=True)
        paths.watchdog_state_file.unlink(missing_ok=True)
        return TickResult(action="not-running", pid=pid)
    loaded = loaded_model_count(read_server_url(paths.ollama_pid_file) or server_url(environ))
    if loaded is None:
        return TickResult(action="unreachable", pid=pid)
    if loaded > 0:
        paths.watchdog_state_file.unlink(missing_ok=True)
        return TickResult(action="busy", pid=pid)
    if not paths.watchdog_state_file.is_file():
        ensure_private_dir(paths.state_dir)
        paths.watchdog_state_file.write_text(f"{current}\n")
        return TickResult(action="idle-started", pid=pid)
    since = float(paths.watchdog_state_file.read_text().strip() or current)
    if current - since < idle_minutes(environ) * 60:
        return TickResult(action="idle", pid=pid)
    os.kill(pid, signal.SIGTERM)
    paths.ollama_pid_file.unlink(missing_ok=True)
    paths.watchdog_state_file.unlink(missing_ok=True)
    return TickResult(action="stopped", pid=pid)
