from __future__ import annotations

import asyncio
import json
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .adapters import CodexAdapter, GrokAdapter
from .config import Settings
from .db import StateDB
from .events import EventHub
from .forensics import ForensicsScanner
from .git import (
    GitError,
    MissionWorkspace,
    assess_risk,
    git,
    is_git_repo,
    path_violations,
)
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
from .profiles import (
    PROFILE_IDS,
    get_profile,
    profile_catalog,
    profile_prompt_context,
    profile_public_dict,
    resolve_profile_agents,
)
from .prompts import (
    architect_bootstrap_prompt,
    architect_next_prompt,
    architect_review_prompt,
    chat_prompt,
    final_audit_prompt,
    load_directive,
    mission_resume_prompt,
    recovery_handoff_prompt,
    worker_prompt,
)
from .protocol import compact_text, extract_json_dict
from .quota import (
    codex_effort_options,
    normalize_codex_quota,
    normalize_grok_quota,
    read_codex_account,
    read_configured_quota,
    read_grok_billing,
)
from .redaction import redact, redact_value
from .validation import run_validation

_BUSY_AGENT_STATES = {
    AgentState.RECOVERING.value,
    AgentState.PLANNING.value,
    AgentState.WORKING.value,
    AgentState.REVIEWING.value,
    AgentState.WAITING.value,
}
_INTERRUPTED_MISSION_STATES = {
    MissionState.CREATED.value,
    MissionState.RECOVERING.value,
    MissionState.RUNNING.value,
    MissionState.PAUSED.value,
    MissionState.AWAITING_HUMAN.value,
}
_OPEN_TASK_STATES = {
    TaskState.PLANNED.value,
    TaskState.READY.value,
    TaskState.IMPLEMENTING.value,
    TaskState.VALIDATING.value,
    TaskState.REVIEWING.value,
    TaskState.REVISION.value,
    TaskState.AWAITING_HUMAN.value,
}
_RESUMABLE_MISSIONS = {
    MissionState.PAUSED.value,
    MissionState.BLOCKED.value,
    MissionState.FAILED.value,
}
_UNBOUNDED_SCOPE_PATTERNS = {"*", "**", "**/*", ".", "./"}


class OrchestratorError(RuntimeError):
    pass


