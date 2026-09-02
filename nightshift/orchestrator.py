from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .adapters import CodexAdapter, GrokAdapter
from .config import Settings
from .db import StateDB
from .events import EventHub
from .forensics import ForensicsScanner
from .git import (GitError, MissionWorkspace, WorkerTree, assess_risk,
                  git, path_violations)
from .models import (
    AgentResult,
    AgentState,
    ArchitectBlocked,
    ArchitectDispatch,
    ArchitectDone,
    MissionState,
    ReviewDecision,
    RiskLevel,
    TaskPacket,
    TaskState,
    utc_now,
)
from .process import ProcessRunner
from .prompts import (
    architect_bootstrap_prompt,
    architect_next_prompt,
    architect_review_prompt,
    chat_prompt,
    final_audit_prompt,
    load_directive,
    recovery_handoff_prompt,
    worker_prompt,
)
from .protocol import compact_text, extract_json_dict
from .redaction import redact
from .quota import (
    codex_effort_options,
    normalize_codex_quota,
    normalize_grok_quota,
    read_codex_account,
    read_configured_quota,
    read_grok_billing,
)
from .validation import run_validation

_TERMINAL_MISSIONS = {
    MissionState.COMPLETED.value,
    MissionState.FAILED.value,
    MissionState.STOPPED.value,
}


class OrchestratorError(RuntimeError):
    pass


class NightshiftOrchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runtime = settings.orchestrator.runtime_path
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.db = StateDB(self.runtime / "nightshift.sqlite3")
        self._load_preferences()
        self.hub = EventHub()
        self.runner = ProcessRunner()
        self.adapters = {
            "grok": GrokAdapter(settings.agent("grok"), self.runner),
            "spark": CodexAdapter(settings.agent("spark"), self.runner),
            "luna": CodexAdapter(settings.agent("luna"), self.runner),
        }
        self.agent_locks = {key: asyncio.Lock() for key in self.adapters}
        self.quota_cache: dict[str, dict[str, Any]] = {}
        self.codex_homes: dict[str, str] = {}
        self.workspace: MissionWorkspace | None = None
        self.mission_id: str = ""
        self.mission_dir: Path | None = None
        self._mission_task: asyncio.Task | None = None
        self._pause_gate = asyncio.Event()
        self._pause_gate.set()
        self._stop_requested = asyncio.Event()
        self._approval_futures: dict[str, asyncio.Future] = {}
        self._grok_session_id: str | None = None
        self._grok_chat_session_id: str | None = None
        self._grok_turns = 0
        self._decision_counter = 0
        self._last_dossier: dict[str, Any] = {}
        self._init_agents()
        self._mark_interrupted_missions_paused()


    def _load_preferences(self) -> None:
        for key, config in self.settings.agents.items():
            saved = self.db.get_preference(f"agent.{key}.effort", "")
            if isinstance(saved, str) and saved in config.effort_options:
                config.effort = saved

    def _init_agents(self) -> None:
        for key, config in self.settings.agents.items():
            binary = config.resolve_binary()
            self.db.upsert_agent(
                config.id, config.role, AgentState.OFFLINE.value,
                model=config.model, binary=binary,
                metadata={"key": key, "effort": config.effort},
            )

    def _mark_interrupted_missions_paused(self) -> None:
        rows = self.db.query(
            "SELECT id,status FROM missions WHERE status NOT IN (?,?,?)",
            tuple(_TERMINAL_MISSIONS),
        )
        for row in rows:
            self.db.update_mission(row["id"], status=MissionState.PAUSED.value,
                                   summary="Nightshift process restarted; mission state preserved")

    async def close(self) -> None:
        self._stop_requested.set()
        self._pause_gate.set()
        for future in list(self._approval_futures.values()):
            if not future.done():
                future.cancel()
        self._approval_futures.clear()
        if self._mission_task and not self._mission_task.done():
            self._mission_task.cancel()
            await asyncio.gather(self._mission_task, return_exceptions=True)
        await self.runner.stop_all()
        self.db.close()

    async def _emit(self, event_type: str, payload: dict[str, Any],
                    sender: str = "nightshift", recipient: str = "ui",
                    task_id: str = "", mission_id: str | None = None) -> None:
        mission = self.mission_id if mission_id is None else mission_id
        seq = self.db.add_event(sender, recipient, event_type, payload,
                                mission_id=mission, task_id=task_id)
        await self.hub.publish({
            "seq": seq, "type": event_type, "payload": payload,
            "sender": sender, "recipient": recipient,
            "task_id": task_id, "mission_id": mission,
            "created_at": utc_now(),
        })

    async def _set_agent(self, key: str, state: AgentState, current_task: str = "",
                         error: str = "", session_id: str | None = None) -> None:
        config = self.settings.agent(key)
        fields: dict[str, Any] = {
            "state": state.value,
            "current_task": current_task,
            "last_error": error,
        }
        if session_id is not None:
            fields["session_id"] = session_id
        self.db.update_agent(config.id, **fields)
        await self._emit(
            "agent.status",
            {"key": key, "agent_id": config.id, "state": state.value,
             "current_task": current_task, "error": error,
             "session_id": session_id},
            sender=config.id,
        )

    def _callback(self, key: str, task_id: str = ""):
        config = self.settings.agent(key)

        async def callback(kind: str, payload: dict[str, Any]) -> None:
            if kind in {"log", "assistant", "assistant_delta", "tool", "validation"}:
                if kind == "log":
                    text = str(payload.get("text", ""))
                    stream = str(payload.get("stream", "stdout"))
                elif kind in {"assistant", "assistant_delta"}:
                    text = str(payload.get("text", ""))
                    stream = "assistant"
                else:
                    text = json.dumps(payload, ensure_ascii=False)
                    stream = kind
                if text:
                    self.db.add_log(config.id, compact_text(text, 20000), stream=stream,
                                    task_id=task_id)
            await self._emit(
                f"agent.{kind}", {"key": key, **payload},
                sender=config.id, task_id=task_id,
            )
        return callback

    async def doctor(self) -> dict[str, Any]:
        repo = self.settings.project.repo_path
        results: dict[str, Any] = {}
        for key, adapter in self.adapters.items():
            config = self.settings.agent(key)
            if not config.enabled:
                results[key] = {"enabled": False, "ready": False}
                continue
            probe = await adapter.probe(repo if repo.exists() else Path.cwd())
            results[key] = probe
            state = AgentState.IDLE if probe.get("ready") else AgentState.OFFLINE
            self.db.upsert_agent(
                config.id, config.role, state.value,
                model=config.model, binary=probe.get("binary", adapter.binary),
                last_error=probe.get("error", ""),
                metadata={"key": key, "effort": config.effort, "probe": probe},
            )
            await self._emit("agent.probe", {"key": key, **probe}, sender=config.id)
        return results

    async def refresh_quotas(self) -> dict[str, Any]:
        repo = self.settings.project.repo_path
        cwd = repo if repo.exists() else Path.cwd()
        snapshots: dict[str, Any] = {}
        for key in ("spark", "luna"):
            config = self.settings.agent(key)
            binary = config.resolve_binary()
            try:
                payload = await read_codex_account(
                    binary, cwd, env=config.subprocess_env()
                )
                snapshot = normalize_codex_quota(config.id, payload)
                home = str(payload.get("codex_home") or "")
                if home:
                    self.codex_homes[key] = home
                options, matched_model = codex_effort_options(
                    payload, config.model, prefer_luna=(key == "luna")
                )
                if options:
                    if config.effort and config.effort not in options:
                        options.append(config.effort)
                    config.effort_options = options
                self.db.update_agent(
                    config.id,
                    model=config.model or matched_model,
                    metadata_json={
                        "key": key,
                        "effort": config.effort,
                        "effort_options": config.effort_options,
                        "resolved_model": matched_model,
                        "quota_source": "codex app-server",
                    },
                )
            except Exception as exc:
                snapshot = {
                    "agent_id": config.id,
                    "available": False,
                    "windows": [],
                    "message": str(exc),
                    "fetched_at": utc_now(),
                }
            data = snapshot.model_dump() if hasattr(snapshot, "model_dump") else snapshot
            snapshots[key] = data
            self.quota_cache[key] = data
            await self._emit("agent.quota", {"key": key, "snapshot": data}, sender=config.id)

        grok_config = self.settings.agent("grok")
        try:
            payload = await read_grok_billing(
                grok_config.resolve_binary(), cwd, env=grok_config.subprocess_env()
            )
            grok_snapshot = normalize_grok_quota(grok_config.id, payload)
        except Exception as acp_exc:
            grok_snapshot = await read_configured_quota(grok_config, self.runner, cwd)
            if not grok_snapshot.available:
                grok_snapshot.message = (
                    f"Grok ACP billing unavailable: {acp_exc}. "
                    + grok_snapshot.message
                )
        grok_data = grok_snapshot.model_dump()
        snapshots["grok"] = grok_data
        self.quota_cache["grok"] = grok_data
        self.db.update_agent(
            grok_config.id,
            metadata_json={
                "key": "grok",
                "effort": grok_config.effort,
                "effort_options": grok_config.effort_options,
                "quota_source": grok_data.get("raw", {}).get("source", "fallback"),
            },
        )
        await self._emit(
            "agent.quota", {"key": "grok", "snapshot": grok_data},
            sender=grok_config.id,
        )
        return snapshots

    async def set_reasoning(self, key: str, effort: str) -> dict[str, Any]:
        if key not in self.settings.agents:
            raise OrchestratorError(f"Unknown agent: {key}")
        config = self.settings.agent(key)
        effort = effort.strip().lower()
        if effort not in config.effort_options:
            raise OrchestratorError(
                f"Unsupported reasoning effort for {key}: {effort}. "
                f"Allowed: {', '.join(config.effort_options)}"
            )
        config.effort = effort
        self.db.set_preference(f"agent.{key}.effort", effort)
        self.db.update_agent(
            config.id,
            model=config.model,
            metadata_json={
                "key": key,
                "effort": effort,
                "effort_options": config.effort_options,
                "applies": "next model turn",
            },
        )
        payload = {
            "key": key,
            "agent_id": config.id,
            "effort": effort,
            "effort_options": config.effort_options,
            "applies": "next model turn",
        }
        await self._emit("agent.reasoning_changed", payload, sender="human")
        return payload

    def snapshot(self) -> dict[str, Any]:
        data = self.db.snapshot(log_tail=self.settings.orchestrator.log_tail_lines)
        data["quotas"] = self.quota_cache
        data["config"] = self.settings.public_dict()
        data["active_mission_id"] = self.mission_id
        data["mission_running"] = bool(self._mission_task and not self._mission_task.done())
        return data

    async def start_mission(self, goal: str) -> str:
        if self._mission_task and not self._mission_task.done():
            raise OrchestratorError("A mission is already running")
        repo = self.settings.project.repo_path
        if not repo.exists():
            raise OrchestratorError(f"Repository does not exist: {repo}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.mission_id = f"ns-{stamp}-{uuid.uuid4().hex[:6]}"
        self.mission_dir = self.runtime / "missions" / self.mission_id
        self.mission_dir.mkdir(parents=True, exist_ok=False)
        directive_copy = self.mission_dir / "EMERGENCY_TAKEOVER_DIRECTIVE.md"
        directive_copy.write_text(load_directive(), encoding="utf-8")
        self.db.create_mission(
            self.mission_id, str(repo), goal, MissionState.CREATED.value,
            directive_path=str(directive_copy),
        )
        self._stop_requested.clear()
        self._pause_gate.set()
        self._grok_session_id = None
        self.db.update_agent(self.settings.agent("grok").id, session_id="")
        self._grok_chat_session_id = None
        self._grok_turns = 0
        self._decision_counter = 0
        self._mission_task = asyncio.create_task(self._run_new_mission(self.mission_id, goal))
        await self._emit("mission.created", {"mission_id": self.mission_id, "goal": goal})
        return self.mission_id

    async def resume_interrupted(self, mission_id: str) -> None:
        if self._mission_task and not self._mission_task.done():
            raise OrchestratorError("A mission is already running")
        rows = self.db.query("SELECT * FROM missions WHERE id=?", (mission_id,))
        if not rows:
            raise OrchestratorError("Mission not found")
        row = rows[0]
        integration_path = Path(row["integration_path"])
        if not integration_path.exists():
            raise OrchestratorError("Mission integration worktree no longer exists")
        self.mission_id = mission_id
        self.mission_dir = self.runtime / "missions" / mission_id
        self.workspace = MissionWorkspace.reopen(
            self.settings, mission_id, self.mission_dir, integration_path,
            row["integration_branch"], row["base_sha"],
        )
        self._stop_requested.clear()
        self._pause_gate.set()
        agent_rows = self.db.query("SELECT session_id FROM agents WHERE id=?",
                                   (self.settings.agent("grok").id,))
        self._grok_session_id = agent_rows[0]["session_id"] if agent_rows and agent_rows[0]["session_id"] else None
        self._mission_task = asyncio.create_task(self._run_resumed_mission(row))
        await self._emit("mission.resumed", {"mission_id": mission_id})

    async def pause(self) -> None:
        self._pause_gate.clear()
        if self.mission_id:
            self.db.update_mission(self.mission_id, status=MissionState.PAUSED.value)
        await self._emit("mission.paused", {"mission_id": self.mission_id})

    async def resume(self) -> None:
        self._pause_gate.set()
        if self.mission_id:
            self.db.update_mission(self.mission_id, status=MissionState.RUNNING.value)
        await self._emit("mission.running", {"mission_id": self.mission_id})

    async def stop(self) -> None:
        self._stop_requested.set()
        self._pause_gate.set()
        await self.runner.stop_all()
        if self._mission_task and not self._mission_task.done():
            self._mission_task.cancel()
            await asyncio.gather(self._mission_task, return_exceptions=True)
        if self.mission_id:
            self.db.update_mission(self.mission_id, status=MissionState.STOPPED.value,
                                   summary="Stopped by human operator")
        await self._emit("mission.stopped", {"mission_id": self.mission_id})

    async def approve_task(self, task_id: str, approved: bool, note: str = "") -> None:
        future = self._approval_futures.get(task_id)
        if future is None or future.done():
            raise OrchestratorError("Task is not waiting for approval")
        future.set_result({"approved": approved, "note": note})
        await self._emit("task.human_decision", {"approved": approved, "note": note},
                         sender="human", task_id=task_id)

    async def chat(self, text: str) -> str:
        """A persistent operator channel that does not pollute mission decisions."""
        text = text.strip()
        if not text:
            raise OrchestratorError("Message is empty")
        self.db.add_chat("user", text)
        await self._emit("chat.message", {"role": "user", "text": text}, sender="human")
        cwd = self.settings.project.repo_path
        if self.workspace:
            cwd = await asyncio.to_thread(self.workspace.sync_architect_worktree)
        digest = self._mission_digest() if self.mission_id else "No active Nightshift mission."
        prompt = chat_prompt(text) + "\n\nCurrent compact mission ledger:\n" + compact_text(digest, 9000)
        await self._set_agent("grok", AgentState.PLANNING, current_task="operator-chat")
        async with self.agent_locks["grok"]:
            result = await self.adapters["grok"].run(
                prompt, cwd, "chat", self._grok_chat_session_id,
                self._callback("grok", "chat"), read_only=True,
            )
        self._grok_chat_session_id = result.session_id or self._grok_chat_session_id
        self._record_usage("grok", "chat", result)
        await self._set_agent(
            "grok",
            AgentState.IDLE if result.ok else (AgentState.LIMITED if result.limit_detected else AgentState.ERROR),
            error=result.error,
        )
        if self.workspace:
            await asyncio.to_thread(self.workspace.sync_architect_worktree)
        answer = result.final_text or result.error or "Grok returned no text."
        self.db.add_chat("assistant", answer)
        await self._emit("chat.message", {"role": "assistant", "text": answer},
                         sender=self.settings.agent("grok").id)
        return answer

    async def _run_new_mission(self, mission_id: str, goal: str) -> None:
        try:
            self.db.update_mission(mission_id, status=MissionState.RECOVERING.value)
            await self._emit("mission.recovering", {"mission_id": mission_id})
            assert self.mission_dir is not None
            self.workspace = MissionWorkspace(self.settings, mission_id, self.mission_dir)
            prepared = await asyncio.to_thread(self.workspace.prepare)
            self.db.update_mission(
                mission_id,
                base_sha=str(prepared["base_sha"]),
                integration_branch=str(prepared["integration_branch"]),
                integration_path=str(prepared["integration_path"]),
            )
            await self._emit("mission.workspace_ready", prepared)
            await self.doctor()
            await self.refresh_quotas()
            scanner = ForensicsScanner(self.settings, self.mission_dir, self.codex_homes)
            dossier = await asyncio.to_thread(scanner.scan)
            self._last_dossier = dossier
            self.db.update_mission(mission_id, forensics_path=dossier["markdown_path"])
            await self._emit("mission.forensics_ready", {
                "dossier": dossier["markdown_path"], "json": dossier["json_path"]
            })
            predecessor_handoffs = await self._recover_predecessors(dossier)
            self._append_handoffs_to_dossier(Path(dossier["markdown_path"]), predecessor_handoffs)
            self.db.update_mission(mission_id, status=MissionState.RUNNING.value)
            await self._emit("mission.running", {"mission_id": mission_id})
            assert self.workspace is not None
            architect_cwd = await asyncio.to_thread(self.workspace.sync_architect_worktree)
            prompt = architect_bootstrap_prompt(
                load_directive(), Path(dossier["markdown_path"]), architect_cwd,
                goal, predecessor_handoffs,
            )
            decision = await self._ask_architect_decision(prompt, phase="bootstrap")
            await self._decision_loop(decision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_mission(exc)

    async def _run_resumed_mission(self, row: dict[str, Any]) -> None:
        try:
            self.db.update_mission(self.mission_id, status=MissionState.RECOVERING.value,
                                   summary="Reconstructing state after Nightshift process interruption")
            await self._emit("mission.recovering", {"mission_id": self.mission_id, "resume": True})
            await self.doctor()
            await self.refresh_quotas()
            assert self.workspace is not None and self.mission_dir is not None
            scanner = ForensicsScanner(self.settings, self.mission_dir / "resume-scans" / utc_now().replace(":", "-"), self.codex_homes)
            dossier = await asyncio.to_thread(scanner.scan)
            self._last_dossier = dossier
            self.db.update_mission(
                self.mission_id, status=MissionState.RUNNING.value,
                summary="Resumed after process interruption",
                forensics_path=dossier["markdown_path"],
            )
            digest = self._mission_digest()
            cwd = await asyncio.to_thread(self.workspace.sync_architect_worktree)
            try:
                dossier_excerpt = compact_text(
                    Path(dossier["markdown_path"]).read_text(
                        encoding="utf-8", errors="replace"
                    ),
                    50_000,
                )
            except OSError as exc:
                dossier_excerpt = f"<dossier read failed: {exc}>"
            prompt = f"""{load_directive()}

# Nightshift process-restart recovery
The control process itself stopped after this mission had already begun. Perform Phase Zero again before dispatching more work. Inspect all preserved worker branches/worktrees and the integration worktree at `{cwd}`. Reconcile any task whose database state disagrees with Git evidence.

# Embedded recovery dossier excerpt
{dossier_excerpt}

Compact persisted mission ledger:
{compact_text(digest, 16000)}

Select exactly one safe continuation task, or declare completion only after the full final audit. End with one `<SOL_LINK_JSON>` object."""
            decision = await self._ask_architect_decision(prompt, phase="resume-recovery")
            await self._decision_loop(decision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_mission(exc)

    async def _decision_loop(self, decision: Any) -> None:
        completed_count = 0
        final_audit_attempted = False
        while not self._stop_requested.is_set():
            await self._pause_gate.wait()
            if isinstance(decision, ArchitectDispatch):
                if completed_count >= self.settings.orchestrator.max_tasks:
                    raise OrchestratorError("Mission task cap reached; refusing an unbounded loop")
                final_audit_attempted = False
                await self._execute_task(decision.task)
                completed_count += 1
                digest = self._mission_digest()
                assert self.workspace is not None
                cwd = await asyncio.to_thread(self.workspace.sync_architect_worktree)
                decision = await self._ask_architect_decision(
                    architect_next_prompt(digest, cwd), phase="next",
                )
                continue
            if isinstance(decision, ArchitectDone):
                if final_audit_attempted or not self.settings.orchestrator.continue_until_backlog_done:
                    self.db.update_mission(
                        self.mission_id, status=MissionState.COMPLETED.value,
                        summary=decision.summary,
                    )
                    await self._emit("mission.completed", decision.model_dump())
                    return
                final_audit_attempted = True
                digest = self._mission_digest()
                assert self.workspace is not None
                cwd = await asyncio.to_thread(self.workspace.sync_architect_worktree)
                decision = await self._ask_architect_decision(
                    final_audit_prompt(digest, cwd), phase="final-audit",
                )
                continue
            if isinstance(decision, ArchitectBlocked):
                state = MissionState.BLOCKED.value
                self.db.update_mission(self.mission_id, status=state, summary=decision.summary)
                await self._emit("mission.blocked", decision.model_dump())
                return
            raise OrchestratorError(f"Unsupported architect decision: {type(decision).__name__}")

    async def _recover_predecessors(self, dossier: dict[str, Any]) -> dict[str, str]:
        if not self.settings.orchestrator.recover_predecessor_sessions:
            await self._emit("recovery.predecessor_sessions_skipped", {"configured": False})
            return {}
        handoffs: dict[str, str] = {}
        sessions = dossier.get("sessions") or {}
        for key, predecessor in (("spark", "SolGoodman"), ("luna", "Sol")):
            config = self.settings.agent(key)
            if not config.enabled or not config.inherit_previous_session:
                continue
            candidates = sessions.get(key) or sessions.get("unassigned") or []
            candidate = next((item for item in candidates if item.get("session_id")), None)
            if not candidate:
                await self._emit("recovery.session_missing", {"key": key, "predecessor": predecessor})
                continue
            session_id = str(candidate["session_id"])
            await self._set_agent(key, AgentState.RECOVERING, current_task="predecessor-handoff")
            async with self.agent_locks[key]:
                result = await self.adapters[key].run(
                    recovery_handoff_prompt(predecessor, self.settings.project.repo_path),
                    self.workspace.integration_path if self.workspace else self.settings.project.repo_path,
                    f"recovery-{key}", session_id, self._callback(key, f"recovery-{key}"),
                    read_only=True,
                )
            self._record_usage(key, f"recovery-{key}", result)
            if result.ok and result.final_text:
                safe_handoff = redact(result.final_text)
                handoffs[key] = safe_handoff
                assert self.mission_dir is not None
                path = self.mission_dir / "forensics" / f"PREDECESSOR_HANDOFF_{predecessor}.md"
                path.write_text(safe_handoff, encoding="utf-8")
                await self._emit("recovery.session_handoff", {
                    "key": key, "predecessor": predecessor, "session_id": session_id,
                    "path": str(path),
                })
            else:
                handoffs[key] = f"Recovery call failed for session {session_id}: {result.error}"
                await self._emit("recovery.session_failed", {
                    "key": key, "session_id": session_id, "error": result.error,
                    "limit_detected": result.limit_detected,
                })
            await self._set_agent(key, AgentState.IDLE,
                                  session_id=result.session_id or session_id,
                                  error=result.error)
        return handoffs

    @staticmethod
    def _append_handoffs_to_dossier(path: Path, handoffs: dict[str, str]) -> None:
        if not handoffs:
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n## Live fallback handoffs from predecessor sessions\n")
            for key, text in handoffs.items():
                handle.write(f"\n### {key}\n\n{compact_text(redact(text), 12000)}\n")

    async def _run_grok(self, prompt: str, cwd: Path, phase: str,
                        read_only: bool = True) -> AgentResult:
        if self._grok_turns >= self.settings.orchestrator.architect_session_max_turns:
            self._grok_session_id = None
            self._grok_turns = 0
            prompt = (
                "This is a rotated architect session. Reconstruct continuity from the repository, "
                "Nightshift ledger, and the prompt below. The emergency directive remains binding.\n\n" + prompt
            )
            await self._emit("architect.session_rotated", {"phase": phase})
        await self._set_agent("grok", AgentState.PLANNING if phase != "review" else AgentState.REVIEWING,
                              current_task=phase)
        async with self.agent_locks["grok"]:
            result = await self.adapters["grok"].run(
                prompt, cwd, phase, self._grok_session_id,
                self._callback("grok", phase), read_only=read_only,
            )
        self._grok_session_id = result.session_id or self._grok_session_id
        self._grok_turns += 1
        self._record_usage("grok", phase, result)
        await self._set_agent(
            "grok", AgentState.IDLE if result.ok else (AgentState.LIMITED if result.limit_detected else AgentState.ERROR),
            session_id=self._grok_session_id, error=result.error,
        )
        if self.workspace:
            # Architect has a disposable detached worktree. Any accidental writes are discarded.
            await asyncio.to_thread(self.workspace.sync_architect_worktree)
        return result

    async def _ask_architect_decision(self, prompt: str, phase: str) -> Any:
        assert self.workspace is not None
        cwd = await asyncio.to_thread(self.workspace.sync_architect_worktree)
        result = await self._run_grok(prompt, cwd, phase, read_only=True)
        if not result.ok:
            if result.limit_detected:
                raise OrchestratorError("Grok architect limit reached; preserved state requires human intervention")
            raise OrchestratorError(f"Grok architect failed: {result.error}")
        try:
            decision = self._parse_architect_decision(result.final_text)
        except (ValueError, ValidationError) as first_error:
            repair_prompt = f"""Your previous response could not be parsed by Sol Link Nightshift:
{first_error}

Repeat the decision only. End with exactly one valid `<SOL_LINK_JSON>` object matching the standing directive. Do not add another JSON object."""
            repair = await self._run_grok(repair_prompt, cwd, f"{phase}-repair", read_only=True)
            if not repair.ok:
                raise OrchestratorError(f"Architect output repair failed: {repair.error}")
            decision = self._parse_architect_decision(repair.final_text)
        await self._emit("architect.decision", {
            "phase": phase,
            "decision": decision.model_dump(),
        }, sender=self.settings.agent("grok").id)
        return decision

    @staticmethod
    def _parse_architect_decision(text: str) -> Any:
        data = extract_json_dict(text)
        action = data.get("action")
        if action == "dispatch":
            return ArchitectDispatch.model_validate(data)
        if action == "done":
            return ArchitectDone.model_validate(data)
        if action in {"blocked", "ask_user"}:
            return ArchitectBlocked.model_validate(data)
        raise ValueError(f"Unknown architect action: {action!r}")

    @staticmethod
    def _parse_review(text: str) -> ReviewDecision:
        return ReviewDecision.model_validate(extract_json_dict(text))

    async def _execute_task(self, original_packet: TaskPacket) -> None:
        assert self.workspace is not None
        packet = self._normalize_packet(original_packet)
        task_id = self._unique_task_id(packet.id)
        packet.id = task_id
        base_sha = self.workspace.integration_head()
        self.db.create_task(self.mission_id, task_id, packet.model_dump(mode="json"),
                            TaskState.READY.value, base_sha=base_sha)
        await self._emit("sol_link.CONTRACT", packet.model_dump(mode="json"),
                         sender=self.settings.agent("grok").id,
                         recipient=self.settings.agent(packet.worker).id,
                         task_id=task_id)
        tree = await asyncio.to_thread(self.workspace.create_worker, task_id, packet.worker)
        active_worker = packet.worker
        revision_context: dict[str, Any] | None = None
        latest_review: ReviewDecision | None = None

        for attempt in range(1, self.settings.orchestrator.max_revisions + 2):
            await self._pause_gate.wait()
            if self._stop_requested.is_set():
                return
            self.db.update_task(task_id, status=(TaskState.IMPLEMENTING.value if attempt == 1 else TaskState.REVISION.value),
                                attempt=attempt)
            await self._set_agent(active_worker, AgentState.WORKING, current_task=task_id)
            prompt = worker_prompt(packet, tree.base_sha, revision_context)
            async with self.agent_locks[active_worker]:
                result = await self.adapters[active_worker].run(
                    prompt, tree.path, task_id, None,
                    self._callback(active_worker, task_id), read_only=False,
                )
            self._record_usage(active_worker, task_id, result)
            await self._set_agent(
                active_worker,
                AgentState.IDLE if result.ok else (AgentState.LIMITED if result.limit_detected else AgentState.ERROR),
                error=result.error,
            )
            safety_violations = await asyncio.to_thread(
                self.workspace.sanitize_worker_changes, tree
            )
            try:
                worker_head = await asyncio.to_thread(
                    self.workspace.commit_worker, tree,
                    f"nightshift({task_id}): worker attempt {attempt} by {active_worker}",
                )
            except GitError as exc:
                worker_head = await asyncio.to_thread(
                    git, tree.path, "rev-parse", "HEAD"
                )
                safety_violations.append(f"worker commit refused: {exc}")
                result = result.model_copy(update={
                    "ok": False,
                    "error": (result.error + "\n" + str(exc)).strip(),
                })
            self.db.update_task(task_id, worker_head=worker_head.strip(),
                                result_json=result.model_dump(mode="json"))

            if result.limit_detected:
                fallback = self._fallback_worker(active_worker, packet)
                await self._emit("task.worker_limited", {
                    "worker": active_worker, "fallback": fallback,
                    "worker_head": worker_head, "error": result.error,
                }, task_id=task_id)
                if fallback and attempt <= self.settings.orchestrator.max_revisions:
                    revision_context = {
                        "reason": f"{active_worker} hit its quota or rollout limit",
                        "instruction": "Inspect and salvage the existing partial diff in this same worktree, then complete the original packet.",
                        "previous_error": result.error,
                    }
                    active_worker = fallback
                    packet.worker = fallback
                    continue

            changed_paths = await asyncio.to_thread(self.workspace.worker_changed_files, tree)
            diff_text = await asyncio.to_thread(self.workspace.worker_diff, tree)
            violations = safety_violations + path_violations(
                changed_paths, packet.allowed_paths, packet.forbidden_paths,
                self.settings.project.protected_paths, packet.max_files,
            )
            measured_risk = assess_risk(
                changed_paths, diff_text, packet.risk,
                self.settings.project.high_risk_paths,
            )
            commands = self._dedupe(packet.validation_commands + self.settings.project.validation_commands)
            self.db.update_task(task_id, status=TaskState.VALIDATING.value)
            validation = await run_validation(
                commands, tree.path, self.runner, f"validate:{task_id}",
                self._callback(active_worker, task_id),
                timeout=self.settings.orchestrator.command_timeout_seconds,
            ) if commands else {"ok": True, "commands": [], "note": "No validation commands configured"}
            if not result.ok:
                validation["worker_error"] = result.error
                validation["ok"] = False
            if not changed_paths:
                violations.append("worker produced no changed files")

            self.db.update_task(task_id, status=TaskState.REVIEWING.value)
            await self._set_agent("grok", AgentState.REVIEWING, current_task=task_id)
            review_prompt = architect_review_prompt(
                packet, tree.path, tree.base_sha, worker_head, changed_paths,
                validation, violations, measured_risk.value,
            )
            grok_result = await self._run_grok(
                review_prompt,
                await asyncio.to_thread(self.workspace.sync_architect_worktree),
                phase="review", read_only=True,
            )
            if not grok_result.ok:
                raise OrchestratorError(f"Grok review failed: {grok_result.error}")
            try:
                latest_review = self._parse_review(grok_result.final_text)
            except (ValueError, ValidationError) as exc:
                repair = await self._run_grok(
                    f"Review JSON failed validation: {exc}. Repeat only one valid marked review object.",
                    await asyncio.to_thread(self.workspace.sync_architect_worktree),
                    phase="review-repair", read_only=True,
                )
                if not repair.ok:
                    raise OrchestratorError("Review repair failed")
                latest_review = self._parse_review(repair.final_text)

            # Deterministic boundaries outrank model acceptance.
            if latest_review.action == "accept" and violations:
                latest_review = latest_review.model_copy(update={
                    "action": "revise",
                    "required_changes": latest_review.required_changes + violations,
                    "summary": "Model acceptance overridden by deterministic scope enforcement",
                })
            if latest_review.action == "accept" and not validation.get("ok", False):
                latest_review = latest_review.model_copy(update={
                    "action": "revise",
                    "required_changes": latest_review.required_changes + ["Make configured validation pass or provide an explicit safe replacement command"],
                    "summary": "Model acceptance overridden because validation failed",
                })
            self.db.update_task(task_id, review_json=latest_review.model_dump(mode="json"))
            await self._emit("sol_link.REVIEW", latest_review.model_dump(mode="json"),
                             sender=self.settings.agent("grok").id,
                             recipient=self.settings.agent(active_worker).id,
                             task_id=task_id)

            if latest_review.action == "accept":
                effective_risk = max(
                    [packet.risk, measured_risk, latest_review.residual_risk],
                    key=lambda level: list(RiskLevel).index(level),
                )
                if self._needs_human_gate(effective_risk):
                    approved = await self._wait_for_human(task_id, packet, latest_review, effective_risk)
                    if not approved:
                        self.db.update_task(task_id, status=TaskState.REJECTED.value)
                        await self._emit("task.rejected_by_human", {"risk": effective_risk.value}, task_id=task_id)
                        return
                integration_head = await asyncio.to_thread(
                    self.workspace.integrate_worker, tree,
                    f"nightshift({task_id}): {packet.title}",
                )
                self.db.update_task(task_id, status=TaskState.ACCEPTED.value,
                                    worker_head=worker_head)
                await self._emit("sol_link.ACCEPTED", {
                    "task_id": task_id, "integration_head": integration_head,
                    "worker_head": worker_head, "changed_paths": changed_paths,
                    "validation": validation, "risk": effective_risk.value,
                }, task_id=task_id)
                await asyncio.to_thread(self.workspace.remove_worker_worktree, tree, True)
                return

            if latest_review.action == "revise" and attempt <= self.settings.orchestrator.max_revisions:
                revision_context = latest_review.model_dump(mode="json")
                await self._emit("sol_link.CHANGES_REQUESTED", revision_context,
                                 sender=self.settings.agent("grok").id,
                                 recipient=self.settings.agent(active_worker).id,
                                 task_id=task_id)
                continue

            if latest_review.action in {"escalate", "reject"}:
                if latest_review.action == "escalate":
                    approved = await self._wait_for_human(
                        task_id, packet, latest_review,
                        latest_review.residual_risk,
                    )
                    if approved and validation.get("ok") and not violations:
                        integration_head = await asyncio.to_thread(
                            self.workspace.integrate_worker, tree,
                            f"nightshift({task_id}): {packet.title} [human-approved]",
                        )
                        self.db.update_task(task_id, status=TaskState.ACCEPTED.value)
                        await self._emit("sol_link.ACCEPTED", {
                            "task_id": task_id, "integration_head": integration_head,
                            "human_approved": True,
                        }, task_id=task_id)
                        await asyncio.to_thread(self.workspace.remove_worker_worktree, tree, True)
                        return
                self.db.update_task(task_id, status=TaskState.REJECTED.value)
                await self._emit("task.rejected", latest_review.model_dump(mode="json"), task_id=task_id)
                return

        self.db.update_task(task_id, status=TaskState.BLOCKED.value,
                            review_json=(latest_review.model_dump(mode="json") if latest_review else {}))
        await self._emit("task.revision_exhausted", {
            "task_id": task_id,
            "review": latest_review.model_dump(mode="json") if latest_review else {},
        }, task_id=task_id)

    def _normalize_packet(self, packet: TaskPacket) -> TaskPacket:
        update: dict[str, Any] = {}
        if packet.worker == "spark":
            too_broad = (
                packet.risk != RiskLevel.LOW
                or (packet.max_files is not None and packet.max_files > 3)
                or len(packet.allowed_paths) > 4
            )
            if too_broad:
                update["worker"] = "luna"
        if packet.worker == "spark" and packet.max_files is None:
            update["max_files"] = 3
        if not packet.stop_conditions:
            update["stop_conditions"] = [
                "A public contract change becomes necessary",
                "The task requires editing outside allowed_paths",
                "A destructive migration or credential access becomes necessary",
            ]
        return packet.model_copy(update=update) if update else packet

    def _unique_task_id(self, suggested: str) -> str:
        self._decision_counter += 1
        base = suggested.strip() or f"NS-{self._decision_counter:04d}"
        base = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-")[:80] or f"NS-{self._decision_counter:04d}"
        candidate = base
        index = 2
        while self.db.query("SELECT id FROM tasks WHERE id=?", (candidate,)):
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def _fallback_worker(self, current: str, packet: TaskPacket) -> str | None:
        if current == "spark" and self.settings.agent("luna").enabled:
            return "luna"
        if current == "luna" and packet.risk == RiskLevel.LOW and (packet.max_files or 3) <= 3 \
                and self.settings.agent("spark").enabled:
            return "spark"
        return None

    def _needs_human_gate(self, risk: RiskLevel) -> bool:
        if risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return self.settings.orchestrator.require_human_for_high_risk
        if risk == RiskLevel.MEDIUM:
            return not self.settings.orchestrator.auto_accept_medium_risk
        return not self.settings.orchestrator.auto_accept_low_risk

    async def _wait_for_human(self, task_id: str, packet: TaskPacket,
                              review: ReviewDecision, risk: RiskLevel) -> bool:
        self.db.update_task(task_id, status=TaskState.AWAITING_HUMAN.value)
        self.db.update_mission(self.mission_id, status=MissionState.AWAITING_HUMAN.value)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._approval_futures[task_id] = future
        await self._emit("task.awaiting_human", {
            "task": packet.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
            "risk": risk.value,
        }, task_id=task_id)
        decision = await future
        self._approval_futures.pop(task_id, None)
        self.db.update_mission(self.mission_id, status=MissionState.RUNNING.value)
        return bool(decision.get("approved"))

    def _record_usage(self, key: str, task_id: str, result: AgentResult) -> None:
        self.db.add_usage(
            self.settings.agent(key).id, task_id,
            result.usage.model_dump(),
        )

    def _mission_digest(self) -> str:
        rows = self.db.query("SELECT * FROM missions WHERE id=?", (self.mission_id,))
        tasks = self.db.query(
            "SELECT id,title,worker,status,risk,base_sha,worker_head,review_json,result_json FROM tasks WHERE mission_id=? ORDER BY created_at",
            (self.mission_id,),
        )
        lines = []
        if rows:
            mission = rows[0]
            lines += [
                f"Mission {mission['id']} status={mission['status']}",
                f"Goal: {mission['goal']}",
                f"Base: {mission['base_sha']}",
                f"Integration branch: {mission['integration_branch']}",
                f"Integration path: {mission['integration_path']}",
                f"Recovery dossier: {mission['forensics_path']}",
            ]
        if self.workspace:
            try:
                lines.append(f"Current integration HEAD: {self.workspace.integration_head()}")
                lines.append("Integration status:\n" + git(self.workspace.integration_path, "status", "--short", "--branch").strip())
            except GitError as exc:
                lines.append(f"Integration inspection error: {exc}")
        lines.append("\nTasks:")
        for task in tasks:
            review = ""
            try:
                review_obj = json.loads(task.get("review_json") or "{}")
                review = compact_text(str(review_obj.get("summary", "")), 500)
            except json.JSONDecodeError:
                pass
            lines.append(
                f"- {task['id']} [{task['status']}] worker={task['worker']} risk={task['risk']} "
                f"base={task['base_sha'][:10]} head={task['worker_head'][:10]} title={task['title']} review={review}"
            )
        return "\n".join(lines)

    async def _fail_mission(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        if self.mission_id:
            self.db.update_mission(self.mission_id, status=MissionState.FAILED.value,
                                   summary=message)
        await self._emit("mission.failed", {"error": message})

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            value = value.strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result
