"""The deterministic layer: parse, look up man/help pages, match options, collect effects."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import List, Mapping, Optional, Self, Sequence, Tuple

from she import effects
from she.effects import Invocation
from she.helppages import HelpLearner
from she.knowledge import (
    BUILTINS,
    KEYWORDS,
    OPERATORS,
    REDIRECTS,
    SHELLS_WITH_INLINE_SCRIPT,
    SUBSTITUTIONS,
    WRAPPER_LEADING_POSITIONALS,
    WRAPPERS,
)
from she.manpages import ManPages
from she.models import BreakdownItem, EnvEffect, FileEffect, ItemKind, Unknown
from she.parser import ParsedCommand, Redirect, SimpleCommand, Word, parse
from she.paths import Paths
from she.textdoc import PageDoc

MAX_WRAPPER_DEPTH = 4
MAX_SUBCOMMAND_DEPTH = 3
FIND_EXEC_PRIMARIES = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
FOUND_FILE_PLACEHOLDER = "<each found file>"
NO_PAGE_REASON = "no man page or help found"
SUBCOMMAND_WORD = "abcdefghijklmnopqrstuvwxyz0123456789-_"
DYNAMIC_MARKERS = ("$", "`")


@dataclass(frozen=True, slots=True)
class PageRef:
    """A man or help page in a lookup chain, with the source label shown to users."""

    doc: PageDoc
    source: str


@dataclass(frozen=True, slots=True)
class OptionMatch:
    help: str
    source: str
    takes_next: bool


@dataclass(frozen=True, slots=True)
class Analysis:
    """Everything the deterministic layer knows about one command.

    Attributes:
        command: The command text as given.
        breakdown: Explained spans, sorted and non-overlapping.
        files: Visible file effects.
        env: Visible environment effects.
        unknowns: What could not be resolved (also sent to the LLM).
        warnings: Human-readable warnings (e.g. parse errors).
        facts_text: The compact fact sheet sent to the LLM (and hashed into the cache key).
        sources: Distinct page sources used, e.g. "man:find(1)".
        has_parse_error: True when tree-sitter reported an error.
        timings: Milliseconds spent per phase ("parse", "lookup").
    """

    command: str
    breakdown: Tuple[BreakdownItem, ...] = ()
    files: Tuple[FileEffect, ...] = ()
    env: Tuple[EnvEffect, ...] = ()
    unknowns: Tuple[Unknown, ...] = ()
    warnings: Tuple[str, ...] = ()
    facts_text: str = ""
    sources: Tuple[str, ...] = ()
    has_parse_error: bool = False
    timings: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}), compare=False, hash=False)

    def copy(self, **changes: object) -> Self:
        return replace(self, **changes)


class _Collector:
    """Mutable accumulator used while analysing one command (internal)."""

    def __init__(self, command: str, functions: frozenset[str]) -> None:
        self.command = command
        self.functions = functions
        self.items: List[BreakdownItem] = []
        self.unknowns: List[Unknown] = []
        self.invocations: List[Invocation] = []

    def item(self, span: Tuple[int, int], kind: ItemKind, help_text: Optional[str], source: Optional[str]) -> None:
        self.items.append(
            BreakdownItem(span=span, text=self.command[span[0] : span[1]], kind=kind, help=help_text, source=source)
        )

    def unknown(self, text: str, reason: str) -> None:
        candidate = Unknown(text=text, reason=reason)
        if candidate not in self.unknowns:
            self.unknowns.append(candidate)


class Explainer:
    """Builds an `Analysis` for a command using local man pages and learned help pages.

    Lookup order for a command word: function defined in the command → builtin → path with
    a slash (unknown, never inspected) → man page → learned help pages → learn → unknown.
    """

    def __init__(self, man: Optional[ManPages], learner: Optional[HelpLearner], learn: bool = True) -> None:
        self.__man = man
        self.__learner = learner
        self.__learn = learn

    @classmethod
    def from_paths(cls, paths: Paths, learn: bool = True) -> Self:
        """The standard explainer: pages.db under `paths.cache_dir`, sandboxed learning if available."""
        from she.sandbox import Sandbox
        from she.store import PageStore

        store = PageStore.from_path(paths.pages_db)
        return cls(man=ManPages.from_env(store), learner=HelpLearner.from_env(store, Sandbox.from_system()), learn=learn)

    def analyze(self, command: str) -> Analysis:
        """Analyse one command string.

        Raises:
            TypeError: If `command` is not a string.
            ValueError: If `command` is empty or only whitespace.
        """
        if not isinstance(command, str):
            raise TypeError(f"expected str command, got {type(command).__name__}")
        if not command.strip():
            raise ValueError(f"expected a non-empty command, got {command=}")
        started = time.perf_counter()
        parsed = parse(command)
        parsed_at = time.perf_counter()
        collector = _Collector(command, parsed.function_names)
        self._syntax_items(parsed, collector)
        for simple in parsed.commands:
            self._simple_command(simple, collector)
        if parsed.has_errors:
            for start, end in parsed.error_spans:
                snippet = command[start:end].strip() or command[max(0, start - 10) : end + 1].strip()
                collector.unknown(snippet[:60] or "(end of input)", "could not be parsed")
        files, env = effects.collect(parsed, collector.invocations)
        finished = time.perf_counter()
        breakdown = _non_overlapping(collector.items)
        unknowns = tuple(collector.unknowns)
        sources = tuple(dict.fromkeys(item.source for item in breakdown if item.source and ":" in item.source))
        warnings = ("the command has a syntax error; the explanation may be incomplete",) if parsed.has_errors else ()
        return Analysis(
            command=command,
            breakdown=breakdown,
            files=files,
            env=env,
            unknowns=unknowns,
            warnings=warnings,
            facts_text=facts_text(breakdown, files, env, unknowns),
            sources=sources,
            has_parse_error=parsed.has_errors,
            timings=MappingProxyType(
                {"parse": round((parsed_at - started) * 1000), "lookup": round((finished - parsed_at) * 1000)}
            ),
        )

    # -- syntax ---------------------------------------------------------------------------

    def _syntax_items(self, parsed: ParsedCommand, out: _Collector) -> None:
        for token in parsed.operators:
            out.item(token.span, "operator", OPERATORS.get(token.text), "syntax")
        for token in parsed.keywords:
            out.item(token.span, "keyword", KEYWORDS.get(token.text), "syntax")
        for token in parsed.functions:
            out.item(token.span, "function", f"define a shell function named {token.text}", "syntax")
        for token in parsed.substitutions:
            out.item(token.span, "substitution", SUBSTITUTIONS.get(token.text), "syntax")
        for assignment in parsed.shell_assignments:
            out.item(assignment.span, "assignment", f"set shell variable {assignment.name}", "syntax")

    def _simple_command(self, simple: SimpleCommand, out: _Collector) -> None:
        for redirect in simple.redirects:
            out.item(redirect.span, "redirect", redirect_help(redirect), "syntax")
        match simple.kind:
            case "redirect-only":
                return
            case "declaration":
                name = simple.name.text
                out.item(simple.name.span, "command", BUILTINS.get(name), "builtin")
                verb = "set and export" if name == "export" else "declare"
                for assignment in simple.assignments:
                    out.item(assignment.span, "assignment", f"{verb} variable {assignment.name}", "syntax")
                for word in simple.args:
                    if word.text.startswith("-"):
                        out.item(word.span, "option", f"{name} option {word.text}", "builtin")
                    else:
                        out.item(word.span, "argument", f"{verb} variable {word.text}", "syntax")
                return
            case "unset":
                out.item(simple.name.span, "command", BUILTINS.get("unset"), "builtin")
                for word in simple.args:
                    if not word.text.startswith("-"):
                        out.item(word.span, "argument", f"remove variable {word.text}", "syntax")
                return
            case _:
                for assignment in simple.assignments:
                    out.item(assignment.span, "assignment", f"set {assignment.name} for this command only", "syntax")
                self._invocation(simple.name, simple.args, out, depth=0, env=())

    # -- commands -------------------------------------------------------------------------

    def _invocation(
        self, name: Word, args: Sequence[Word], out: _Collector, depth: int, env: Tuple[Tuple[str, Optional[str]], ...]
    ) -> None:
        text = name.text
        arg_texts = tuple(word.text for word in args)
        match text:
            case "":
                return
            case _ if any(marker in name.raw for marker in DYNAMIC_MARKERS):
                out.item(name.span, "command", "command name comes from an expansion (only known at run time)", None)
                out.unknown(name.raw, "command name is computed at run time")
            case _ if text in out.functions:
                out.item(name.span, "command", f"call the shell function {text} defined in this command", "syntax")
                out.invocations.append(Invocation(name=text, args=arg_texts, env=env))
            case _ if text in WRAPPERS and depth < MAX_WRAPPER_DEPTH:
                self._wrapper(name, args, out, depth, env)
            case _ if text in BUILTINS:
                self._builtin(name, args, out)
                out.invocations.append(Invocation(name=text, args=arg_texts, env=env))
            case _ if "/" in text:
                out.item(name.span, "command", "run the program or script at this path (not inspected)", None)
                out.unknown(text, "path to a program or script; its contents are not inspected")
                out.invocations.append(Invocation(name=text, args=arg_texts, env=env))
            case _:
                self._external(name, args, out, depth, env)

    def _builtin(self, name: Word, args: Sequence[Word], out: _Collector) -> None:
        text = name.text
        out.item(name.span, "command", BUILTINS[text], "builtin")
        operands = [word for word in args if not word.text.startswith("-")]
        match text:
            case "cd" | "pushd" if operands:
                out.item(operands[0].span, "argument", "directory to change to", None)
            case "source" | ".":
                target = operands[0].text if operands else "(no file)"
                out.unknown(f"{text} {target}", "the file runs in the current shell; its contents are not analysed")
            case "eval":
                out.unknown("eval " + " ".join(word.raw for word in args), "eval runs text built at run time; not analysed")
            case _:
                pass

    def _wrapper(
        self, name: Word, args: Sequence[Word], out: _Collector, depth: int, env: Tuple[Tuple[str, Optional[str]], ...]
    ) -> None:
        text = name.text
        chain = self._man_chain(text, [], [])[0]
        description = chain[0].doc.description if chain else None
        source = chain[0].source if chain else ("builtin" if text in BUILTINS else None)
        out.item(name.span, "command", description or BUILTINS.get(text) or "run another command", source)
        value_options = WRAPPERS[text]
        leading = WRAPPER_LEADING_POSITIONALS.get(text, 0)
        wrapped_env = list(env)
        index = 0
        while index < len(args):
            word = args[index]
            match word.text:
                case "--":
                    out.item(word.span, "argument", "end of options", "syntax")
                    index += 1
                    break
                case flag if flag.startswith("-") and flag != "-":
                    match_ = _match_option(flag, list(reversed(chain)))
                    takes_next = flag in value_options or (match_ is not None and match_.takes_next)
                    help_text = match_.help if match_ is not None else f"{text} option"
                    end = index + 1 if takes_next and index + 1 < len(args) else index
                    out.item((word.span[0], args[end].span[1]), "option", help_text, match_.source if match_ else source)
                    index = end + 1
                case assignment if text == "env" and "=" in assignment and assignment.split("=", 1)[0].isidentifier():
                    variable, value = assignment.split("=", 1)
                    wrapped_env.append((variable, value))
                    out.item(word.span, "assignment", f"set {variable} for the wrapped command", "syntax")
                    index += 1
                case _ if leading > 0:
                    out.item(word.span, "argument", f"{text} argument", source)
                    leading -= 1
                    index += 1
                case _:
                    break
        inner = list(args[index:])
        if text == "xargs":
            out.unknown("xargs", "arguments are read from stdin at run time and are not visible")
        if inner:
            self._invocation(inner[0], inner[1:], out, depth + 1, tuple(wrapped_env))
        else:
            out.invocations.append(Invocation(name=text, args=tuple(word.text for word in args), env=tuple(wrapped_env)))

    def _external(
        self, name: Word, args: Sequence[Word], out: _Collector, depth: int, env: Tuple[Tuple[str, Optional[str]], ...]
    ) -> None:
        text = name.text
        arg_texts = [word.text for word in args]
        candidates = [(index, word) for index, word in enumerate(arg_texts) if not word.startswith("-")]
        chain, used = self._pages(text, candidates)
        if text in SHELLS_WITH_INLINE_SCRIPT:
            _shell_unknowns(text, arg_texts, out)
        if not chain:
            out.item(name.span, "command", NO_PAGE_REASON, None)
            out.unknown(text, NO_PAGE_REASON)
            out.invocations.append(Invocation(name=text, args=tuple(arg_texts), env=env))
            return
        out.item(name.span, "command", chain[0].doc.description, chain[0].source)
        for position, index in enumerate(used):
            parent = chain[position].doc
            page = chain[position + 1]
            out.item(args[index].span, "subcommand", page.doc.description or parent.subcommand_help(arg_texts[index]), page.source)
        rest = [word for index, word in enumerate(args) if index not in set(used)]
        self._options(text, rest, chain, out, depth, env)

    def _options(
        self,
        command: str,
        words: Sequence[Word],
        chain: Sequence[PageRef],
        out: _Collector,
        depth: int,
        env: Tuple[Tuple[str, Optional[str]], ...],
    ) -> None:
        deepest_first = list(reversed(chain))
        options_done = False
        seen_option = False
        args: List[str] = []
        index = 0
        while index < len(words):
            word = words[index]
            flag = word.text
            if options_done or not flag.startswith("-") or flag == "-":
                if command == "find" and not seen_option:
                    out.item(word.span, "argument", "starting directory", None)
                args.append(flag)
                index += 1
                continue
            if flag == "--":
                out.item(word.span, "argument", "end of options: everything after is a positional argument", "syntax")
                options_done = True
                args.append(flag)
                index += 1
                continue
            seen_option = True
            if command == "find" and flag in FIND_EXEC_PRIMARIES:
                index = self._find_exec(words, index, deepest_first, out, depth, env, args)
                continue
            found = _match_option(flag, deepest_first)
            if found is None:
                out.item(word.span, "option", None, None)
                args.append(flag)
                index += 1
                continue
            has_value = found.takes_next and index + 1 < len(words)
            value = words[index + 1] if has_value else None
            mergeable = value is not None and not any(marker in value.raw for marker in ("$(", "`", "<(", ">("))
            end = value.span[1] if mergeable and value is not None else word.span[1]
            out.item((word.span[0], end), "option", found.help, found.source)
            args.append(flag)
            if value is not None:
                args.append(value.text)
            index += 2 if has_value else 1
        out.invocations.append(Invocation(name=command, args=tuple(args), env=env))

    def _find_exec(
        self,
        words: Sequence[Word],
        index: int,
        deepest_first: Sequence[PageRef],
        out: _Collector,
        depth: int,
        env: Tuple[Tuple[str, Optional[str]], ...],
        args: List[str],
    ) -> int:
        """`find ... -exec cmd {} ;` — explain the inner command; returns the index after the terminator."""
        word = words[index]
        found = _match_option(word.text, deepest_first)
        out.item(word.span, "option", found.help if found else "run a command on each found file", found.source if found else None)
        terminator = next(
            (position for position in range(index + 1, len(words)) if words[position].text in {";", "+"}), len(words)
        )
        inner = [
            Word(text=FOUND_FILE_PLACEHOLDER, raw=item.raw, span=item.span) if item.text == "{}" else item
            for item in words[index + 1 : terminator]
        ]
        if inner and depth < MAX_WRAPPER_DEPTH:
            self._invocation(inner[0], inner[1:], out, depth + 1, env)
        if terminator < len(words):
            ending = words[terminator]
            help_text = "end of the -exec command" + (" (pass many files per run)" if ending.text == "+" else "")
            out.item(ending.span, "argument", help_text, None)
        args.append(word.text)
        return terminator + 1

    # -- page lookup -----------------------------------------------------------------------

    def _pages(self, name: str, candidates: Sequence[Tuple[int, str]]) -> Tuple[List[PageRef], List[int]]:
        """The page chain (root first) and the argument indices consumed as subcommands."""
        chain, used = self._man_chain(name, [], list(candidates))
        if chain:
            return chain, used
        if self.__learner is None:
            return [], []
        words = [word for _, word in candidates[:MAX_SUBCOMMAND_DEPTH]]
        pages = self.__learner.chain(name, words, self.__learn)
        refs = [PageRef(doc=page.doc, source=page.source) for page in pages]
        return refs, [index for index, _ in candidates[: max(0, len(refs) - 1)]]

    def _man_chain(
        self, name: str, prefix: List[str], candidates: List[Tuple[int, str]]
    ) -> Tuple[List[PageRef], List[int]]:
        if self.__man is None:
            return [], []
        root = self.__man.load(name)
        if root is None:
            return [], []
        chain = [PageRef(doc=root.doc, source=root.source)]
        used: List[int] = []
        words = list(prefix)
        for index, word in candidates[:MAX_SUBCOMMAND_DEPTH]:
            if not word or not all(character in SUBCOMMAND_WORD for character in word) or not word[0].isalpha():
                break
            page = self.__man.load("-".join([name, *words, word]), fallback=False)
            if page is None:
                break
            chain.append(PageRef(doc=page.doc, source=page.source))
            used.append(index)
            words.append(word)
        return chain, used


def _shell_unknowns(shell: str, args: Sequence[str], out: _Collector) -> None:
    """A shell runs code `she` can't see: an inline `-c` script, a script file, or commands from stdin."""
    if any(word.startswith("-") and not word.startswith("--") and "c" in word[1:] for word in args):
        out.unknown(f"{shell} -c …", "inline script passed to the shell is not analysed")
        return
    scripts = [word for word in args if not word.startswith(("-", "+"))]
    if scripts:
        out.unknown(f"{shell} {scripts[0]}", "script file run by the shell; its contents are not analysed")
    else:
        out.unknown(f"{shell} (stdin)", "the shell runs commands read from stdin; they are not analysed")


