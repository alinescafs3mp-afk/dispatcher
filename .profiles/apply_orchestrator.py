#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "nightshift" / "orchestrator.py"
text = PATH.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match, found {count}: {old[:160]!r}")
    text = text.replace(old, new, 1)


def replace_between(start: str, end: str, replacement: str) -> None:
    global text
    first = text.find(start)
    if first < 0:
        raise RuntimeError(f"start marker not found: {start!r}")
    last = text.find(end, first + len(start))
    if last < 0:
        raise RuntimeError(f"end marker not found: {end!r}")
    text = text[:first] + replacement + text[last:]


replace_once("import asyncio\n", "import asyncio\nfrom copy import deepcopy\n")
replace_once(
    "    load_directive,\n    recovery_handoff_prompt,\n",
    "    load_directive,\n    mission_resume_prompt,\n    recovery_handoff_prompt,\n",
)
replace_once(
    "from .process import ProcessRunner\n",
    "from .process import ProcessRunner\n"
    "from .profiles import (\n"
    "    PROFILE_IDS,\n"
    "    get_profile,\n"
    "    profile_catalog,\n"
    "    profile_prompt_context,\n"
    "    profile_public_dict,\n"
    "    resolve_profile_agents,\n"
    ")\n",
)

init_start = "        self.settings = settings\n"
init_end = "        self._mark_interrupted_missions_paused()\n"
init_block = '''        self.settings = settings
        self.runtime = settings.orchestrator.runtime_path
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.db = StateDB(self.runtime / "nightshift.sqlite3")
        self._agent_templates = deepcopy(settings.agents)
        saved_profile = self.db.get_preference("profile.active", settings.profiles.default)
        if not isinstance(saved_profile, str) or saved_profile not in PROFILE_IDS:
            saved_profile = settings.profiles.default
        if saved_profile not in PROFILE_IDS:
            saved_profile = "reserve"
        self.profile_id = saved_profile
        self.combat_grok_enabled = bool(
            self.db.get_preference(
                "profile.combat.grok_enabled",
                settings.profiles.combat_grok_enabled,
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
'''
replace_between(init_start, init_end, init_block)

methods_marker = "\n\n    def _load_preferences(self) -> None:\n"
methods = '''

    def _apply_profile_configuration(self) -> None:
        self.profile = get_profile(self.profile_id)
        self.settings.agents = resolve_profile_agents(
            self._agent_templates,
            self.settings.profiles,
            self.profile_id,
            self.combat_grok_enabled,
        )

    def _build_adapters(self) -> dict[str, CodexAdapter | GrokAdapter]:
        adapters: dict[str, CodexAdapter | GrokAdapter] = {}
        for key, config in self.settings.agents.items():
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
        return profile_prompt_context(self.profile, self.settings.agents)

    async def set_profile(
        self,
        profile_id: str,
        combat_grok_enabled: bool | None = None,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        if profile_id not in PROFILE_IDS:
            raise OrchestratorError(f"Unknown operating profile: {profile_id}")
        if self._mission_task_running():
            raise OrchestratorError(
                "The operating profile cannot change while a mission is running or paused"
            )
        async with self._profile_lock:
            async with self._doctor_lock:
                async with self._quota_lock:
                    if any(lock.locked() for lock in self.agent_locks.values()):
                        raise OrchestratorError(
                            "The operating profile cannot change while an agent turn is active"
                        )
                    self.profile_id = profile_id
                    if combat_grok_enabled is not None:
                        self.combat_grok_enabled = bool(combat_grok_enabled)
                    self._apply_profile_configuration()
                    self._load_preferences()
                    self.adapters = self._build_adapters()
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
                    payload = self._profile_public()
                    await self._emit("profile.changed", payload, sender="human")
                    return payload

    def _resolve_chat_recipient(self, recipient: str) -> str:
        value = recipient.strip().casefold()
        if not value or value == "architect":
            return self.profile.architect_key
        for key, config in self.settings.agents.items():
            candidates = {
                key.casefold(),
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

    async def _queue_operator_nudge(self, key: str, text: str) -> dict[str, Any]:
        config = self.settings.agent(key)
        seq = self.db.add_chat(
            "user",
            text,
            agent_key=key,
            agent_id=config.id,
            profile=self.profile_id,
            mission_id=self.mission_id,
            kind="nudge",
            status="queued",
        )
        payload = {
            "status": "queued",
            "recipient": key,
            "agent_id": config.id,
            "display_name": config.display_name,
            "seq": seq,
            "message": "Queued for the participant's next model turn",
        }
        await self._emit(
            "chat.queued",
            payload,
            sender="human",
            recipient=config.id,
        )
        return payload

    async def _inject_operator_notes(self, key: str, prompt: str) -> str:
        notes = self.db.pending_nudges(self.profile_id, key, self.mission_id)
        if not notes:
            return prompt
        seqs = [int(note["seq"]) for note in notes]
        self.db.mark_chat_delivered(seqs)
        block = "\n\n# Queued human steering notes for this participant\n" + "\n".join(
            f"- note {note['seq']}: {note['text']}" for note in notes
        )
        await self._emit(
            "chat.nudges_delivered",
            {"recipient": key, "seqs": seqs},
            sender="human",
            recipient=self.settings.agent(key).id,
        )
        return prompt + block
'''
replace_once(methods_marker, methods + methods_marker)

