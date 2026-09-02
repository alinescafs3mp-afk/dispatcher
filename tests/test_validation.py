from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from nightshift.process import ProcessRunner
from nightshift.validation import UnsafeValidationCommand, parse_validation_command, run_validation


def test_validation_rejects_shell_operators(tmp_path: Path) -> None:
    with pytest.raises(UnsafeValidationCommand):
        parse_validation_command("pytest && rm -rf /", tmp_path)
    with pytest.raises(UnsafeValidationCommand):
        parse_validation_command("pytest | tee out", tmp_path)


def test_validation_allows_known_command(tmp_path: Path) -> None:
    assert parse_validation_command("python -m compileall .", tmp_path)[:2] == ["python", "-m"]


def test_validation_local_executable_cannot_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "tool"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(UnsafeValidationCommand):
        parse_validation_command("./../tool", tmp_path)


@pytest.mark.asyncio
async def test_validation_scrubs_ambient_secret(monkeypatch, tmp_path: Path) -> None:
    script = tmp_path / "check-env"
    script.write_text(
        "#!/usr/bin/env python3\nimport os,sys\nsys.exit(1 if os.getenv('OPENAI_API_KEY') else 0)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-12345678901234567890")
    events = []

    async def event(kind, payload):
        events.append((kind, payload))

    result = await run_validation(["./check-env"], tmp_path, ProcessRunner(), "test", event, timeout=5)
    assert result["ok"]
    assert events
