from __future__ import annotations

import os
import re
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BACKLOG_GLOBS = [
    "BACKLOG*.md",
    "**/BACKLOG*.md",
    "TODO*.md",
    "**/TODO*.md",
    "ROADMAP*.md",
    "**/ROADMAP*.md",
    "HANDOFF*.md",
    "**/HANDOFF*.md",
    "outer_sol/**/*.md",
    "handoffs/**/*.md",
    ".sol-link/**/*.md",
]

# Keep the official CLI's persisted subscription login authoritative. These
# variables can silently switch a CLI from a consumer subscription to metered
# API billing, which is the opposite of what this dispatcher is for.
CODEX_API_ENV_VARS = [
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "AZURE_OPENAI_API_KEY",
    "OPENAI_BASE_URL",
]
GROK_API_ENV_VARS = [
    "XAI_API_KEY",
    "GROK_CODE_XAI_API_KEY",
]

_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_?KEY|"
    r"COOKIE|AUTH|SESSION)(?:_|$)",
    re.I,
)


def sanitized_child_env(
    *,
    strip: list[str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a usable process environment without ambient credential variables."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if _SENSITIVE_ENV_NAME.search(key):
            continue
        env[key] = value
    for name in strip or []:
        env.pop(name, None)
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


@dataclass(slots=True)
class AgentConfig:
    id: str
    role: str
    binary_candidates: list[str]
    adapter: str = "codex"
    display_name: str = ""
    lane: str = ""
    physical_key: str = ""
    optional: bool = False
    model: str = ""
    effort: str = ""
    effort_options: list[str] = field(default_factory=list)
    timeout_seconds: int = 7200
    max_turns: int = 80
    extra_args: list[str] = field(default_factory=list)
    quota_command: list[str] = field(default_factory=list)
    strip_env: list[str] = field(default_factory=list)
    scrub_sensitive_env: bool = True
    inherit_previous_session: bool = False
    enabled: bool = True
    unsafe_full_access: bool = False

    def resolve_binary(self) -> str:
        for candidate in self.binary_candidates:
            expanded = os.path.expanduser(candidate)
            if os.path.isabs(expanded) and os.access(expanded, os.X_OK):
                return expanded
            found = shutil.which(expanded)
            if found:
                return found
        return ""

    def subprocess_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Return a complete child environment with subscription auth authoritative."""
        if self.scrub_sensitive_env:
            return sanitized_child_env(strip=self.strip_env, extra=extra)
        env = os.environ.copy()
        for name in self.strip_env:
            env.pop(name, None)
        if extra:
            env.update({str(key): str(value) for key, value in extra.items()})
        return env


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    open_browser: bool = True


@dataclass(slots=True)
class ProjectConfig:
    repo: str = ""
    # Additional Jericho work surfaces used for continuity discovery. They are
    # context roots, not implicit Git integration or write scopes.
    operational_roots: list[str] = field(default_factory=lambda: ["~/.jericho"])
    backlog_globs: list[str] = field(default_factory=lambda: list(DEFAULT_BACKLOG_GLOBS))
    validation_commands: list[str] = field(default_factory=list)
    protected_paths: list[str] = field(default_factory=lambda: [
        ".git/**", ".env", ".env.*", "**/*.pem", "**/*.key",
        "**/id_rsa*", "**/*.p12", "**/*.token",
    ])
    high_risk_paths: list[str] = field(default_factory=lambda: [
        "**/migrations/**", "**/security/**", "**/auth/**", "**/sandbox/**",
        "**/engineer_mode/**", "**/permissions/**", "**/secrets/**",
    ])
    session_search_roots: list[str] = field(default_factory=lambda: [
        "~/.codex/sessions",
        "~/.config/codex-multi/profiles/*/sessions",
    ])

    @property
    def repo_path(self) -> Path:
        return Path(os.path.expanduser(self.repo)).resolve()

    @property
    def operational_root_entries(self) -> list[tuple[str, Path]]:
        repo = self.repo_path
        entries: list[tuple[str, Path]] = []
        seen = {repo}
        for raw in self.operational_roots:
            value = str(raw).strip()
            if not value:
                continue
            try:
                path = Path(os.path.expandvars(os.path.expanduser(value))).resolve()
            except (OSError, RuntimeError):
                continue
            if path in seen:
                continue
            seen.add(path)
            entries.append((value, path))
        return entries

    @property
    def operational_paths(self) -> list[Path]:
        return [path for _, path in self.operational_root_entries]

    @property
    def known_working_paths(self) -> list[Path]:
        return [self.repo_path, *self.operational_paths]


@dataclass(slots=True)
class ProfilesConfig:
    default: str = "reserve"
    combat_grok_enabled: bool = False
    # Every automated reserve participant is trusted with the operator's full
    # host-access posture. Direct chats and recovery handoffs remain read-only.
    reserve_grok_full_access: bool = True
    reserve_luna_effort: str = "max"
    reserve_luna_full_access: bool = True
    reserve_spark_full_access: bool = True
    # Empty combat Codex model names intentionally delegate model selection to
    # the corresponding authenticated wrapper/profile. Both normal Codex lanes
    # still force Ultra reasoning, the CLI's ultracode tier.
    combat_sol_model: str = ""
    combat_sol_effort: str = "ultra"
    combat_sol_full_access: bool = True
    combat_goodman_model: str = ""
    combat_goodman_effort: str = "ultra"
    combat_goodman_full_access: bool = True
    combat_grok_model: str = "grok-4.6"
    combat_grok_effort: str = "xhigh"
    combat_grok_full_access: bool = True


@dataclass(slots=True)
class OrchestratorConfig:
    runtime_dir: str = "~/.local/state/sol-link-nightshift"
    max_tasks: int = 100
    max_revisions: int = 2
    architect_session_max_turns: int = 16
    recover_predecessor_sessions: bool = True
    continue_until_backlog_done: bool = True
    auto_accept_low_risk: bool = True
    auto_accept_medium_risk: bool = False
    require_human_for_high_risk: bool = True
    copy_untracked_max_file_mb: int = 25
    copy_untracked_total_mb: int = 250
    log_tail_lines: int = 1000
    command_timeout_seconds: int = 3600
    poll_seconds: float = 1.0

    @property
    def runtime_path(self) -> Path:
        return Path(os.path.expanduser(self.runtime_dir)).resolve()


@dataclass(slots=True)
class Settings:
    server: ServerConfig
    project: ProjectConfig
    profiles: ProfilesConfig
    orchestrator: OrchestratorConfig
    agents: dict[str, AgentConfig]
    config_path: Path | None = None

    def agent(self, key: str) -> AgentConfig:
        return self.agents[key]

    def public_dict(self) -> dict[str, Any]:
        """Return only configuration fields required by the browser dashboard."""
        return {
            "server": asdict(self.server),
            "project": {
                "repo": self.project.repo,
                "operational_roots": list(self.project.operational_roots),
                "known_working_paths": [
                    str(path) for path in self.project.known_working_paths
                ],
                "protected_paths": list(self.project.protected_paths),
                "high_risk_paths": list(self.project.high_risk_paths),
            },
            "profiles": asdict(self.profiles),
            "orchestrator": asdict(self.orchestrator),
            "agents": {
                key: {
                    "id": value.id,
                    "role": value.role,
                    "display_name": value.display_name,
                    "lane": value.lane,
                    "adapter": value.adapter,
                    "physical_key": value.physical_key,
                    "optional": value.optional,
                    "binary_label": value.binary_candidates[0] if value.binary_candidates else "",
                    "model": value.model,
                    "effort": value.effort,
                    "effort_options": list(value.effort_options),
                    "enabled": value.enabled,
                    "timeout_seconds": value.timeout_seconds,
                    "max_turns": value.max_turns,
                    "unsafe_full_access": value.unsafe_full_access,
                }
                for key, value in self.agents.items()
            },
            "config_path": str(self.config_path or ""),
        }


def default_settings(repo: str = "") -> Settings:
    return Settings(
        server=ServerConfig(),
        project=ProjectConfig(repo=repo),
        profiles=ProfilesConfig(),
        orchestrator=OrchestratorConfig(),
        agents={
            "grok": AgentConfig(
                id="grok-architect",
                role="temporary chief architect and reviewer",
                display_name="Grok 4.6",
                lane="lead architect",
                physical_key="grok",
                adapter="grok",
                binary_candidates=["grok-build", "grok"],
                model="grok-4.6",
                effort="xhigh",
                effort_options=["low", "medium", "high", "xhigh"],
                strip_env=list(GROK_API_ENV_VARS),
                inherit_previous_session=False,
                unsafe_full_access=True,
            ),
            "spark": AgentConfig(
                id="codex-spark",
                role="micro-implementation worker",
                display_name="Codex Spark",
                lane="micro worker",
                physical_key="spark",
                adapter="codex",
                binary_candidates=["codex"],
                model="gpt-5.3-codex-spark",
                effort="high",
                effort_options=["low", "medium", "high", "xhigh"],
                strip_env=list(CODEX_API_ENV_VARS),
                inherit_previous_session=True,
                unsafe_full_access=True,
            ),
            "luna": AgentConfig(
                id="codex-luna",
                role="implementation owner and debugger",
                display_name="Codex Luna",
                lane="primary worker",
                physical_key="luna",
                adapter="codex",
                binary_candidates=["codex-solgoodman"],
                model="gpt-5.6-luna",
                effort="max",
                effort_options=[
                    "none", "low", "medium", "high", "xhigh", "max",
                ],
                strip_env=list(CODEX_API_ENV_VARS),
                inherit_previous_session=True,
                unsafe_full_access=True,
            ),
        },
    )


def _merge_dataclass(instance: Any, data: dict[str, Any]) -> Any:
    for key, value in data.items():
        if hasattr(instance, key):
            setattr(instance, key, value)
    return instance


def load_settings(path: str | Path | None = None, repo_override: str = "") -> Settings:
    settings = default_settings(repo_override)
    cfg_path = Path(path).expanduser().resolve() if path else Path("nightshift.toml").resolve()
    if cfg_path.exists():
        with cfg_path.open("rb") as handle:
            raw = tomllib.load(handle)
        _merge_dataclass(settings.server, raw.get("server", {}))
        _merge_dataclass(settings.project, raw.get("project", {}))
        _merge_dataclass(settings.profiles, raw.get("profiles", {}))
        _merge_dataclass(settings.orchestrator, raw.get("orchestrator", {}))
        for key, data in raw.get("agents", {}).items():
            if key in settings.agents and isinstance(data, dict):
                _merge_dataclass(settings.agents[key], data)
        settings.config_path = cfg_path
    if repo_override:
        settings.project.repo = repo_override
    return settings


def render_example(repo: str = "/path/to/friday") -> str:
    return f'''# Sol Link Dispatcher configuration\n\n[server]\nhost = "127.0.0.1"\nport = 8787\nopen_browser = true\n\n[project]\nrepo = "{repo}"\noperational_roots = ["~/.jericho"]\nbacklog_globs = ["BACKLOG*.md", "**/BACKLOG*.md", "TODO*.md", "**/TODO*.md", "ROADMAP*.md", "**/ROADMAP*.md", "HANDOFF*.md", "**/HANDOFF*.md", "outer_sol/**/*.md", "handoffs/**/*.md", ".sol-link/**/*.md"]\nvalidation_commands = []\nprotected_paths = [".git/**", ".env", ".env.*", "**/*.pem", "**/*.key", "**/*.token"]\nhigh_risk_paths = ["**/migrations/**", "**/security/**", "**/auth/**", "**/sandbox/**", "**/engineer_mode/**", "**/permissions/**"]\nsession_search_roots = ["~/.codex/sessions", "~/.config/codex-multi/profiles/*/sessions"]\n\n[profiles]\ndefault = "reserve"\ncombat_grok_enabled = false\nreserve_grok_full_access = true\nreserve_luna_effort = "max"\nreserve_luna_full_access = true\nreserve_spark_full_access = true\n# Empty model names let the authenticated codex wrappers select their normal model.\ncombat_sol_model = ""\ncombat_sol_effort = "ultra"\ncombat_sol_full_access = true\ncombat_goodman_model = ""\ncombat_goodman_effort = "ultra"\ncombat_goodman_full_access = true\ncombat_grok_model = "grok-4.6"\ncombat_grok_effort = "xhigh"\ncombat_grok_full_access = true\n\n[orchestrator]\nruntime_dir = "~/.local/state/sol-link-nightshift"\nmax_tasks = 100\nmax_revisions = 2\narchitect_session_max_turns = 16\nrecover_predecessor_sessions = true\ncontinue_until_backlog_done = true\nauto_accept_low_risk = true\nauto_accept_medium_risk = false\nrequire_human_for_high_risk = true\ncopy_untracked_max_file_mb = 25\ncopy_untracked_total_mb = 250\nlog_tail_lines = 1000\ncommand_timeout_seconds = 3600\npoll_seconds = 1.0\n\n[agents.grok]\nid = "grok-architect"\nrole = "temporary chief architect and reviewer"\nadapter = "grok"\nbinary_candidates = ["grok-build", "grok"]\nmodel = "grok-4.6"\neffort = "xhigh"\neffort_options = ["low", "medium", "high", "xhigh"]\nstrip_env = ["XAI_API_KEY", "GROK_CODE_XAI_API_KEY"]\nscrub_sensitive_env = true\ntimeout_seconds = 7200\nmax_turns = 80\nextra_args = []\nquota_command = []\ninherit_previous_session = false\nenabled = true\nunsafe_full_access = true\n\n[agents.spark]\nid = "codex-spark"\nrole = "micro-implementation worker"\nadapter = "codex"\nbinary_candidates = ["codex"]\nmodel = "gpt-5.3-codex-spark"\neffort = "high"\neffort_options = ["low", "medium", "high", "xhigh"]\nstrip_env = ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "AZURE_OPENAI_API_KEY", "OPENAI_BASE_URL"]\nscrub_sensitive_env = true\ntimeout_seconds = 7200\nextra_args = []\nquota_command = []\ninherit_previous_session = true\nenabled = true\nunsafe_full_access = true\n\n[agents.luna]\nid = "codex-luna"\nrole = "implementation owner and debugger"\nadapter = "codex"\nbinary_candidates = ["codex-solgoodman"]\nmodel = "gpt-5.6-luna"\neffort = "max"\neffort_options = ["none", "low", "medium", "high", "xhigh", "max"]\nstrip_env = ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "AZURE_OPENAI_API_KEY", "OPENAI_BASE_URL"]\nscrub_sensitive_env = true\ntimeout_seconds = 7200\nextra_args = []\nquota_command = []\ninherit_previous_session = true\nenabled = true\nunsafe_full_access = true\n'''