old_load = '''    def _load_preferences(self) -> None:
        for key, config in self.settings.agents.items():
            saved = self.db.get_preference(f"agent.{key}.effort", "")
            if isinstance(saved, str) and saved in config.effort_options:
                config.effort = saved
'''
new_load = '''    def _load_preferences(self) -> None:
        for key, config in self.settings.agents.items():
            legacy = self.db.get_preference(f"agent.{key}.effort", "")
            saved = self.db.get_preference(
                f"profile.{self.profile_id}.agent.{key}.effort",
                legacy if self.profile_id == "reserve" else "",
            )
            if isinstance(saved, str) and saved in config.effort_options:
                config.effort = saved
'''
replace_once(old_load, new_load)

replace_once(
    'metadata={"key": key, "effort": config.effort},\n',
    'metadata={\n'
    '                    "key": key,\n'
    '                    "profile": self.profile_id,\n'
    '                    "display_name": config.display_name,\n'
    '                    "lane": config.lane,\n'
    '                    "physical_key": config.physical_key,\n'
    '                    "adapter": config.adapter,\n'
    '                    "optional": config.optional,\n'
    '                    "effort": config.effort,\n'
    '                },\n',
)

refresh_start = "    async def refresh_quotas(self) -> dict[str, Any]:\n"
refresh_end = "    async def set_reasoning(self, key: str, effort: str) -> dict[str, Any]:\n"
refresh_method = '''    async def refresh_quotas(self) -> dict[str, Any]:
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
                        options, matched_model = codex_effort_options(
                            payload,
                            config.model,
                            prefer_luna=config.physical_key == "luna",
                        )
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

'''
replace_between(refresh_start, refresh_end, refresh_method)

replace_once(
    '        self.db.set_preference(f"agent.{key}.effort", effort)\n',
    '        self.db.set_preference(\n'
    '            f"profile.{self.profile_id}.agent.{key}.effort", effort\n'
    '        )\n'
    '        if self.profile_id == "reserve":\n'
    '            self.db.set_preference(f"agent.{key}.effort", effort)\n',
)
replace_once(
    '                "key": key,\n                "effort": effort,\n',
    '                "key": key,\n                "profile": self.profile_id,\n'
    '                "display_name": config.display_name,\n'
    '                "effort": effort,\n',
)
replace_once(
    '            "key": key,\n            "agent_id": config.id,\n            "effort": effort,\n',
    '            "key": key,\n            "profile": self.profile_id,\n'
    '            "agent_id": config.id,\n            "display_name": config.display_name,\n'
    '            "effort": effort,\n',
)

