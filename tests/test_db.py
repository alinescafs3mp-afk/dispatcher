from __future__ import annotations

from pathlib import Path

from nightshift.db import StateDB


def test_agent_upsert_preserves_session(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.sqlite3")
    try:
        db.upsert_agent("grok", "architect", "idle", session_id="session-a")
        db.upsert_agent("grok", "architect", "working", session_id="")
        assert db.query("SELECT session_id FROM agents WHERE id='grok'")[0]["session_id"] == "session-a"
        db.update_agent("grok", session_id="")
        assert db.query("SELECT session_id FROM agents WHERE id='grok'")[0]["session_id"] == ""
    finally:
        db.close()


def test_task_json_is_redacted(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.sqlite3")
    token = "ghp_123456789012345678901234567890123456"
    try:
        db.create_mission("m", "/repo", f"goal {token}", "created")
        db.create_task("m", "t", {"title": "x", "worker": "spark", "risk": "low", "context": token}, "ready")
        db.update_task("t", result_json={"secret": token})
        task = db.query("SELECT packet_json,result_json FROM tasks WHERE id='t'")[0]
        mission = db.query("SELECT goal FROM missions WHERE id='m'")[0]
        assert token not in task["packet_json"]
        assert token not in task["result_json"]
        assert token not in mission["goal"]
    finally:
        db.close()


def test_task_worker_can_follow_runtime_fallback(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.sqlite3")
    try:
        db.create_mission("m", "/repo", "goal", "running")
        db.create_task(
            "m", "t", {"title": "x", "worker": "spark", "risk": "low"}, "ready"
        )
        db.update_task("t", worker="luna")
        assert db.query("SELECT worker FROM tasks WHERE id='t'")[0]["worker"] == "luna"
    finally:
        db.close()


def test_preferences_and_snapshot(tmp_path: Path) -> None:
    db = StateDB(tmp_path / "state.sqlite3")
    try:
        db.set_preference("agent.grok.effort", "xhigh")
        assert db.get_preference("agent.grok.effort") == "xhigh"
        assert db.get_preference("missing", "fallback") == "fallback"
        db.upsert_agent("grok", "architect", "idle", metadata={"effort": "xhigh"})
        snap = db.snapshot()
        assert snap["agents"][0]["metadata"]["effort"] == "xhigh"
    finally:
        db.close()
