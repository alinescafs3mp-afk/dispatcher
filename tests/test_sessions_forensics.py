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