snapshot_old = '''    def snapshot(self) -> dict[str, Any]:
        data = self.db.snapshot(log_tail=self.settings.orchestrator.log_tail_lines)
        data["quotas"] = self.quota_cache
        data["config"] = self.settings.public_dict()
        data["active_mission_id"] = self.mission_id
        data["mission_running"] = self._mission_task_running()
        return data
'''
snapshot_new = '''    def snapshot(self) -> dict[str, Any]:
        data = self.db.snapshot(log_tail=self.settings.orchestrator.log_tail_lines)
        data["quotas"] = self.quota_cache
        data["config"] = self.settings.public_dict()
        data["profile"] = self._profile_public()
        data["profiles"] = profile_catalog(
            self.settings.profiles,
            self.combat_grok_enabled,
        )
        data["active_mission_id"] = self.mission_id
        data["mission_running"] = self._mission_task_running()
        data["profile_switch_locked"] = self._mission_task_running() or any(
            lock.locked() for lock in self.agent_locks.values()
        )
        return data
'''
replace_once(snapshot_old, snapshot_new)

replace_once(
    '        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")\n',
    '        if not self.settings.agent(self.profile.architect_key).enabled:\n'
    '            raise OrchestratorError(\n'
    '                f"Profile architect is disabled: "\n'
    '                f"{self.settings.agent(self.profile.architect_key).display_name}"\n'
    '            )\n'
    '        if not self.settings.agent(self.profile.primary_worker_key).enabled:\n'
    '            raise OrchestratorError(\n'
    '                f"Profile implementation owner is disabled: "\n'
    '                f"{self.settings.agent(self.profile.primary_worker_key).display_name}"\n'
    '            )\n'
    '        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")\n',
)
replace_once(
    '        directive_copy = self.mission_dir / "EMERGENCY_TAKEOVER_DIRECTIVE.md"\n'
    '        directive_copy.write_text(load_directive(), encoding="utf-8")\n',
    '        directive_copy = self.mission_dir / self.profile.directive_name\n'
    '        directive_copy.write_text(load_directive(self.profile_id), encoding="utf-8")\n',
)
replace_once(
    '            directive_path=str(directive_copy),\n        )\n',
    '            directive_path=str(directive_copy),\n'
    '            profile=self.profile_id,\n'
    '            profile_options=self._profile_options(),\n'
    '        )\n',
)
replace_once("        self._grok_chat_session_id = None\n", "")
replace_once(
    '        await self._emit("mission.created", {"mission_id": self.mission_id, "goal": goal})\n',
    '        await self._emit(\n'
    '            "mission.created",\n'
    '            {"mission_id": self.mission_id, "goal": goal, "profile": self.profile_id},\n'
    '        )\n',
)

resume_row = '        row = rows[0]\n        if row["status"] not in _RESUMABLE_MISSIONS:\n'
resume_insert = '''        row = rows[0]
        stored_profile = str(row.get("profile") or "reserve")
        try:
            stored_options = json.loads(row.get("profile_options_json") or "{}")
        except json.JSONDecodeError:
            stored_options = {}
        await self.set_profile(
            stored_profile,
            combat_grok_enabled=bool(
                stored_options.get(
                    "combat_grok_enabled",
                    self.combat_grok_enabled,
                )
            ),
            persist=True,
        )
        if row["status"] not in _RESUMABLE_MISSIONS:
'''
replace_once(resume_row, resume_insert)
replace_once("        self._grok_chat_session_id = None\n", "")
replace_once(
    '        await self._emit("mission.resumed", {"mission_id": mission_id})\n',
    '        await self._emit(\n'
    '            "mission.resumed",\n'
    '            {"mission_id": mission_id, "profile": self.profile_id},\n'
    '        )\n',
)

