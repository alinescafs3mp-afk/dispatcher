from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..models import AgentResult, Usage
from ..protocol import limit_like
from .base import AgentAdapter, EventCallback


def _first_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return "".join(_first_text(item) for item in obj)
    if not isinstance(obj, dict):
        return ""
    for key in ("text", "delta", "result", "message", "content"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (dict, list)):
            nested = _first_text(value)
            if nested:
                return nested
    update = obj.get("update")
    if isinstance(update, (dict, list)):
        return _first_text(update)
    return ""


def _usage_from(obj: dict[str, Any]) -> Usage | None:
    raw = obj.get("usage")
    if not isinstance(raw, dict):
        result = obj.get("result")
        raw = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(raw, dict):
        return None
    return Usage(
        input_tokens=int(raw.get("input_tokens", raw.get("prompt_tokens", 0)) or 0),
        cached_input_tokens=int(
            raw.get("cached_input_tokens", 0)
            or (
                (raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                if isinstance(raw.get("prompt_tokens_details"), dict)
                else 0
            )
        ),
        output_tokens=int(raw.get("output_tokens", raw.get("completion_tokens", 0)) or 0),
        reasoning_tokens=int(
            raw.get("reasoning_tokens", 0)
            or (
                (raw.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
                if isinstance(raw.get("completion_tokens_details"), dict)
                else 0
            )
        ),
    )


class GrokAdapter(AgentAdapter):
    def _command(self, cwd: Path, session_id: str, existing: bool,
                 prompt: str, read_only: bool) -> list[str]:
        command = [
            self.binary,
            "--no-auto-update",
            "--no-alt-screen",
            "--cwd",
            str(cwd),
            "--output-format",
            "streaming-json",
        ]
        if self.config.model:
            command += ["--model", self.config.model]
        if self.config.effort:
            command += ["--effort", self.config.effort]
        if self.config.max_turns:
            command += ["--max-turns", str(self.config.max_turns)]
        if read_only:
            # A headless architect must not stop for approval prompts, but the
            # read-only sandbox remains the actual filesystem/network boundary.
            command += [
                "--always-approve",
                "--sandbox", "read-only",
                "--disallowed-tools", "Edit,Write,NotebookEdit",
            ]
        else:
            # Grok only receives a writable checkout for explicitly approved
            # implementation work. The workspace sandbox keeps writes scoped.
            command += ["--always-approve", "--sandbox", "workspace"]
        command += self.config.extra_args
        if existing:
            command += ["--resume", session_id]
        else:
            command += ["--session-id", session_id]
        command += ["-p", prompt]
        return command

    async def run(self, prompt: str, cwd: Path, task_id: str,
                  session_id: str | None, event: EventCallback,
                  read_only: bool = False) -> AgentResult:
        self.binary = self.config.resolve_binary()
        if not self.binary:
            return AgentResult(ok=False, returncode=127, error="Grok Build binary not found")
        actual_session = session_id or str(uuid.uuid4())
        final_chunks: list[str] = []
        usage = Usage()
        raw_events = 0
        limit_detected = False
        command = self._command(cwd, actual_session, bool(session_id), prompt, read_only)

        async def on_line(stream: str, line: str) -> None:
            nonlocal usage, raw_events, limit_detected
            if limit_like(line):
                limit_detected = True
            if stream == "stderr":
                await event("log", {"stream": stream, "text": line})
                return
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                await event("log", {"stream": stream, "text": line})
                return
            raw_events += 1
            maybe_usage = _usage_from(obj)
            if maybe_usage:
                usage = maybe_usage
            event_type = str(obj.get("type") or obj.get("event") or obj.get("method") or "event")
            text = _first_text(obj)
            assistant_like = any(
                token in event_type.lower()
                for token in ("assistant", "agent_message", "message_chunk", "result", "final")
            )
            if (text and assistant_like) or (text and event_type.lower() in {"event", "output"}):
                final_chunks.append(text)
                await event("assistant_delta", {"text": text})
            else:
                await event("event", {"kind": event_type})

        result = await self.runner.run(
            self.config.id,
            command,
            cwd,
            timeout=self.config.timeout_seconds,
            env=self.config.subprocess_env(),
            on_line=on_line,
        )
        combined = (result.stdout + "\n" + result.stderr).strip()
        if limit_like(combined):
            limit_detected = True
        final_text = "".join(final_chunks).strip()
        if not final_text and result.stdout.strip():
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            for line in reversed(lines):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    if not final_text:
                        final_text = line
                    continue
                text = _first_text(obj)
                if text:
                    final_text = text
                    break
        error = ""
        if result.timed_out:
            error = f"Grok timed out after {self.config.timeout_seconds}s"
        elif result.returncode != 0:
            error = (result.stderr or result.stdout)[-2000:] or f"Grok exited with {result.returncode}"
        return AgentResult(
            ok=result.returncode == 0 and not result.timed_out,
            returncode=result.returncode,
            final_text=final_text,
            session_id=actual_session,
            usage=usage,
            limit_detected=limit_detected,
            error=error,
            raw_events=raw_events,
        )

    async def probe(self, cwd: Path) -> dict[str, Any]:
        base = await super().probe(cwd)
        if not base.get("installed"):
            return base
        lines: list[str] = []

        async def sink(_stream: str, line: str) -> None:
            lines.append(line)

        result = await self.runner.run(
            f"probe-models:{self.config.id}",
            [self.binary, "models"],
            cwd,
            timeout=45,
            env=self.config.subprocess_env(),
            on_line=sink,
        )
        text = "\n".join(lines)
        base.update(
            {
                "authenticated": result.returncode == 0,
                "ready": bool(base.get("installed") and result.returncode == 0),
                "auth_detail": text[-1000:] if text else (result.stderr or "")[-1000:],
            }
        )
        return base
