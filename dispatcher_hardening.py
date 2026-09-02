"""Runtime hardening for Friday Emergency Dispatcher.

Kept separate from the UI/application module so scope enforcement and the
closed Sol Link task cycle remain independently auditable.
"""

from __future__ import annotations

import asyncio
import fnmatch
from typing import Any


def _matches(path: str, scopes: list[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    for raw in scopes:
        scope = raw.replace("\\", "/").lstrip("./").strip()
        if not scope:
            continue
        if any(char in scope for char in "*?["):
            if fnmatch.fnmatchcase(normalized, scope):
                return True
            continue
        prefix = scope.rstrip("/")
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def apply_hardening(core: Any) -> None:
    """Attach strict scope checks and a complete protocol event cycle."""

    def changed_paths_since(self: Any, worktree: Any, base_sha: str) -> list[str]:
        output = core._git(worktree, "diff", "--name-only", "-z", f"{base_sha}..HEAD").stdout
        return sorted(set(core._decode_z(output)))

    def commit_task(self: Any, worktree: Any, task: Any) -> str | None:
        tracked, untracked = core._changed_paths(worktree)
        changed = sorted(set(tracked + untracked))
        if not changed:
            return None

        sensitive = [path for path in changed if core.is_sensitive_path(path)]
        if sensitive:
            raise core.GitError("worker touched sensitive paths: " + ", ".join(sensitive))

        if not task.allowed_paths:
            raise core.GitError("task packet has no allowed_paths; refusing an unbounded commit")
        outside = [path for path in changed if not _matches(path, task.allowed_paths)]
        if outside:
            raise core.GitError("worker changed paths outside task scope: " + ", ".join(outside))

        forbidden = [path for path in changed if _matches(path, task.forbidden_paths)]
        if forbidden:
            raise core.GitError("worker changed explicitly forbidden paths: " + ", ".join(forbidden))

        core._git(worktree, "add", "-A")
        core._git(
            worktree,
            "commit",
            "-m",
            f"feat(dispatcher): {task.title[:70]}",
            env=core.GIT_IDENTITY,
        )
        return core._git(worktree, "rev-parse", "HEAD").stdout.decode().strip()

    async def protocol(self: Any, event_type: str, source: str, target: str, **payload: Any) -> None:
        await self.sol_link.append(event_type, source, target, payload)
        await self.bus.emit(source, "sol_link." + event_type.lower(), payload.get("summary", event_type), payload)

    async def mark_blocked(self: Any, record: Any, worker_id: str, reason: str) -> bool:
        record.status = "BLOCKED"
        record.error = reason
        record.updated_at = core.utc_now()
        self.run_state["blocked"] = int(self.run_state.get("blocked", 0)) + 1
        await protocol(
            self,
            "BLOCKER",
            worker_id + "-worker",
            "grok-architect",
            task_id=record.task_id,
            base_sha=record.base_sha,
            commit_shas=record.commit_shas,
            summary=reason,
        )
        await self._set_phase(worker_id, "idle", "blocked: " + reason[:180])
        self._save_state()
        return False

    async def execute_task(self: Any, packet: Any) -> bool:
        assert self.recovery
        if packet.task_id in self.tasks:
            await self.bus.emit(
                "system",
                "task.duplicate",
                f"architect repeated task_id {packet.task_id}; a new stable ID is required",
            )
            return False

        worker_id = self._route_worker(packet)
        packet.worker = worker_id
        worktree, branch, base = await asyncio.to_thread(
            self.git.create_task_worktree,
            self.recovery.integration_root,
            packet.task_id,
        )
        record = core.TaskRecord(
            packet.task_id,
            packet.title,
            worker_id,
            "IMPLEMENTING",
            base,
            branch,
            str(worktree),
        )
        self.tasks[packet.task_id] = record
        await protocol(
            self,
            "CONTRACT",
            "grok-architect",
            worker_id + "-worker",
            task_id=packet.task_id,
            base_sha=base,
            task=core.asdict(packet),
            summary=packet.title,
        )
        await self._set_phase(worker_id, "working", f"implementing {packet.task_id}: {packet.title}")

        try:
            result = await self.workers[worker_id].run(self._worker_prompt(packet, base), worktree)
        except Exception as exc:
            return await mark_blocked(self, record, worker_id, str(exc))

        if result.session_id:
            self.agent_states[worker_id].session_id = result.session_id
        self.agent_states[worker_id].usage = result.usage
        if result.returncode:
            return await mark_blocked(
                self,
                record,
                worker_id,
                result.error or f"worker exited {result.returncode}",
            )

        try:
            commit = await asyncio.to_thread(self.git.commit_task, worktree, packet)
        except Exception as exc:
            return await mark_blocked(self, record, worker_id, str(exc))
        if not commit:
            return await mark_blocked(self, record, worker_id, "worker produced no repository change")

        record.commit_shas.append(commit)
        changed = await asyncio.to_thread(self.git.changed_paths_since, worktree, base)
        await protocol(
            self,
            "HANDOFF",
            worker_id + "-worker",
            "grok-architect",
            task_id=packet.task_id,
            base_sha=base,
            commit_shas=list(record.commit_shas),
            changed_paths=changed,
            usage=result.usage,
            summary=result.text[-2000:] if result.text else "implementation handoff",
        )
        await self._set_phase(worker_id, "idle", f"handoff ready for {packet.task_id}")

        review_round = 0
        while review_round <= self.cfg.max_review_rounds:
            commits = await asyncio.to_thread(self.git.commits_since, worktree, base)
            await self._set_phase("grok", "reviewing", f"reviewing {packet.task_id}")
            try:
                review = await self._review(packet, worktree, base, commits)
            except Exception as exc:
                await self._set_phase("grok", "idle", "review failed")
                return await mark_blocked(self, record, worker_id, f"architect review failed: {exc}")
            finally:
                if self.agent_states["grok"].phase == "reviewing":
                    await self._set_phase("grok", "idle", "ready")

            record.review = review
            record.updated_at = core.utc_now()
            verdict = str(review.get("verdict", "BLOCKED")).upper()
            await protocol(
                self,
                "REVIEW",
                "grok-architect",
                worker_id + "-worker",
                task_id=packet.task_id,
                base_sha=base,
                commit_shas=commits,
                verdict=verdict,
                required_changes=review.get("required_changes", []),
                remaining_risks=review.get("remaining_risks", []),
                summary=str(review.get("summary") or verdict),
            )

            if verdict == "ACCEPT":
                self.recovery.integration_head = await self.git.integrate(
                    self.recovery.integration_root,
                    commits,
                )
                record.status = "ACCEPTED"
                record.commit_shas = commits
                record.updated_at = core.utc_now()
                self.run_state["accepted"] = int(self.run_state.get("accepted", 0)) + 1
                await protocol(
                    self,
                    "ACCEPT",
                    "grok-architect",
                    "integration",
                    task_id=packet.task_id,
                    base_sha=base,
                    commit_shas=commits,
                    integration_head=self.recovery.integration_head,
                    remaining_risks=review.get("remaining_risks", []),
                    summary=str(review.get("summary") or "accepted"),
                )
                if not self.cfg.keep_task_worktrees:
                    await asyncio.to_thread(
                        self.git.remove_task_worktree,
                        self.recovery.integration_root,
                        worktree,
                        branch,
                    )
                self._save_state()
                return True

            if verdict == "CHANGES_REQUESTED" and review_round < self.cfg.max_review_rounds:
                required = review.get("required_changes") or [review.get("summary")]
                followup = (
                    "Apply only these architect review corrections, rerun affected validation, "
                    "remain inside the original task scope, and do not commit: "
                    + core.json.dumps(required, ensure_ascii=False)
                )
                await self._set_phase(worker_id, "working", f"review corrections for {packet.task_id}")
                try:
                    result = await self.workers[worker_id].run(
                        followup,
                        worktree,
                        session_id=result.session_id,
                    )
                    next_commit = (
                        await asyncio.to_thread(self.git.commit_task, worktree, packet)
                        if not result.returncode
                        else None
                    )
                except Exception as exc:
                    return await mark_blocked(self, record, worker_id, str(exc))
                if not next_commit:
                    return await mark_blocked(
                        self,
                        record,
                        worker_id,
                        result.error or "review correction produced no acceptable change",
                    )
                record.commit_shas.append(next_commit)
                changed = await asyncio.to_thread(self.git.changed_paths_since, worktree, base)
                await protocol(
                    self,
                    "HANDOFF",
                    worker_id + "-worker",
                    "grok-architect",
                    task_id=packet.task_id,
                    base_sha=base,
                    commit_shas=list(record.commit_shas),
                    changed_paths=changed,
                    usage=result.usage,
                    summary=result.text[-2000:] if result.text else "review correction handoff",
                )
                await self._set_phase(worker_id, "idle", f"corrections ready for {packet.task_id}")
                review_round += 1
                continue

            return await mark_blocked(
                self,
                record,
                worker_id,
                str(review.get("blocker") or review.get("summary") or "architect blocked task"),
            )

        return await mark_blocked(self, record, worker_id, "review round cap reached")

    core.GitWorkspace.changed_paths_since = changed_paths_since
    core.GitWorkspace.commit_task = commit_task
    core.Dispatcher._execute_task = execute_task