class NightshiftOrchestrator:
    def __init__(self, settings: Settings) -> None:
        # Resolving a profile rewrites the logical agent map. Keep the caller's
        # Settings object immutable so multiple app/test instances cannot leak
        # an active profile into one another.
        self.settings = deepcopy(settings)
        self.runtime = self.settings.orchestrator.runtime_path
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.db = StateDB(self.runtime / "nightshift.sqlite3")
        self._agent_templates = deepcopy(self.settings.agents)
        saved_profile = self.db.get_preference(
            "profile.active", self.settings.profiles.default
        )
        if not isinstance(saved_profile, str) or saved_profile not in PROFILE_IDS:
            saved_profile = self.settings.profiles.default
        if saved_profile not in PROFILE_IDS:
            saved_profile = "reserve"
        self.profile_id = saved_profile
        self.combat_grok_enabled = bool(
            self.db.get_preference(
                "profile.combat.grok_enabled",
                self.settings.profiles.combat_grok_enabled,
            )
        )
        self.profile = get_profile(self.profile_id)
        self._apply_profile_configuration()
        self._load_preferences()
        self.hub = EventHub()
        self.runner = ProcessRunner()
        self.adapters = self._build_adapters()
        self.agent_locks = {key: asyncio.Lock() for key in self.adapters}
        self._profile_lock = asyncio.Lock()
        self._doctor_lock = asyncio.Lock()
        self._quota_lock = asyncio.Lock()
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
        # The logical `grok` slot is always the active architect. In combat it is
        # backed by the normal codex/Sol profile rather than the Grok CLI.
        self._grok_session_id: str | None = None
        self._grok_turns = 0
        self._chat_session_ids: dict[tuple[str, str], str | None] = {}
        self._decision_counter = 0
        self._last_dossier: dict[str, Any] = {}
        self._init_agents()
        self._mark_interrupted_missions_paused()

    def _apply_profile_configuration(self) -> None:
        self.profile = get_profile(self.profile_id)
        self.settings.agents = resolve_profile_agents(
            self._agent_templates,
            self.settings.profiles,
            self.profile_id,
            self.combat_grok_enabled,
        )

    def _build_adapters(
        self,
        agents: dict[str, Any] | None = None,
    ) -> dict[str, CodexAdapter | GrokAdapter]:
        adapters: dict[str, CodexAdapter | GrokAdapter] = {}
        for key, config in (agents or self.settings.agents).items():
            if config.adapter == "codex":
                adapters[key] = CodexAdapter(config, self.runner)
            elif config.adapter == "grok":
                adapters[key] = GrokAdapter(config, self.runner)
            else:
                raise OrchestratorError(
                    f"Unsupported adapter {config.adapter!r} for logical lane {key}"
                )
        return adapters

    def _profile_options(self) -> dict[str, Any]:
        return {"combat_grok_enabled": self.combat_grok_enabled}

    def _profile_public(self) -> dict[str, Any]:
        return profile_public_dict(
            self.profile,
            self.settings.agents,
            self.combat_grok_enabled,
        )

    def _profile_context(self) -> str:
        return profile_prompt_context(
            self.profile,
            self.settings.agents,
            repository=str(self.settings.project.repo_path),
            operational_roots=self.settings.project.operational_roots,
        )

    def _requested_combat_grok(self, value: bool | None) -> bool:
        return self.combat_grok_enabled if value is None else bool(value)

    async def _acquire_all_agent_locks(
        self,
        *,
        timeout: float = 0.1,
    ) -> list[asyncio.Lock]:
        acquired: list[asyncio.Lock] = []
        try:
            for key in sorted(self.agent_locks):
                lock = self.agent_locks[key]
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
                acquired.append(lock)
        except TimeoutError as exc:
            for lock in reversed(acquired):
                lock.release()
            raise OrchestratorError(
                "The operation cannot start while an agent turn is active"
            ) from exc
        return acquired

    @staticmethod
    def _release_agent_locks(locks: list[asyncio.Lock]) -> None:
        for lock in reversed(locks):
            lock.release()

    async def _activate_profile_locked(
        self,
        profile_id: str,
        combat_grok_enabled: bool,
        *,
        persist: bool,
    ) -> dict[str, Any]:
        """Activate a profile while the caller owns ``_profile_lock``."""
        async with self._doctor_lock, self._quota_lock:
            acquired = await self._acquire_all_agent_locks()
            try:
                # Resolve and construct the replacement wiring before mutating the
                # active state. A bad profile cannot leave a half-switched control room.
                new_agents = resolve_profile_agents(
                    self._agent_templates,
                    self.settings.profiles,
                    profile_id,
                    combat_grok_enabled,
                )
                new_adapters = self._build_adapters(new_agents)
                self.profile_id = profile_id
                self.combat_grok_enabled = combat_grok_enabled
                self.profile = get_profile(profile_id)
                self.settings.agents = new_agents
                self._load_preferences()
                self.adapters = new_adapters
                self.quota_cache.clear()
                self.codex_homes.clear()
                self.workspace = None
                self.mission_id = ""
                self.mission_dir = None
                self._grok_session_id = None
                self._grok_turns = 0
                self._chat_session_ids.clear()
                self._init_agents()
                if persist:
                    self.db.set_preference("profile.active", self.profile_id)
                    self.db.set_preference(
                        "profile.combat.grok_enabled",
                        self.combat_grok_enabled,
                    )
                return self._profile_public()
            finally:
                self._release_agent_locks(acquired)

    async def set_profile(
        self,
        profile_id: str,
        combat_grok_enabled: bool | None = None,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        if profile_id not in PROFILE_IDS:
            raise OrchestratorError(f"Unknown operating profile: {profile_id}")
        requested_grok = self._requested_combat_grok(combat_grok_enabled)
        if self._mission_task_running():
            raise OrchestratorError(
                "The operating profile cannot change while a mission is running or paused"
            )
        async with self._profile_lock:
            if (
                profile_id == self.profile_id
                and requested_grok == self.combat_grok_enabled
            ):
                return self._profile_public()
            if self._mission_task_running():
                raise OrchestratorError(
                    "The operating profile cannot change while a mission is running or paused"
                )
            payload = await self._activate_profile_locked(
                profile_id,
                requested_grok,
                persist=persist,
            )
            await self._emit("profile.changed", payload, sender="human")
            return payload

    def _resolve_chat_recipient(self, recipient: str) -> str:
        value = recipient.strip().casefold()
        if not value or value == "architect":
            return self.profile.architect_key
        # Explicit slot/key aliases always address the stable logical contract.
        for prefix in ("slot:", "key:"):
            if value.startswith(prefix):
                key = value.removeprefix(prefix)
                if key in self.settings.agents:
                    return key
                raise OrchestratorError(f"Unknown chat recipient: {recipient}")
        # Public API and browser controls use stable logical lane keys. Resolve
        # those before physical aliases, because combat maps physical Codex/Sol
        # onto logical `grok` and physical Grok onto logical `spark`.
        logical_keys = {key.casefold(): key for key in self.settings.agents}
        if value in logical_keys:
            return logical_keys[value]
        for key, config in self.settings.agents.items():
            candidates = {
                config.id.casefold(),
                config.display_name.casefold(),
                config.physical_key.casefold(),
            }
            candidates.update(item.casefold() for item in config.binary_candidates)
            if value in candidates:
                return key
        aliases = {
            "primary": self.profile.primary_worker_key,
            "goodman": "luna",
            "solgoodman": "luna",
            "sol": self.profile.architect_key,
            "helper": "spark",
            "assistant": "spark",
            "grok-helper": "spark",
        }
        key = aliases.get(value)
        if key in self.settings.agents:
            return key
        raise OrchestratorError(f"Unknown chat recipient: {recipient}")

    def _operator_note_mission_id(self) -> str:
        """Bind steering to a live/resumable mission, otherwise keep it global."""
        row = self._mission_row()
        if row and row.get("status") not in {
            MissionState.COMPLETED.value,
            MissionState.STOPPED.value,
        }:
            return self.mission_id
        return ""

    async def _expire_mission_nudges(self, reason: str) -> None:
        """Mark mission-bound steering as expired when no future turn can consume it."""
        seqs = self.db.expire_mission_nudges(self.profile_id, self.mission_id)
        if not seqs:
            return
        await self._emit(
            "chat.nudges_expired",
            {
                "profile": self.profile_id,
                "mission_id": self.mission_id,
                "seqs": seqs,
                "reason": reason,
            },
            sender="nightshift",
            recipient="human",
        )

    async def _queue_operator_nudge(self, key: str, text: str) -> dict[str, Any]:
        config = self.settings.agent(key)
        note_mission_id = self._operator_note_mission_id()
        seq = self.db.add_chat(
            "user",
            text,
            agent_key=key,
            agent_id=config.id,
            profile=self.profile_id,
            mission_id=note_mission_id,
            kind="nudge",
            status="queued",
        )
        payload = {
            "status": "queued",
            "recipient": key,
            "agent_id": config.id,
            "display_name": config.display_name,
            "seq": seq,
            "mission_id": note_mission_id,
            "message": "Queued for the participant's next model turn",
        }
        await self._emit(
            "chat.queued",
            payload,
            sender="human",
            recipient=config.id,
        )
        return payload

    def _prepare_operator_notes(self, key: str, prompt: str) -> tuple[str, list[int]]:
        """Attach queued steering without acknowledging it before the model returns.

        A process can fail after stdin has been prepared but before the provider accepts
        the turn. Keeping rows queued until provider-side evidence appears gives the
        operator at-least-once delivery instead of silently dropping an intervention.
        """
        notes = self.db.pending_nudges(self.profile_id, key, self.mission_id)
        if not notes:
            return prompt, []
        seqs = [int(note["seq"]) for note in notes]
        block = "\n\n# Queued human steering notes for this participant\n" + "\n".join(
            f"- note {note['seq']}: {note['text']}" for note in notes
        )
        return prompt + block, seqs

    async def _settle_operator_notes(
        self,
        key: str,
        seqs: list[int],
        result: AgentResult,
    ) -> None:
        if not seqs:
            return
        config = self.settings.agent(key)
        final_text = result.final_text.strip()
        delivered = result.ok or result.raw_events > 0 or bool(final_text)
        if delivered:
            if result.ok:
                evidence = "successful result"
            elif final_text:
                evidence = "provider final response"
            else:
                evidence = "provider event stream"
            self.db.mark_chat_delivered(seqs)
            await self._emit(
                "chat.nudges_delivered",
                {"recipient": key, "seqs": seqs, "evidence": evidence},
                sender="human",
                recipient=config.id,
            )
            return
        await self._emit(
            "chat.nudges_deferred",
            {
                "recipient": key,
                "seqs": seqs,
                "limit_detected": result.limit_detected,
                "error": result.error,
            },
            sender="nightshift",
            recipient=config.id,
        )

    def _load_preferences(self) -> None:
        for key, config in self.settings.agents.items():
            legacy = self.db.get_preference(f"agent.{key}.effort", "")
            saved = self.db.get_preference(
                f"profile.{self.profile_id}.agent.{key}.effort",
                legacy if self.profile_id == "reserve" else "",
            )
            if isinstance(saved, str) and saved in config.effort_options:
                config.effort = saved

    def _init_agents(self) -> None:
        for key, config in self.settings.agents.items():
            binary = config.resolve_binary()
            self.db.upsert_agent(
                config.id, config.role, AgentState.OFFLINE.value,
                model=config.model, binary=binary,
                metadata={
                    "key": key,
                    "profile": self.profile_id,
                    "display_name": config.display_name,
                    "lane": config.lane,
                    "physical_key": config.physical_key,
                    "adapter": config.adapter,
                    "optional": config.optional,
                    "effort": config.effort,
                },
            )

    def _mark_interrupted_missions_paused(self) -> None:
        placeholders = ",".join("?" for _ in _INTERRUPTED_MISSION_STATES)
        rows = self.db.query(
            f"SELECT id,status FROM missions WHERE status IN ({placeholders})",
            tuple(_INTERRUPTED_MISSION_STATES),
        )
        for row in rows:
            self._block_open_tasks(
                row["id"],
                "Nightshift process restarted before this task reached a terminal state",
            )
            self.db.update_mission(
                row["id"],
                status=MissionState.PAUSED.value,
                summary="Nightshift process restarted; mission state preserved",
            )

    def _mission_task_running(self) -> bool:
        return bool(self._mission_task and not self._mission_task.done())

    def _mission_row(self, mission_id: str | None = None) -> dict[str, Any] | None:
        selected = mission_id if mission_id is not None else self.mission_id
        if not selected:
            return None
        rows = self.db.query("SELECT * FROM missions WHERE id=?", (selected,))
        return rows[0] if rows else None

    def _require_running_mission(self, action: str) -> dict[str, Any]:
        row = self._mission_row()
        if row is None or not self._mission_task_running():
            raise OrchestratorError(f"No running mission to {action}")
        return row

    def _block_open_tasks(self, mission_id: str, reason: str) -> None:
        placeholders = ",".join("?" for _ in _OPEN_TASK_STATES)
        rows = self.db.query(
            f"SELECT id,result_json FROM tasks WHERE mission_id=? AND status IN ({placeholders})",
            (mission_id, *tuple(_OPEN_TASK_STATES)),
        )
        for row in rows:
            try:
                result = json.loads(row.get("result_json") or "{}")
            except json.JSONDecodeError:
                result = {}
            result["interruption"] = reason
            self.db.update_task(row["id"], status=TaskState.BLOCKED.value, result_json=result)

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
        safe_payload = redact_value(payload)
        if not isinstance(safe_payload, dict):
            safe_payload = {"value": safe_payload}
        seq = self.db.add_event(
            sender, recipient, event_type, safe_payload,
            mission_id=mission, task_id=task_id,
        )
        await self.hub.publish({
            "seq": seq, "type": event_type, "payload": safe_payload,
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
            {
                "key": key,
                "profile": self.profile_id,
                "agent_id": config.id,
                "display_name": config.display_name,
                "state": state.value,
                "current_task": current_task,
                "error": error,
                "session_id": session_id,
            },
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
                f"agent.{kind}",
                {"key": key, "profile": self.profile_id, **payload},
                sender=config.id, task_id=task_id,
            )
        return callback

    async def doctor(self) -> dict[str, Any]:
        repo = self.settings.project.repo_path
        cwd = repo if repo.exists() else Path.cwd()
        async with self._doctor_lock:
            results: dict[str, Any] = {}
            for key, adapter in self.adapters.items():
                config = self.settings.agent(key)
                if not config.enabled:
                    probe: dict[str, Any] = {
                        "enabled": False,
                        "installed": bool(config.resolve_binary()),
                        "ready": False,
                        "error": "agent disabled by configuration",
                    }
                else:
                    try:
                        async with self.agent_locks[key]:
                            probe = await adapter.probe(cwd)
                    except Exception as exc:
                        probe = {
                            "enabled": True,
                            "installed": bool(config.resolve_binary()),
                            "ready": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                probe.update(
                    {
                        "enabled": config.enabled,
                        "profile": self.profile_id,
                        "display_name": config.display_name,
                        "role": config.role,
                        "lane": config.lane,
                        "physical_key": config.physical_key,
                        "adapter": config.adapter,
                    }
                )
                safe_probe = redact_value(probe)
                assert isinstance(safe_probe, dict)
                results[key] = safe_probe
                existing_rows = self.db.query("SELECT * FROM agents WHERE id=?", (config.id,))
                existing = existing_rows[0] if existing_rows else {}
                preserve_busy = (
                    existing.get("state") in _BUSY_AGENT_STATES
                    and bool(existing.get("current_task"))
                )
                state = (
                    str(existing.get("state"))
                    if preserve_busy
                    else (AgentState.IDLE.value if safe_probe.get("ready") else AgentState.OFFLINE.value)
                )
                current_task = str(existing.get("current_task") or "") if preserve_busy else ""
                last_error = str(existing.get("last_error") or "") if preserve_busy else str(
                    safe_probe.get("error") or ""
                )
                self.db.upsert_agent(
                    config.id, config.role, state,
                    model=config.model,
                    binary=str(safe_probe.get("binary") or adapter.binary or ""),
                    current_task=current_task,
                    last_error=last_error,
                    metadata={
                        "key": key,
                        "profile": self.profile_id,
                        "display_name": config.display_name,
                        "lane": config.lane,
                        "physical_key": config.physical_key,
                        "adapter": config.adapter,
                        "optional": config.optional,
                        "effort": config.effort,
                        "probe": safe_probe,
                    },
                )
                await self._emit("agent.probe", {"key": key, **safe_probe}, sender=config.id)
            return results

    async def refresh_quotas(self) -> dict[str, Any]:
        repo = self.settings.project.repo_path
        cwd = repo if repo.exists() else Path.cwd()
        async with self._quota_lock:
            snapshots: dict[str, Any] = {}
            for key, config in self.settings.agents.items():
                matched_model = ""
                if not config.enabled:
                    data: dict[str, Any] = {
                        "agent_id": config.id,
                        "available": False,
                        "windows": [],
                        "message": "Agent disabled by the active profile",
                        "fetched_at": utc_now(),
                    }
                    quota_source = "disabled"
                elif config.adapter == "codex":
                    try:
                        async with self.agent_locks[key]:
                            payload = await read_codex_account(
                                config.resolve_binary(),
                                cwd,
                                env=config.subprocess_env(),
                            )
                        snapshot = normalize_codex_quota(config.id, payload)
                        home = str(payload.get("codex_home") or "")
                        if home:
                            self.codex_homes[key] = home
                        prefer_luna = (
                            self.profile_id == "reserve"
                            and config.physical_key == "luna"
                        )
                        if config.model or prefer_luna:
                            options, matched_model = codex_effort_options(
                                payload,
                                config.model,
                                prefer_luna=prefer_luna,
                            )
                        else:
                            # Combat Codex wrappers own their Sol model selection.
                            # Do not label them with an arbitrary catalog default.
                            options, matched_model = [], ""
                        if options:
                            if config.effort and config.effort not in options:
                                options.append(config.effort)
                            config.effort_options = options
                        data = snapshot.model_dump()
                        quota_source = "codex app-server"
                    except Exception as exc:
                        data = {
                            "agent_id": config.id,
                            "available": False,
                            "windows": [],
                            "message": f"{type(exc).__name__}: {exc}",
                            "fetched_at": utc_now(),
                        }
                        quota_source = "codex error"
                elif config.adapter == "grok":
                    try:
                        async with self.agent_locks[key]:
                            payload = await read_grok_billing(
                                config.resolve_binary(),
                                cwd,
                                env=config.subprocess_env(),
                            )
                        grok_snapshot = normalize_grok_quota(config.id, payload)
                    except Exception as acp_exc:
                        async with self.agent_locks[key]:
                            grok_snapshot = await read_configured_quota(
                                config,
                                self.runner,
                                cwd,
                            )
                        if not grok_snapshot.available:
                            grok_snapshot.message = (
                                f"Grok ACP billing unavailable: {acp_exc}. "
                                + grok_snapshot.message
                            )
                    data = grok_snapshot.model_dump()
                    quota_source = str(data.get("raw", {}).get("source", "grok fallback"))
                else:
                    data = {
                        "agent_id": config.id,
                        "available": False,
                        "windows": [],
                        "message": f"Unsupported adapter: {config.adapter}",
                        "fetched_at": utc_now(),
                    }
                    quota_source = "unsupported"

                safe_data = redact_value(data)
                assert isinstance(safe_data, dict)
                snapshots[key] = safe_data
                self.quota_cache[key] = safe_data
                self.db.update_agent(
                    config.id,
                    model=config.model or matched_model,
                    metadata_json={
                        "key": key,
                        "profile": self.profile_id,
                        "display_name": config.display_name,
                        "lane": config.lane,
                        "physical_key": config.physical_key,
                        "adapter": config.adapter,
                        "optional": config.optional,
                        "effort": config.effort,
                        "effort_options": config.effort_options,
                        "resolved_model": matched_model,
                        "quota_source": quota_source,
                    },
                )
                await self._emit(
                    "agent.quota",
                    {"key": key, "snapshot": safe_data},
                    sender=config.id,
                )
            return snapshots

    async def set_reasoning(self, key: str, effort: str) -> dict[str, Any]:
        async with self._profile_lock:
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
            self.db.set_preference(
                f"profile.{self.profile_id}.agent.{key}.effort", effort
            )
            if self.profile_id == "reserve":
                self.db.set_preference(f"agent.{key}.effort", effort)
            self.db.update_agent(
                config.id,
                model=config.model,
                metadata_json={
                    "key": key,
                    "profile": self.profile_id,
                    "display_name": config.display_name,
                    "lane": config.lane,
                    "physical_key": config.physical_key,
                    "adapter": config.adapter,
                    "optional": config.optional,
                    "effort": effort,
                    "effort_options": config.effort_options,
                    "applies": "next model turn",
                },
            )
            payload = {
                "key": key,
                "profile": self.profile_id,
                "agent_id": config.id,
                "display_name": config.display_name,
                "effort": effort,
                "effort_options": config.effort_options,
                "applies": "next model turn",
            }
            await self._emit("agent.reasoning_changed", payload, sender="human")
            return payload

    def snapshot(self) -> dict[str, Any]:
        data = self.db.snapshot(log_tail=self.settings.orchestrator.log_tail_lines)
        # Agent ids differ between profiles. Do not make the browser reconcile
        # stale rows from a previously active wiring with the current cards.
        active_agent_ids = {config.id for config in self.settings.agents.values()}
        data["agents"] = [
            row for row in data["agents"] if row.get("id") in active_agent_ids
        ]
        data["usage"] = [
            row for row in data["usage"] if row.get("agent_id") in active_agent_ids
        ]
        data["logs"] = {
            agent_id: rows
            for agent_id, rows in data["logs"].items()
            if agent_id in active_agent_ids
        }
        data["quotas"] = self.quota_cache
        data["config"] = self.settings.public_dict()
        data["profile"] = self._profile_public()
        data["profiles"] = profile_catalog(
            self.settings.profiles,
            self.combat_grok_enabled,
        )
        data["active_mission_id"] = self.mission_id
        data["mission_running"] = self._mission_task_running()
        data["profile_switch_locked"] = (
            self._mission_task_running()
            or self._profile_lock.locked()
            or self._doctor_lock.locked()
            or self._quota_lock.locked()
            or any(lock.locked() for lock in self.agent_locks.values())
        )
        return data

    async def start_mission(self, goal: str) -> str:
        goal = goal.strip()
        if len(goal) < 3:
            raise OrchestratorError("Mission goal is too short")

        async with self._profile_lock:
            if self._mission_task_running():
                raise OrchestratorError("A mission is already running")
            agent_locks = await self._acquire_all_agent_locks()
            try:
                repo = self.settings.project.repo_path
                if not repo.exists():
                    raise OrchestratorError(f"Repository does not exist: {repo}")
                if not is_git_repo(repo):
                    raise OrchestratorError(f"Not a Git repository: {repo}")
                if not self.settings.agent(self.profile.architect_key).enabled:
                    raise OrchestratorError(
                        "Profile architect is disabled: "
                        f"{self.settings.agent(self.profile.architect_key).display_name}"
                    )
                if not self.settings.agent(self.profile.primary_worker_key).enabled:
                    raise OrchestratorError(
                        "Profile implementation owner is disabled: "
                        f"{self.settings.agent(self.profile.primary_worker_key).display_name}"
                    )
                stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                mission_id = f"ns-{stamp}-{uuid.uuid4().hex[:6]}"
                mission_dir = self.runtime / "missions" / mission_id
                mission_dir.mkdir(parents=True, exist_ok=False)
                directive_copy = mission_dir / self.profile.directive_name
                directive_copy.write_text(
                    load_directive(self.profile_id), encoding="utf-8"
                )
                self.db.create_mission(
                    mission_id,
                    str(repo),
                    goal,
                    MissionState.CREATED.value,
                    directive_path=str(directive_copy),
                    profile=self.profile_id,
                    profile_options=self._profile_options(),
                )
                self.mission_id = mission_id
                self.mission_dir = mission_dir
                self.workspace = None
                self._stop_requested.clear()
                self._pause_gate.set()
                for future in list(self._approval_futures.values()):
                    if not future.done():
                        future.cancel()
                self._approval_futures.clear()
                self._grok_session_id = None
                self.db.update_agent(
                    self.settings.agent("grok").id, session_id=""
                )
                self._grok_turns = 0
                self._decision_counter = 0
                await self._emit(
                    "mission.created",
                    {
                        "mission_id": mission_id,
                        "goal": goal,
                        "profile": self.profile_id,
                    },
                )
                self._mission_task = asyncio.create_task(
                    self._run_new_mission(mission_id, goal)
                )
                return mission_id
            finally:
                self._release_agent_locks(agent_locks)

    async def resume_interrupted(self, mission_id: str) -> None:
        profile_payload: dict[str, Any] | None = None
        async with self._profile_lock:
            if self._mission_task_running():
                raise OrchestratorError("A mission is already running")
            rows = self.db.query("SELECT * FROM missions WHERE id=?", (mission_id,))
            if not rows:
                raise OrchestratorError("Mission not found")
            row = rows[0]
            if row["status"] not in _RESUMABLE_MISSIONS:
                raise OrchestratorError(
                    f"Mission in state {row['status']!r} cannot be resumed"
                )

            stored_profile = str(row.get("profile") or "reserve")
            if stored_profile not in PROFILE_IDS:
                raise OrchestratorError(
                    f"Mission has unknown operating profile: {stored_profile}"
                )
            try:
                stored_options = json.loads(row.get("profile_options_json") or "{}")
            except json.JSONDecodeError:
                stored_options = {}
            stored_grok = bool(
                stored_options.get(
                    "combat_grok_enabled",
                    self.combat_grok_enabled,
                )
            )

            # Validate every durable path before changing the active profile. A
            # broken old mission must not rewire the live control room as a side effect.
            try:
                stored_repo = Path(row["repo"]).expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                raise OrchestratorError(
                    f"Mission repository path is invalid: {exc}"
                ) from exc
            if stored_repo != self.settings.project.repo_path:
                raise OrchestratorError(
                    "Mission belongs to a different configured repository"
                )
            raw_integration_path = str(row.get("integration_path") or "").strip()
            if not raw_integration_path:
                raise OrchestratorError(
                    "Mission has no preserved integration worktree"
                )
            integration_path = Path(raw_integration_path).expanduser().resolve()
            if not integration_path.is_dir() or not is_git_repo(integration_path):
                raise OrchestratorError(
                    "Mission integration worktree no longer exists or is invalid"
                )
            expected_branch = str(row.get("integration_branch") or "").strip()
            actual_branch = git(
                integration_path, "rev-parse", "--abbrev-ref", "HEAD"
            ).strip()
            if expected_branch and actual_branch != expected_branch:
                raise OrchestratorError(
                    f"Integration worktree is on {actual_branch!r}, "
                    f"expected {expected_branch!r}"
                )
            mission_dir = self.runtime / "missions" / mission_id
            if not mission_dir.is_dir():
                raise OrchestratorError(
                    "Mission runtime directory no longer exists"
                )
            candidate_workspace = MissionWorkspace.reopen(
                self.settings,
                mission_id,
                mission_dir,
                integration_path,
                row["integration_branch"],
                row["base_sha"],
            )

            profile_changed = (
                stored_profile != self.profile_id
                or stored_grok != self.combat_grok_enabled
            )
            if profile_changed:
                profile_payload = await self._activate_profile_locked(
                    stored_profile,
                    stored_grok,
                    persist=True,
                )
            else:
                agent_locks = await self._acquire_all_agent_locks()
                self._release_agent_locks(agent_locks)

            self.mission_id = mission_id
            self.mission_dir = mission_dir
            self.workspace = candidate_workspace
            self._stop_requested.clear()
            self._pause_gate.set()
            self._approval_futures.clear()
            mission_session = str(row.get("architect_session_id") or "").strip()
            self._grok_session_id = mission_session or None
            self._grok_turns = max(0, int(row.get("architect_turns") or 0))
            self._decision_counter = int(
                self.db.query(
                    "SELECT COUNT(*) AS count FROM tasks WHERE mission_id=?",
                    (mission_id,),
                )[0]["count"]
            )
            if profile_payload is not None:
                await self._emit(
                    "profile.changed", profile_payload, sender="nightshift"
                )
            await self._emit(
                "mission.resumed",
                {"mission_id": mission_id, "profile": self.profile_id},
            )
            self._mission_task = asyncio.create_task(
                self._run_resumed_mission(row)
            )

    async def pause(self) -> None:
        row = self._require_running_mission("pause")
        if row["status"] == MissionState.PAUSED.value:
            return
        self._pause_gate.clear()
        self.db.update_mission(self.mission_id, status=MissionState.PAUSED.value)
        await self._emit("mission.paused", {"mission_id": self.mission_id})

    async def resume(self) -> None:
        row = self._require_running_mission("resume")
        if row["status"] != MissionState.PAUSED.value:
            raise OrchestratorError("Mission is not paused")
        self._pause_gate.set()
        if self._approval_futures:
            state = MissionState.AWAITING_HUMAN.value
            event_type = "mission.awaiting_human"
        else:
            state = MissionState.RUNNING.value
            event_type = "mission.running"
        self.db.update_mission(self.mission_id, status=state)
        await self._emit(event_type, {"mission_id": self.mission_id})

    async def stop(self) -> None:
        self._require_running_mission("stop")
        self._stop_requested.set()
        self._pause_gate.set()
        for future in list(self._approval_futures.values()):
            if not future.done():
                future.cancel()
        self._approval_futures.clear()
        task = self._mission_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._mission_task = None
        await self.runner.stop_all()
        self._block_open_tasks(self.mission_id, "Stopped by human operator")
        for key, config in self.settings.agents.items():
            rows = self.db.query("SELECT state,current_task FROM agents WHERE id=?", (config.id,))
            if rows and rows[0]["state"] in _BUSY_AGENT_STATES:
                await self._set_agent(
                    key, AgentState.STOPPED, error="Stopped by human operator"
                )
        self.db.update_mission(
            self.mission_id,
            status=MissionState.STOPPED.value,
            summary="Stopped by human operator",
        )
        await self._expire_mission_nudges("mission stopped by human operator")
        await self._emit("mission.stopped", {"mission_id": self.mission_id})

    async def approve_task(self, task_id: str, approved: bool, note: str = "") -> None:
        future = self._approval_futures.get(task_id)
        if future is None or future.done():
            raise OrchestratorError("Task is not waiting for approval")
        future.set_result({"approved": approved, "note": note})
        await self._emit("task.human_decision", {"approved": approved, "note": note},
                         sender="human", task_id=task_id)

    async def chat(
        self,
        text: str,
        recipient: str = "architect",
        delivery: str = "auto",
    ) -> dict[str, Any]:
        """Talk to any active participant or queue steering for its next work turn."""
        text = text.strip()
        if not text:
            raise OrchestratorError("Message is empty")
        if delivery not in {"auto", "chat", "nudge"}:
            raise OrchestratorError(f"Unknown chat delivery mode: {delivery}")

        acquired = False
        async with self._profile_lock:
            key = self._resolve_chat_recipient(recipient)
            config = self.settings.agent(key)
            if not config.enabled:
                raise OrchestratorError(
                    f"Participant is disabled in the active profile: {config.display_name}"
                )
            lock = self.agent_locks[key]
            if delivery == "nudge" or (delivery == "auto" and lock.locked()):
                return await self._queue_operator_nudge(key, text)
            try:
                await asyncio.wait_for(lock.acquire(), timeout=0.05)
                acquired = True
            except TimeoutError:
                if delivery == "auto":
                    return await self._queue_operator_nudge(key, text)
                raise OrchestratorError(
                    f"{config.display_name} is busy; use delivery='nudge' or 'auto'"
                ) from None

            # Capture the channel identity while profile mutation is excluded. The
            # participant lock then prevents profile switching until both sides of
            # the direct conversation have been persisted. Mission start performs
            # the same lock check, so a reply cannot leak into a newly created mission.
            chat_profile_id = self.profile_id
            chat_mission_id = self.mission_id
            chat_architect_key = self.profile.architect_key
            chat_profile_context = self._profile_context()
            chat_workspace = self.workspace

        task_label = f"chat:{chat_profile_id}:{key}"
        try:
            self.db.add_chat(
                "user",
                text,
                agent_key=key,
                agent_id=config.id,
                profile=chat_profile_id,
                mission_id=chat_mission_id,
                kind="message",
                status="sent",
            )
            await self._emit(
                "chat.message",
                {
                    "role": "user",
                    "text": text,
                    "recipient": key,
                    "profile": chat_profile_id,
                },
                sender="human",
                recipient=config.id,
                mission_id=chat_mission_id,
            )
            digest = (
                self._mission_digest()
                if chat_mission_id
                else "No active Sol Link mission."
            )
            prompt = chat_prompt(
                text,
                participant_name=config.display_name,
                participant_role=config.role,
                profile_id=chat_profile_id,
            )
            prompt += "\n\nCurrent compact mission ledger:\n" + compact_text(
                digest, 9000
            )
            prompt = chat_profile_context + "\n\n" + prompt
            # Direct lines are deliberately separate from operational steering.
            # A queued nudge remains pending for the participant's next architect,
            # review, recovery, or implementation turn and is never consumed by a
            # read-only chat.
            cwd = self.settings.project.repo_path
            if chat_workspace:
                if key == chat_architect_key:
                    cwd = await asyncio.to_thread(
                        chat_workspace.sync_architect_worktree
                    )
                else:
                    cwd = chat_workspace.integration_path
            await self._set_agent(key, AgentState.PLANNING, current_task=task_label)
            session_key = (chat_profile_id, key)
            if session_key not in self._chat_session_ids:
                saved = self.db.get_preference(
                    f"chat.session.{chat_profile_id}.{key}",
                    "",
                )
                self._chat_session_ids[session_key] = (
                    saved if isinstance(saved, str) and saved else None
                )
            try:
                result = await self.adapters[key].run(
                    prompt,
                    cwd,
                    task_label,
                    self._chat_session_ids[session_key],
                    self._callback(key, task_label),
                    read_only=True,
                )
            except asyncio.CancelledError:
                await self._set_agent(
                    key, AgentState.STOPPED, error="Operator chat cancelled"
                )
                raise
            except Exception as exc:
                result = AgentResult(
                    ok=False,
                    returncode=1,
                    error=f"{type(exc).__name__}: {exc}",
                )
            self._chat_session_ids[session_key] = (
                result.session_id or self._chat_session_ids[session_key]
            )
            if self._chat_session_ids[session_key]:
                self.db.set_preference(
                    f"chat.session.{chat_profile_id}.{key}",
                    self._chat_session_ids[session_key],
                )
            self._record_usage(key, task_label, result)
            if chat_workspace and key == chat_architect_key:
                try:
                    await asyncio.to_thread(
                        chat_workspace.sync_architect_worktree
                    )
                except Exception as exc:
                    result = result.model_copy(
                        update={
                            "ok": False,
                            "error": (
                                result.error
                                + f"\nArchitect worktree reset failed: {exc}"
                            ).strip(),
                        }
                    )
            await self._set_agent(
                key,
                AgentState.IDLE
                if result.ok
                else (
                    AgentState.LIMITED
                    if result.limit_detected
                    else AgentState.ERROR
                ),
                error=result.error,
            )

            answer = redact(
                result.final_text
                or result.error
                or f"{config.display_name} returned no text."
            )
            self.db.add_chat(
                "assistant",
                answer,
                agent_key=key,
                agent_id=config.id,
                profile=chat_profile_id,
                mission_id=chat_mission_id,
                kind="message",
                status="sent",
            )
            await self._emit(
                "chat.message",
                {
                    "role": "assistant",
                    "text": answer,
                    "recipient": key,
                    "profile": chat_profile_id,
                },
                sender=config.id,
                recipient="human",
                mission_id=chat_mission_id,
            )
            return {
                "status": "answered",
                "recipient": key,
                "agent_id": config.id,
                "display_name": config.display_name,
                "answer": answer,
            }
        finally:
            if acquired:
                lock.release()

    async def _run_new_mission(self, mission_id: str, goal: str) -> None:
        try:
            self.db.update_mission(mission_id, status=MissionState.RECOVERING.value)
            await self._emit(
                "mission.recovering",
                {"mission_id": mission_id, "profile": self.profile_id},
            )
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
            dossier = await asyncio.to_thread(
                scanner.scan,
                include_sessions=self.profile.recover_predecessors,
            )
            self._last_dossier = dossier
            self.db.update_mission(mission_id, forensics_path=dossier["markdown_path"])
            await self._emit("mission.forensics_ready", {
                "dossier": dossier["markdown_path"], "json": dossier["json_path"]
            })
            predecessor_handoffs = (
                await self._recover_predecessors(dossier)
                if self.profile.recover_predecessors
                else {}
            )
            self._append_handoffs_to_dossier(Path(dossier["markdown_path"]), predecessor_handoffs)
            self.db.update_mission(mission_id, status=MissionState.RUNNING.value)
            await self._emit("mission.running", {"mission_id": mission_id})
            assert self.workspace is not None
            architect_cwd = self.workspace.architect_path
            prompt = architect_bootstrap_prompt(
                load_directive(self.profile_id),
                Path(dossier["markdown_path"]),
                architect_cwd,
                goal,
                predecessor_handoffs,
                profile_id=self.profile_id,
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
            scanner = ForensicsScanner(
                self.settings,
                self.mission_dir / "resume-scans" / utc_now().replace(":", "-"),
                self.codex_homes,
            )
            dossier = await asyncio.to_thread(
                scanner.scan,
                include_sessions=self.profile.recover_predecessors,
            )
            self._last_dossier = dossier
            self.db.update_mission(
                self.mission_id, status=MissionState.RUNNING.value,
                summary="Resumed after process interruption",
                forensics_path=dossier["markdown_path"],
            )
            digest = self._mission_digest()
            cwd = self.workspace.architect_path
            try:
                dossier_excerpt = compact_text(
                    Path(dossier["markdown_path"]).read_text(
                        encoding="utf-8", errors="replace"
                    ),
                    50_000,
                )
            except OSError as exc:
                dossier_excerpt = f"<dossier read failed: {exc}>"
            prompt = mission_resume_prompt(
                load_directive(self.profile_id),
                dossier_excerpt,
                digest,
                cwd,
                profile_id=self.profile_id,
            )
            decision = await self._ask_architect_decision(prompt, phase="resume-recovery")
            await self._decision_loop(decision)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_mission(exc)

    async def _decision_loop(self, decision: Any) -> None:
        completed_count = int(
            self.db.query(
                "SELECT COUNT(*) AS count FROM tasks WHERE mission_id=?",
                (self.mission_id,),
            )[0]["count"]
        )
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
                cwd = self.workspace.architect_path
                decision = await self._ask_architect_decision(
                    architect_next_prompt(
                        digest, cwd, profile_id=self.profile_id
                    ),
                    phase="next",
                )
                continue
            if isinstance(decision, ArchitectDone):
                if final_audit_attempted or not self.settings.orchestrator.continue_until_backlog_done:
                    self.db.update_mission(
                        self.mission_id, status=MissionState.COMPLETED.value,
                        summary=decision.summary,
                    )
                    await self._expire_mission_nudges("mission completed")
                    await self._emit("mission.completed", decision.model_dump())
                    return
                final_audit_attempted = True
                digest = self._mission_digest()
                assert self.workspace is not None
                cwd = self.workspace.architect_path
                decision = await self._ask_architect_decision(
                    final_audit_prompt(
                        digest, cwd, profile_id=self.profile_id
                    ),
                    phase="final-audit",
                )
                continue
            if isinstance(decision, ArchitectBlocked):
                state = MissionState.BLOCKED.value
                self.db.update_mission(self.mission_id, status=state, summary=decision.summary)
                await self._emit("mission.blocked", decision.model_dump())
                return
            raise OrchestratorError(f"Unsupported architect decision: {type(decision).__name__}")

    async def _recover_predecessors(self, dossier: dict[str, Any]) -> dict[str, str]:
        if (
            not self.profile.recover_predecessors
            or not self.settings.orchestrator.recover_predecessor_sessions
        ):
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
            await self._set_agent(
                key, AgentState.RECOVERING, current_task="predecessor-handoff"
            )
            async with self.agent_locks[key]:
                prompt, note_seqs = self._prepare_operator_notes(
                    key,
                    self._profile_context()
                    + "\n\n"
                    + recovery_handoff_prompt(
                        predecessor, self.settings.project.repo_path
                    ),
                )
                try:
                    result = await self.adapters[key].run(
                        prompt,
                        self.workspace.integration_path
                        if self.workspace
                        else self.settings.project.repo_path,
                        f"recovery-{key}",
                        session_id,
                        self._callback(key, f"recovery-{key}"),
                        read_only=True,
                    )
                except asyncio.CancelledError:
                    await self._set_agent(
                        key,
                        AgentState.STOPPED,
                        session_id=session_id,
                        error="Predecessor recovery cancelled",
                    )
                    raise
                except Exception as exc:
                    result = AgentResult(
                        ok=False,
                        returncode=1,
                        session_id=session_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                await self._settle_operator_notes(key, note_seqs, result)
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
            await self._set_agent(
                key,
                AgentState.IDLE if result.ok else (
                    AgentState.LIMITED if result.limit_detected else AgentState.ERROR
                ),
                session_id=result.session_id or session_id,
                error=result.error,
            )
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
        async with self.agent_locks["grok"]:
            if self._grok_turns >= self.settings.orchestrator.architect_session_max_turns:
                self._grok_session_id = None
                self._grok_turns = 0
                if self.mission_id:
                    self.db.update_mission(
                        self.mission_id,
                        architect_session_id="",
                        architect_turns=0,
                    )
                prompt = (
                    "This is a rotated architect session. Reconstruct continuity from "
                    "the repository, durable Sol Link ledger, and the prompt below. "
                    "The active profile directive remains binding.\n\n"
                    + prompt
                )
                await self._emit("architect.session_rotated", {"phase": phase})
            if self.workspace:
                cwd = await asyncio.to_thread(self.workspace.sync_architect_worktree)
            prompt = self._profile_context() + "\n\n" + prompt
            prompt, note_seqs = self._prepare_operator_notes("grok", prompt)
            state = AgentState.REVIEWING if phase.startswith("review") else AgentState.PLANNING
            await self._set_agent("grok", state, current_task=phase)
            try:
                result = await self.adapters["grok"].run(
                    prompt, cwd, phase, self._grok_session_id,
                    self._callback("grok", phase), read_only=read_only,
                )
            except asyncio.CancelledError:
                await self._set_agent(
                    "grok",
                    AgentState.STOPPED,
                    error=f"{self.settings.agent('grok').display_name} turn cancelled",
                )
                raise
            except Exception as exc:
                result = AgentResult(
                    ok=False, returncode=1,
                    error=f"{type(exc).__name__}: {exc}",
                )
            self._grok_session_id = result.session_id or self._grok_session_id
            self._grok_turns += 1
            if self.mission_id:
                self.db.update_mission(
                    self.mission_id,
                    architect_session_id=self._grok_session_id or "",
                    architect_turns=self._grok_turns,
                )
            self._record_usage("grok", phase, result)
            if self.workspace:
                try:
                    await asyncio.to_thread(self.workspace.sync_architect_worktree)
                except Exception as exc:
                    result = result.model_copy(update={
                        "ok": False,
                        "error": (result.error + f"\nArchitect worktree reset failed: {exc}").strip(),
                    })
            await self._settle_operator_notes("grok", note_seqs, result)
            await self._set_agent(
                "grok",
                AgentState.IDLE if result.ok else (
                    AgentState.LIMITED if result.limit_detected else AgentState.ERROR
                ),
                session_id=self._grok_session_id,
                error=result.error,
            )
            return result

    async def _ask_architect_decision(self, prompt: str, phase: str) -> Any:
        assert self.workspace is not None
        cwd = self.workspace.architect_path
        result = await self._run_grok(
            prompt,
            cwd,
            phase,
            read_only=self.profile.architect_read_only,
        )
        if not result.ok:
            if result.limit_detected:
                raise OrchestratorError(
                    f"{self.settings.agent('grok').display_name} architect limit reached; "
                    "preserved state requires human intervention"
                )
            raise OrchestratorError(
                f"{self.settings.agent('grok').display_name} architect failed: "
                f"{result.error}"
            )
        try:
            decision = self._parse_architect_decision(result.final_text)
        except (ValueError, ValidationError) as first_error:
            repair_prompt = f"""Your previous response could not be parsed by Sol Link Nightshift:
{first_error}

Repeat the decision only. End with exactly one valid `<SOL_LINK_JSON>` object matching the standing directive. Do not add another JSON object."""
            repair = await self._run_grok(
                repair_prompt,
                cwd,
                f"{phase}-repair",
                read_only=self.profile.architect_read_only,
            )
            if not repair.ok:
                raise OrchestratorError(f"Architect output repair failed: {repair.error}") from first_error
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
            prompt = worker_prompt(
                packet,
                tree.base_sha,
                revision_context,
                profile_id=self.profile_id,
            )
            prompt = self._profile_context() + "\n\n" + prompt
            async with self.agent_locks[active_worker]:
                prompt, note_seqs = self._prepare_operator_notes(active_worker, prompt)
                await self._set_agent(active_worker, AgentState.WORKING, current_task=task_id)
                try:
                    result = await self.adapters[active_worker].run(
                        prompt, tree.path, task_id, None,
                        self._callback(active_worker, task_id), read_only=False,
                    )
                except asyncio.CancelledError:
                    await self._set_agent(
                        active_worker, AgentState.STOPPED, error="Worker turn cancelled"
                    )
                    raise
                except Exception as exc:
                    result = AgentResult(
                        ok=False, returncode=1,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                self._record_usage(active_worker, task_id, result)
                await self._settle_operator_notes(active_worker, note_seqs, result)
                await self._set_agent(
                    active_worker,
                    AgentState.IDLE if result.ok else (
                        AgentState.LIMITED if result.limit_detected else AgentState.ERROR
                    ),
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
                    self.db.update_task(task_id, worker=fallback)
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
            review_prompt = architect_review_prompt(
                packet, tree.path, tree.base_sha, worker_head, changed_paths,
                validation,
                violations,
                measured_risk.value,
                profile_id=self.profile_id,
            )
            grok_result = await self._run_grok(
                review_prompt,
                self.workspace.architect_path,
                phase="review",
                read_only=self.profile.architect_read_only,
            )
            if not grok_result.ok:
                raise OrchestratorError(
                    f"{self.settings.agent('grok').display_name} review failed: "
                    f"{grok_result.error}"
                )
            try:
                latest_review = self._parse_review(grok_result.final_text)
            except (ValueError, ValidationError) as exc:
                repair = await self._run_grok(
                    f"Review JSON failed validation: {exc}. Repeat only one valid marked review object.",
                    self.workspace.architect_path,
                    phase="review-repair",
                    read_only=self.profile.architect_read_only,
                )
                if not repair.ok:
                    raise OrchestratorError("Review repair failed") from exc
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
                    "required_changes": [*latest_review.required_changes, "Make configured validation pass or provide an explicit safe replacement command"],
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
        worker = packet.worker
        risk = packet.risk
        unbounded_scope = any(
            path.strip() in _UNBOUNDED_SCOPE_PATTERNS for path in packet.allowed_paths
        )
        if unbounded_scope and risk in {RiskLevel.LOW, RiskLevel.MEDIUM}:
            risk = RiskLevel.HIGH
            update["risk"] = risk
        if worker == "spark":
            if self.profile_id == "combat":
                too_broad = (
                    risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}
                    or (packet.max_files is not None and packet.max_files > 5)
                    or len(packet.allowed_paths) > 6
                    or unbounded_scope
                )
            else:
                too_broad = (
                    risk != RiskLevel.LOW
                    or (packet.max_files is not None and packet.max_files > 3)
                    or len(packet.allowed_paths) > 4
                    or unbounded_scope
                )
            if too_broad:
                if not self.settings.agent("luna").enabled:
                    secondary = self.settings.agent("spark").display_name
                    primary = self.settings.agent("luna").display_name
                    raise OrchestratorError(
                        f"Task is too broad for {secondary} and {primary} is disabled"
                    )
                worker = "luna"
                update["worker"] = worker
        if not self.settings.agent(worker).enabled:
            if worker == "spark" and self.settings.agent("luna").enabled:
                worker = "luna"
                update["worker"] = worker
            else:
                secondary_max_files = 5 if self.profile_id == "combat" else 3
                secondary_max_paths = 6 if self.profile_id == "combat" else 4
                secondary_risk_ok = (
                    risk in {RiskLevel.LOW, RiskLevel.MEDIUM}
                    if self.profile_id == "combat"
                    else risk == RiskLevel.LOW
                )
                can_use_spark = (
                    worker == "luna"
                    and self.settings.agent("spark").enabled
                    and secondary_risk_ok
                    and not unbounded_scope
                    and len(packet.allowed_paths) <= secondary_max_paths
                    and (packet.max_files or secondary_max_files) <= secondary_max_files
                )
                if not can_use_spark:
                    raise OrchestratorError(
                        f"Selected worker lane is disabled: {worker}"
                    )
                worker = "spark"
                update["worker"] = worker
        if worker == "spark" and packet.max_files is None:
            update["max_files"] = 5 if self.profile_id == "combat" else 3
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
        secondary_max_files = 5 if self.profile_id == "combat" else 3
        secondary_max_paths = 6 if self.profile_id == "combat" else 4
        risk_ok = (
            packet.risk in {RiskLevel.LOW, RiskLevel.MEDIUM}
            if self.profile_id == "combat"
            else packet.risk == RiskLevel.LOW
        )
        unbounded_scope = any(
            path.strip() in _UNBOUNDED_SCOPE_PATTERNS
            for path in packet.allowed_paths
        )
        if (
            current == "luna"
            and risk_ok
            and not unbounded_scope
            and len(packet.allowed_paths) <= secondary_max_paths
            and (packet.max_files or secondary_max_files) <= secondary_max_files
            and self.settings.agent("spark").enabled
        ):
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
        try:
            decision = await future
            await self._pause_gate.wait()
            if self._stop_requested.is_set():
                return False
            self.db.update_mission(self.mission_id, status=MissionState.RUNNING.value)
            await self._emit(
                "mission.running",
                {"mission_id": self.mission_id, "after_human_gate": True},
            )
            return bool(decision.get("approved"))
        finally:
            self._approval_futures.pop(task_id, None)

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
                f"Mission {mission['id']} status={mission['status']} "
                f"profile={mission.get('profile', 'reserve')}",
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
                f"- {task['id']} [{task['status']}] "
                f"worker={task['worker']}/"
                f"{self.settings.agent(task['worker']).display_name if task['worker'] in self.settings.agents else 'unknown'} "
                f"risk={task['risk']} "
                f"base={task['base_sha'][:10]} head={task['worker_head'][:10]} title={task['title']} review={review}"
            )
        return "\n".join(lines)

    async def _fail_mission(self, exc: Exception) -> None:
        message = redact(f"{type(exc).__name__}: {exc}")
        row = self._mission_row()
        if row and row["status"] == MissionState.STOPPED.value:
            return
        if self.mission_id:
            self._block_open_tasks(self.mission_id, message)
            self.db.update_mission(
                self.mission_id,
                status=MissionState.FAILED.value,
                summary=message,
            )
        for key, config in self.settings.agents.items():
            rows = self.db.query("SELECT state FROM agents WHERE id=?", (config.id,))
            if rows and rows[0]["state"] in _BUSY_AGENT_STATES:
                await self._set_agent(key, AgentState.ERROR, error=message)
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
