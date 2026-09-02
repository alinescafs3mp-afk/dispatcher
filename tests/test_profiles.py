from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nightshift.app import create_app
from nightshift.config import default_settings
from nightshift.db import StateDB
from nightshift.git import MissionWorkspace
from nightshift.models import AgentResult, RiskLevel, TaskPacket
from nightshift.orchestrator import NightshiftOrchestrator, OrchestratorError
from nightshift.profiles import resolve_profile_agents


def make_settings(repo: Path, tmp_path: Path):
    settings = default_settings(str(repo))
    settings.orchestrator.runtime_dir = str(tmp_path / "runtime")
    for config in settings.agents.values():
        config.binary_candidates = [str(tmp_path / "missing")]
    return settings


def test_reserve_profile_preserves_emergency_wiring() -> None:
    settings = default_settings()
    agents = resolve_profile_agents(
        settings.agents,
        settings.profiles,
        "reserve",
        combat_grok_enabled=False,
    )
    assert agents["grok"].adapter == "grok"
    assert agents["grok"].display_name == "Grok 4.6"
    assert agents["luna"].binary_candidates == ["codex-solgoodman"]
    assert agents["spark"].binary_candidates == ["codex"]
    assert agents["spark"].model == "gpt-5.3-codex-spark"


def test_combat_profile_maps_sol_goodman_and_optional_grok() -> None:
    settings = default_settings()
    agents = resolve_profile_agents(
        settings.agents,
        settings.profiles,
        "combat",
        combat_grok_enabled=False,
    )
    assert agents["grok"].adapter == "codex"
    assert agents["grok"].binary_candidates == ["codex"]
    assert agents["grok"].display_name == "Sol"
    assert agents["grok"].model == ""
    assert agents["luna"].adapter == "codex"
    assert agents["luna"].binary_candidates == ["codex-solgoodman"]
    assert agents["luna"].display_name == "SolGoodman"
    assert agents["spark"].adapter == "grok"
    assert agents["spark"].display_name == "Grok 4.6"
    assert agents["spark"].optional is True
    assert agents["spark"].enabled is False

    enabled = resolve_profile_agents(
        settings.agents,
        settings.profiles,
        "combat",
        combat_grok_enabled=True,
    )
    assert enabled["spark"].enabled is True


@pytest.mark.asyncio
async def test_profile_switch_is_persisted_and_reroutes_disabled_helper(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        result = await orch.set_profile("combat", combat_grok_enabled=False)
        assert result["id"] == "combat"
        assert result["agents"]["grok"]["display_name"] == "Sol"
        assert result["agents"]["spark"]["enabled"] is False
        assert orch.db.get_preference("profile.active") == "combat"
        assert orch.db.get_preference("profile.combat.grok_enabled") is False

        packet = TaskPacket(
            title="bounded helper task",
            goal="make one safe change",
            worker="spark",
            allowed_paths=["feature.py"],
            acceptance_criteria=["feature works"],
            risk=RiskLevel.LOW,
            max_files=1,
        )
        assert orch._normalize_packet(packet).worker == "luna"
    finally:
        await orch.close()

    reopened = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        assert reopened.profile_id == "combat"
        assert reopened.settings.agent("grok").display_name == "Sol"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_profile_cannot_change_while_mission_task_is_alive(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    blocker = asyncio.Event()

    async def running() -> None:
        await blocker.wait()

    orch._mission_task = asyncio.create_task(running())
    try:
        with pytest.raises(OrchestratorError, match="cannot change"):
            await orch.set_profile("combat")
    finally:
        blocker.set()
        await orch.close()


class ChatAdapter:
    binary = "fake-chat"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        prompt: str,
        cwd: Path,
        task_id: str,
        session_id: str | None,
        event,
        read_only: bool = False,
    ) -> AgentResult:
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": str(cwd),
                "task_id": task_id,
                "session_id": session_id,
                "read_only": read_only,
            }
        )
        return AgentResult(
            ok=True,
            final_text="Goodman acknowledges the operator.",
            session_id="chat-goodman",
        )


