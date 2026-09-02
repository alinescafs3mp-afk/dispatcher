from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import AgentConfig, ProfilesConfig

PROFILE_IDS = ("reserve", "combat")
LOGICAL_AGENT_KEYS = ("grok", "luna", "spark")


@dataclass(frozen=True, slots=True)
class SlotSpec:
    source_key: str
    agent_id: str
    display_name: str
    role: str
    lane: str
    optional: bool = False


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    id: str
    label: str
    short_label: str
    eyebrow: str
    description: str
    directive_name: str
    default_goal: str
    architect_key: str
    primary_worker_key: str
    worker_keys: tuple[str, ...]
    slot_order: tuple[str, ...]
    recover_predecessors: bool
    architect_read_only: bool
    slots: dict[str, SlotSpec]


_PROFILES: dict[str, ProfileSpec] = {
    "reserve": ProfileSpec(
        id="reserve",
        label="Reserve / emergency takeover",
        short_label="RESERVE",
        eyebrow="EMERGENCY CONTINUITY",
        description=(
            "Grok temporarily leads the operation; Luna owns substantial implementation "
            "and Spark handles bounded micro-tasks while the normal Sol pair is unavailable."
        ),
        directive_name="EMERGENCY_TAKEOVER_DIRECTIVE.md",
        default_goal=(
            "Recover the interrupted Sol and SolGoodman work, reconcile the real backlog, "
            "and carry it to completion without architectural drift."
        ),
        architect_key="grok",
        primary_worker_key="luna",
        worker_keys=("luna", "spark"),
        slot_order=("grok", "luna", "spark"),
        recover_predecessors=True,
        architect_read_only=True,
        slots={
            "grok": SlotSpec(
                source_key="grok",
                agent_id="",
                display_name="Grok 4.6",
                role="temporary chief architect and reviewer",
                lane="lead architect",
            ),
            "luna": SlotSpec(
                source_key="luna",
                agent_id="",
                display_name="Codex Luna",
                role="implementation owner and debugger",
                lane="primary worker",
            ),
            "spark": SlotSpec(
                source_key="spark",
                agent_id="",
                display_name="Codex Spark",
                role="micro-implementation worker",
                lane="micro worker",
            ),
        },
    ),
    "combat": ProfileSpec(
        id="combat",
        label="Combat / normal development",
        short_label="COMBAT",
        eyebrow="STABLE DEVELOPMENT",
        description=(
            "Sol owns architecture and the evolving backlog, SolGoodman implements and debugs, "
            "and Grok can be attached as a fast optional assistant with an independent viewpoint."
        ),
        directive_name="COMBAT_OPERATIONS_DIRECTIVE.md",
        default_goal=(
            "Reconcile and improve the current Friday backlog, then implement it task by task "
            "with Sol as lead architect and SolGoodman as implementation owner."
        ),
        architect_key="grok",
        primary_worker_key="luna",
        worker_keys=("luna", "spark"),
        slot_order=("grok", "luna", "spark"),
        recover_predecessors=False,
        # Sol is deliberately granted the operator's normal ultracode/full-access
        # execution mode. The architect checkout remains disposable and is hard
        # reset after every turn; accepted product changes still flow through a
        # separately reviewed worker packet.
        architect_read_only=False,
        slots={
            # Logical slot names stay stable so the existing closed loop remains small.
            # In combat, the architect slot is backed by the normal `codex` account.
            "grok": SlotSpec(
                source_key="spark",
                agent_id="combat-sol",
                display_name="Sol",
                role="lead architect, backlog owner, reviewer, and integration authority",
                lane="lead architect",
            ),
            "luna": SlotSpec(
                source_key="luna",
                agent_id="combat-solgoodman",
                display_name="SolGoodman",
                role="principal implementation engineer and debugger",
                lane="implementation owner",
            ),
            # The secondary worker slot is backed by Grok and remains optional.
            "spark": SlotSpec(
                source_key="grok",
                agent_id="combat-grok-helper",
                display_name="Grok 4.6",
                role="optional fast implementation assistant and alternate perspective",
                lane="optional assistant",
                optional=True,
            ),
        },
    ),
}


def get_profile(profile_id: str) -> ProfileSpec:
    try:
        return _PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(f"Unknown operating profile: {profile_id}") from exc