chat_start = "    async def chat(self, text: str) -> str:\n"
chat_end = "    async def _run_new_mission(self, mission_id: str, goal: str) -> None:\n"
chat_method = '''    async def chat(
        self,
        text: str,
        recipient: str = "architect",
        delivery: str = "auto",
    ) -> dict[str, Any]:
        """Talk to any active participant or queue steering for its next turn."""
        text = text.strip()
        if not text:
            raise OrchestratorError("Message is empty")
        if delivery not in {"auto", "chat", "nudge"}:
            raise OrchestratorError(f"Unknown chat delivery mode: {delivery}")
        key = self._resolve_chat_recipient(recipient)
        config = self.settings.agent(key)
        if not config.enabled:
            raise OrchestratorError(
                f"Participant is disabled in the active profile: {config.display_name}"
            )
        lock = self.agent_locks[key]
        if delivery == "nudge" or (delivery == "auto" and lock.locked()):
            return await self._queue_operator_nudge(key, text)
        if delivery == "chat" and lock.locked():
            raise OrchestratorError(
                f"{config.display_name} is busy; use delivery='nudge' or 'auto'"
            )

        acquired = False
        if delivery == "auto":
            try:
                await asyncio.wait_for(lock.acquire(), timeout=0.05)
                acquired = True
            except TimeoutError:
                return await self._queue_operator_nudge(key, text)
        else:
            await lock.acquire()
            acquired = True

        task_label = f"chat:{self.profile_id}:{key}"
        try:
            self.db.add_chat(
                "user",
                text,
                agent_key=key,
                agent_id=config.id,
                profile=self.profile_id,
                mission_id=self.mission_id,
                kind="message",
                status="sent",
            )
            await self._emit(
                "chat.message",
                {
                    "role": "user",
                    "text": text,
                    "recipient": key,
                    "profile": self.profile_id,
                },
                sender="human",
                recipient=config.id,
            )
            digest = (
                self._mission_digest()
                if self.mission_id
                else "No active Sol Link mission."
            )
            prompt = chat_prompt(
                text,
                participant_name=config.display_name,
                participant_role=config.role,
                profile_id=self.profile_id,
            )
            prompt += "\n\nCurrent compact mission ledger:\n" + compact_text(digest, 9000)
            prompt = self._profile_context() + "\n\n" + prompt
            prompt = await self._inject_operator_notes(key, prompt)
            cwd = self.settings.project.repo_path
            if self.workspace:
                if key == self.profile.architect_key:
                    cwd = await asyncio.to_thread(self.workspace.sync_architect_worktree)
                else:
                    cwd = self.workspace.integration_path
            await self._set_agent(key, AgentState.PLANNING, current_task=task_label)
            session_key = (self.profile_id, key)
            if session_key not in self._chat_session_ids:
                saved = self.db.get_preference(
                    f"chat.session.{self.profile_id}.{key}",
                    "",
                )
                self._chat_session_ids[session_key] = saved if isinstance(saved, str) and saved else None
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
                await self._set_agent(key, AgentState.STOPPED, error="Operator chat cancelled")
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
                    f"chat.session.{self.profile_id}.{key}",
                    self._chat_session_ids[session_key],
                )
            self._record_usage(key, task_label, result)
            if self.workspace and key == self.profile.architect_key:
                try:
                    await asyncio.to_thread(self.workspace.sync_architect_worktree)
                except Exception as exc:
                    result = result.model_copy(update={
                        "ok": False,
                        "error": (
                            result.error
                            + f"\nArchitect worktree reset failed: {exc}"
                        ).strip(),
                    })
            await self._set_agent(
                key,
                AgentState.IDLE if result.ok else (
                    AgentState.LIMITED if result.limit_detected else AgentState.ERROR
                ),
                error=result.error,
            )
        finally:
            if acquired:
                lock.release()

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
            profile=self.profile_id,
            mission_id=self.mission_id,
            kind="message",
            status="sent",
        )
        await self._emit(
            "chat.message",
            {
                "role": "assistant",
                "text": answer,
                "recipient": key,
                "profile": self.profile_id,
            },
            sender=config.id,
            recipient="human",
        )
        return {
            "status": "answered",
            "recipient": key,
            "agent_id": config.id,
            "display_name": config.display_name,
            "answer": answer,
        }

'''
replace_between(chat_start, chat_end, chat_method)

