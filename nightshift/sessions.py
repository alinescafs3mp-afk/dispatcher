from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .protocol import compact_text
from .redaction import redact


@dataclass(slots=True)
class SessionSummary:
    path: str
    session_id: str
    cwd: str
    modified_at: float
    score: int
    last_user: str
    last_assistant: str
    model: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _extract_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        parts: list[str] = []
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                text = _extract_text(item)
                if text:
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(payload, dict):
        for key in ("text", "message", "content", "input"):
            if key in payload:
                text = _extract_text(payload[key])
                if text:
                    return text
    return ""


def summarize_session(path: Path, repo: Path) -> SessionSummary | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(min(size, 256_000))
            if size > 512_000:
                handle.seek(max(0, size - 512_000))
            tail = handle.read(512_000)
    except OSError:
        return None
    raw_lines = (head + b"\n" + tail).decode("utf-8", errors="replace").splitlines()
    session_id = ""
    cwd = ""
    model = ""
    users: list[str] = []
    assistants: list[str] = []
    for line in raw_lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if not isinstance(payload, dict):
            payload = obj if isinstance(obj, dict) else {}
        event_type = str(obj.get("type", "")) if isinstance(obj, dict) else ""
        subtype = str(payload.get("type", ""))
        session_id = session_id or str(
            payload.get("thread_id") or payload.get("threadId") or
            (payload.get("id") if event_type in {"session_meta", "thread.started"} else "") or ""
        )
        cwd = cwd or str(payload.get("cwd") or payload.get("working_directory") or "")
        model = model or str(payload.get("model") or payload.get("model_id") or "")
        role = str(payload.get("role") or "")
        text = _extract_text(payload)
        if subtype in {"user_message", "user"} or role == "user":
            if text:
                users.append(text)
        elif subtype in {"agent_message", "assistant_message", "assistant"} or role == "assistant":
            if text:
                assistants.append(text)
    if not session_id:
        # Rollout filenames usually end with a UUID, but the timestamp itself
        # also contains dashes, so splitting on "-" cannot recover it.
        matches = re.findall(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            path.stem,
        )
        if matches:
            session_id = matches[-1]
    repo_s = str(repo.resolve())
    score = 0
    if cwd:
        try:
            cwd_resolved = str(Path(cwd).expanduser().resolve())
        except OSError:
            cwd_resolved = cwd
        if cwd_resolved == repo_s:
            score += 100
        elif repo.name.lower() in cwd_resolved.lower():
            score += 40
    combined = "\n".join(users[-2:] + assistants[-2:]).lower()
    if repo.name.lower() in combined:
        score += 10
    return SessionSummary(
        path=str(path), session_id=session_id, cwd=cwd,
        modified_at=path.stat().st_mtime, score=score,
        last_user=compact_text(redact(users[-1]), 4000) if users else "",
        last_assistant=compact_text(redact(assistants[-1]), 6000) if assistants else "",
        model=model,
    )


def discover_sessions(search_roots: list[str], repo: Path, limit: int = 12) -> list[SessionSummary]:
    files: set[Path] = set()
    for root in search_roots:
        expanded = os.path.expanduser(root)
        for candidate in glob.glob(expanded, recursive=True):
            path = Path(candidate)
            if path.is_dir():
                files.update(path.rglob("*.jsonl"))
            elif path.suffix == ".jsonl":
                files.add(path)
    ordered = sorted(files, key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)[:80]
    summaries = [summary for path in ordered if (summary := summarize_session(path, repo))]
    summaries.sort(key=lambda item: (item.score, item.modified_at), reverse=True)
    return summaries[:limit]
