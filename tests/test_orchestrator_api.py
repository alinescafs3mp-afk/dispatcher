from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

import nightshift.app as app_module
from nightshift.app import create_app
from nightshift.config import default_settings
from nightshift.models import RiskLevel, TaskPacket
from nightshift.orchestrator import NightshiftOrchestrator, OrchestratorError


def make_settings(repo: Path, tmp_path: Path):
    settings = default_settings(str(repo))
    settings.orchestrator.runtime_dir = str(tmp_path / "runtime")
    settings.server.allowed_hosts = ["testserver"]
    for agent in settings.agents.values():
        agent.enabled = False
        agent.binary_candidates = [str(tmp_path / "missing")]
    return settings


def test_packet_routing_and_human_gate(git_repo: Path, tmp_path: Path) -> None:
    settings = make_settings(git_repo, tmp_path)
    settings.agent("luna").enabled = True
    orch = NightshiftOrchestrator(settings)
    try:
        broad = TaskPacket(
            title="broad", goal="investigate several modules", worker="spark",
            allowed_paths=["src/a.py", "src/b.py", "tests/a.py", "tests/b.py"],
            acceptance_criteria=["works"], risk=RiskLevel.MEDIUM,
        )
        normalized = orch._normalize_packet(broad)
        assert normalized.worker == "luna"
        assert orch._needs_human_gate(RiskLevel.HIGH)
        assert not orch._needs_human_gate(RiskLevel.LOW)
    finally:
        orch.db.close()


def test_reasoning_validation_and_persistence(git_repo: Path, tmp_path: Path) -> None:
    settings = make_settings(git_repo, tmp_path)
    orch = NightshiftOrchestrator(settings)
    try:
        result = asyncio.run(orch.set_reasoning("grok", "high"))
        assert result["effort"] == "high"
        assert orch.db.get_preference("agent.grok.effort") == "high"
        try:
            asyncio.run(orch.set_reasoning("grok", "banana"))
        except OrchestratorError:
            pass
        else:
            raise AssertionError("invalid reasoning was accepted")
    finally:
        orch.db.close()


def test_api_health_state_reasoning_and_directive(git_repo: Path, tmp_path: Path) -> None:
    settings = make_settings(git_repo, tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json()["ok"] is True
        assert client.get("/").status_code == 200
        state = client.get("/api/state").json()
        assert set(state["config"]["agents"]) == {"grok", "spark", "luna"}
        response = client.put("/api/agents/grok/reasoning", json={"effort": "medium"})
        assert response.status_code == 200
        assert response.json()["applies"] == "next model turn"
        directive = client.get("/api/directive")
        assert directive.status_code == 200
        assert "Phase Zero" in directive.text
        assert "codex-solgoodman" in directive.text


def test_predecessor_recovery_can_be_disabled(git_repo: Path, tmp_path: Path) -> None:
    settings = make_settings(git_repo, tmp_path)
    settings.orchestrator.recover_predecessor_sessions = False
    orch = NightshiftOrchestrator(settings)
    try:
        import asyncio
        assert asyncio.run(orch._recover_predecessors({"sessions": {}})) == {}
        rows = orch.db.query("SELECT type FROM events")
        assert any(row["type"] == "recovery.predecessor_sessions_skipped" for row in rows)
    finally:
        orch.db.close()


def test_websocket_snapshot_watermark_and_heartbeat(
    git_repo: Path, tmp_path: Path, monkeypatch,
) -> None:
    settings = make_settings(git_repo, tmp_path)

    async def no_warmup(_orchestrator) -> None:
        return None

    monkeypatch.setattr(app_module, "_safe_warmup", no_warmup)
    monkeypatch.setattr(app_module, "WEBSOCKET_HEARTBEAT_SECONDS", 0.01)
    with (
        TestClient(create_app(settings)) as client,
        client.websocket_connect("/ws") as websocket,
    ):
        snapshot = websocket.receive_json()
        assert snapshot["type"] == "state.snapshot"
        assert snapshot["seq"] == snapshot["payload"]["event_seq"]
        heartbeat = websocket.receive_json()
        assert heartbeat["type"] == "system.heartbeat"
        assert heartbeat["seq"] == snapshot["payload"]["event_seq"]