replace_once(
    '            await self._emit("mission.recovering", {"mission_id": mission_id})\n',
    '            await self._emit(\n'
    '                "mission.recovering",\n'
    '                {"mission_id": mission_id, "profile": self.profile_id},\n'
    '            )\n',
)
replace_once(
    '            predecessor_handoffs = await self._recover_predecessors(dossier)\n',
    '            predecessor_handoffs = (\n'
    '                await self._recover_predecessors(dossier)\n'
    '                if self.profile.recover_predecessors\n'
    '                else {}\n'
    '            )\n',
)
replace_once(
    '                load_directive(), Path(dossier["markdown_path"]), architect_cwd,\n'
    '                goal, predecessor_handoffs,\n'
    '            )\n',
    '                load_directive(self.profile_id),\n'
    '                Path(dossier["markdown_path"]),\n'
    '                architect_cwd,\n'
    '                goal,\n'
    '                predecessor_handoffs,\n'
    '                profile_id=self.profile_id,\n'
    '            )\n',
)

resume_prompt_start = '            prompt = f"""{load_directive()}\n\n# Nightshift process-restart recovery\n'
resume_prompt_end = '            decision = await self._ask_architect_decision(prompt, phase="resume-recovery")\n'
resume_prompt = '''            prompt = mission_resume_prompt(
                load_directive(self.profile_id),
                dossier_excerpt,
                digest,
                cwd,
                profile_id=self.profile_id,
            )
'''
replace_between(resume_prompt_start, resume_prompt_end, resume_prompt)

replace_once(
    '                    architect_next_prompt(digest, cwd), phase="next",\n',
    '                    architect_next_prompt(\n'
    '                        digest, cwd, profile_id=self.profile_id\n'
    '                    ),\n'
    '                    phase="next",\n',
)
replace_once(
    '                    final_audit_prompt(digest, cwd), phase="final-audit",\n',
    '                    final_audit_prompt(\n'
    '                        digest, cwd, profile_id=self.profile_id\n'
    '                    ),\n'
    '                    phase="final-audit",\n',
)

replace_once(
    '        if not self.settings.orchestrator.recover_predecessor_sessions:\n',
    '        if (\n'
    '            not self.profile.recover_predecessors\n'
    '            or not self.settings.orchestrator.recover_predecessor_sessions\n'
    '        ):\n',
)
replace_once(
    '                    recovery_handoff_prompt(predecessor, self.settings.project.repo_path),\n',
    '                    await self._inject_operator_notes(\n'
    '                        key,\n'
    '                        self._profile_context()\n'
    '                        + "\\n\\n"\n'
    '                        + recovery_handoff_prompt(\n'
    '                            predecessor, self.settings.project.repo_path\n'
    '                        ),\n'
    '                    ),\n',
)

replace_once(
    '                prompt = (\n'
    '                    "This is a rotated architect session. Reconstruct continuity from the repository, "\n'
    '                    "Nightshift ledger, and the prompt below. The emergency directive remains binding.\\n\\n"\n'
    '                    + prompt\n'
    '                )\n',
    '                prompt = (\n'
    '                    "This is a rotated architect session. Reconstruct continuity from "\n'
    '                    "the repository, durable Sol Link ledger, and the prompt below. "\n'
    '                    "The active profile directive remains binding.\\n\\n"\n'
    '                    + prompt\n'
    '                )\n',
)
replace_once(
    '            state = AgentState.REVIEWING if phase.startswith("review") else AgentState.PLANNING\n',
    '            prompt = self._profile_context() + "\\n\\n" + prompt\n'
    '            prompt = await self._inject_operator_notes("grok", prompt)\n'
    '            state = AgentState.REVIEWING if phase.startswith("review") else AgentState.PLANNING\n',
)
replace_once(
    '                await self._set_agent("grok", AgentState.STOPPED, error="Grok turn cancelled")\n',
    '                await self._set_agent(\n'
    '                    "grok",\n'
    '                    AgentState.STOPPED,\n'
    '                    error=f"{self.settings.agent(\'grok\').display_name} turn cancelled",\n'
    '                )\n',
)
replace_once(
    '                raise OrchestratorError("Grok architect limit reached; preserved state requires human intervention")\n'
    '            raise OrchestratorError(f"Grok architect failed: {result.error}")\n',
    '                raise OrchestratorError(\n'
    '                    f"{self.settings.agent(\'grok\').display_name} architect limit reached; "\n'
    '                    "preserved state requires human intervention"\n'
    '                )\n'
    '            raise OrchestratorError(\n'
    '                f"{self.settings.agent(\'grok\').display_name} architect failed: "\n'
    '                f"{result.error}"\n'
    '            )\n',
)