def resolve_profile_agents(
    templates: dict[str, AgentConfig],
    profile_config: ProfilesConfig,
    profile_id: str,
    combat_grok_enabled: bool,
) -> dict[str, AgentConfig]:
    """Map stable logical slots onto the physical subscription-backed CLIs."""
    spec = get_profile(profile_id)
    resolved: dict[str, AgentConfig] = {}
    for logical_key in LOGICAL_AGENT_KEYS:
        slot = spec.slots[logical_key]
        config = deepcopy(templates[slot.source_key])
        if slot.agent_id:
            config.id = slot.agent_id
        config.display_name = slot.display_name
        config.role = slot.role
        config.lane = slot.lane
        config.physical_key = slot.source_key
        config.optional = slot.optional
        if profile_id == "reserve":
            if logical_key == "luna":
                config.effort = profile_config.reserve_luna_effort
                config.unsafe_full_access = profile_config.reserve_luna_full_access
        else:
            if logical_key == "grok":
                config.model = profile_config.combat_sol_model
                config.effort = profile_config.combat_sol_effort
                config.unsafe_full_access = profile_config.combat_sol_full_access
            elif logical_key == "luna":
                config.model = profile_config.combat_goodman_model
                config.effort = profile_config.combat_goodman_effort
                config.unsafe_full_access = profile_config.combat_goodman_full_access
            else:
                config.model = profile_config.combat_grok_model
                config.effort = profile_config.combat_grok_effort
                config.enabled = bool(config.enabled and combat_grok_enabled)
        if config.effort and config.effort not in config.effort_options:
            config.effort_options.append(config.effort)
        resolved[logical_key] = config
    return resolved


def profile_public_dict(
    spec: ProfileSpec,
    agents: dict[str, AgentConfig],
    combat_grok_enabled: bool,
) -> dict[str, Any]:
    return {
        "id": spec.id,
        "label": spec.label,
        "short_label": spec.short_label,
        "eyebrow": spec.eyebrow,
        "description": spec.description,
        "directive_name": spec.directive_name,
        "default_goal": spec.default_goal,
        "architect_key": spec.architect_key,
        "primary_worker_key": spec.primary_worker_key,
        "worker_keys": list(spec.worker_keys),
        "slot_order": list(spec.slot_order),
        "recover_predecessors": spec.recover_predecessors,
        "architect_read_only": spec.architect_read_only,
        "combat_grok_enabled": combat_grok_enabled,
        "agents": {
            key: {
                "key": key,
                "id": config.id,
                "display_name": config.display_name,
                "role": config.role,
                "lane": config.lane,
                "adapter": config.adapter,
                "physical_key": config.physical_key,
                "binary_label": config.binary_candidates[0] if config.binary_candidates else "",
                "model": config.model,
                "effort": config.effort,
                "enabled": config.enabled,
                "optional": config.optional,
                "unsafe_full_access": config.unsafe_full_access,
            }
            for key, config in agents.items()
        },
    }


def profile_catalog(profile_config: ProfilesConfig, combat_grok_enabled: bool) -> list[dict[str, Any]]:
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "short_label": spec.short_label,
            "eyebrow": spec.eyebrow,
            "description": spec.description,
            "default": spec.id == profile_config.default,
            "combat_grok_enabled": combat_grok_enabled if spec.id == "combat" else True,
        }
        for spec in _PROFILES.values()
    ]


def profile_prompt_context(
    spec: ProfileSpec,
    agents: dict[str, AgentConfig],
    *,
    repository: str = "",
    operational_roots: tuple[str, ...] | list[str] = (),
) -> str:
    lines = [
        "# Active Sol Link operating profile",
        f"Profile: `{spec.id}` ({spec.label})",
        spec.description,
        "",
        "The machine contract keeps stable logical lane keys even when the physical CLI changes:",
    ]
    for key in spec.slot_order:
        config = agents[key]
        availability = "enabled" if config.enabled else "disabled"
        access = "full access" if config.unsafe_full_access else "sandboxed"
        worker_hint = ""
        if key in spec.worker_keys:
            worker_hint = f"; dispatch with worker `{key}`"
        lines.append(
            f"- `{key}`: {config.display_name}, {config.role}, backed by "
            f"`{config.binary_candidates[0] if config.binary_candidates else 'unresolved'}` "
            f"({availability}; {access}{worker_hint})."
        )
    lines += [
        "",
        f"The lead architect is {agents[spec.architect_key].display_name}. "
        "Workers never reinterpret logical keys from another profile.",
    ]
    roots = [str(root).strip() for root in operational_roots if str(root).strip()]
    if repository or roots:
        lines += ["", "# Known Jericho work surfaces"]
        if repository:
            lines.append(f"- Product repository: `{repository}`")
        lines.extend(f"- Additional operational root: `{root}`" for root in roots)
        lines += [
            "Do not assume relevant history or operational evidence was created from "
            "the repository cwd. Sessions, handoffs, watcher state, and supporting "
            "artifacts may originate from any listed root.",
            "Additional operational roots are continuity context, not implicit Git "
            "write or integration scopes. Persistent product changes still follow "
            "the active task packet and reviewed integration path.",
        ]
    return "\n".join(lines)
