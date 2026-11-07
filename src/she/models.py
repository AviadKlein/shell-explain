"""Output data model for `she` (the `she/v1` JSON schema)."""

from __future__ import annotations

from typing import Dict, Literal, Optional, Self, Tuple

from pydantic import BaseModel, ConfigDict, Field

FileOp = Literal["read", "write", "append", "delete", "create", "chmod"]
EnvOp = Literal["read", "set", "unset"]
Basis = Literal["visible", "inferred"]
ItemKind = Literal[
    "command",
    "subcommand",
    "option",
    "argument",
    "operator",
    "redirect",
    "assignment",
    "keyword",
    "function",
    "substitution",
]
CacheState = Literal["hit", "miss", "off"]
InstructionsSource = Literal["ad-hoc", "local", "global", "none"]

SCHEMA_VERSION = "she/v1"
FILE_OPS: Tuple[str, ...] = ("read", "write", "append", "delete", "create", "chmod")
ENV_OPS: Tuple[str, ...] = ("read", "set", "unset")


class FrozenModel(BaseModel):
    """Base for all immutable output models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def copy(self, **changes: object) -> Self:  # type: ignore[override]
        """Return a new instance with selected fields replaced."""
        return self.model_copy(update=changes)


class BreakdownItem(FrozenModel):
    """One explained piece of the command line."""

    span: Tuple[int, int] = Field(description="Start and end character offsets in the command")
    text: str
    kind: ItemKind
    help: Optional[str] = None
    source: Optional[str] = Field(default=None, description="e.g. man:find(1), help:uv pip@0.9.4, builtin, syntax")


class FileEffect(FrozenModel):
    path: str
    op: FileOp
    basis: Basis
    detail: Optional[str] = None


class EnvEffect(FrozenModel):
    name: str
    op: EnvOp
    basis: Basis
    value: Optional[str] = None
    detail: Optional[str] = None


class Unknown(FrozenModel):
    text: str
    reason: str


class LlmAnswer(FrozenModel):
    """What the LLM contributes; this is also the answer-cache value."""

    summary: str
    files: Tuple[FileEffect, ...] = ()
    env: Tuple[EnvEffect, ...] = ()


class Summary(FrozenModel):
    text: str
    source: Literal["llm"] = "llm"
    model: str
    cache: CacheState


class InstructionsInfo(FrozenModel):
    source: InstructionsSource
    path: Optional[str] = None


class Explanation(FrozenModel):
    """Complete result for one command."""

    schema_version: str = Field(default=SCHEMA_VERSION, serialization_alias="schema")
    command: str
    summary: Optional[Summary] = None
    instructions: InstructionsInfo
    breakdown: Tuple[BreakdownItem, ...] = ()
    files: Tuple[FileEffect, ...] = ()
    env: Tuple[EnvEffect, ...] = ()
    unknowns: Tuple[Unknown, ...] = ()
    warnings: Tuple[str, ...] = ()
    timing_ms: Dict[str, int] = Field(default_factory=dict)
