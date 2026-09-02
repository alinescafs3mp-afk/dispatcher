from __future__ import annotations

import json
from pathlib import Path

from nightshift.config import default_settings
from nightshift.forensics import ForensicsScanner
from nightshift.sessions import discover_sessions, summarize_session


def test_session_summary_extracts_account_thread_and_redacts(git_repo: Path, tmp_path: Path) -> None:
    token = "ghp_123456789012345678901234567890123456"
    path = tmp_path / "rollout-550e8400-e29b-41d4-a716-446655440000.jsonl"
    events = [
        {"type": "session_meta", "payload": {"id": "550e8400-e29b-41d4-a716-446655440000", "cwd": str(git_repo), "model": "sol"}},
        {"payload": {"role": "user", "content": "continue Friday"}},
        {"payload": {"role": "assistant", "content": f"done {token}"}},
    ]
    path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    summary = summarize_session(path, git_repo)
    assert summary is not None
    assert summary.session_id.startswith("550e")
    assert summary.score >= 100
    assert token not in summary.last_assistant


def test_discover_sessions_orders_repo_match(git_repo: Path, tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    for index, cwd in enumerate(["/tmp/other", str(git_repo)]):
        path = root / f"s{index}.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": f"id-{index}", "cwd": cwd}}), encoding="utf-8")
    found = discover_sessions([str(root)], git_repo)
    assert found[0].cwd == str(git_repo)


def test_discover_sessions_accepts_additional_working_root(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    operational_root = tmp_path / ".jericho"
    operational_root.mkdir()
    unrelated = session_root / "unrelated.jsonl"
    unrelated.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "other", "cwd": "/tmp/unrelated"},
            }
        ),
        encoding="utf-8",
    )
    relevant = session_root / "operational.jsonl"
    relevant.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "jericho-root",
                    "cwd": str(operational_root / "sol"),
                },
            }
        ),
        encoding="utf-8",
    )

    found = discover_sessions(
        [str(session_root)],
        git_repo,
        working_roots=[git_repo, operational_root],
    )

    assert found[0].session_id == "jericho-root"
    assert found[0].score >= 90
    assert found[0].matched_root == str(operational_root.resolve())


def test_forensics_scans_configured_operational_roots(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    operational_root = tmp_path / ".jericho"
    operational_root.mkdir()
    (operational_root / "HANDOFF_SOL.md").write_text(
        "Continue from the operational root.\n",
        encoding="utf-8",
    )
    watcher = operational_root / ".sol-link" / "watch-state.json"
    watcher.parent.mkdir()
    watcher.write_text('{"cursor": 17}\n', encoding="utf-8")
    (operational_root / "unrelated.txt").write_text(
        "must not be harvested",
        encoding="utf-8",
    )
    git_metadata = operational_root / ".git" / "HANDOFF_INTERNAL.md"
    git_metadata.parent.mkdir()
    git_metadata.write_text("must stay private to git metadata", encoding="utf-8")
    outside = tmp_path / "HANDOFF_OUTSIDE.md"
    outside.write_text("must not cross the operational root boundary", encoding="utf-8")
    (operational_root / "HANDOFF_ESCAPE.md").symlink_to(outside)

    settings = default_settings(str(git_repo))
    settings.project.operational_roots = [str(operational_root)]
    scanner = ForensicsScanner(settings, tmp_path / "mission")
    report = scanner.scan(include_sessions=False)
    markdown = Path(report["markdown_path"]).read_text(encoding="utf-8")

    assert any(
        item["configured"] == str(operational_root) and item["available"]
        for item in report["working_roots"]
    )
    assert any(
        item["path"].endswith("HANDOFF_SOL.md")
        and "operational root" in item["excerpt"]
        for item in report["backlog_files"]
    )
    assert any(
        item["path"].endswith(".sol-link/watch-state.json")
        and "17" in item["content"]
        for item in report["watcher_state"]
    )
    assert "unrelated.txt" not in markdown
    assert "HANDOFF_INTERNAL.md" not in markdown
    assert "HANDOFF_ESCAPE.md" not in markdown
    assert str(operational_root) in markdown


def test_forensics_writes_dossier_and_filters_protected_untracked(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / "BACKLOG.md").write_text("- continue recovery\n", encoding="utf-8")
    (git_repo / ".env").write_text("PASSWORD=abcdefghijklmnop\n", encoding="utf-8")
    settings = default_settings(str(git_repo))
    scanner = ForensicsScanner(settings, tmp_path / "mission")
    report = scanner.scan()
    markdown = Path(report["markdown_path"]).read_text(encoding="utf-8")
    assert "BACKLOG.md" in markdown
    assert "continue recovery" in markdown
    assert ".env" not in "\n".join(
        path for wt in report["worktrees"] for path in wt.get("untracked", [])
    )
    assert Path(report["json_path"]).exists()