replace_once(
    '            prompt = worker_prompt(packet, tree.base_sha, revision_context)\n',
    '            prompt = worker_prompt(\n'
    '                packet,\n'
    '                tree.base_sha,\n'
    '                revision_context,\n'
    '                profile_id=self.profile_id,\n'
    '            )\n'
    '            prompt = self._profile_context() + "\\n\\n" + prompt\n'
    '            prompt = await self._inject_operator_notes(active_worker, prompt)\n',
)
replace_once(
    '                validation, violations, measured_risk.value,\n'
    '            )\n',
    '                validation,\n'
    '                violations,\n'
    '                measured_risk.value,\n'
    '                profile_id=self.profile_id,\n'
    '            )\n',
)
replace_once(
    '                raise OrchestratorError(f"Grok review failed: {grok_result.error}")\n',
    '                raise OrchestratorError(\n'
    '                    f"{self.settings.agent(\'grok\').display_name} review failed: "\n'
    '                    f"{grok_result.error}"\n'
    '                )\n',
)

old_disabled = '''        if not self.settings.agent(worker).enabled:
            can_use_spark = (
                worker == "luna"
                and self.settings.agent("spark").enabled
                and risk == RiskLevel.LOW
                and not unbounded_scope
                and len(packet.allowed_paths) <= 4
                and (packet.max_files or 3) <= 3
            )
            if not can_use_spark:
                raise OrchestratorError(f"Selected worker lane is disabled: {worker}")
            worker = "spark"
            update["worker"] = worker
'''
new_disabled = '''        if not self.settings.agent(worker).enabled:
            if worker == "spark" and self.settings.agent("luna").enabled:
                worker = "luna"
                update["worker"] = worker
            else:
                can_use_spark = (
                    worker == "luna"
                    and self.settings.agent("spark").enabled
                    and risk == RiskLevel.LOW
                    and not unbounded_scope
                    and len(packet.allowed_paths) <= 4
                    and (packet.max_files or 3) <= 3
                )
                if not can_use_spark:
                    raise OrchestratorError(
                        f"Selected worker lane is disabled: {worker}"
                    )
                worker = "spark"
                update["worker"] = worker
'''
replace_once(old_disabled, new_disabled)

replace_once(
    '                f"Mission {mission[\'id\']} status={mission[\'status\']}",\n',
    '                f"Mission {mission[\'id\']} status={mission[\'status\']} "\n'
    '                f"profile={mission.get(\'profile\', \'reserve\')}",\n',
)
replace_once(
    '                f"- {task[\'id\']} [{task[\'status\']}] worker={task[\'worker\']} risk={task[\'risk\']} "\n',
    '                f"- {task[\'id\']} [{task[\'status\']}] "\n'
    '                f"worker={task[\'worker\']}/"\n'
    '                f"{self.settings.agent(task[\'worker\']).display_name if task[\'worker\'] in self.settings.agents else \'unknown\'} "\n'
    '                f"risk={task[\'risk\']} "\n',
)

PATH.write_text(text, encoding="utf-8", newline="\n")
print("orchestrator profile refactor applied")
