"""Best-effort credential detection and redaction.

Nightshift delegates authentication to already logged-in official CLI clients and never
reads their credential stores.  These helpers are a defence-in-depth barrier for text
that a command, repository file, model, or watcher artifact accidentally exposes.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_MASK = "***REDACTED***"

# Patterns intentionally retain useful prefixes where possible.  Named labels are also
# used by the pre-commit secret gate, which reports only the label and path, never the
# matched value.
_NAMED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("Anthropic-style key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b")),
    ("xAI-style key", re.compile(r"\bxai-[A-Za-z0-9_\-]{16,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b")),
    (
        "Bearer credential",
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._~+/\-=]+"),
    ),
    (
        "key or token assignment",
        re.compile(
            r"(?i)((?:x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"client[_-]?secret|private[_-]?key|password|passwd)\s*[:=]\s*)"
            r"[\"']?[A-Za-z0-9._~+/\-=]{12,}[\"']?"
        ),
    ),
    ("credential in URL", re.compile(r"(?i)(https?://[^\s/:]+:)[^@\s/]+(@)")),
    (
        "credential in query string",
        re.compile(r"(?i)([?&](?:token|key|api_key|access_token|secret)=)[^&#\s]+"),
    ),
    (
        "private key block",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
            re.I | re.S,
        ),
    ),
)


def secret_findings(text: str) -> list[str]:
    """Return credential-shape labels without returning matched secret values."""
    if not text:
        return []
    return [label for label, pattern in _NAMED_PATTERNS if pattern.search(text)]


def contains_secret(text: str) -> bool:
    return bool(secret_findings(text))


def redact(text: str) -> str:
    """Mask common credential shapes while preserving useful surrounding context."""
    if not text:
        return text
    value = text
    for _label, pattern in _NAMED_PATTERNS:
        if pattern.groups == 1:
            value = pattern.sub(lambda match: match.group(1) + _MASK, value)
        elif pattern.groups >= 2:
            value = pattern.sub(lambda match: match.group(1) + _MASK + match.group(2), value)
        else:
            value = pattern.sub(_MASK, value)
    return value


_SENSITIVE_STRUCTURED_KEYS = frozenset({
    "api_key",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "secret",
    "client_secret",
    "private_key",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "set_cookie",
    "credential",
    "credentials",
})


def _normalized_key(key: Any) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _sensitive_structured_key(key: Any) -> bool:
    return _normalized_key(key) in _SENSITIVE_STRUCTURED_KEYS


def redact_value(value: Any) -> Any:
    """Recursively redact strings and values stored under credential keys."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if item is not None and _sensitive_structured_key(name):
                result[name] = _MASK
            else:
                result[name] = redact_value(item)
        return result
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def unique_findings(chunks: Iterable[str]) -> list[str]:
    """Collect stable, de-duplicated finding labels over multiple chunks."""
    seen: set[str] = set()
    result: list[str] = []
    for chunk in chunks:
        for finding in secret_findings(chunk):
            if finding not in seen:
                seen.add(finding)
                result.append(finding)
    return result
