from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import sanitized_child_env
from .process import ProcessRunner

ValidationEvent = Callable[[str, dict[str, Any]], Awaitable[None]]

_ALLOWED = {
    "python", "python3", "pytest", "ruff", "mypy", "pyright", "uv", "poetry",
    "tox", "nox", "npm", "npx", "pnpm", "yarn", "bun", "cargo", "rustc",
    "go", "make", "cmake", "ctest", "meson", "ninja", "gradle", "gradlew",
    "dotnet", "java", "javac", "mvn", "mvnw", "php", "composer", "ruby",
    "bundle", "rspec", "swift", "xcodebuild", "git",
}
_FORBIDDEN_SHELL = re.compile(r"(?:&&|\|\||[;|<>`]|\$\(|\n|\r)")


class UnsafeValidationCommand(ValueError):
    pass


def parse_validation_command(command: str, cwd: Path) -> list[str]:
    if _FORBIDDEN_SHELL.search(command):
        raise UnsafeValidationCommand("shell operators/redirections are not allowed")
    args = shlex.split(command)
    if not args:
        raise UnsafeValidationCommand("empty command")
    executable = args[0]
    base = os.path.basename(executable)
    if base in _ALLOWED:
        return args
    if executable.startswith("./"):
        candidate = (cwd / executable).resolve()
        try:
            candidate.relative_to(cwd.resolve())
        except ValueError as exc:
            raise UnsafeValidationCommand("local executable escapes worktree") from exc
        if candidate.exists() and candidate.is_file():
            return args
    raise UnsafeValidationCommand(f"executable is not in validation allowlist: {executable}")


async def run_validation(commands: list[str], cwd: Path, runner: ProcessRunner,
                         key_prefix: str, event: ValidationEvent,
                         timeout: int = 3600) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        try:
            args = parse_validation_command(command, cwd)
        except UnsafeValidationCommand as exc:
            item = {"command": command, "ok": False, "returncode": 126,
                    "output": str(exc), "unsafe": True}
            results.append(item)
            await event("validation", item)
            continue
        lines: list[str] = []

        async def on_line(stream: str, line: str) -> None:
            lines.append(f"[{stream}] {line}")
            await event("log", {"stream": f"validation-{stream}", "text": line})

        result = await runner.run(
            f"{key_prefix}:{index}", args, cwd, timeout=timeout,
            env=sanitized_child_env(extra={"CI": "1", "PYTHONUNBUFFERED": "1"}),
            on_line=on_line,
        )
        item = {
            "command": command,
            "ok": result.returncode == 0 and not result.timed_out,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "output": "\n".join(lines)[-12000:],
            "unsafe": False,
        }
        results.append(item)
        await event("validation", item)
    return {"ok": all(item["ok"] for item in results), "commands": results}
