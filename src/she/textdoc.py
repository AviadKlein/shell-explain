"""One parser for rendered man pages and `--help` output.

Both formats list options as indented header lines that start with `-` (`-f`, `--force`,
`-i identity_file`, `--pretty[=<format>]`), followed by a description either on the same
line (after two or more spaces) or on the following, more-indented lines.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Self, Tuple

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

PARSER_VERSION = 3  # bump whenever parsing changes, so stored pages are re-parsed from their text
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
OVERSTRIKE = re.compile(r".\x08")
LEADING_WHITESPACE = re.compile(r"^[ \t]+", re.MULTILINE)
INLINE_SPLIT =re.compile(r"\s{2,}|\t")
FLAG = re.compile(r"^(?P<flag>--?(?:\[no-?\])?[A-Za-z0-9?#@][\w.?#@+-]*)(?P<rest>.*)$")
OPTIONAL_ARGUMENT = re.compile(r"\[=?[^\]]*\]")
NO_PREFIX = re.compile(r"\[no-?\]")
SUBCOMMAND_HEADING = re.compile(
    r"^(?:(?:available|core|additional|management|main|other|common|all)\s+)?(?:sub)?commands:?$|^[A-Z][A-Z ]{0,30}COMMANDS:?$",
    re.IGNORECASE,
)
SUBCOMMAND_LINE = re.compile(r"^(?P<name>[a-z][\w.-]*)(?:,\s*[\w-]+)*(?:\s{2,}|\t)(?P<description>\S.*)$")
GROUP_HEADING = re.compile(r"^[A-Z][\w/&,'() -]{0,48}:?$")
NOT_COMMAND_GROUPS = frozenset(
    {"usage", "flags", "global flags", "options", "global options", "arguments", "examples", "example",
     "environment", "description", "aliases", "see also", "name", "synopsis"}
)
HEADER_SEPARATOR = re.compile(r",\s*|\s+[|/]\s+")
SENTENCE_END = re.compile(r"(?<=[a-z0-9)\]'\"])\.(?:\s|$)")
MAX_ARGUMENT_WORDS = 2
SUMMARY_LIMIT = 160


class OptionDoc(BaseModel):
    """One documented option.

    Attributes:
        flags: Every spelling of the option, e.g. ("-n", "--max-count").
        takes_arg: True when the option consumes a value (`-i FILE`, `--format=<f>`).
        description: The first paragraph of the option's description.
    """

    model_config = ConfigDict(frozen=True)

    flags: Tuple[str, ...]
    takes_arg: bool = False
    description: str = ""

    def copy(self, **changes: object) -> Self:  # type: ignore[override]
        """Return a new instance with selected fields replaced."""
        return self.model_copy(update=changes)

    @property
    def summary(self) -> str:
        """The first sentence of the description, at most `SUMMARY_LIMIT` characters."""
        return summarize(self.description)


class PageDoc(BaseModel):
    """The structured content of one man page or help page."""

    model_config = ConfigDict(frozen=True)

    title: Optional[str] = None
    options: Tuple[OptionDoc, ...] = ()
    subcommands: Tuple[Tuple[str, str], ...] = ()
    _index: Dict[str, OptionDoc] = PrivateAttr(default_factory=dict)

    def model_post_init(self, context: object, /) -> None:
        index: Dict[str, OptionDoc] = {}
        for option in self.options:
            for flag in option.flags:
                index.setdefault(flag, option)
        self._index = index

    def copy(self, **changes: object) -> Self:  # type: ignore[override]
        """Return a new instance with selected fields replaced (the index is rebuilt)."""
        return type(self).model_validate({**self.model_dump(), **changes})

    @classmethod
    def from_text(cls, text: str) -> Self:
        """Parse rendered man-page or help text.

        Args:
            text: Plain text; ANSI escapes and backspace overstrike are stripped here.

        Raises:
            TypeError: If `text` is not a string.
        """
        if not isinstance(text, str):
            raise TypeError(f"expected str page text, got {type(text).__name__}")
        lines = clean_text(text).splitlines()
        return cls(title=_title(lines), options=tuple(_options(lines)), subcommands=tuple(_subcommands(lines)))

    @classmethod
    def from_json(cls, data: str) -> Self:
        """Rebuild a page stored with `to_json`."""
        return cls.model_validate_json(data)

    def to_json(self) -> str:
        return self.model_dump_json()

    def lookup(self, flag: str) -> Optional[OptionDoc]:
        """The option documented under exactly this spelling, if any."""
        return self._index.get(flag)

    @property
    def description(self) -> Optional[str]:
        """The title without a leading `name -` / `name –` (man NAME line style)."""
        if self.title is None:
            return None
        match re.split(r"\s+[-–—]\s+", self.title, maxsplit=1):
            case [_, rest]:
                return rest.strip()
            case _:
                return self.title.strip()

    @property
    def subcommand_names(self) -> frozenset[str]:
        return frozenset(name for name, _ in self.subcommands)

    def subcommand_help(self, name: str) -> Optional[str]:
        return next((description for sub, description in self.subcommands if sub == name), None)

    @property
    def looks_like_help(self) -> bool:
        """Heuristic used to reject output of tools that ignored `--help`."""
        return len(self.options) >= 2 or bool(self.subcommands)


def clean_text(text: str) -> str:
    """Remove ANSI escapes, backspace overstrike and carriage returns; expand tabs in indentation."""
    without_overstrike = OVERSTRIKE.sub("", text)
    cleaned = ANSI.sub("", without_overstrike).replace("\r\n", "\n").replace("\r", "\n")
    return LEADING_WHITESPACE.sub(lambda found: found.group(0).expandtabs(8), cleaned)


def looks_like_help(text: str) -> bool:
    """True when text mentions usage or documents two or more options."""
    cleaned = clean_text(text)
    return "usage" in cleaned.lower() or len(_options(cleaned.splitlines())) >= 2


def summarize(description: str) -> str:
    """First sentence of a description, capped at `SUMMARY_LIMIT` characters."""
    collapsed = " ".join(description.split())
    match SENTENCE_END.search(collapsed):
        case None:
            sentence = collapsed
        case found:
            sentence = collapsed[: found.start()]
    return sentence if len(sentence) <= SUMMARY_LIMIT else sentence[: SUMMARY_LIMIT - 1].rstrip() + "…"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _title(lines: List[str]) -> Optional[str]:
    stripped = [line.strip() for line in lines]
    name_heading = next((i for i, line in enumerate(stripped) if line == "NAME" and _indent(lines[i]) == 0), None)
    if name_heading is not None:
        return next((line for line in stripped[name_heading + 1 :] if line), None)
    first = next((line for line in stripped if line), None)
    match first:
        case None:
            return None
        case text if text.lower().startswith("usage") or text.startswith("-"):
            return None
        case text:
            return text


def _parse_header(body: str) -> Optional[Tuple[Tuple[str, ...], bool, str, int]]:
    """Split an option header into (flags, takes_arg, inline description, description column)."""
    parts = INLINE_SPLIT.split(body, maxsplit=1)
    header = parts[0].rstrip().removesuffix(":")
    inline = parts[1] if len(parts) > 1 else ""
    column = body.index(inline) if inline else -1
    flags: List[str] = []
    takes_arg = False
    for piece in HEADER_SEPARATOR.split(header):
        match FLAG.match(piece.strip()):
            case None:
                continue
            case found:
                flags.extend(_expand_no(found.group("flag").rstrip(".")))
                rest = found.group("rest").strip().removesuffix("...").strip()
                match rest:
                    case "":
                        pass
                    case value if OPTIONAL_ARGUMENT.fullmatch(value):
                        pass
                    case value if len(value.split()) <= MAX_ARGUMENT_WORDS or any(c in value for c in "[<|"):
                        takes_arg = True
                    case value:
                        inline = f"{value} {inline}".strip()
    if not flags:
        return None
    return tuple(flags), takes_arg, inline, column


def _expand_no(flag: str) -> List[str]:
    """`--[no-]color` documents both `--color` and `--no-color`."""
    match NO_PREFIX.search(flag):
        case None:
            return [flag]
        case found:
            return [NO_PREFIX.sub("", flag), flag[: found.start()] + "no-" + flag[found.end() :]]


def _options(lines: List[str]) -> List[OptionDoc]:
    options: List[OptionDoc] = []
    seen: set[str] = set()
    current: Optional[dict] = None
    previous_blank = True
    previous_indent = 0

    def finish() -> None:
        if current is None:
            return
        new_flags = tuple(flag for flag in dict.fromkeys(current["flags"]) if flag not in seen)
        if new_flags:
            seen.update(new_flags)
            description = " ".join(current["lines"])
            options.append(OptionDoc(flags=new_flags, takes_arg=current["takes_arg"], description=description))

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current is not None and current["lines"]:
                current["paragraph_done"] = True
            previous_blank = True
            continue
        indent = _indent(line)
        in_body = current is not None and indent >= (
            current["desc_indent"] if current["desc_indent"] is not None else current["indent"] + 1
        )
        header = _parse_header(stripped) if indent > 0 and stripped.startswith("-") and not in_body else None
        starts_block = previous_blank or current is not None or previous_indent < indent or bool(header and header[2])
        if header is not None and current is not None and not current["lines"] and current["desc_indent"] is None:
            flags, takes_arg, inline, column = header
            current["flags"] = (*current["flags"], *flags)
            current["takes_arg"] = current["takes_arg"] or takes_arg
            if inline:
                current["lines"].append(inline)
                current["desc_indent"] = indent + column if column >= 0 else None
        elif header is not None and starts_block:
            finish()
            flags, takes_arg, inline, column = header
            current = {
                "indent": indent,
                "desc_indent": indent + column if inline and column >= 0 else None,
                "flags": flags,
                "takes_arg": takes_arg,
                "lines": [inline] if inline else [],
                "paragraph_done": False,
            }
        elif current is not None and indent > current["indent"]:
            if current["desc_indent"] is None:
                current["desc_indent"] = indent
            if not current["paragraph_done"]:
                current["lines"].append(stripped)
        else:
            finish()
            current = None
        previous_blank = False
        previous_indent = indent
    finish()
    return options


def _is_group_heading(line: str) -> bool:
    """A free-form section heading such as cobra's command groups (`Compute`, `Unity Catalog`)."""
    stripped = line.strip()
    return (
        _indent(line) == 0
        and GROUP_HEADING.match(stripped) is not None
        and stripped.removesuffix(":").strip().lower() not in NOT_COMMAND_GROUPS
    )


