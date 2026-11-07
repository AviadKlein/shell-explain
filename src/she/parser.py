"""Shell syntax analysis with tree-sitter-bash.

Turns a command string into simple commands, operators, redirects, assignments and
environment reads, with character spans into the original text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional, Tuple

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

OPERATOR_TOKENS = frozenset({"|", "|&", "&&", "||", ";", "&"})
KEYWORD_TOKENS = frozenset({"if", "for", "while", "until", "case", "select", "!"})
REDIRECT_NODE_TYPES = frozenset({"file_redirect", "heredoc_redirect", "herestring_redirect"})
SPECIAL_VARIABLES = frozenset({"@", "*", "#", "?", "-", "$", "!", "0", "_"})


@dataclass(frozen=True, slots=True)
class Word:
    """A shell word: `text` is unquoted (best effort), `raw` is as written."""

    text: str
    raw: str
    span: Tuple[int, int]


@dataclass(frozen=True, slots=True)
class Redirect:
    operator: str
    target: Optional[Word]
    descriptor: Optional[str]
    span: Tuple[int, int]


@dataclass(frozen=True, slots=True)
class Assignment:
    name: str
    value: Optional[str]
    span: Tuple[int, int]


@dataclass(frozen=True, slots=True)
class SimpleCommand:
    """A command invocation: optional prefix assignments, a name, arguments, redirects.

    `kind` is "command" for ordinary commands, or "declaration" / "unset" for the
    `export`/`declare`/`local`/`readonly` and `unset` builtins, which bash parses specially.
    """

    name: Word
    args: Tuple[Word, ...] = ()
    assignments: Tuple[Assignment, ...] = ()
    redirects: Tuple[Redirect, ...] = ()
    kind: str = "command"


@dataclass(frozen=True, slots=True)
class Token:
    """An operator, keyword, function definition or substitution marker."""

    text: str
    span: Tuple[int, int]


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    source: str
    commands: Tuple[SimpleCommand, ...] = ()
    operators: Tuple[Token, ...] = ()
    keywords: Tuple[Token, ...] = ()
    functions: Tuple[Token, ...] = ()
    substitutions: Tuple[Token, ...] = ()
    shell_assignments: Tuple[Assignment, ...] = ()
    env_reads: Tuple[Token, ...] = ()
    error_spans: Tuple[Tuple[int, int], ...] = ()
    loop_variables: Tuple[str, ...] = ()

    @property
    def has_errors(self) -> bool:
        return bool(self.error_spans)

    @property
    def function_names(self) -> frozenset[str]:
        return frozenset(token.text for token in self.functions)


@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(Language(tree_sitter_bash.language()))


def parse(source: str) -> ParsedCommand:
    """Parse a shell command string.

    Args:
        source: The command text exactly as it would be run.

    Returns:
        The syntax facts. Parse errors don't raise; they are reported in `error_spans`.

    Raises:
        TypeError: If `source` is not a string.
    """
    if not isinstance(source, str):
        raise TypeError(f"expected str command, got {type(source).__name__}")
    tree = _parser().parse(source.encode("utf-8"))
    walker = _Walker(source)
    walker.visit(tree.root_node)
    return walker.result()


def split_statements(text: str) -> List[str]:
    """Split a script into its top-level statements (heredocs and continuations stay whole)."""
    if not isinstance(text, str):
        raise TypeError(f"expected str script, got {type(text).__name__}")
    encoded = text.encode("utf-8")
    root = _parser().parse(encoded).root_node
    statements: List[Tuple[int, int]] = []
    for child in root.named_children:
        match child.type:
            case "comment":
                continue
            case "heredoc_body" if statements:
                statements[-1] = (statements[-1][0], child.end_byte)
            case _:
                statements.append((child.start_byte, child.end_byte))
    return [encoded[start:end].decode("utf-8").strip() for start, end in statements if encoded[start:end].strip()]


class _Walker:
    """Collects syntax facts while walking a tree (internal, mutable while building)."""

    def __init__(self, source: str) -> None:
        self.__source = source
        self.__encoded = source.encode("utf-8")
        self.__commands: List[SimpleCommand] = []
        self.__operators: List[Token] = []
        self.__keywords: List[Token] = []
        self.__functions: List[Token] = []
        self.__substitutions: List[Token] = []
        self.__shell_assignments: List[Assignment] = []
        self.__env_reads: List[Token] = []
        self.__errors: List[Tuple[int, int]] = []
        self.__loop_variables: List[str] = []

    def result(self) -> ParsedCommand:
        return ParsedCommand(
            source=self.__source,
            commands=tuple(self.__commands),
            operators=tuple(self.__operators),
            keywords=tuple(self.__keywords),
            functions=tuple(self.__functions),
            substitutions=tuple(self.__substitutions),
            shell_assignments=tuple(self.__shell_assignments),
            env_reads=tuple(self.__env_reads),
            error_spans=tuple(self.__errors),
            loop_variables=tuple(self.__loop_variables),
        )

    # -- helpers -------------------------------------------------------------------

    def _char(self, byte_offset: int) -> int:
        return len(self.__encoded[:byte_offset].decode("utf-8", errors="ignore"))

    def _span(self, node: Node) -> Tuple[int, int]:
        return (self._char(node.start_byte), self._char(node.end_byte))

    def _raw(self, node: Node) -> str:
        return self.__encoded[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def _word(self, node: Node) -> Word:
        return Word(text=_unquote(node, self._raw(node), self._raw), raw=self._raw(node), span=self._span(node))

    # -- traversal -----------------------------------------------------------------

    def visit(self, node: Node) -> None:
        if node.is_missing:
            self.__errors.append(self._span(node))
            return
        match node.type:
            case "ERROR":
                self.__errors.append(self._span(node))
                self._visit_children(node)
            case "command":
                self._command(node, ())
            case "redirected_statement":
                self._redirected(node)
            case "declaration_command":
                self._declaration(node)
            case "unset_command":
                self._unset(node)
            case "variable_assignment":
                self.__shell_assignments.append(self._assignment(node))
                self._visit_children(node)
            case "function_definition":
                name = node.child_by_field_name("name")
                if name is not None:
                    self.__functions.append(Token(text=self._raw(name), span=self._span(name)))
                body = node.child_by_field_name("body")
                if body is not None:
                    self.visit(body)
            case "command_substitution" | "process_substitution":
                opening = node.children[0] if node.children else node
                self.__substitutions.append(Token(text=self._raw(opening), span=self._span(opening)))
                self._visit_children(node)
            case "simple_expansion" | "expansion":
                self._expansion(node)
            case "for_statement" | "select_statement":
                variable = node.child_by_field_name("variable")
                if variable is not None:
                    self.__loop_variables.append(self._raw(variable))
                self._visit_children(node)
            case "comment" | "heredoc_start" | "heredoc_end":
                return
            case _ if not node.is_named and node.type in OPERATOR_TOKENS:
                self.__operators.append(Token(text=node.type, span=self._span(node)))
            case _ if not node.is_named and node.type in KEYWORD_TOKENS:
                self.__keywords.append(Token(text=node.type, span=self._span(node)))
            case _:
                self._visit_children(node)

    def _visit_children(self, node: Node) -> None:
        for child in node.children:
            self.visit(child)

    def _visit_redirect_parts(self, node: Node) -> None:
        """Visit a redirect's own parts; nested redirects (after a heredoc) are handled separately."""
        for child in node.children:
            if child.type not in REDIRECT_NODE_TYPES:
                self.visit(child)

    def _expansion(self, node: Node) -> None:
        names = [child for child in node.named_children if child.type == "variable_name"]
        if names:
            name = self._raw(names[0])
            if name not in SPECIAL_VARIABLES and not name.isdigit():
                self.__env_reads.append(Token(text=name, span=self._span(node)))
        for child in node.named_children:
            if child.type != "variable_name":
                self.visit(child)

    def _redirected(self, node: Node) -> None:
        body = node.child_by_field_name("body")
        redirect_nodes = _flatten_redirects([child for child in node.named_children if child.type in REDIRECT_NODE_TYPES])
        match body.type if body is not None else None:
            case "command":
                self._command(body, tuple(redirect_nodes))
            case None:
                pass
            case _:
                self.visit(body)
                orphan = [self._redirect(redirect) for redirect in redirect_nodes]
                if orphan:
                    self.__commands.append(
                        SimpleCommand(
                            name=Word(text="", raw="", span=orphan[0].span), redirects=tuple(orphan), kind="redirect-only"
                        )
                    )

    def _command(self, node: Node, extra_redirects: Tuple[Node, ...]) -> None:
        name_node = node.child_by_field_name("name")
        arg_nodes = node.children_by_field_name("argument")
        assignment_nodes = [child for child in node.named_children if child.type == "variable_assignment"]
        redirect_nodes = _flatten_redirects([child for child in node.named_children if child.type in REDIRECT_NODE_TYPES])
        redirect_nodes.extend(extra_redirects)
        for child in arg_nodes:
            self.visit(child)
        for child in [*assignment_nodes, *redirect_nodes]:
            self._visit_redirect_parts(child) if child.type in REDIRECT_NODE_TYPES else self._visit_children(child)
        if name_node is not None:
            self._visit_children(name_node)
        if name_node is None:
            self.__shell_assignments.extend(self._assignment(child) for child in assignment_nodes)
            return
        word_node = name_node.named_children[0] if name_node.named_children else name_node
        self.__commands.append(
            SimpleCommand(
                name=self._word(word_node),
                args=tuple(self._word(arg) for arg in arg_nodes),
                assignments=tuple(self._assignment(child) for child in assignment_nodes),
                redirects=tuple(self._redirect(redirect) for redirect in redirect_nodes),
            )
        )

    def _declaration(self, node: Node) -> None:
        keyword = node.children[0]
        args: List[Word] = []
        assignments: List[Assignment] = []
        for child in node.named_children:
            match child.type:
                case "variable_assignment":
                    assignments.append(self._assignment(child))
                    self._visit_children(child)
                case _:
                    args.append(self._word(child))
                    self.visit(child)
        self.__commands.append(
            SimpleCommand(
                name=self._word(keyword), args=tuple(args), assignments=tuple(assignments), kind="declaration"
            )
        )

    def _unset(self, node: Node) -> None:
        keyword = node.children[0]
        args = tuple(self._word(child) for child in node.named_children)
        self.__commands.append(SimpleCommand(name=self._word(keyword), args=args, kind="unset"))

    def _assignment(self, node: Node) -> Assignment:
        name_node = node.child_by_field_name("name")
        value_node = node.child_by_field_name("value")
        name = self._raw(name_node) if name_node is not None else self._raw(node).split("=", 1)[0]
        value = _unquote(value_node, self._raw(value_node), self._raw) if value_node is not None else None
        return Assignment(name=name, value=value, span=self._span(node))

    def _redirect(self, node: Node) -> Redirect:
        descriptor_nodes = [child for child in node.named_children if child.type == "file_descriptor"]
        operator_nodes = [child for child in node.children if not child.is_named]
        destination = node.child_by_field_name("destination")
        if destination is None:
            candidates = [
                child
                for child in node.named_children
                if child.type not in {"file_descriptor", "heredoc_body", "heredoc_start"}
            ]
            destination = candidates[0] if candidates else None
        span = self._span(node)
        match node.type:
            case "heredoc_redirect":
                operator = self._raw(operator_nodes[0]) if operator_nodes else "<<"
                target = None
                starts = [child for child in node.named_children if child.type == "heredoc_start"]
                span = (span[0], self._span(starts[0])[1]) if starts else span
            case "herestring_redirect":
                operator = "<<<"
                target = self._word(destination) if destination is not None else None
            case _:
                operator = "".join(self._raw(child) for child in operator_nodes) or ">"
                target = self._word(destination) if destination is not None else None
        return Redirect(
            operator=operator,
            target=target,
            descriptor=self._raw(descriptor_nodes[0]) if descriptor_nodes else None,
            span=span,
        )


def _flatten_redirects(nodes: List[Node]) -> List[Node]:
    """Redirects in source order, including those tree-sitter nests inside a heredoc redirect."""
    flat: List[Node] = []
    for node in nodes:
        flat.append(node)
        flat.extend(_flatten_redirects([child for child in node.named_children if child.type in REDIRECT_NODE_TYPES]))
    return flat


_ESCAPE = re.compile(r"\\(.)", re.DOTALL)


def _unquote(node: Node, raw: str, raw_of) -> str:  # noqa: ANN001 - raw_of is the walker's slicer
    """Best-effort removal of shell quoting; expansions are kept as written."""
    match node.type:
        case "raw_string":
            return raw[1:-1]
        case "ansi_c_string":
            return raw[2:-1]
        case "string":
            return raw[1:-1]
        case "concatenation":
            return "".join(_unquote(child, raw_of(child), raw_of) for child in node.children)
        case "word" | "command_name":
            return _ESCAPE.sub(r"\1", raw)
        case _:
            return raw
