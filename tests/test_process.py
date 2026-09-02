from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nightshift.process import ProcessRunner


@pytest.mark.asyncio
async def test_runner_drains_jsonl_line_larger_than_asyncio_default(tmp_path: Path) -> None:
    lines: list[str] = []

    async def on_line(_stream: str, line: str) -> None:
        lines.append(line)

    result = await ProcessRunner().run(
        "large-line",
        [sys.executable, "-c", "print('x' * 200000)"],
        tmp_path,
        timeout=10,
        on_line=on_line,
    )
    assert result.returncode == 0
    assert len(lines) == 1
    assert len(lines[0]) == 200000
