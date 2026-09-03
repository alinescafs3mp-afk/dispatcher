from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nightshift.adapters.grok import GrokAdapter
from nightshift.app import create_app
from nightshift.config import default_settings
from nightshift.db import StateDB
from nightshift.git import GitError, MissionWorkspace, git, path_violations
from nightshift.models import MissionState, RiskLevel, TaskPacket
from nightshift.orchestrator import NightshiftOrchestrator
from nightshift.process import PROCESS_CAPTURE_LIMIT, ProcessRunner
from nightshift.protocol import compact_text


def make_settings(repo: Path, tmp_path: Path):
    settings = default_settings(str(repo))
    settings.orchestrator.runtime_dir = str(tmp_path / "runtime")
    settings.server.allowed_hosts = ["testserver"]
    for agent in settings.agents.values():
        agent.binary_candidates = [str(tmp_path / "missing")]
    return settings


def test_matching_attacker_origin_cannot_bypass_host_allowlist(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    settings = make_settings(git_repo, tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/api/state",
            headers={
                "Host": "rebind.attacker.example",
                "Origin": "http://rebind.attacker.example",
            },
        )
    assert response.status_code == 421
    assert response.json()["detail"] == "Untrusted Host header"


def test_compaction_never_exceeds_small_limit() -> None:
    text = "HEAD" + ("x" * 3000) + "TAIL"
    result = compact_text(text, 500)
    assert len(result) <= 500
    assert result.startswith("HEAD")
    assert result.endswith("TAIL")
    assert "compacted" in result
    assert compact_text(text, 0) == ""


@pytest.mark.asyncio
async def test_grok_current_streaming_json_contract(
    tmp_path: Path,
    make_executable,
) -> None:
    binary = make_executable(
        "fake-grok",
        """#!/usr/bin/env python3
import json

events = [
    {"type": "thought", "data": "internal reasoning"},
    {"type": "text", "data": "hello "},
    {"type": "text", "data": "world"},
    {
        "type": "usage",
        "usage": {
            "input_tokens": 7,
            "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 1,
            "output_tokens": 2,
            "reasoning_tokens": 1,
        },
    },
    {
        "type": "end",
        "stopReason": "end_turn",
        "sessionId": "server-session",
        "requestId": "request-1",
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 1,
            "output_tokens": 3,
            "reasoning_tokens": 2,
        },
    },
]
for item in events:
    print(json.dumps(item), flush=True)
""",
    )
    config = default_settings().agent("grok")
    config.binary_candidates = [str(binary)]
    config.timeout_seconds = 10
    adapter = GrokAdapter(config, ProcessRunner())
    observed: list[tuple[str, dict[str, object]]] = []

    async def event(kind: str, payload: dict[str, object]) -> None:
        observed.append((kind, payload))

    result = await adapter.run(
        "say hello",
        tmp_path,
        "grok-contract",
        None,
        event,
        read_only=True,
    )

    assert result.ok
    assert result.final_text == "hello world"
    assert result.session_id == "server-session"
    assert result.usage.input_tokens == 10
    assert result.usage.cached_input_tokens == 5
    assert result.usage.output_tokens == 3
    assert result.usage.reasoning_tokens == 2
    assert [payload["text"] for kind, payload in observed if kind == "assistant_delta"] == [
        "hello ",
        "world",
    ]


@pytest.mark.asyncio
async def test_grok_error_event_cannot_be_reported_as_success(
    tmp_path: Path,
    make_executable,
) -> None:
    binary = make_executable(
        "fake-grok-error",
        """#!/usr/bin/env python3
import json

print(json.dumps({"type": "error", "message": "provider rejected the turn"}))
""",
    )
    config = default_settings().agent("grok")
    config.binary_candidates = [str(binary)]
    config.timeout_seconds = 10
    adapter = GrokAdapter(config, ProcessRunner())

    async def event(_kind: str, _payload: dict[str, object]) -> None:
        return None

    result = await adapter.run(
        "fail",
        tmp_path,
        "grok-error",
        None,
        event,
        read_only=True,
    )

    assert not result.ok
    assert "provider rejected" in result.error


@pytest.mark.asyncio
async def test_process_result_capture_is_bounded_but_callback_is_complete(
    tmp_path: Path,
) -> None:
    payload_size = PROCESS_CAPTURE_LIMIT + 4096
    line_lengths: list[int] = []

    async def on_line(_stream: str, line: str) -> None:
        line_lengths.append(len(line))

    result = await ProcessRunner().run(
        "bounded-capture",
        [sys.executable, "-c", f"print('x' * {payload_size})"],
        tmp_path,
        timeout=20,
        on_line=on_line,
    )

    assert result.returncode == 0
    assert line_lengths == [payload_size]
    assert len(result.stdout) == PROCESS_CAPTURE_LIMIT
    assert result.stdout.endswith("x" * 128)


def test_rename_reports_both_protected_source_and_destination(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    settings = make_settings(git_repo, tmp_path)
    workspace = MissionWorkspace(settings, "rename-scope", tmp_path / "mission")
    workspace.prepare()
    try:
        tree = workspace.create_worker("rename-task", "luna")
        (tree.path / "README.md").rename(tree.path / "safe-name.md")
        workspace.commit_worker(tree, "rename")
        changed = set(workspace.worker_changed_files(tree))
        assert changed == {"README.md", "safe-name.md"}
        violations = path_violations(
            changed,
            allowed=["safe-name.md"],
            forbidden=[],
            protected=["README.md"],
        )
        assert "protected path changed: README.md" in violations
    finally:
        workspace.cleanup(keep_integration=False)


def test_architect_sync_removes_ignored_side_effects(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    (git_repo / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
    git(git_repo, "add", ".gitignore")
    git(git_repo, "commit", "-m", "ignore generated file")

    settings = make_settings(git_repo, tmp_path)
    workspace = MissionWorkspace(settings, "architect-clean", tmp_path / "mission")
    workspace.prepare()
    try:
        ignored = workspace.architect_path / "ignored.tmp"
        ignored.write_text("side effect", encoding="utf-8")
        assert ignored.exists()
        workspace.sync_architect_worktree()
        assert not ignored.exists()
    finally:
        workspace.cleanup(keep_integration=False)


def test_failed_integration_rolls_back_index_and_worktree(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    settings = make_settings(git_repo, tmp_path)
    workspace = MissionWorkspace(settings, "rollback", tmp_path / "mission")
    workspace.prepare()
    try:
        tree = workspace.create_worker("rollback-task", "luna")
        (tree.path / "README.md").write_text("worker change\n", encoding="utf-8")
        workspace.commit_worker(tree, "worker change")
        baseline = workspace.integration_head()

        hook = git_repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        with pytest.raises(GitError):
            workspace.integrate_worker(tree, "must fail")
        assert workspace.integration_head() == baseline
        assert git(workspace.integration_path, "status", "--porcelain") == ""
        assert (workspace.integration_path / "README.md").read_text(
            encoding="utf-8"
        ) == "initial\n"
    finally:
        workspace.cleanup(keep_integration=False)


def test_legacy_database_gets_mission_scoped_architect_columns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """CREATE TABLE missions (
                id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL,
                base_sha TEXT NOT NULL DEFAULT '',
                integration_branch TEXT NOT NULL DEFAULT '',
                integration_path TEXT NOT NULL DEFAULT '',
                directive_path TEXT NOT NULL DEFAULT '',
                forensics_path TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.commit()
    finally:
        connection.close()

    db = StateDB(path)
    try:
        columns = {row["name"] for row in db.query("PRAGMA table_info(missions)")}
        assert {"architect_session_id", "architect_turns"} <= columns
    finally:
        db.close()


@pytest.mark.asyncio
async def test_resume_uses_the_missions_own_architect_session(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    settings = make_settings(git_repo, tmp_path)
    orchestrator = NightshiftOrchestrator(settings)
    mission_id = "mission-scoped-session"
    mission_dir = orchestrator.runtime / "missions" / mission_id
    workspace = MissionWorkspace(orchestrator.settings, mission_id, mission_dir)
    prepared = workspace.prepare()
    orchestrator.db.create_mission(
        mission_id,
        str(git_repo),
        "resume the correct architect",
        MissionState.PAUSED.value,
        profile=orchestrator.profile_id,
        profile_options=orchestrator._profile_options(),
    )
    orchestrator.db.update_mission(
        mission_id,
        base_sha=prepared["base_sha"],
        integration_branch=prepared["integration_branch"],
        integration_path=prepared["integration_path"],
        architect_session_id="mission-session",
        architect_turns=7,
    )
    orchestrator.db.update_agent(
        orchestrator.settings.agent("grok").id,
        session_id="different-mission-session",
    )
    release = asyncio.Event()

    async def held_resume(_row) -> None:
        await release.wait()

    orchestrator._run_resumed_mission = held_resume  # type: ignore[method-assign]
    try:
        await orchestrator.resume_interrupted(mission_id)
        assert orchestrator._grok_session_id == "mission-session"
        assert orchestrator._grok_turns == 7
    finally:
        release.set()
        await orchestrator.close()
        workspace.cleanup(keep_integration=False)


@pytest.mark.asyncio
async def test_luna_reserve_route_requires_backend_authorization(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orchestrator = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        orchestrator.quota_cache["luna"] = {
            "raw": {
                "luna_reserve_available": True,
                "luna_reserve_model": "gpt-reserve",
            }
        }
        assert orchestrator._luna_reserve_model("luna") == "gpt-reserve"
        assert orchestrator._luna_reserve_model("spark") is None

        orchestrator.quota_cache["luna"]["raw"]["luna_reserve_blocked_model"] = "other"
        assert orchestrator._luna_reserve_model("luna") is None
        orchestrator.quota_cache["luna"]["raw"].pop("luna_reserve_blocked_model")

        orchestrator.quota_cache["luna"]["raw"]["luna_reserve_available"] = False
        assert orchestrator._luna_reserve_model("luna") is None

        await orchestrator.set_profile("combat", combat_grok_enabled=True)
        orchestrator.quota_cache["luna"] = {
            "raw": {
                "luna_reserve_available": True,
                "luna_reserve_model": "gpt-reserve",
            }
        }
        assert orchestrator._luna_reserve_model("luna") is None
    finally:
        await orchestrator.close()


@pytest.mark.asyncio
async def test_fallback_worker_respects_scope_width(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orchestrator = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        await orchestrator.set_profile("combat", combat_grok_enabled=True)
        packet = TaskPacket(
            title="too many bounded areas",
            goal="touch many independent areas",
            worker="luna",
            allowed_paths=[f"area-{index}/file.py" for index in range(7)],
            acceptance_criteria=["focused tests pass"],
            risk=RiskLevel.LOW,
            max_files=3,
        )
        assert orchestrator._fallback_worker("luna", packet) is None
    finally:
        await orchestrator.close()
