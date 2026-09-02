from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

LineCallback = Callable[[str, str], Awaitable[None]]


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
            )
        except OSError as exc:
            return ProcessResult(127, "", str(exc), command=command)

        async with self._lock:
            self._active[key] = proc

        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        async def pump(stream: asyncio.StreamReader | None, name: str, sink: list[str]) -> None:
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
        except asyncio.TimeoutError:
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
            "\n".join(stdout_parts),
            "\n".join(stderr_parts),
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
        except (ProcessLookupError, asyncio.TimeoutError):
            pass
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        except ProcessLookupError:
            pass
