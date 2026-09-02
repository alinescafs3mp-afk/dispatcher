from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nightshift.adapters.codex import CodexAdapter
from nightshift.app import create_app
from nightshift.config import default_settings
from nightshift.db import StateDB
from nightshift.forensics import ForensicsScanner
from nightshift.git import MissionWorkspace
from nightshift.models import AgentResult, ArchitectDone, RiskLevel, TaskPacket
from nightshift.orchestrator import NightshiftOrchestrator, OrchestratorError
from nightshift.process import ProcessRunner
from nightshift.profiles import get_profile, profile_prompt_context, resolve_profile_agents


def make_settings(repo: Path, tmp_path: Path):
    settings = default_settings(str(repo))
    settings.orchestrator.runtime_dir = str(tmp_path / "runtime")
    settings.server.allowed_hosts = ["testserver"]
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
    assert agents["grok"].unsafe_full_access is True
    assert agents["luna"].binary_candidates == ["codex-solgoodman"]
    assert agents["luna"].effort == "max"
    assert agents["luna"].unsafe_full_access is True
    assert agents["spark"].binary_candidates == ["codex"]
    assert agents["spark"].model == "gpt-5.3-codex-spark"
    assert agents["spark"].unsafe_full_access is True
    assert get_profile("reserve").architect_read_only is False


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
    assert agents["grok"].effort == "ultra"
    assert agents["grok"].unsafe_full_access is True
    assert agents["luna"].adapter == "codex"
    assert agents["luna"].binary_candidates == ["codex-solgoodman"]
    assert agents["luna"].display_name == "SolGoodman"
    assert agents["luna"].effort == "ultra"
    assert agents["luna"].unsafe_full_access is True
    assert agents["spark"].adapter == "grok"
    assert agents["spark"].display_name == "Grok 4.6"
    assert agents["spark"].optional is True
    assert agents["spark"].unsafe_full_access is True
    assert agents["spark"].enabled is False
    assert get_profile("combat").architect_read_only is False

    enabled = resolve_profile_agents(
        settings.agents,
        settings.profiles,
        "combat",
        combat_grok_enabled=True,
    )
    assert enabled["spark"].enabled is True




def test_profile_prompt_names_repository_and_operational_roots() -> None:
    settings = default_settings("/jericho/jericho")
    agents = resolve_profile_agents(
        settings.agents,
        settings.profiles,
        "combat",
        combat_grok_enabled=True,
    )

    prompt = profile_prompt_context(
        get_profile("combat"),
        agents,
        repository="/jericho/jericho",
        operational_roots=["~/.jericho"],
    )

    assert "`/jericho/jericho`" in prompt
    assert "`~/.jericho`" in prompt
    assert "may inspect or maintain an operational root" in prompt
    assert "not implicit Git integration scopes" in prompt


def test_task_packets_require_bounded_paths_and_acceptance_evidence() -> None:
    with pytest.raises(ValidationError):
        TaskPacket(
            title="unbounded",
            goal="change something",
            worker="luna",
            acceptance_criteria=["works"],
        )
    with pytest.raises(ValidationError):
        TaskPacket(
            title="unverifiable",
            goal="change something",
            worker="luna",
            allowed_paths=["src/**"],
        )