def _match_option(flag: str, deepest_first: Sequence[PageRef]) -> Optional[OptionMatch]:
    """Match a flag against pages: exact, `--x=v`, attached short value `-n5`, combined shorts `-rf`."""
    for page in deepest_first:
        option = page.doc.lookup(flag)
        if option is not None:
            return OptionMatch(help=option.summary, source=page.source, takes_next=option.takes_arg)
    if flag.startswith("--") and "=" in flag:
        name = flag.split("=", 1)[0]
        for page in deepest_first:
            option = page.doc.lookup(name)
            if option is not None:
                return OptionMatch(help=option.summary, source=page.source, takes_next=False)
        return None
    if flag.startswith("--") or len(flag) <= 2:
        return None
    for page in deepest_first:
        option = page.doc.lookup(flag[:2])
        if option is not None and option.takes_arg:
            return OptionMatch(help=f"{option.summary} (value: {flag[2:]})", source=page.source, takes_next=False)
    parts: List[str] = []
    source: Optional[str] = None
    takes_next = False
    letters = flag[1:]
    for position, letter in enumerate(letters):
        located = next(
            ((page, page.doc.lookup(f"-{letter}")) for page in deepest_first if page.doc.lookup(f"-{letter}") is not None),
            None,
        )
        if located is None:
            return None
        page, option = located
        assert option is not None
        parts.append(f"-{letter}: {option.summary}")
        source = source or page.source
        if option.takes_arg:
            takes_next = position == len(letters) - 1
            break
    return OptionMatch(help="; ".join(parts), source=source or "", takes_next=takes_next)


