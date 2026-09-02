from __future__ import annotations

import asyncio
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

LineCallback = Callable[[str, str], Awaitable[None]]

# CLI JSONL events and model catalogs can legitimately exceed asyncio's 64 KiB
# default StreamReader limit. Keep draining large records instead of deadlocking a child.
SUBPROCESS_STREAM_LIMIT = 8 * 1024 * 1024
PROCESS_CAPTURE_LIMIT = 2 * 1024 * 1024


class _TailBuffer:
    """Bound ProcessResult memory while callbacks continue receiving every line."""

    def __init__(self, limit: int = PROCESS_CAPTURE_LIMIT) -> None:
        self.limit = max(1, limit)
        self._chunks: deque[str] = deque()
        self._size = 0

    def append(self, text: str) -> None:
        chunk = ("\n" if self._chunks else "") + text
        if len(chunk) >= self.limit:
            self._chunks.clear()
            self._chunks.append(chunk[-self.limit :])
            self._size = self.limit
            return
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size > self.limit and self._chunks:
            excess = self._size - self.limit
            first = self._chunks[0]
            if len(first) <= excess:
                self._size -= len(self._chunks.popleft())
                continue
            self._chunks[0] = first[excess:]
            self._size -= excess

    def render(self) -> str:
        return "".join(self._chunks).lstrip("\n")


@dataclass(slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    command: list[str] = field(default_factory=list)


class ProcessRunner:
    def __init__(self) -> None:
        self._active: dict[str, asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()

    async def run(self, key: str, command: list[str], cwd: Path,
                  stdin_text: str = "", timeout: int = 3600,
                  env: dict[str, str] | None = None,
                  on_line: LineCallback | None = None) -> ProcessResult:
        # A supplied env is a complete child environment, not an overlay.  This
        # lets adapters reliably remove API-key variables and use CLI subscriptions.
        child_env = dict(env) if env is not None else os.environ.copy()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name != "nt"),
                limit=SUBPROCESS_STREAM_LIMIT,
            )
        except OSError as exc:
            return ProcessResult(127, "", str(exc), command=command)

        async with self._lock:
            self._active[key] = proc

        stdout_parts = _TailBuffer()
        stderr_parts = _TailBuffer()

        async def pump(
            stream: asyncio.StreamReader | None,
            name: str,
            sink: _TailBuffer,
        ) -> None:
            if stream is None:
                return
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                sink.append(text)
                if on_line:
                    await on_line(name, text)

        stdout_task = asyncio.create_task(pump(proc.stdout, "stdout", stdout_parts))
        stderr_task = asyncio.create_task(pump(proc.stderr, "stderr", stderr_parts))
        if proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        timed_out = False
        cancelled = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await self._terminate(proc)
        except asyncio.CancelledError:
            cancelled = True
            await self._terminate(proc)
            raise
        finally:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            async with self._lock:
                self._active.pop(key, None)

        return ProcessResult(
            proc.returncode if proc.returncode is not None else -1,
            stdout_parts.render(),
            stderr_parts.render(),
            timed_out=timed_out,
            cancelled=cancelled,
            command=command,
        )

    async def stop(self, key: str) -> bool:
        async with self._lock:
            proc = self._active.get(key)
        if proc is None:
            return False
        await self._terminate(proc)
        return True

    async def stop_all(self) -> None:
        async with self._lock:
            processes = list(self._active.values())
        await asyncio.gather(*(self._terminate(proc) for proc in processes), return_exceptions=True)

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), timeout=8)
            return
        except (TimeoutError, ProcessLookupError):
            pass
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        except ProcessLookupError:
            pass
