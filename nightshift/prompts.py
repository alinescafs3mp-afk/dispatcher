from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import TaskPacket
from .protocol import compact_text

_DIRECTIVES = {
    "reserve": "EMERGENCY_TAKEOVER_DIRECTIVE.md",
    "combat": "COMBAT_OPERATIONS_DIRECTIVE.md",
}


def directive_path(profile_id: str = "reserve") -> Path:
    try:
        filename = _DIRECTIVES[profile_id]
    except KeyError as exc:
        raise ValueError(f"Unknown operating profile: {profile_id}") from exc
    return Path(__file__).with_name("data") / filename


def load_directive(profile_id: str = "reserve") -> str:
    return directive_path(profile_id).read_text(encoding="utf-8")


def recovery_handoff_prompt(role_name: str, repo: Path) -> str:
    return f"""You are now the fallback model inheriting an interrupted Codex session previously owned by {role_name}.
The predecessor's quota ended unexpectedly. Work in READ-ONLY mode. Do not edit files and do not run destructive commands.

Reconstruct the exact unfinished state from this session's retained context and the repository at `{repo}`.
Return a compact handoff with:
1. the last completed change;
2. the task that was active when work stopped;
3. files already changed or intended to change;
4. decisions and constraints that were explicitly agreed;
5. commands/tests already run and their results;
6. unresolved failures, blockers, or uncertainty;
7. the safest concrete next action;
8. anything in the session that may now be stale relative to current HEAD.

Separate evidence from inference. Do not continue implementation in this turn."""


def _dossier_excerpt(dossier_path: Path, limit: int = 60_000) -> str:
    try:
        return compact_text(
            dossier_path.read_text(encoding="utf-8", errors="replace"), limit
        )
    except OSError as exc:
        return f"<dossier read failed: {exc}>"


def architect_bootstrap_prompt(
    directive: str,
    dossier_path: Path,
    integration_path: Path,
    goal: str,
    predecessor_handoffs: dict[str, str],
    profile_id: str = "reserve",
) -> str:
    dossier_excerpt = _dossier_excerpt(dossier_path)
    if profile_id == "combat":
        return f"""{directive}

# Current stable-development mission
Human goal: {goal}
Integration worktree: `{integration_path}`
Repository reconnaissance artifact: `{dossier_path}`

The bounded repository excerpt below is embedded so the lead architect can recover context even when its CLI sandbox cannot read outside the worktree. Inspect current files, tests, branches, and the integration HEAD directly before trusting prose.

# Repository reconnaissance excerpt
{dossier_excerpt}

Reconcile the human goal with the actual repository and backlog. Improve or decompose the backlog where evidence shows gaps, then select exactly one implementation-ready next task. End with one `<SOL_LINK_JSON>` object using the dispatch, done, or blocked contract from the combat directive."""

    handoffs = "\n\n".join(
        f"## {name}\n{compact_text(text, 8000)}"
        for name, text in predecessor_handoffs.items()
    ) or "No predecessor session handoff was available."
    return f"""{directive}

# Current mission
Human goal: {goal}
Integration worktree: `{integration_path}`
Recovery dossier artifact: `{dossier_path}`

The bounded dossier excerpt below is embedded so the architect can recover even when its CLI sandbox cannot read outside the worktree. Inspect the integration worktree, current branches, and relevant artifacts directly before trusting prose. The source working tree may have been rescued into the integration branch without modifying the original checkout.

# Recovery dossier excerpt
{dossier_excerpt}

# Resumed predecessor-session handoffs
{handoffs}

Perform Phase Zero now. Reconcile all evidence, identify the last safe continuation point, and select exactly one implementation-ready next task. End with one `<SOL_LINK_JSON>` object using the dispatch, done, or blocked contract from the directive."""


def architect_next_prompt(
    mission_digest: str,
    integration_path: Path,
    profile_id: str = "reserve",
) -> str:
    if profile_id == "combat":
        return f"""Continue stable Friday development under the standing combat directive.
Integration worktree: `{integration_path}`

Compact mission ledger:
{compact_text(mission_digest, 14000)}

Inspect the current integration HEAD and backlog evidence. Reconcile accepted work against the human goal, add or decompose missing backlog work when needed, and select exactly one implementation-ready task. Declare done only after the directive's final audit. End with one `<SOL_LINK_JSON>` object."""
    return f"""Continue the emergency takeover under the standing Nightshift directive.
Integration worktree: `{integration_path}`

Compact mission ledger:
{compact_text(mission_digest, 14000)}

Inspect the current integration HEAD and backlog evidence. Reconcile accepted work against remaining items. Select exactly one next implementation-ready task, or declare done only after the directive's final completion audit. End with one `<SOL_LINK_JSON>` object."""


