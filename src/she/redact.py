"""Best-effort secret redaction for text stored in the answer cache: hits become `<REDACTED:kind>`."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Tuple

from she.models import EnvEffect, FileEffect, LlmAnswer

PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.DOTALL),
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("api-key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("databricks-token", re.compile(r"\bdapi[0-9a-f]{32}(?:-\d+)?")),
)
# Patterns where only one group (the secret value) is replaced.
VALUE_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("authorization", re.compile(r"(?i)(authorization:\s*(?:bearer|basic|token)?\s*)([^\s'\"]+)")),
    ("url-credentials", re.compile(r"([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)", re.IGNORECASE)),
    (
        "secret-assignment",
        re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY)[A-Z0-9_]*\s*=\s*['\"]?)([^\s'\"]+)"),
    ),
    ("secret-flag", re.compile(r"(?i)(--(?:password|passwd|token|secret|api-key)(?:=|\s+)['\"]?)([^\s'\"]+)")),
)
HIGH_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+_=-]{32,}")
HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")
ENTROPY_THRESHOLD = 4.0
MAX_HEX_LENGTH_KEPT = 40


def shannon_entropy(text: str) -> float:
    """Bits per character of `text`."""
    if not text:
        return 0.0
    counts = Counter(text)
    return -sum(count / len(text) * math.log2(count / len(text)) for count in counts.values())


def _is_high_entropy(token: str) -> bool:
    has_letters_and_digits = any(c.isalpha() for c in token) and any(c.isdigit() for c in token)
    looks_like_hash = bool(HEX_ONLY.match(token)) and len(token) <= MAX_HEX_LENGTH_KEPT
    return has_letters_and_digits and not looks_like_hash and shannon_entropy(token) >= ENTROPY_THRESHOLD


def redact(text: str) -> str:
    """Replace known secret formats and high-entropy strings with `<REDACTED:kind>`.

    Raises:
        TypeError: If `text` is not a string.
    """
    if not isinstance(text, str):
        raise TypeError(f"expected str to redact, got {type(text).__name__}")
    result = text
    for kind, pattern in PATTERNS:
        result = pattern.sub(f"<REDACTED:{kind}>", result)
    for kind, pattern in VALUE_PATTERNS:
        result = pattern.sub(lambda found, kind=kind: _replace_value(found, kind), result)
    return HIGH_ENTROPY_CANDIDATE.sub(
        lambda found: "<REDACTED:high-entropy>" if _is_high_entropy(found.group(0)) else found.group(0), result
    )


def _replace_value(found: re.Match[str], kind: str) -> str:
    value = found.group(2)
    if value.startswith("<REDACTED:"):
        return found.group(0)
    suffix = found.group(3) if found.re.groups >= 3 else ""
    return f"{found.group(1)}<REDACTED:{kind}>{suffix}"


def redact_answer(answer: LlmAnswer) -> LlmAnswer:
    """A copy of an LLM answer with every text field redacted (this is what the cache stores)."""
    return answer.copy(
        summary=redact(answer.summary),
        files=tuple(
            FileEffect(
                path=redact(item.path),
                op=item.op,
                basis=item.basis,
                detail=redact(item.detail) if item.detail else item.detail,
            )
            for item in answer.files
        ),
        env=tuple(
            EnvEffect(
                name=item.name,
                op=item.op,
                basis=item.basis,
                value=redact(item.value) if item.value else item.value,
                detail=item.detail,
            )
            for item in answer.env
        ),
    )
