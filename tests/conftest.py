from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


def run(*args: str, cwd: Path) -> str:
    proc = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run("git", "init", "-b", "main", cwd=repo)
    run("git", "config", "user.name", "Test User", cwd=repo)
    run("git", "config", "user.email", "test@example.invalid", cwd=repo)
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    run("git", "add", "README.md", cwd=repo)
    run("git", "commit", "-m", "initial", cwd=repo)
    return repo


@pytest.fixture
def make_executable(tmp_path: Path):
    def factory(name: str, content: str) -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path
    return factory