def worker_prompt(
    packet: TaskPacket,
    integration_base: str,
    revision: dict[str, Any] | None = None,
    profile_id: str = "reserve",
) -> str:
    operation = (
        "stable development operation"
        if profile_id == "combat"
        else "emergency continuity operation"
    )
    worker_rules = f"""
You are the implementation worker in a {operation}.
Implement only this packet in the supplied isolated Git worktree.
Read existing code and tests before editing. Preserve current contracts unless the packet explicitly changes them.
Do not broaden scope, redesign neighbouring systems, edit protected credentials, or commit secrets.
Run every safe validation command in the packet when possible. Sol Link will independently validate and review the actual diff.
If a stop condition occurs, stop editing and report BLOCKED with evidence rather than inventing architecture.
Do not mark the backlog item complete yourself; report what changed, tests, assumptions, and residual risk.
""".strip()
    revision_text = ""
    if revision:
        revision_text = (
            "\n\n# Architect review requiring revision\n"
            + json.dumps(revision, ensure_ascii=False, indent=2)
        )
    return f"""{worker_rules}

# Task packet
{packet.model_dump_json(indent=2)}

Task base commit: `{integration_base}`
{revision_text}

Complete the task now. Keep the final response concise and evidence-based."""


def architect_review_prompt(
    packet: TaskPacket,
    tree_path: Path,
    base_sha: str,
    worker_head: str,
    changed_paths: list[str],
    validation: dict[str, Any],
    policy_violations: list[str],
    measured_risk: str,
    profile_id: str = "reserve",
) -> str:
    directive_label = "combat" if profile_id == "combat" else "Nightshift"
    return f"""Review the completed worker task under the standing {directive_label} directive.

# Task packet
{packet.model_dump_json(indent=2)}

# Actual repository evidence
Worker worktree: `{tree_path}`
Base SHA: `{base_sha}`
Worker HEAD: `{worker_head}`
Changed paths: {json.dumps(changed_paths, ensure_ascii=False)}
Measured risk: `{measured_risk}`
Deterministic scope violations: {json.dumps(policy_violations, ensure_ascii=False)}
Validation report:
{json.dumps(validation, ensure_ascii=False, indent=2)}

Inspect the real diff with `git diff {base_sha}..{worker_head}` and any relevant neighbouring contracts/tests. Do not rely on the worker summary. Return exactly one `<SOL_LINK_JSON>` review object with action `accept`, `revise`, `reject`, or `escalate`. Acceptance is forbidden while deterministic scope violations remain."""


def final_audit_prompt(
    mission_digest: str,
    integration_path: Path,
    profile_id: str = "reserve",
) -> str:
    if profile_id == "combat":
        return f"""Perform the final stable-development audit required by the combat directive.
Integration worktree: `{integration_path}`

Mission ledger:
{compact_text(mission_digest, 18000)}

Inspect the current branch, human goal, backlog sources, worker branches, dirty worktrees, documentation, migrations, and validation evidence. Map every discovered item to a final disposition. Return `done` only when the reconciled goal has no unexplained remainder. Otherwise dispatch the next concrete task. End with one `<SOL_LINK_JSON>` object."""
    return f"""Perform the final Nightshift completion audit required by the standing emergency directive.
Integration worktree: `{integration_path}`

Mission ledger:
{compact_text(mission_digest, 18000)}

Inspect the current branch, backlog sources, worker branches, dirty worktrees, and validation evidence. Map every discovered item to a final disposition. Return `done` only when `remaining_items` is empty or contains only explicitly evidenced external/security-gated dispositions. Otherwise dispatch the next concrete task. End with one `<SOL_LINK_JSON>` object."""


def mission_resume_prompt(
    directive: str,
    dossier_excerpt: str,
    mission_digest: str,
    architect_path: Path,
    profile_id: str = "reserve",
) -> str:
    if profile_id == "combat":
        return f"""{directive}

# Stable mission process-restart recovery
The control process stopped after this mission had begun. Reconstruct the durable state before dispatching more work. Inspect preserved worker branches/worktrees and the integration worktree at `{architect_path}`. Reconcile any task whose database state disagrees with Git evidence.

# Embedded repository dossier excerpt
{dossier_excerpt}

Compact persisted mission ledger:
{compact_text(mission_digest, 16000)}

Select exactly one safe continuation task, improve the backlog if necessary, or declare completion only after the full final audit. End with one `<SOL_LINK_JSON>` object."""
    return f"""{directive}

# Nightshift process-restart recovery
The control process itself stopped after this mission had already begun. Perform Phase Zero again before dispatching more work. Inspect all preserved worker branches/worktrees and the integration worktree at `{architect_path}`. Reconcile any task whose database state disagrees with Git evidence.

# Embedded recovery dossier excerpt
{dossier_excerpt}

Compact persisted mission ledger:
{compact_text(mission_digest, 16000)}

Select exactly one safe continuation task, or declare completion only after the full final audit. End with one `<SOL_LINK_JSON>` object."""


def chat_prompt(
    user_text: str,
    participant_name: str = "Grok",
    participant_role: str = "chief architect",
    profile_id: str = "reserve",
) -> str:
    return f"""The human operator is speaking directly to you as {participant_name}, {participant_role}, in the `{profile_id}` Sol Link profile.
Answer directly and honestly. You may inspect the repository when useful, but this is a READ-ONLY operator channel. Do not silently dispatch, edit, integrate, or alter the automated mission loop from this chat message. If the operator is steering ongoing work, explain what will change on your next eligible turn.

Human message:
{user_text}"""
