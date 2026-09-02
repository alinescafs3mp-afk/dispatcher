from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

_MARKER_RE = re.compile(r"<SOL_LINK_JSON>\s*(.*?)\s*</SOL_LINK_JSON>", re.I | re.S)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.I | re.S)


def _balanced_objects(text: str) -> list[str]:
    objects: list[str] = []
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if start < 0:
            if char == "{":
                start = index
                depth = 1
                in_string = False
                escaped = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                objects.append(text[start:index + 1])
                start = -1
    return objects


def extract_json_dict(text: str) -> dict[str, Any]:
    """Extract structured output with explicit markers taking precedence.

    A model may mention other JSON objects after its marked decision. Those
    objects must not override the Sol Link contract merely because they occur
    later in the response.
    """
    groups = (
        [match.group(1) for match in _MARKER_RE.finditer(text)],
        [match.group(1) for match in _FENCE_RE.finditer(text)],
        _balanced_objects(text),
    )
    for candidates in groups:
        for candidate in reversed(candidates):
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    raise ValueError("No valid JSON object found in model output")


def parse_model(text: str, model: type[T]) -> T:
    data = extract_json_dict(text)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Structured model output failed validation: {exc}") from exc


def limit_like(text: str) -> bool:
    lowered = text.lower()
    needles = (
        "usage limit", "rate limit", "weekly limit", "quota exhausted",
        "you have no weighted tokens left", "limit reached", "out of codex",
        "gpt-reserve", "shared rollout token budget exhausted",
    )
    return any(needle in lowered for needle in needles)


def compact_text(text: str, limit: int = 6000) -> str:
    text = text.strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n\n… <compacted> …\n\n"
    if len(marker) >= limit:
        return text[:limit]
    content_budget = limit - len(marker)
    head = max(1, content_budget // 3)
    tail = content_budget - head
    compacted = text[:head] + marker
    if tail:
        compacted += text[-tail:]
    return compacted
