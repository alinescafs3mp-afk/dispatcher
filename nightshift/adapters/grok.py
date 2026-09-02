from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..models import AgentResult, Usage
from ..protocol import limit_like
from .base import AgentAdapter, EventCallback


def _sandbox_resume_mismatch(text: str) -> bool:
    lowered = text.lower()
    return (
        "sandbox" in lowered
        and ("resume" in lowered or "session" in lowered)
        and any(
            token in lowered
            for token in ("differ", "mismatch", "refus", "saved profile", "cannot change")
        )
    )


def _first_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return "".join(_first_text(item) for item in obj)
    if not isinstance(obj, dict):
        return ""
    for key in ("text", "delta", "result", "message", "content", "data"):
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


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _usage_from(obj: dict[str, Any]) -> Usage | None:
    raw = obj.get("usage")
    if not isinstance(raw, dict):
        result = obj.get("result")
        raw = result.get("usage") if isinstance(result, dict) else None
    if not isinstance(raw, dict):
        return None

    input_value = raw.get("input_tokens")
    if input_value is None:
        input_value = raw.get("inputTokens")
    if input_value is None:
        input_value = raw.get("prompt_tokens", 0)

    output_value = raw.get("output_tokens")
    if output_value is None:
        output_value = raw.get("outputTokens")
    if output_value is None:
        output_value = raw.get("completion_tokens", 0)

    cache_keys = (
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cacheReadInputTokens",
        "cacheCreationInputTokens",
    )
    if any(raw.get(key) is not None for key in cache_keys):
        cached_value = sum(_safe_int(raw.get(key)) for key in cache_keys)
    elif raw.get("cached_input_tokens") is not None:
        cached_value = _safe_int(raw.get("cached_input_tokens"))
    else:
        details = raw.get("prompt_tokens_details")
        cached_value = (
            _safe_int(details.get("cached_tokens"))
            if isinstance(details, dict)
            else 0
        )

    reasoning_value = raw.get("reasoning_tokens")
    if reasoning_value is None:
        reasoning_value = raw.get("reasoningTokens")
    if reasoning_value is None:
        details = raw.get("completion_tokens_details")
        reasoning_value = (
            details.get("reasoning_tokens", 0)
            if isinstance(details, dict)
            else 0
        )

    return Usage(
        input_tokens=_safe_int(input_value),
        cached_input_tokens=cached_value,
        output_tokens=_safe_int(output_value),
        reasoning_tokens=_safe_int(reasoning_value),
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
            # Direct operator chat, explicit consultation, and recovery inspection
            # remain non-mutating even for a normally full-access participant.
            command += [
                "--always-approve",
                "--sandbox", "read-only",
                "--disallowed-tools", "Edit,Write,NotebookEdit",
            ]
        elif self.config.unsafe_full_access:
            # The operator explicitly trusts automated Grok turns with host access.
            command += ["--always-approve", "--sandbox", "off"]
        else:
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
        streamed_session = actual_session
        streamed_error = ""
        final_chunks: list[str] = []
        usage = Usage()
        raw_events = 0
        limit_detected = False
        command = self._command(cwd, actual_session, bool(session_id), prompt, read_only)

        async def on_line(stream: str, line: str) -> None:
            nonlocal usage, raw_events, limit_detected
            nonlocal streamed_session, streamed_error
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
            if not isinstance(obj, dict):
                await event("event", {"kind": "non_object_json"})
                return

            raw_events += 1
            maybe_usage = _usage_from(obj)
            if maybe_usage:
                usage = maybe_usage
            candidate_session = obj.get("sessionId") or obj.get("session_id")
            if isinstance(candidate_session, str) and candidate_session:
                streamed_session = candidate_session

            event_type = str(
                obj.get("type") or obj.get("event") or obj.get("method") or "event"
            )
            lowered_type = event_type.casefold()

            if lowered_type == "text":
                text = obj.get("data")
                if isinstance(text, str) and text:
                    final_chunks.append(text)
                    await event("assistant_delta", {"text": text})
                else:
                    await event("event", {"kind": event_type})
                return

            if lowered_type == "error":
                message = _first_text(obj).strip() or "Grok stream reported an error"
                streamed_error = message
                await event("log", {"stream": "stderr", "text": message})
                return

            if lowered_type == "max_turns_reached":
                streamed_error = "Grok reached its configured maximum turns"
                await event("log", {"stream": "stderr", "text": streamed_error})
                return

            if lowered_type == "end":
                stop_reason = str(
                    obj.get("stopReason") or obj.get("stop_reason") or ""
                ).casefold()
                if stop_reason in {"cancelled", "refusal", "max_turn_requests"}:
                    streamed_error = f"Grok stopped with reason: {stop_reason}"
                await event("event", {"kind": event_type})
                return

            text = _first_text(obj)
            assistant_like = any(
                token in lowered_type
                for token in (
                    "assistant",
                    "agent_message",
                    "message_chunk",
                    "result",
                    "final",
                )
            )
            if (text and assistant_like) or (
                text and lowered_type in {"event", "output"}
            ):
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
        if session_id and result.returncode != 0 and _sandbox_resume_mismatch(combined):
            await event(
                "log",
                {
                    "stream": "stderr",
                    "text": (
                        "Stored Grok session used a different sandbox profile; "
                        "starting a fresh session with the active permission policy."
                    ),
                },
            )
            actual_session = str(uuid.uuid4())
            streamed_session = actual_session
            streamed_error = ""
            final_chunks.clear()
            usage = Usage()
            raw_events = 0
            limit_detected = False
            command = self._command(cwd, actual_session, False, prompt, read_only)
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
                if not isinstance(obj, dict):
                    continue
                event_type = str(
                    obj.get("type") or obj.get("event") or obj.get("method") or "event"
                ).casefold()
                if event_type == "text":
                    text = obj.get("data")
                elif any(
                    token in event_type
                    for token in (
                        "assistant",
                        "agent_message",
                        "message_chunk",
                        "result",
                        "final",
                    )
                ):
                    text = _first_text(obj)
                else:
                    text = ""
                if isinstance(text, str) and text:
                    final_text = text
                    break

        error = ""
        if result.timed_out:
            error = f"Grok timed out after {self.config.timeout_seconds}s"
        elif streamed_error:
            error = streamed_error[-2000:]
        elif result.returncode != 0:
            error = (
                (result.stderr or result.stdout)[-2000:]
                or f"Grok exited with {result.returncode}"
            )
        return AgentResult(
            ok=(
                result.returncode == 0
                and not result.timed_out
                and not streamed_error
            ),
            returncode=result.returncode,
            final_text=final_text,
            session_id=streamed_session,
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