@pytest.mark.asyncio
async def test_operator_can_chat_with_non_architect_participant(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    fake = ChatAdapter()
    orch.adapters["luna"] = fake  # type: ignore[assignment]
    try:
        response = await orch.chat(
            "Check why your last implementation looked suspicious.",
            recipient="luna",
            delivery="chat",
        )
        assert response["status"] == "answered"
        assert response["recipient"] == "luna"
        assert "acknowledges" in response["answer"]
        assert fake.calls[0]["read_only"] is True
        rows = orch.db.query("SELECT role,agent_key,kind,status FROM chat ORDER BY seq")
        assert [(row["role"], row["agent_key"]) for row in rows] == [
            ("user", "luna"),
            ("assistant", "luna"),
        ]
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_busy_participant_receives_durable_nudge_on_next_turn(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    lock = orch.agent_locks["luna"]
    await lock.acquire()
    try:
        response = await orch.chat(
            "Do not broaden the migration scope.",
            recipient="SolGoodman",
            delivery="auto",
        )
        assert response["status"] == "queued"
    finally:
        lock.release()

    try:
        prompt = await orch._inject_operator_notes("luna", "base prompt")
        assert "Do not broaden the migration scope" in prompt
        row = orch.db.query("SELECT kind,status,delivered_at FROM chat")[0]
        assert row["kind"] == "nudge"
        assert row["status"] == "delivered"
        assert row["delivered_at"]
        assert await orch._inject_operator_notes("luna", "next prompt") == "next prompt"
    finally:
        await orch.close()


def test_additive_database_migration_accepts_pre_profile_schema(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE missions (
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
            );
            CREATE TABLE chat (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    db = StateDB(path)
    try:
        mission_columns = {row["name"] for row in db.query("PRAGMA table_info(missions)")}
        chat_columns = {row["name"] for row in db.query("PRAGMA table_info(chat)")}
        assert {"profile", "profile_options_json"} <= mission_columns
        assert {
            "agent_key",
            "agent_id",
            "profile",
            "mission_id",
            "kind",
            "status",
            "delivered_at",
        } <= chat_columns
        indexes = {row["name"] for row in db.query("PRAGMA index_list(chat)")}
        assert "idx_chat_channel" in indexes
    finally:
        db.close()


def test_profile_api_switches_dashboard_contract(git_repo: Path, tmp_path: Path) -> None:
    settings = make_settings(git_repo, tmp_path)
    with TestClient(create_app(settings)) as client:
        response = client.put(
            "/api/profile",
            json={"profile": "combat", "combat_grok_enabled": True},
        )
        assert response.status_code == 200
        assert response.json()["agents"]["grok"]["display_name"] == "Sol"
        snapshot = client.get("/api/state").json()
        assert snapshot["profile"]["id"] == "combat"
        assert snapshot["profile"]["agents"]["luna"]["display_name"] == "SolGoodman"
        assert snapshot["profile"]["agents"]["spark"]["enabled"] is True
        assert client.get("/api/directive").headers["content-disposition"].endswith(
            'filename="COMBAT_OPERATIONS_DIRECTIVE.md"'
        )

@pytest.mark.asyncio
async def test_orchestrator_does_not_mutate_shared_settings(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    settings = make_settings(git_repo, tmp_path)
    original_grok_id = settings.agents["grok"].id
    original_grok_adapter = settings.agents["grok"].adapter
    original_spark_binary = list(settings.agents["spark"].binary_candidates)

    orch = NightshiftOrchestrator(settings)
    try:
        await orch.set_profile("combat", combat_grok_enabled=True)
        assert orch.settings.agent("grok").display_name == "Sol"
        assert settings.agents["grok"].id == original_grok_id
        assert settings.agents["grok"].adapter == original_grok_adapter
        assert settings.agents["spark"].binary_candidates == original_spark_binary
    finally:
        await orch.close()

    reopened = NightshiftOrchestrator(settings)
    try:
        assert reopened.profile_id == "combat"
        assert reopened.settings.agent("grok").adapter == "codex"
        assert reopened.settings.agent("spark").adapter == "grok"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_reselecting_active_profile_is_a_noop(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        orch._chat_session_ids[("reserve", "grok")] = "keep-session"
        before_adapters = orch.adapters
        result = await orch.set_profile(
            "reserve",
            combat_grok_enabled=orch.combat_grok_enabled,
        )
        assert result["id"] == "reserve"
        assert orch.adapters is before_adapters
        assert orch._chat_session_ids[("reserve", "grok")] == "keep-session"
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_invalid_resume_does_not_switch_profile(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        orch.db.create_mission(
            "combat-missing-worktree",
            str(git_repo),
            "resume me",
            "paused",
            profile="combat",
            profile_options={"combat_grok_enabled": True},
        )
        with pytest.raises(OrchestratorError, match="integration worktree"):
            await orch.resume_interrupted("combat-missing-worktree")
        assert orch.profile_id == "reserve"
        assert orch.combat_grok_enabled is False
        assert orch.settings.agent("grok").display_name == "Grok 4.6"
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_valid_resume_restores_mission_profile(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    settings = make_settings(git_repo, tmp_path)
    orch = NightshiftOrchestrator(settings)
    mission_id = "combat-resume"
    mission_dir = orch.runtime / "missions" / mission_id
    workspace = MissionWorkspace(orch.settings, mission_id, mission_dir)
    prepared = workspace.prepare()
    orch.db.create_mission(
        mission_id,
        str(git_repo),
        "resume combat development",
        "paused",
        profile="combat",
        profile_options={"combat_grok_enabled": True},
    )
    orch.db.update_mission(
        mission_id,
        base_sha=prepared["base_sha"],
        integration_branch=prepared["integration_branch"],
        integration_path=prepared["integration_path"],
    )
    release = asyncio.Event()

    async def held_resume(_row) -> None:
        await release.wait()

    orch._run_resumed_mission = held_resume  # type: ignore[method-assign]
    try:
        await orch.resume_interrupted(mission_id)
        assert orch.profile_id == "combat"
        assert orch.combat_grok_enabled is True
        assert orch.settings.agent("grok").display_name == "Sol"
        assert orch.settings.agent("spark").display_name == "Grok 4.6"
        assert orch.workspace is not None
        assert orch.workspace.integration_path == workspace.integration_path
    finally:
        release.set()
        await orch.close()
        workspace.cleanup(keep_integration=False)


class BlockingChatAdapter(ChatAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(
        self,
        prompt: str,
        cwd: Path,
        task_id: str,
        session_id: str | None,
        event,
        read_only: bool = False,
    ) -> AgentResult:
        self.started.set()
        await self.release.wait()
        return await super().run(
            prompt,
            cwd,
            task_id,
            session_id,
            event,
            read_only,
        )


@pytest.mark.asyncio
async def test_active_direct_chat_blocks_profile_switch(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    fake = BlockingChatAdapter()
    orch.adapters["luna"] = fake  # type: ignore[assignment]
    chat_task = asyncio.create_task(
        orch.chat("Explain the current implementation.", recipient="luna", delivery="chat")
    )
    try:
        await asyncio.wait_for(fake.started.wait(), timeout=1)
        with pytest.raises(OrchestratorError, match="agent turn is active"):
            await orch.set_profile("combat")
        assert orch.profile_id == "reserve"
    finally:
        fake.release.set()
        await chat_task
        await orch.close()


@pytest.mark.asyncio
async def test_snapshot_carries_latest_event_sequence(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        before = orch.snapshot()["event_seq"]
        await orch._emit("test.profile_event", {"ok": True})
        after = orch.snapshot()["event_seq"]
        assert after == before + 1
    finally:
        await orch.close()


def test_websocket_snapshot_exposes_event_sequence(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    settings = make_settings(git_repo, tmp_path)
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect("/ws") as websocket:
            message = websocket.receive_json()
        assert message["type"] == "state.snapshot"
        assert isinstance(message["payload"]["event_seq"], int)

@pytest.mark.asyncio
async def test_human_names_do_not_collide_with_internal_lane_keys(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        await orch.set_profile("combat", combat_grok_enabled=True)
        assert orch._resolve_chat_recipient("Sol") == "grok"
        assert orch._resolve_chat_recipient("Grok") == "spark"
        assert orch._resolve_chat_recipient("slot:grok") == "grok"
        assert orch._resolve_chat_recipient("slot:spark") == "spark"
    finally:
        await orch.close()
