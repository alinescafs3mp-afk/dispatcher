from __future__ import annotations

from pathlib import Path

from nightshift.__main__ import _browser_environment
from nightshift.config import default_settings, load_settings, render_example, sanitized_child_env


def test_default_subscription_wiring() -> None:
    settings = default_settings("/tmp/friday")
    assert settings.agent("spark").binary_candidates == ["codex"]
    assert settings.agent("spark").model == "gpt-5.3-codex-spark"
    assert settings.agent("luna").binary_candidates == ["codex-solgoodman"]
    assert settings.agent("luna").model == "gpt-5.6-luna"
    assert settings.agent("luna").unsafe_full_access is True
    assert settings.agent("spark").unsafe_full_access is True
    assert settings.agent("grok").model == "grok-4.6"
    assert settings.agent("grok").unsafe_full_access is True
    assert settings.project.operational_roots == ["~/.jericho"]
    assert settings.server.host == "0.0.0.0"


def test_default_reasoning_menus() -> None:
    settings = default_settings()
    assert settings.agent("grok").effort == "xhigh"
    assert "xhigh" in settings.agent("spark").effort_options
    assert settings.agent("luna").effort == "max"
    assert settings.agent("luna").effort_options[-1] == "max"
    assert settings.profiles.reserve_luna_effort == "max"
    assert settings.profiles.combat_sol_effort == "ultra"
    assert settings.profiles.combat_goodman_effort == "ultra"


def test_render_example_round_trip(tmp_path: Path) -> None:
    cfg = tmp_path / "nightshift.toml"
    cfg.write_text(render_example(str(tmp_path / "friday")), encoding="utf-8")
    settings = load_settings(cfg)
    assert settings.project.repo == str(tmp_path / "friday")
    assert settings.agent("grok").scrub_sensitive_env is True
    assert settings.agent("spark").inherit_previous_session is True
    assert settings.profiles.reserve_luna_effort == "max"
    assert settings.profiles.reserve_grok_full_access is True
    assert settings.profiles.reserve_luna_full_access is True
    assert settings.profiles.reserve_spark_full_access is True
    assert settings.profiles.combat_sol_full_access is True
    assert settings.profiles.combat_goodman_full_access is True
    assert settings.profiles.combat_grok_full_access is True
    assert settings.project.operational_roots == ["~/.jericho"]
    assert settings.server.host == "0.0.0.0"


def test_operational_roots_expand_and_deduplicate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    support = tmp_path / "support"
    settings = default_settings(str(repo))
    settings.project.operational_roots = [
        str(support),
        str(repo),
        str(support),
        "",
    ]

    assert settings.project.operational_root_entries == [(str(support), support.resolve())]
    assert settings.project.known_working_paths == [repo.resolve(), support.resolve()]
    public = settings.public_dict()["project"]
    assert public["operational_roots"] == settings.project.operational_roots
    assert public["known_working_paths"] == [str(repo.resolve()), str(support.resolve())]


def test_sanitized_child_env_removes_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-12345678901234567890")
    monkeypatch.setenv("CUSTOM_PASSWORD", "very-secret")
    monkeypatch.setenv("GH_TOKEN", "ghp_123456789012345678901234567890")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/tmp/home")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex")
    env = sanitized_child_env(extra={"CI": "1"})
    assert "OPENAI_API_KEY" not in env
    assert "CUSTOM_PASSWORD" not in env
    assert "GH_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/tmp/home"
    assert env["CODEX_HOME"] == "/tmp/codex"
    assert env["CI"] == "1"


def test_browser_environment_removes_obsolete_atk_bridge(monkeypatch) -> None:
    monkeypatch.setenv("GTK_MODULES", "atk-bridge:canberra-gtk-module")
    monkeypatch.setenv("GTK3_MODULES", "atk-bridge")
    env = _browser_environment()
    assert env["GTK_MODULES"] == "canberra-gtk-module"
    assert "GTK3_MODULES" not in env


def test_agent_explicit_strip_applies(monkeypatch) -> None:
    monkeypatch.setenv("NOT_SECRET_BY_NAME", "remove-me")
    settings = default_settings()
    agent = settings.agent("grok")
    agent.strip_env.append("NOT_SECRET_BY_NAME")
    assert "NOT_SECRET_BY_NAME" not in agent.subprocess_env()
