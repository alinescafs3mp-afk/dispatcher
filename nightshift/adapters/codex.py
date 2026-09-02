from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from .base import AgentAdapter, EventCallback
from ..models import AgentResult, Usage
from ..protocol import limit_like


class CodexAdapter(AgentAdapter):
    def __init__(self, config, runner) -> None:
        super().__init__(config, runner)
        self._json_flag: str | None = None
        self._flag_lock = asyncio.Lock()

    async def _ensure_json_flag(self, cwd: Path) -> str:
        if self._json_flag:
            return self._json_flag
        async with self._flag_lock:
            if self._json_flag:
                return self._json_flag
            result = await self.runner.run(
                f"probe-exec-help:{self.config.id}",
                [self.binary, "exec", "--help"],
                cwd,
                timeout=30,
                env=self.config.subprocess_env(),
            )
            help_text = f"{result.stdout}\n{result.stderr}"
            if "--experimental-json" in help_text:
                self._json_flag = "--experimental-json"
            elif "--json" in help_text:
                self._json_flag = "--json"
            else:
                # Current Codex uses --experimental-json.  Keeping a default also
                # makes fake/test wrappers and transient help failures usable.
                self._json_flag = "--experimental-json"
            return self._json_flag

    def _command(self, cwd: Path, last_message: Path, session_id: str | None,
                 read_only: bool, json_flag: str | None = None) -> list[str]:
        command = [
            self.binary,
            "exec",
            json_flag or self._json_flag or "--experimental-json",
            "--color",
            "never",
            "--skip-git-repo-check",
            "--cd",
            str(cwd),
            "--output-last-message",
            str(last_message),
            "-c",
            'approval_policy="never"',
        ]
        if self.config.unsafe_full_access and not read_only:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command += ["--sandbox", "read-only" if read_only else "workspace-write"]
        if self.config.model:
            command += ["--model", self.config.model]
        if self.config.effort:
            command += ["-c", f'model_reasoning_effort="{self.config.effort}"']
        command += self.config.extra_args
        if session_id:
            command += ["resume", session_id]
        command += ["-"]
        return command

    async def run(self, prompt: str, cwd: Path, task_id: str,
                  session_id: str | None, event: EventCallback,
                  read_only: bool = False) -> AgentResult:
        self.binary = self.config.resolve_binary()
        if not self.binary:
            return AgentResult(ok=False, returncode=127, error="Codex binary not found")
        usage = Usage()
        final_text = ""
        observed_session = session_id
        raw_events = 0
        limit_detected = False
        json_flag = await self._ensure_json_flag(cwd)

        with tempfile.TemporaryDirectory(prefix="nightshift-codex-") as tmp:
            last_path = Path(tmp) / "last-message.txt"
            command = self._command(cwd, last_path, session_id, read_only, json_flag)

            async def on_line(stream: str, line: str) -> None:
                nonlocal usage, final_text, observed_session, raw_events, limit_detected
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
                kind = str(obj.get("type", "event"))
                if kind in {"thread.started", "thread_started"}:
                    observed_session = str(
                        obj.get("thread_id") or obj.get("threadId") or observed_session or ""
                    )
                elif kind in {"turn.completed", "turn_completed"}:
                    raw_usage = obj.get("usage") or {}
                    usage = Usage(
                        input_tokens=int(raw_usage.get("input_tokens", 0) or 0),
                        cached_input_tokens=int(raw_usage.get("cached_input_tokens", 0) or 0),
                        output_tokens=int(raw_usage.get("output_tokens", 0) or 0),
                        reasoning_tokens=int(
                            raw_usage.get("reasoning_output_tokens", 0)
                            or raw_usage.get("reasoning_tokens", 0)
                            or 0
                        ),
                    )
                item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
                item_type = str(item.get("type", ""))
                if item_type in {"agent_message", "assistant_message"} and item.get("text"):
                    final_text = str(item["text"])
                    await event("assistant", {"text": final_text})
                elif item_type == "command_execution":
                    command_text = str(item.get("command", ""))
                    output = str(item.get("aggregated_output", ""))
                    status = str(item.get("status", ""))
                    await event(
                        "tool",
                        {
                            "tool": "command",
                            "command": command_text,
                            "status": status,
                            "output": output[-4000:],
                        },
                    )
                elif item_type == "file_change":
                    await event("tool", {"tool": "file_change", "item": item})
                elif item_type == "error":
                    message = str(item.get("message", ""))
                    if limit_like(message):
                        limit_detected = True
                    await event("log", {"stream": "error", "text": message})
                elif kind in {"turn.failed", "turn_failed", "error"}:
                    message = json.dumps(obj.get("error") or obj, ensure_ascii=False)
                    if limit_like(message):
                        limit_detected = True
                    await event("log", {"stream": "error", "text": message})
                else:
                    await event("event", {"kind": kind, "item_type": item_type})

            result = await self.runner.run(
                self.config.id,
                command,
                cwd,
                stdin_text=prompt,
                timeout=self.config.timeout_seconds,
                env=self.config.subprocess_env(),
                on_line=on_line,
            )
            if last_path.exists():
                candidate = last_path.read_text(encoding="utf-8", errors="replace").strip()
                if candidate:
                    final_text = candidate
            combined_error = (result.stderr or result.stdout)[-2000:]
            if limit_like(combined_error):
                limit_detected = True
            error = ""
            if result.timed_out:
                error = f"Codex timed out after {self.config.timeout_seconds}s"
            elif result.returncode != 0:
                error = combined_error or f"Codex exited with {result.returncode}"
            return AgentResult(
                ok=result.returncode == 0 and not result.timed_out,
                returncode=result.returncode,
                final_text=final_text,
                session_id=observed_session or None,
                usage=usage,
                limit_detected=limit_detected,
                error=error,
                raw_events=raw_events,
            )

    async def probe(self, cwd: Path) -> dict[str, Any]:
        base = await super().probe(cwd)
        if not base.get("installed"):
            return base
        self._json_flag = await self._ensure_json_flag(cwd)
        lines: list[str] = []

        async def sink(_stream: str, line: str) -> None:
            lines.append(line)

        result = await self.runner.run(
            f"probe-login:{self.config.id}",
            [self.binary, "login", "status"],
            cwd,
            timeout=30,
            env=self.config.subprocess_env(),
            on_line=sink,
        )
        text = "\n".join(lines)
        authenticated = result.returncode == 0 and "not logged" not in text.lower()
        base.update(
            {
                "authenticated": authenticated,
                "ready": bool(base.get("installed") and authenticated),
                "auth_detail": text[-1000:],
                "json_flag": self._json_flag,
            }
        )
        return base
