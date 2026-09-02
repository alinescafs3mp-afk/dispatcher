from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nightshift.app import create_app
from nightshift.config import default_settings
from nightshift.db import StateDB
from nightshift.models import MissionState, TaskState
from nightshift.orchestrator import NightshiftOrchestrator, OrchestratorError


def make_settings(repo: Path, tmp_path: Path):
    settings = default_settings(str(repo))
    settings.orchestrator.runtime_dir = str(tmp_path / "runtime")
    for agent in settings.agents.values():
        agent.enabled = False
        agent.binary_candidates = [str(tmp_path / "missing")]
    return settings


def test_control_api_rejects_missing_active_mission(git_repo: Path, tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(git_repo, tmp_path))) as client:
        for path in ("/api/mission/pause", "/api/mission/resume", "/api/mission/stop"):
            response = client.post(path, json={})
            assert response.status_code == 409


def test_control_api_rejects_cross_origin_browser_request(
    git_repo: Path, tmp_path: Path,
) -> None:
    with TestClient(create_app(make_settings(git_repo, tmp_path))) as client:
        response = client.post(
            "/api/mission/pause",
            json={},
            headers={"Origin": "https://attacker.example"},
        )
        assert response.status_code == 403
        same_origin = client.post(
            "/api/mission/pause",
            json={},
            headers={"Origin": "http://testserver"},
        )
        assert same_origin.status_code == 409


def test_terminal_mission_cannot_be_resumed(git_repo: Path, tmp_path: Path) -> None:
    settings = make_settings(git_repo, tmp_path)
    orch = NightshiftOrchestrator(settings)
    try:
        orch.db.create_mission(
            "done", str(git_repo), "completed mission", MissionState.COMPLETED.value
        )
        orch.db.update_mission(
            "done",
            integration_path=str(git_repo),
            integration_branch="main",
            base_sha="deadbeef",
        )
        with pytest.raises(OrchestratorError, match="cannot be resumed"):
            asyncio.run(orch.resume_interrupted("done"))
    finally:
        orch.db.close()


def test_restart_blocks_stale_inflight_task(git_repo: Path, tmp_path: Path) -> None:
    settings = make_settings(git_repo, tmp_path)
    db_path = settings.orchestrator.runtime_path / "nightshift.sqlite3"
    db = StateDB(db_path)
    try:
        db.create_mission(
            "interrupted", str(git_repo), "resume work", MissionState.AWAITING_HUMAN.value
        )
        db.create_task(
            "interrupted",
            "task-1",
            {"title": "pending", "worker": "spark", "risk": "high"},
            TaskState.AWAITING_HUMAN.value,
        )
    finally:
        db.close()
    orch = NightshiftOrchestrator(settings)
    try:
        mission = orch.db.query("SELECT status FROM missions WHERE id='interrupted'")[0]
        task = orch.db.query("SELECT status,result_json FROM tasks WHERE id='task-1'")[0]
        assert mission["status"] == MissionState.PAUSED.value
        assert task["status"] == TaskState.BLOCKED.value
        assert "restarted" in json.loads(task["result_json"])["interruption"]
    finally:
        orch.db.close()


@pytest.mark.asyncio
async def test_live_event_payload_is_structurally_redacted(
    git_repo: Path, tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    queue = await orch.hub.subscribe(replay=False)
    try:
        await orch._emit("test.secret", {"accessToken": "opaque-not-pattern-shaped"})
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event["payload"]["accessToken"] == "***REDACTED***"
        stored = orch.db.query("SELECT payload_json FROM events ORDER BY seq DESC LIMIT 1")[0]
        assert "opaque-not-pattern-shaped" not in stored["payload_json"]
    finally:
        await orch.hub.unsubscribe(queue)
        await orch.close()
