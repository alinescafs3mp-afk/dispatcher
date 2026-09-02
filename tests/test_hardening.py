from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import dispatcher


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.decode().strip()


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "README.md").write_text("# Friday\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "initial")
    return path


def test_hardening_hook_is_active() -> None:
    assert hasattr(dispatcher.GitWorkspace, "changed_paths_since")


def test_commit_refuses_changes_outside_explicit_scope(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "outside.txt").write_text("not allowed\n", encoding="utf-8")
    workspace = dispatcher.GitWorkspace(tmp_path / "state")
    packet = dispatcher.TaskPacket(
        task_id="scope",
        title="Scoped change",
        goal="Only change allowed.txt",
        worker="spark",
        risk="low",
        allowed_paths=["allowed.txt"],
        acceptance=["allowed.txt exists"],
    )
    with pytest.raises(dispatcher.GitError, match="outside task scope"):
        workspace.commit_task(repo, packet)


@pytest.mark.asyncio
async def test_closed_task_cycle_writes_contract_handoff_review_and_accept(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "friday")
    cfg = dispatcher.AppConfig(
        state_dir=tmp_path / "state",
        project_root=repo,
        agents=dispatcher.default_agents(),
        keep_task_worktrees=True,
    )
    control = dispatcher.Dispatcher(cfg)
    control.recovery = control.git.prepare_recovery(repo)

    async def fake_worker(prompt: str, cwd: Path, *, session_id: str | None = None):
        assert "marker.txt" in prompt
        (cwd / "marker.txt").write_text("completed\n", encoding="utf-8")
        return dispatcher.RunResult(
            0,
            "HANDOFF: marker created",
            "fake-worker-session",
            {"input_tokens": 10, "output_tokens": 4},
        )

    async def fake_review(packet, worktree, base, commits):
        assert packet.task_id == "cycle"
        assert commits
        assert (worktree / "marker.txt").exists()
        return {
            "verdict": "ACCEPT",
            "summary": "marker is correctly scoped",
            "remaining_risks": [],
        }

    control.workers["spark"].run = fake_worker
    control._review = fake_review
    packet = dispatcher.TaskPacket(
        task_id="cycle",
        title="Create marker",
        goal="Create marker.txt",
        worker="spark",
        risk="low",
        allowed_paths=["marker.txt"],
        forbidden_paths=["README.md"],
        acceptance=["marker.txt contains completed"],
        validation=["test -f marker.txt"],
    )

    assert await control._execute_task(packet) is True
    assert control.tasks["cycle"].status == "ACCEPTED"
    assert (control.recovery.integration_root / "marker.txt").read_text() == "completed\n"

    lines = [
        json.loads(line)
        for line in control.sol_link.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [line["type"] for line in lines] == [
        "CONTRACT",
        "HANDOFF",
        "REVIEW",
        "ACCEPT",
    ]
