"""Ollama client (stdlib urllib) and the streaming answer splitter/parser."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Self, Tuple

from she.models import ENV_OPS, FILE_OPS, EnvEffect, FileEffect, LlmAnswer
from she.paths import Paths, ensure_private_dir

DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_URL = "http://localhost:11434"
DEFAULT_KEEP_ALIVE = "10m"
DEFAULT_NUM_PREDICT = 160
SERVER_START_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.2
PROBE_TIMEOUT_SECONDS = 0.5
MARKERS: Tuple[str, ...] = ("FILE:", "ENV:", "END")
SUMMARY_PREFIX = "SUMMARY:"
DECORATION = "*-•# \t"
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LlmError(RuntimeError):
    """The LLM layer failed (server down, timeout, bad reply). Maps to exit code 4."""


class OllamaMissingError(LlmError):
    """No Ollama server answers and the `ollama` command is not installed."""


class ModelMissingError(LlmError):
    """The configured model is not pulled. `she` never pulls one itself."""


def model_required_message(model: str, installed: List[str], is_default: bool = False) -> str:
    """The `Model required` error text: the two ways out, and what is installed.

    `is_default` says no model was chosen (no `--model`, no `SHE_MODEL`), so the
    built-in default was tried.
    """
    have = f"installed: {', '.join(installed)}" if installed else "no models installed"
    chosen = " (the default; no SHE_MODEL set)" if is_default else ""
    return (
        f"Model required: {model!r}{chosen} is not installed in Ollama ({have}). "
        f"Pull it with `ollama pull {model}`, or set SHE_MODEL to an installed model."
    )


def models_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    """Where Ollama keeps model files: `$OLLAMA_MODELS`, else `~/.ollama/models` (Ollama's own default)."""
    env = os.environ if environ is None else environ
    configured = env.get("OLLAMA_MODELS")
    return Path(configured).expanduser() if configured else Path.home() / ".ollama" / "models"


def manifest_path(root: Path, model: str) -> Path:
    """The manifest file Ollama writes for `model` under `root`.

    `qwen3.5:4b` → `manifests/registry.ollama.ai/library/qwen3.5/4b`; `user/m` →
    `.../registry.ollama.ai/user/m/latest`; `hf.co/org/repo:q4` → `manifests/hf.co/org/repo/q4`.
    """
    name, _, tag = model.rpartition(":") if ":" in model.rsplit("/", 1)[-1] else (model, "", "latest")
    parts = name.split("/")
    if len(parts) == 1:
        parts = ["registry.ollama.ai", "library", *parts]
    elif "." not in parts[0] and ":" not in parts[0]:
        parts = ["registry.ollama.ai", *parts]
    return root.joinpath("manifests", *parts, tag)


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    """Connection and generation settings.

    Attributes:
        base_url: Ollama server URL (`SHE_OLLAMA_URL`).
        model: Model tag (`--model` > `SHE_MODEL` > default).
        keep_alive: How long Ollama keeps the model loaded (`SHE_KEEP_ALIVE`).
        num_predict: Output token cap.
        is_default_model: True when neither `--model` nor `SHE_MODEL` chose the model.
    """

    base_url: str = DEFAULT_URL
    model: str = DEFAULT_MODEL
    keep_alive: str = DEFAULT_KEEP_ALIVE
    num_predict: int = DEFAULT_NUM_PREDICT
    is_default_model: bool = False

    @classmethod
    def from_env(cls, model: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> Self:
        env = os.environ if environ is None else environ
        return cls(
            base_url=(env.get("SHE_OLLAMA_URL") or DEFAULT_URL).rstrip("/"),
            model=model or env.get("SHE_MODEL") or DEFAULT_MODEL,
            keep_alive=env.get("SHE_KEEP_ALIVE") or DEFAULT_KEEP_ALIVE,
            is_default_model=not (model or env.get("SHE_MODEL")),
        )

    def copy(self, **changes: object) -> Self:
        return replace(self, **changes)


def _remaining(deadline: Optional[float], cap: float) -> float:
    if deadline is None:
        return cap
    left = deadline - time.monotonic()
    if left <= 0:
        raise LlmError("timed out waiting for the LLM (see --timeout)")
    return min(cap, left)


class OllamaClient:
    """Talks to a local Ollama server; starts `ollama serve` when it isn't running."""

    def __init__(self, config: OllamaConfig, paths: Paths) -> None:
        self.__config = config
        self.__paths = paths

    @property
    def config(self) -> OllamaConfig:
        return self.__config

    # -- HTTP -----------------------------------------------------------------------------

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]], timeout: float):  # noqa: ANN202
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.__config.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        return urllib.request.urlopen(request, timeout=timeout)

    def _json(self, method: str, path: str, body: Optional[Dict[str, Any]], timeout: float) -> Dict[str, Any]:
        try:
            with self._request(method, path, body, timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise LlmError(f"Ollama {path} returned HTTP {error.code}: {_error_text(error)}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise LlmError(f"cannot reach Ollama at {self.__config.base_url}: {error}") from error

    def is_up(self) -> bool:
        try:
            self._json("GET", "/api/version", None, PROBE_TIMEOUT_SECONDS)
        except LlmError:
            return False
        return True

    def server_version(self) -> Optional[str]:
        try:
            return str(self._json("GET", "/api/version", None, PROBE_TIMEOUT_SECONDS).get("version"))
        except LlmError:
            return None

    # -- lifecycle ------------------------------------------------------------------------

    def ensure_running(self, deadline: Optional[float] = None) -> bool:
        """Start `ollama serve` detached if the server is down. Returns True if it was started here.

        Raises:
            LlmError: If ollama isn't installed or doesn't come up within 15 s (or the deadline).
        """
        if self.is_up():
            return False
        executable = shutil.which("ollama")
        if executable is None:
            raise OllamaMissingError(
                f"No Ollama installed: nothing answers at {self.__config.base_url} and `ollama` is not on PATH. "
                "Install Ollama, then pull a model."
            )
        process = subprocess.Popen(
            [executable, "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        ensure_private_dir(self.__paths.state_dir)
        self.__paths.ollama_pid_file.write_text(f"{process.pid}\n")
        started = time.monotonic()
        while time.monotonic() - started < SERVER_START_SECONDS:
            _remaining(deadline, POLL_INTERVAL_SECONDS)
            if self.is_up():
                return True
            if process.poll() is not None:
                raise LlmError(f"`ollama serve` exited with code {process.returncode} before it was reachable")
            time.sleep(POLL_INTERVAL_SECONDS)
        raise LlmError(f"Ollama did not come up within {SERVER_START_SECONDS:.0f} s at {self.__config.base_url}")

    def installed_models(self, deadline: Optional[float] = None) -> List[Dict[str, Any]]:
        """Every model Ollama has, as `/api/tags` lists them (name, digest, size, ...)."""
        return list(self._json("GET", "/api/tags", None, _remaining(deadline, 5.0)).get("models", []))

    def installed_model(self, deadline: Optional[float] = None) -> Dict[str, Any]:
        """The configured model's `/api/tags` entry.

        Raises:
            ModelMissingError: If the model isn't pulled.
        """
        models = self.installed_models(deadline)
        wanted = self.__config.model
        names = {wanted, wanted if ":" in wanted else f"{wanted}:latest"}
        for model in models:
            if model.get("name") in names or model.get("model") in names:
                return model
        raise ModelMissingError(
            model_required_message(wanted, [str(model.get("name", "")) for model in models], self.__config.is_default_model)
        )

    def model_digest(self, deadline: Optional[float] = None) -> str:
        """The installed model's digest from `/api/tags`.

        Raises:
            ModelMissingError: If the model isn't pulled.
        """
        return str(self.installed_model(deadline).get("digest", ""))

    def running_models(self) -> List[Dict[str, Any]]:
        return list(self._json("GET", "/api/ps", None, 2.0).get("models", []))

    def warm(self, deadline: Optional[float] = None) -> None:
        """Load the model into memory (empty generate request) so the next explain is fast."""
        self._json(
            "POST",
            "/api/generate",
            {"model": self.__config.model, "keep_alive": self.__config.keep_alive},
            _remaining(deadline, 120.0),
        )

    # -- chat -----------------------------------------------------------------------------

    def chat_stream(self, messages: List[Dict[str, str]], deadline: Optional[float] = None) -> Iterator[str]:
        """Stream content chunks from `/api/chat` (thinking off, temperature 0, fixed seed)."""
        body = {
            "model": self.__config.model,
            "messages": messages,
            "stream": True,
            "think": False,
            "keep_alive": self.__config.keep_alive,
            "options": {"temperature": 0, "seed": 0, "num_predict": self.__config.num_predict},
        }
        try:
            with self._request("POST", "/api/chat", body, _remaining(deadline, 120.0)) as response:
                for raw_line in response:
                    _remaining(deadline, 120.0)
                    if not raw_line.strip():
                        continue
                    event = json.loads(raw_line.decode("utf-8"))
                    if "error" in event:
                        raise LlmError(f"Ollama error: {event['error']}")
                    content = event.get("message", {}).get("content", "")
                    if content:
                        yield content
                    if event.get("done"):
                        return
        except urllib.error.HTTPError as error:
            raise LlmError(f"Ollama /api/chat returned HTTP {error.code}: {_error_text(error)}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise LlmError(f"LLM request failed: {error}") from error


def _error_text(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", errors="replace")[:200]
    except OSError:
        return ""


# -- answer parsing ---------------------------------------------------------------------------

def _marker_of(line: str) -> Optional[str]:
    """The marker a line starts with (case-insensitive, ignoring bullets/bold), if any.

    `END` must be the whole line; `FILE:` and `ENV:` are prefixes.
    """
    text = line.strip().lstrip(DECORATION).upper()
    if text.rstrip(".*: ") == "END":
        return "END"
    return next((marker for marker in MARKERS if marker != "END" and text.startswith(marker)), None)


def _could_become_marker(fragment: str) -> bool:
    """True if an incomplete line might still turn into a marker line."""
    text = fragment.strip().lstrip(DECORATION).upper()
    return any(marker.startswith(text) for marker in MARKERS) or _marker_of(fragment) is not None


class AnswerStream:
    """Splits the streamed reply: summary text is released as it arrives, the rest parsed at the end.

    A trailing partial line that might be the start of a `FILE:`/`ENV:`/`END` marker is held
    back until it is complete, so marker lines never leak into the printed summary.
    """

    def __init__(self) -> None:
        self.__buffer = ""
        self.__summary_start: Optional[int] = None
        self.__summary_end: Optional[int] = None
        self.__emitted = 0

    def feed(self, chunk: str) -> str:
        """Add a chunk; return the newly releasable summary text (may be empty)."""
        self.__buffer += chunk
        return self._release(final=False)

    def finish(self) -> Tuple[str, LlmAnswer]:
        """Return the remaining summary text and the parsed answer.

        Raises:
            LlmError: If the reply has no summary text.
        """
        tail = self._release(final=True)
        summary_text = self.__buffer[self.__summary_start or 0 : self._summary_end()]
        summary = " ".join(summary_text.split())
        if not summary:
            raise LlmError(f"the model returned no summary (reply was {self.__buffer[:120]!r})")
        files, env = parse_effect_lines(self.__buffer[self._summary_end() :])
        return tail, LlmAnswer(summary=summary, files=files, env=env)

    def _summary_end(self) -> int:
        return self.__summary_end if self.__summary_end is not None else len(self.__buffer)

    def _locate_start(self, final: bool) -> bool:
        if self.__summary_start is not None:
            return True
        stripped = self.__buffer.lstrip()
        if not stripped:
            return False
        leading = len(self.__buffer) - len(stripped)
        candidate = stripped.lstrip(DECORATION)
        offset = leading + (len(stripped) - len(candidate))
        if candidate.upper().startswith(SUMMARY_PREFIX):
            after = offset + len(SUMMARY_PREFIX)
            rest = self.__buffer[after:]
            if not rest.strip() and not final:
                return False
            self.__summary_start = after + (len(rest) - len(rest.lstrip(" *\t")))
            return True
        if SUMMARY_PREFIX.startswith(candidate.upper()) and not final:
            return False
        self.__summary_start = leading
        return True

    def _release(self, final: bool) -> str:
        if not self._locate_start(final) or self.__summary_start is None:
            return ""
        if self.__summary_end is None:
            position = self.__summary_start
            safe_end = len(self.__buffer)
            while position <= len(self.__buffer):
                newline = self.__buffer.find("\n", position)
                line_end = len(self.__buffer) if newline < 0 else newline
                line = self.__buffer[position:line_end]
                if _marker_of(line) is not None and (newline >= 0 or final):
                    self.__summary_end = position
                    safe_end = position
                    break
                if newline < 0:
                    if not final and line.strip() and _could_become_marker(line):
                        safe_end = position
                    break
                position = newline + 1
        else:
            safe_end = self.__summary_end
        start = max(self.__emitted, self.__summary_start)
        released = self.__buffer[start:safe_end]
        if final or self.__summary_end is not None:
            released = released.rstrip()
        self.__emitted = max(self.__emitted, safe_end)
        return released


def parse_effect_lines(text: str) -> Tuple[Tuple[FileEffect, ...], Tuple[EnvEffect, ...]]:
    """Parse `FILE: <op> <path>` and `ENV: <op> <NAME>[=value]` lines up to `END`; invalid lines are ignored."""
    files: List[FileEffect] = []
    env: List[EnvEffect] = []
    for line in text.splitlines():
        marker = _marker_of(line)
        body = line.strip().lstrip(DECORATION)[len(marker) :].lstrip(DECORATION).strip() if marker else ""
        match marker, body.split(None, 1):
            case "END", _:
                break
            case "FILE:", [op, path] if op.lower() in FILE_OPS and path.strip():
                files.append(FileEffect(path=path.strip().strip("`'\""), op=op.lower(), basis="inferred"))  # type: ignore[arg-type]
            case "ENV:", [op, name] if op.lower() in ENV_OPS:
                variable, _, value = name.strip().strip("`$").partition("=")
                if ENV_NAME.match(variable):
                    env.append(EnvEffect(name=variable, op=op.lower(), basis="inferred", value=value or None))  # type: ignore[arg-type]
            case _:
                continue
    return tuple(dict.fromkeys(files)), tuple(dict.fromkeys(env))


def without_visible(
    answer: LlmAnswer, visible_files: Tuple[FileEffect, ...], visible_env: Tuple[EnvEffect, ...]
) -> LlmAnswer:
    """Drop inferred effects that merely repeat visible ones."""
    seen_files = {(item.path, item.op) for item in visible_files}
    seen_env = {(item.name, item.op) for item in visible_env}
    return answer.copy(
        files=tuple(item for item in answer.files if (item.path, item.op) not in seen_files),
        env=tuple(item for item in answer.env if (item.name, item.op) not in seen_env),
    )
