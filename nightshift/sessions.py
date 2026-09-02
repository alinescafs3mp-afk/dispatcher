from __future__ import annotations

import glob
import json
import os
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .protocol import compact_text
from .redaction import redact


@dataclass(slots=True)
class SessionSummary:
    path: str
    session_id: str
    cwd: str
    matched_root: str
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


def _resolve_path(value: str | Path) -> Path | None:
    try:
        return Path(os.path.expandvars(str(value))).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _score_working_directory(
    cwd: str,
    repo: Path,
    working_roots: Iterable[Path] | None,
) -> tuple[int, str]:
    cwd_path = _resolve_path(cwd)
    repo_path = _resolve_path(repo)
    if cwd_path is None or repo_path is None:
        return 0, ""

    roots: list[tuple[Path, int, int]] = [(repo_path, 120, 110)]
    seen = {repo_path}
    for raw_root in working_roots or ():
        root = _resolve_path(raw_root)
        if root is None or root in seen:
            continue
        seen.add(root)
        roots.append((root, 100, 90))

    best_score = 0
    matched_root = ""
    for root, exact_score, descendant_score in roots:
        if cwd_path == root:
            score = exact_score
        else:
            try:
                cwd_path.relative_to(root)
            except ValueError:
                continue
            score = descendant_score
        if score > best_score:
            best_score = score
            matched_root = str(root)
    return best_score, matched_root


def summarize_session(
    path: Path,
    repo: Path,
    working_roots: Iterable[Path] | None = None,
) -> SessionSummary | None:
    try:
        stat = path.stat()
        size = stat.st_size
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
        elif text and (
            subtype in {"agent_message", "assistant_message", "assistant"}
            or role == "assistant"
        ):
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
    score, matched_root = _score_working_directory(cwd, repo, working_roots)
    if score == 0 and cwd and repo.name.lower() in cwd.lower():
        score = 40
    combined = "\n".join(users[-2:] + assistants[-2:]).lower()
    if repo.name.lower() in combined:
        score += 10
    return SessionSummary(
        path=str(path), session_id=session_id, cwd=cwd,
        matched_root=matched_root,
        modified_at=stat.st_mtime, score=score,
        last_user=compact_text(redact(users[-1]), 4000) if users else "",
        last_assistant=compact_text(redact(assistants[-1]), 6000) if assistants else "",
        model=model,
    )


def discover_sessions(
    search_roots: list[str],
    repo: Path,
    limit: int = 12,
    working_roots: Iterable[Path] | None = None,
) -> list[SessionSummary]:
    files: set[Path] = set()
    for root in search_roots:
        expanded = os.path.expandvars(os.path.expanduser(root))
        try:
            candidates = glob.glob(expanded, recursive=True)
        except OSError:
            continue
        for candidate in candidates:
            path = Path(candidate)
            try:
                if path.is_dir():
                    files.update(path.rglob("*.jsonl"))
                elif path.suffix == ".jsonl":
                    files.add(path)
            except OSError:
                continue

    def modified_at(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0

    ordered = sorted(files, key=modified_at, reverse=True)[:80]
    summaries = [
        summary
        for path in ordered
        if (summary := summarize_session(path, repo, working_roots))
    ]
    summaries.sort(key=lambda item: (item.score, item.modified_at), reverse=True)
    return summaries[:limit]
