"""Human and JSON output. Nothing printed is ever redrawn, so terminals and pipes see the same text."""

from __future__ import annotations

import json
import shutil
from typing import Iterable, List, Optional, Sequence, TextIO, Tuple

from she.models import BreakdownItem, EnvEffect, Explanation, FileEffect, Unknown

INDENT = "  "
DEFAULT_WIDTH = 100
MIN_WIDTH = 40
LIST_LABEL_WIDTH = 24
FILE_OP_WORDS = {
    "read": "read",
    "write": "written",
    "append": "appended",
    "delete": "deleted",
    "create": "created",
    "chmod": "chmod",
}
LABELLED_KINDS = frozenset({"command", "subcommand", "option", "function"})
SECTIONS_ALL: Tuple[str, ...] = ("breakdown", "files", "env", "unknowns", "summary")


def terminal_width() -> int:
    """Terminal columns (100 when not a terminal), never below 40."""
    columns = shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns
    return max(MIN_WIDTH, columns)


def truncate(line: str, width: int) -> str:
    return line if len(line) <= width else line[: max(0, width - 1)] + "…"


def format_source(source: Optional[str]) -> Optional[str]:
    """`man:find(1)` → `man find(1)`; builtin/syntax/None are not shown as sources."""
    match source:
        case None | "" | "syntax" | "builtin":
            return None
        case text if ":" in text:
            kind, _, rest = text.partition(":")
            return f"{kind} {rest}"
        case text:
            return text


def item_label(item: BreakdownItem, max_width: Optional[int] = None) -> str:
    """One breakdown line's text: `name: help [source]`, `name (builtin): help`, or just the help.

    With `max_width`, the help text is shortened first so the source tag stays visible.
    """
    help_text = item.help or "(no description found)"
    head = item.text.split()[0] if item.text.split() else item.text
    match item.kind, item.source:
        case _, "builtin":
            label = f"{head} (builtin): {help_text}"
        case kind, _ if kind in LABELLED_KINDS:
            label = f"{head}: {help_text}"
        case _:
            label = help_text
    source = format_source(item.source)
    suffix = f"  [{source}]" if source else ""
    if max_width is not None and len(label) + len(suffix) > max_width:
        label = truncate(label, max(1, max_width - len(suffix)))
    return label + suffix


