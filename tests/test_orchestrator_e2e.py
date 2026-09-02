from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from nightshift.config import default_settings
from nightshift.models import AgentResult, Usage
from nightshift.orchestrator import NightshiftOrchestrator


class ScriptedAdapter:
    def __init__(self, key: str) -> None:
        self.key = key
        self.calls: list[dict[str, Any]] = []
        self.binary = f"fake-{key}"

    async def probe(self, _cwd: Path) -> dict[str, Any]:
        return {
            "installed": True,
            "authenticated": True,
            "ready": True,
            "binary": self.binary,
            "version": "fake 1.0",
        }

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
        await event("log", {"stream": "stdout", "text": f"{self.key}:{task_id}"})

        if self.key == "grok":
            if "Review the completed worker task" in prompt:
                payload = {
                    "action": "accept",
                    "summary": "Diff is in scope and the fallback implementation is complete.",
                    "findings": [],
                    "required_changes": [],
                    "residual_risk": "low",
                    "acceptance_evidence": ["feature.py contains the completed implementation"],
                }
            elif "Perform the final Nightshift completion audit" in prompt:
                payload = {
                    "action": "done",
                    "summary": "Final audit confirms the reconstructed backlog is complete.",
                    "evidence": ["Accepted task NS-E2E is present on the integration branch"],
                    "remaining_items": [],
                }
            elif "Continue the emergency takeover" in prompt:
                payload = {
                    "action": "done",
                    "summary": "No implementation-ready backlog items remain.",
                    "evidence": ["NS-E2E accepted"],
                    "remaining_items": [],
                }
            else:
                payload = {
                    "action": "dispatch",
                    "summary": "Recovered the interruption point and selected one bounded task.",
                    "task": {
                        "id": "NS-E2E",
                        "title": "Finish interrupted feature",
                        "goal": "Complete feature.py after the original Spark lane reaches its limit.",
                        "worker": "spark",
                        "context": "The previous agents stopped unexpectedly.",
                        "source_ref": "BACKLOG.md:1",
                        "architectural_intent": "Preserve the existing tiny file contract.",
                        "allowed_paths": ["feature.py"],
                        "forbidden_paths": [],
                        "acceptance_criteria": ["feature.py contains COMPLETE"],
                        "validation_commands": [],
                        "stop_conditions": ["Any other file must change"],
                        "risk": "low",
                        "max_files": 1,
                    },
                    "backlog_remaining_estimate": 1,
                    "confidence": "high",
                }
            text = f"<SOL_LINK_JSON>{json.dumps(payload)}</SOL_LINK_JSON>"
            await event("assistant_delta", {"text": text})
            return AgentResult(
                ok=True,
                final_text=text,
                session_id=session_id or "grok-session",
                usage=Usage(input_tokens=100, output_tokens=25),
            )

        if self.key == "spark":
            (cwd / "feature.py").write_text("PARTIAL\n", encoding="utf-8")
            return AgentResult(
                ok=False,
                returncode=1,
                final_text="Started the file, then the subscription lane reached its limit.",
                limit_detected=True,
                error="weekly limit reached",
                usage=Usage(input_tokens=20, output_tokens=5),
            )

        if self.key == "luna":
            assert (cwd / "feature.py").read_text(encoding="utf-8") == "PARTIAL\n"
            (cwd / "feature.py").write_text("COMPLETE\n", encoding="utf-8")
            return AgentResult(
                ok=True,
                final_text="Salvaged Spark's partial commit and completed the task.",
                session_id="luna-task-session",
                usage=Usage(input_tokens=30, output_tokens=10),
            )

        raise AssertionError(f"Unexpected adapter key: {self.key}")


@pytest.mark.asyncio
async def test_full_takeover_spark_limit_falls_back_to_luna_and_completes(
    git_repo: Path, tmp_path: Path,
) -> None:
    (git_repo / "BACKLOG.md").write_text("- [ ] Finish interrupted feature\n", encoding="utf-8")
    settings = default_settings(str(git_repo))
    settings.orchestrator.runtime_dir = str(tmp_path / "runtime")
    settings.project.session_search_roots = []
    settings.orchestrator.recover_predecessor_sessions = False
    settings.orchestrator.max_revisions = 2
    for config in settings.agents.values():
        config.binary_candidates = ["/bin/echo"]
        config.inherit_previous_session = False

    orchestrator = NightshiftOrchestrator(settings)
    scripted = {key: ScriptedAdapter(key) for key in ("grok", "spark", "luna")}
    orchestrator.adapters = scripted
    orchestrator.agent_locks = {key: asyncio.Lock() for key in scripted}

    async def no_quota_network() -> dict[str, Any]:
        return {}

    orchestrator.refresh_quotas = no_quota_network  # type: ignore[method-assign]

    try:
        mission_id = await orchestrator.start_mission(
            "Recover the exact interruption point and finish the complete backlog."
        )
        assert orchestrator._mission_task is not None
        await orchestrator._mission_task

        mission = orchestrator.db.query("SELECT * FROM missions WHERE id=?", (mission_id,))[0]
        task = orchestrator.db.query("SELECT * FROM tasks WHERE mission_id=?", (mission_id,))[0]
        assert mission["status"] == "completed"
        assert task["status"] == "accepted"
        assert task["worker"] == "luna"
        assert task["attempt"] == 2
        assert orchestrator.workspace is not None
        assert (orchestrator.workspace.integration_path / "feature.py").read_text(
            encoding="utf-8"
        ) == "COMPLETE\n"

        event_types = {
            row["type"]
            for row in orchestrator.db.query(
                "SELECT type FROM events WHERE mission_id=?", (mission_id,)
            )
        }
        assert {
            "mission.forensics_ready",
            "sol_link.CONTRACT",
            "task.worker_limited",
            "sol_link.REVIEW",
            "sol_link.ACCEPTED",
            "mission.completed",
        } <= event_types
        assert len(scripted["spark"].calls) == 1
        assert len(scripted["luna"].calls) == 1
        assert all(not call["read_only"] for call in scripted["grok"].calls)
    finally:
        await orchestrator.close()
