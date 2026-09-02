from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import AgentConfig
from ..models import AgentResult
from ..process import ProcessRunner

EventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


class AgentAdapter(ABC):
    def __init__(self, config: AgentConfig, runner: ProcessRunner) -> None:
        self.config = config
        self.runner = runner
        self.binary = config.resolve_binary()

    @abstractmethod
    async def run(self, prompt: str, cwd: Path, task_id: str,
                  session_id: str | None, event: EventCallback,
                  read_only: bool = False) -> AgentResult:
        raise NotImplementedError

    async def probe(self, cwd: Path) -> dict[str, Any]:
        self.binary = self.config.resolve_binary()
        if not self.binary:
            return {"installed": False, "ready": False, "error": "binary not found"}

        async def sink(_stream: str, _line: str) -> None:
            return None

        result = await self.runner.run(
            f"probe:{self.config.id}", [self.binary, "--version"], cwd,
            timeout=30, env=self.config.subprocess_env(), on_line=sink,
        )
        version = (result.stdout or result.stderr).splitlines()
        return {
            "installed": result.returncode == 0,
            "ready": result.returncode == 0,
            "binary": self.binary,
            "version": version[0] if version else "",
            "model": self.config.model,
            "effort": self.config.effort,
            "subscription_env": True,
            "stripped_env": list(self.config.strip_env),
            "error": "" if result.returncode == 0 else (result.stderr or result.stdout)[-500:],
        }
