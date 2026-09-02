from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nightshift.config import default_settings
from nightshift.git import (
    GitError,
    MissionWorkspace,
    assess_risk,
    git,
    path_violations,
    safe_git_diff,
)
from nightshift.models import RiskLevel


def commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)
    return git(repo, "rev-parse", "HEAD").strip()


def test_scope_and_risk_rules() -> None:
    violations = path_violations(
        ["src/a.py", ".env", "docs/x.md"],
        ["src/**"], ["src/generated/**"], [".env", "**/*.key"], max_files=2,
    )
    assert any("maximum" in item for item in violations)
    assert any("protected" in item for item in violations)
    assert any("outside allowed" in item for item in violations)
    assert assess_risk(["src/security/auth.py"], "+x\n", RiskLevel.LOW, ["**/security/**"]) == RiskLevel.HIGH


def test_safe_diff_excludes_protected(git_repo: Path) -> None:
    (git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    (git_repo / ".env").write_text("PASSWORD=abcdefghijklmnop\n", encoding="utf-8")
    patch = safe_git_diff(git_repo, [".env"])
    assert "README.md" in patch
    assert ".env" not in patch


def test_prepare_rescues_dirty_source_without_modifying_it(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / "README.md").write_text("dirty source\n", encoding="utf-8")
    (git_repo / "new.txt").write_text("untracked\n", encoding="utf-8")
    settings = default_settings(str(git_repo))
    settings.orchestrator.runtime_dir = str(tmp_path / "runtime")
    workspace = MissionWorkspace(settings, "m1", tmp_path / "runtime" / "m1")
    prepared = workspace.prepare()
    assert (git_repo / "README.md").read_text() == "dirty source\n"
    assert (workspace.integration_path / "README.md").read_text() == "dirty source\n"
    assert (workspace.integration_path / "new.txt").read_text() == "untracked\n"
    assert prepared["rescue"]["snapshot_committed"] is True
    workspace.cleanup(keep_integration=True)


def test_prepare_preserves_rename(git_repo: Path, tmp_path: Path) -> None:
    old = git_repo / "README.md"
    new = git_repo / "RENAMED.md"
    old.rename(new)
    settings = default_settings(str(git_repo))
    workspace = MissionWorkspace(settings, "m2", tmp_path / "m2")
    workspace.prepare()
    assert not (workspace.integration_path / "README.md").exists()
    assert (workspace.integration_path / "RENAMED.md").exists()
    workspace.cleanup(keep_integration=True)


def test_rescue_skips_secret_file(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / "secret.txt").write_text("api_key=abcdefghijklmnopqrstu\n", encoding="utf-8")
    settings = default_settings(str(git_repo))
    workspace = MissionWorkspace(settings, "m3", tmp_path / "m3")
    prepared = workspace.prepare()
    assert not (workspace.integration_path / "secret.txt").exists()
    assert any("credential-shaped" in item for item in prepared["rescue"]["skipped_untracked"])
    workspace.cleanup(keep_integration=True)


def test_worker_sanitizes_and_integrates(git_repo: Path, tmp_path: Path) -> None:
    settings = default_settings(str(git_repo))
    workspace = MissionWorkspace(settings, "m4", tmp_path / "m4")
    workspace.prepare()
    tree = workspace.create_worker("task", "spark")
    (tree.path / "feature.py").write_text("answer = 42\n", encoding="utf-8")
    (tree.path / ".env").write_text("PASSWORD=abcdefghijklmnop\n", encoding="utf-8")
    violations = workspace.sanitize_worker_changes(tree)
    assert any("protected path" in item for item in violations)
    assert not (tree.path / ".env").exists()
    head = workspace.commit_worker(tree, "feature")
    assert head != tree.base_sha
    integrated = workspace.integrate_worker(tree, "integrate feature")
    assert integrated == workspace.integration_head()
    assert (workspace.integration_path / "feature.py").exists()
    workspace.cleanup(keep_integration=True)


def test_worker_discards_credential_content(git_repo: Path, tmp_path: Path) -> None:
    settings = default_settings(str(git_repo))
    workspace = MissionWorkspace(settings, "m5", tmp_path / "m5")
    workspace.prepare()
    tree = workspace.create_worker("task", "luna")
    (tree.path / "oops.txt").write_text("github_pat_abcdefghijklmnopqrstuvwxyz123456\n", encoding="utf-8")
    violations = workspace.sanitize_worker_changes(tree)
    assert any("credential-shaped" in item for item in violations)
    assert not (tree.path / "oops.txt").exists()
    workspace.cleanup(keep_integration=True)


def test_integration_refuses_moved_head(git_repo: Path, tmp_path: Path) -> None:
    settings = default_settings(str(git_repo))
    workspace = MissionWorkspace(settings, "m6", tmp_path / "m6")
    workspace.prepare()
    tree = workspace.create_worker("task", "spark")
    (tree.path / "worker.txt").write_text("worker\n", encoding="utf-8")
    workspace.commit_worker(tree, "worker")
    (workspace.integration_path / "other.txt").write_text("other\n", encoding="utf-8")
    commit(workspace.integration_path, "move integration")
    with pytest.raises(GitError, match="Integration HEAD moved"):
        workspace.integrate_worker(tree, "nope")
    workspace.cleanup(keep_integration=True)