def redirect_help(redirect: Redirect) -> Optional[str]:
    """Plain-English help for a redirect, naming the stream (output, errors, fd N)."""
    streams = {None: "output", "1": "output", "2": "errors", "0": "input"}
    stream = streams.get(redirect.descriptor, f"file descriptor {redirect.descriptor}")
    target = redirect.target.text if redirect.target is not None else ""
    match redirect.operator:
        case ">&" if target.isdigit():
            return f"send {stream} to the same place as {streams.get(target, f'file descriptor {target}')}"
        case ">&" if target == "-":
            return f"close {stream}"
        case ">" | ">>" | ">|" if redirect.descriptor not in (None, "1"):
            base = REDIRECTS[redirect.operator]
            return base.replace("output", stream)
        case operator:
            return REDIRECTS.get(operator)


def _non_overlapping(items: Sequence[BreakdownItem]) -> Tuple[BreakdownItem, ...]:
    """Sort items by position, dropping any that overlap an earlier (or longer) one."""
    ordered = sorted(items, key=lambda item: (item.span[0], -(item.span[1] - item.span[0])))
    kept: List[BreakdownItem] = []
    for item in ordered:
        if item.span[1] <= item.span[0]:
            continue
        if kept and item.span[0] < kept[-1].span[1]:
            continue
        kept.append(item)
    return tuple(kept)


def facts_text(
    breakdown: Sequence[BreakdownItem],
    files: Sequence[FileEffect],
    env: Sequence[EnvEffect],
    unknowns: Sequence[Unknown],
) -> str:
    """The compact, deterministic fact sheet sent to the LLM (never whole pages)."""
    pieces = [
        f"- {item.text} [{item.kind}] {item.help or '(no description found)'}"
        + (f" ({item.source})" if item.source and ":" in item.source else "")
        for item in breakdown
    ]
    file_lines = [f"- {effect.op} {effect.path}" for effect in files]
    env_lines = [
        f"- {effect.op} {effect.name}" + (f"={effect.value}" if effect.value is not None else "") for effect in env
    ]
    unknown_lines = [f"- {unknown.text}: {unknown.reason}" for unknown in unknowns]
    sections = [
        ("Pieces:", pieces),
        ("Visible file effects:", file_lines),
        ("Visible env effects:", env_lines),
        ("Unknowns (you are guessing about these):", unknown_lines),
    ]
    return "\n".join(line for title, body in sections for line in (title, *(body or ["- none"])))


def default_explainer(learn: bool) -> Explainer:
    return Explainer.from_paths(Paths.from_env(), learn=learn)