def test_requested_full_access_posture_reaches_codex_commands(tmp_path: Path) -> None:
    settings = default_settings()
    reserve = resolve_profile_agents(
        settings.agents,
        settings.profiles,
        "reserve",
        combat_grok_enabled=False,
    )
    combat = resolve_profile_agents(
        settings.agents,
        settings.profiles,
        "combat",
        combat_grok_enabled=False,
    )

    cases = [
        (reserve["luna"], 'model_reasoning_effort="max"'),
        (reserve["spark"], 'model_reasoning_effort="high"'),
        (combat["grok"], 'model_reasoning_effort="ultra"'),
        (combat["luna"], 'model_reasoning_effort="ultra"'),
    ]
    for config, effort in cases:
        config.binary_candidates = ["/bin/echo"]
        adapter = CodexAdapter(config, ProcessRunner())
        adapter.binary = "/bin/echo"
        command = adapter._command(
            tmp_path,
            tmp_path / "last-message",
            None,
            read_only=False,
            json_flag="--json",
        )
        assert "--dangerously-bypass-approvals-and-sandbox" in command
        assert effort in command

        chat_command = adapter._command(
            tmp_path,
            tmp_path / "last-message",
            None,
            read_only=True,
            json_flag="--json",
        )
        assert "--dangerously-bypass-approvals-and-sandbox" not in chat_command
        assert chat_command[chat_command.index("--sandbox") + 1] == "read-only"


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
        assert {row["id"] for row in orch.snapshot()["agents"]} == {
            "combat-sol",
            "combat-solgoodman",
            "combat-grok-helper",
        }

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
async def test_combat_logical_chat_keys_outrank_physical_template_aliases(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        await orch.set_profile("combat", combat_grok_enabled=True)
        assert orch._resolve_chat_recipient("grok") == "grok"
        assert orch._resolve_chat_recipient("spark") == "spark"
        assert orch._resolve_chat_recipient("Grok 4.6") == "spark"
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_combat_grok_accepts_bounded_medium_work_and_reroutes_high_risk(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        await orch.set_profile("combat", combat_grok_enabled=True)
        medium = TaskPacket(
            title="bounded diagnosis",
            goal="fix a contained integration defect",
            worker="spark",
            allowed_paths=["src/feature.py", "tests/test_feature.py"],
            acceptance_criteria=["the focused regression test passes"],
            risk=RiskLevel.MEDIUM,
        )
        normalized = orch._normalize_packet(medium)
        assert normalized.worker == "spark"
        assert normalized.max_files == 5
        assert orch._fallback_worker("luna", normalized) == "spark"

        high = medium.model_copy(update={"risk": RiskLevel.HIGH})
        assert orch._normalize_packet(high).worker == "luna"
    finally:
        await orch.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", ["reserve", "combat"])
async def test_architect_turn_uses_requested_full_access_posture(
    profile_id: str,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    class Workspace:
        architect_path = git_repo

        def sync_architect_worktree(self) -> Path:
            return git_repo

    class ArchitectAdapter:
        binary = "fake-sol"

        def __init__(self) -> None:
            self.read_only: list[bool] = []

        async def run(
            self,
            prompt: str,
            cwd: Path,
            task_id: str,
            session_id: str | None,
            event,
            read_only: bool = False,
        ) -> AgentResult:
            self.read_only.append(read_only)
            return AgentResult(
                ok=True,
                final_text=(
                    '<SOL_LINK_JSON>{"action":"done","summary":"checked",'
                    '"evidence":[],"remaining_items":[]}</SOL_LINK_JSON>'
                ),
                session_id="sol-session",
                raw_events=1,
            )

    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    fake = ArchitectAdapter()
    try:
        await orch.set_profile(profile_id, combat_grok_enabled=False)
        orch.workspace = Workspace()  # type: ignore[assignment]
        orch.adapters["grok"] = fake  # type: ignore[assignment]
        decision = await orch._ask_architect_decision("inspect", phase="test")
        assert decision.action == "done"
        assert fake.read_only == [False]
    finally:
        await orch.close()


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
        prompt, seqs = orch._prepare_operator_notes("luna", "base prompt")
        assert "Do not broaden the migration scope" in prompt
        row = orch.db.query("SELECT kind,status,delivered_at FROM chat")[0]
        assert row["kind"] == "nudge"
        assert row["status"] == "queued"
        assert not row["delivered_at"]

        await orch._settle_operator_notes(
            "luna",
            seqs,
            AgentResult(ok=False, returncode=1, error="transient failure"),
        )
        row = orch.db.query("SELECT status,delivered_at FROM chat")[0]
        assert row["status"] == "queued"
        assert not row["delivered_at"]

        retry_prompt, retry_seqs = orch._prepare_operator_notes("luna", "retry prompt")
        assert "Do not broaden the migration scope" in retry_prompt
        assert retry_seqs == seqs
        await orch._settle_operator_notes(
            "luna",
            retry_seqs,
            AgentResult(
                ok=False,
                returncode=1,
                final_text="The provider processed the steering note before exit.",
            ),
        )
        row = orch.db.query("SELECT status,delivered_at FROM chat")[0]
        assert row["status"] == "delivered"
        assert row["delivered_at"]
        assert orch._prepare_operator_notes("luna", "next prompt") == (
            "next prompt",
            [],
        )
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_nudge_after_completed_mission_becomes_global_for_the_next_turn(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        orch.mission_id = "completed-mission"
        orch.db.create_mission(
            "completed-mission",
            str(git_repo),
            "finished",
            "completed",
        )
        response = await orch.chat(
            "Carry this constraint into the next mission.",
            recipient="luna",
            delivery="nudge",
        )
        assert response["mission_id"] == ""
        row = orch.db.query("SELECT mission_id,status FROM chat")[0]
        assert row == {"mission_id": "", "status": "queued"}

        orch.mission_id = "new-mission"
        prompt, seqs = orch._prepare_operator_notes("luna", "new turn")
        assert "Carry this constraint" in prompt
        assert seqs
    finally:
        await orch.close()


def test_combat_forensics_skips_emergency_predecessor_session_scan(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    settings = make_settings(git_repo, tmp_path)
    scanner = ForensicsScanner(settings, tmp_path / "combat-scan")

    def unexpected_session_scan():
        raise AssertionError("combat profile must not enumerate predecessor sessions")

    scanner._scan_sessions = unexpected_session_scan  # type: ignore[method-assign]
    report = scanner.scan(include_sessions=False)
    assert report["sessions"] == {}
    assert report["sessions_scanned"] is False
    dossier = Path(report["markdown_path"]).read_text(encoding="utf-8")
    assert "Session discovery skipped" in dossier


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
        assert {
            "profile",
            "profile_options_json",
            "architect_session_id",
            "architect_turns",
        } <= mission_columns
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
async def test_direct_chat_persists_reply_before_releasing_profile_guard(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    fake = ChatAdapter()
    orch.adapters["luna"] = fake  # type: ignore[assignment]
    original_add_chat = orch.db.add_chat
    assistant_observed = False

    def guarded_add_chat(role: str, text: str, **kwargs):
        nonlocal assistant_observed
        if role == "assistant":
            assistant_observed = True
            assert orch.agent_locks["luna"].locked()
            assert kwargs["profile"] == "reserve"
            assert kwargs["agent_id"] == orch.settings.agent("luna").id
        return original_add_chat(role, text, **kwargs)

    orch.db.add_chat = guarded_add_chat  # type: ignore[method-assign]
    try:
        response = await orch.chat("Report your current concern.", recipient="luna")
        assert response["status"] == "answered"
        assert assistant_observed
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_direct_chat_does_not_consume_work_nudge(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    fake = ChatAdapter()
    orch.adapters["luna"] = fake  # type: ignore[assignment]
    try:
        queued = await orch.chat(
            "Keep the implementation inside the existing contract.",
            recipient="luna",
            delivery="nudge",
        )
        assert queued["status"] == "queued"

        response = await orch.chat(
            "What are you currently watching for?",
            recipient="luna",
            delivery="chat",
        )
        assert response["status"] == "answered"
        assert "Keep the implementation" not in str(fake.calls[-1]["prompt"])
        note = orch.db.query(
            "SELECT kind,status,delivered_at FROM chat WHERE kind='nudge'"
        )[0]
        assert note == {"kind": "nudge", "status": "queued", "delivered_at": ""}

        work_prompt, seqs = orch._prepare_operator_notes("luna", "worker turn")
        assert "Keep the implementation" in work_prompt
        assert seqs == [queued["seq"]]
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_mission_start_is_rejected_while_direct_agent_turn_is_active(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingChatAdapter(ChatAdapter):
        async def run(
            self,
            prompt: str,
            cwd: Path,
            task_id: str,
            session_id: str | None,
            event,
            read_only: bool = False,
        ) -> AgentResult:
            started.set()
            await release.wait()
            return await super().run(
                prompt,
                cwd,
                task_id,
                session_id,
                event,
                read_only=read_only,
            )

    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    orch.adapters["luna"] = BlockingChatAdapter()  # type: ignore[assignment]
    chat_task = asyncio.create_task(
        orch.chat("Inspect this before the mission begins.", recipient="luna")
    )
    try:
        await started.wait()
        with pytest.raises(OrchestratorError, match="agent turn is active"):
            await orch.start_mission("Reconcile and implement the backlog")
    finally:
        release.set()
        await chat_task
        await orch.close()


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
        before_seq = orch.db.latest_event_seq()
        result = await orch.set_profile(
            "reserve",
            combat_grok_enabled=orch.combat_grok_enabled,
        )
        assert result["id"] == "reserve"
        assert orch.adapters is before_adapters
        assert orch._chat_session_ids[("reserve", "grok")] == "keep-session"
        assert orch.db.latest_event_seq() == before_seq
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


class BlockingProfileChatAdapter(ChatAdapter):
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
            read_only=read_only,
        )


@pytest.mark.asyncio
async def test_active_direct_chat_blocks_profile_switch(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    fake = BlockingProfileChatAdapter()
    orch.adapters["luna"] = fake  # type: ignore[assignment]
    chat_task = asyncio.create_task(
        orch.chat(
            "Explain the current implementation.",
            recipient="luna",
            delivery="chat",
        )
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
async def test_explicit_chat_slot_aliases_do_not_collide_with_human_names(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    try:
        await orch.set_profile("combat", combat_grok_enabled=True)
        assert orch._resolve_chat_recipient("Sol") == "grok"
        assert orch._resolve_chat_recipient("Grok 4.6") == "spark"
        assert orch._resolve_chat_recipient("slot:grok") == "grok"
        assert orch._resolve_chat_recipient("slot:spark") == "spark"
    finally:
        await orch.close()


@pytest.mark.asyncio
async def test_stopping_mission_expires_only_mission_bound_nudges(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    orch = NightshiftOrchestrator(make_settings(git_repo, tmp_path))
    mission_id = "mission-with-nudges"
    orch.mission_id = mission_id
    orch.db.create_mission(
        mission_id,
        str(git_repo),
        "test stop",
        "running",
        profile=orch.profile_id,
        profile_options=orch._profile_options(),
    )
    mission_seq = orch.db.add_chat(
        "user",
        "Stop broadening this task.",
        agent_key="luna",
        agent_id=orch.settings.agent("luna").id,
        profile=orch.profile_id,
        mission_id=mission_id,
        kind="nudge",
        status="queued",
    )
    global_seq = orch.db.add_chat(
        "user",
        "Remember this on the next mission.",
        agent_key="luna",
        agent_id=orch.settings.agent("luna").id,
        profile=orch.profile_id,
        kind="nudge",
        status="queued",
    )
    release = asyncio.Event()

    async def running() -> None:
        await release.wait()

    orch._mission_task = asyncio.create_task(running())
    try:
        await orch.stop()
        rows = {
            int(row["seq"]): row
            for row in orch.db.query(
                "SELECT seq,status,delivered_at FROM chat WHERE seq IN (?,?)",
                (mission_seq, global_seq),
            )
        }
        assert rows[mission_seq]["status"] == "expired"
        assert rows[mission_seq]["delivered_at"]
        assert rows[global_seq]["status"] == "queued"
        events = orch.db.query(
            "SELECT type FROM events WHERE mission_id=?",
            (mission_id,),
        )
        assert any(row["type"] == "chat.nudges_expired" for row in events)
    finally:
        release.set()
        await orch.close()


@pytest.mark.asyncio
async def test_completed_mission_expires_undelivered_nudges(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    settings = make_settings(git_repo, tmp_path)
    settings.orchestrator.continue_until_backlog_done = False
    orch = NightshiftOrchestrator(settings)
    mission_id = "completed-with-nudge"
    orch.mission_id = mission_id
    orch.db.create_mission(
        mission_id,
        str(git_repo),
        "finish cleanly",
        "running",
        profile=orch.profile_id,
        profile_options=orch._profile_options(),
    )
    seq = orch.db.add_chat(
        "user",
        "Double-check the last change.",
        agent_key="luna",
        agent_id=orch.settings.agent("luna").id,
        profile=orch.profile_id,
        mission_id=mission_id,
        kind="nudge",
        status="queued",
    )
    try:
        await orch._decision_loop(
            ArchitectDone(action="done", summary="complete", evidence=["verified"])
        )
        row = orch.db.query("SELECT status FROM chat WHERE seq=?", (seq,))[0]
        assert row["status"] == "expired"
        mission = orch.db.query("SELECT status FROM missions WHERE id=?", (mission_id,))[0]
        assert mission["status"] == "completed"
    finally:
        await orch.close()