def _command_block(lines: List[str], start: int) -> Optional[List[Tuple[str, str]]]:
    """Entries of the block under a free-form heading, or None unless every row is a command row.

    The block runs to the next blank or unindented line. Rows indented deeper than the
    first entry are wrapped descriptions and are skipped.
    """
    entries: List[Tuple[str, str]] = []
    entry_indent: Optional[int] = None
    for line in lines[start:]:
        stripped = line.strip()
        indent = _indent(line)
        if not stripped or indent == 0:
            break
        if entry_indent is not None and indent > entry_indent:
            continue
        match SUBCOMMAND_LINE.match(stripped):
            case entry if entry is not None and indent == (entry_indent if entry_indent is not None else indent):
                entry_indent = indent
                entries.append((entry.group("name"), entry.group("description").strip()))
            case _:
                return None
    return entries or None


def _subcommands(lines: List[str]) -> List[Tuple[str, str]]:
    found: List[Tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, description: str) -> None:
        if name not in seen:
            seen.add(name)
            found.append((name, description))

    in_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        indent = _indent(line)
        if indent <= 2 and SUBCOMMAND_HEADING.match(stripped):
            in_section = True
            continue
        if indent == 0:
            in_section = False
            if _is_group_heading(line):
                for name, description in _command_block(lines, index + 1) or ():
                    add(name, description)
            continue
        if not in_section:
            continue
        match SUBCOMMAND_LINE.match(stripped):
            case None:
                continue
            case entry:
                add(entry.group("name"), entry.group("description").strip())
    return found