def breakdown_lines(command: str, items: Sequence[BreakdownItem], width: int) -> List[str]:
    """explainshell-style art: underline each span, then one connector line per item, right to left.

    Falls back to a simple list for multi-line commands or commands wider than the terminal.
    """
    if not items:
        return [truncate(INDENT + line, width) for line in command.splitlines()] + [INDENT + "(nothing to explain)"]
    if "\n" in command or len(INDENT) + len(command) > width:
        return list_lines(items, width)
    underline = [" "] * len(command)
    for item in items:
        for position in range(item.span[0], min(item.span[1], len(command))):
            underline[position] = "─"
    anchors = [item.span[0] + max(0, item.span[1] - item.span[0] - 1) // 2 for item in items]
    lines = [INDENT + command, INDENT + "".join(underline).rstrip()]
    for index in reversed(range(len(items))):
        row = [" "] * anchors[index]
        for earlier in anchors[:index]:
            row[earlier] = "│"
        prefix = INDENT + "".join(row) + "└─ "
        lines.append(truncate(prefix + item_label(items[index], width - len(prefix)), width))
    return lines


def list_lines(items: Sequence[BreakdownItem], width: int) -> List[str]:
    """Simple list form of the breakdown: `text   label`."""
    texts = [" ".join(item.text.split()) for item in items]
    column = min(LIST_LABEL_WIDTH, max(len(text) for text in texts))
    return [
        truncate(f"{INDENT}{text:<{column}}  {item_label(item, width - len(INDENT) - column - 2)}", width)
        for text, item in zip(texts, items, strict=True)
    ]


def file_lines(files: Iterable[FileEffect]) -> List[str]:
    return [
        f"{INDENT}{FILE_OP_WORDS[effect.op]:<9} {effect.path:<26} {effect.basis}"
        + (f" ({effect.detail})" if effect.detail else "")
        for effect in files
    ]


def env_lines(env: Iterable[EnvEffect]) -> List[str]:
    lines = []
    for effect in env:
        shown = f"{effect.name}={effect.value}" if effect.value is not None else effect.name
        lines.append(f"{INDENT}{effect.op:<9} {shown:<26} {effect.basis}" + (f" ({effect.detail})" if effect.detail else ""))
    return lines


def unknown_lines(unknowns: Iterable[Unknown]) -> List[str]:
    lines = [f"{INDENT}{unknown.text} — {unknown.reason}" for unknown in unknowns]
    return lines or [f"{INDENT}(none)"]


def deterministic_text(
    command: str,
    breakdown: Sequence[BreakdownItem],
    files: Sequence[FileEffect],
    env: Sequence[EnvEffect],
    unknowns: Sequence[Unknown],
    warnings: Sequence[str],
    sections: Sequence[str],
    width: int,
) -> str:
    """BREAKDOWN, FILES, ENV and UNKNOWNS (only the selected sections)."""
    blocks: List[str] = []
    if "breakdown" in sections:
        blocks.append("\n".join(["BREAKDOWN", *breakdown_lines(command, breakdown, width)]))
    if "files" in sections:
        blocks.append("\n".join(["FILES", *(file_lines(files) or [f"{INDENT}(none visible)"])]))
    if "env" in sections:
        blocks.append("\n".join(["ENV", *(env_lines(env) or [f"{INDENT}(none visible)"])]))
    if "unknowns" in sections:
        blocks.append("\n".join(["UNKNOWNS", *unknown_lines(unknowns)]))
    if warnings:
        blocks.append("\n".join(["WARNINGS", *(f"{INDENT}{warning}" for warning in warnings)]))
    return "\n\n".join(blocks) + "\n"


def inferred_text(files: Sequence[FileEffect], env: Sequence[EnvEffect], sections: Sequence[str]) -> str:
    """The INFERRED (LLM guess) section, or "" when there's nothing to show."""
    lines = [*(file_lines(files) if "files" in sections else []), *(env_lines(env) if "env" in sections else [])]
    return "\n".join(["INFERRED (LLM guess)", *lines]) + "\n" if lines else ""


def footer(sources: Sequence[str], model: Optional[str], instructions_source: str) -> str:
    shown = [format_source(source) for source in sources]
    source_text = ", ".join(text for text in shown if text) or "none"
    if model is None:
        model_text = "model unavailable"
    else:
        name, _, digest = model.partition("@")
        model_text = f"model {name}@{digest[:12]}" if digest else f"model {name}"
    return f"sources: {source_text} · {model_text} · instructions: {instructions_source}\n"


class SummaryPrinter:
    """Writes streamed summary text with word wrapping at the terminal width, flushing as it goes."""

    def __init__(self, stream: TextIO, width: int) -> None:
        self.__stream = stream
        self.__width = width
        self.__column = 0
        self.__pending = ""

    def start(self, title: str = "SUMMARY (LLM guess)") -> None:
        self.__stream.write(f"\n{title}\n")
        self.__stream.flush()

    def write(self, text: str) -> None:
        self.__pending += text
        cut = max(self.__pending.rfind(" "), self.__pending.rfind("\n"), self.__pending.rfind("\t"))
        if cut < 0:
            return
        complete, self.__pending = self.__pending[: cut + 1], self.__pending[cut + 1 :]
        for word in complete.split():
            self._word(word)
        self.__stream.flush()

    def finish(self) -> None:
        for word in self.__pending.split():
            self._word(word)
        self.__pending = ""
        if self.__column:
            self.__stream.write("\n")
        self.__column = 0
        self.__stream.flush()

    def _word(self, word: str) -> None:
        if self.__column == 0:
            self.__stream.write(INDENT + word)
            self.__column = len(INDENT) + len(word)
        elif self.__column + 1 + len(word) > self.__width:
            self.__stream.write("\n" + INDENT + word)
            self.__column = len(INDENT) + len(word)
        else:
            self.__stream.write(" " + word)
            self.__column += 1 + len(word)


def explanation_json(explanation: Explanation, sections: Sequence[str]) -> dict:
    """The `she/v1` object, limited to the selected sections when `--list`/`--list-env` are used."""
    data = explanation.model_dump(by_alias=True, mode="json")
    if set(sections) >= set(SECTIONS_ALL):
        return data
    keep = {"schema", "command", *(name for name in sections if name in {"files", "env"})}
    return {key: value for key, value in data.items() if key in keep}


def json_line(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
