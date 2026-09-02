from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import dispatcher


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.decode().strip()


def test_default_profile_mapping_matches_requested_commands() -> None:
    cfg = dispatcher.load_config(None)
    assert cfg.agents["grok"].command == ["grok-build"]
    assert cfg.agents["luna"].command == ["codex-solgoodman"]
    assert cfg.agents["spark"].command == ["codex"]
    assert cfg.agents["grok"].reasoning == "xhigh"
    assert cfg.agents["luna"].model == "gpt-5.6-luna"
    assert cfg.agents["spark"].model == "gpt-5.3-codex-spark"


def test_toml_overrides_and_shell_free_command_array(tmp_path: Path) -> None:
    config = tmp_path / "dispatcher.toml"
    config.write_text(
        """
[server]
port = 9911
state_dir = "~/tmp/friday-dispatcher-test"
max_tasks = 7

[agents.luna]
command = ["/opt/bin/codex-luna", "--profile", "reserve"]
model = ""
reasoning = "max"

[sol_link]
enabled = true
inbox = "~/sol-link/events.jsonl"
""",
        encoding="utf-8",
    )
    cfg = dispatcher.load_config(config)
    assert cfg.port == 9911
    assert cfg.max_tasks == 7
    assert cfg.agents["luna"].command == ["/opt/bin/codex-luna", "--profile", "reserve"]
    assert cfg.agents["luna"].model == ""
    assert cfg.agents["luna"].reasoning == "max"
    assert cfg.sol_link.enabled is True
    assert cfg.sol_link.inbox is not None and cfg.sol_link.inbox.name == "events.jsonl"


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "ops/.env.prod",
        "id_rsa",
        "keys/server.pem",
        "auth/credentials.json",
        "nested/client.token",
    ],
)
def test_sensitive_paths_are_detected(path: str) -> None:
    assert dispatcher.is_sensitive_path(path)


@pytest.mark.parametrize("path", ["src/env.py", "docs/credentials-guide.md", "tests/test_tokenizer.py", "README.md"])
def test_normal_paths_are_not_overblocked(path: str) -> None:
    assert not dispatcher.is_sensitive_path(path)


def test_extract_json_object_accepts_fenced_and_prefixed_json() -> None:
    assert dispatcher.extract_json_object('noise ```json\n{"status":"DONE"}\n``` tail') == {"status": "DONE"}
    assert dispatcher.extract_json_object('Architect says: {"verdict":"ACCEPT","remaining_risks":[]}') == {
        "verdict": "ACCEPT",
        "remaining_risks": [],
    }
    assert dispatcher.extract_json_object("no object here") is None


def test_parse_codex_limits_preserves_independent_buckets() -> None:
    snapshot = dispatcher.parse_codex_limits(
        {
            "result": {
                "accountId": "acct-1",
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitName": "Codex",
                        "planType": "pro",
                        "primary": {"usedPercent": 73, "windowDurationMins": 300, "resetsAt": 1000},
                        "secondary": {"usedPercent": 73, "windowDurationMins": 10080, "resetsAt": 2000},
                    },
                    "gpt-reserve": {
                        "limitName": "gpt-reserve",
                        "primary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 3000},
                    },
                },
            }
        }
    )
    assert snapshot.account_id == "acct-1"
    assert snapshot.plan == "pro"
    assert len(snapshot.windows) == 3
    assert any(window.limit_id == "gpt-reserve" and window.remaining_percent == 100 for window in snapshot.windows)
    assert any(window.label == "5h limit" and window.remaining_percent == 27 for window in snapshot.windows)


def test_parse_grok_billing_unwraps_acp_extension_response() -> None:
    snapshot = dispatcher.parse_grok_billing(
        {
            "result": {
                "result": {
                    "creditUsagePercent": 31.5,
                    "currentPeriod": {"type": "WEEKLY", "end": "2026-09-09T00:00:00Z"},
                    "subscriptionTier": "supergrok",
                }
            }
        }
    )
    assert snapshot.error is None
    assert snapshot.plan == "supergrok"
    assert snapshot.windows[0].remaining_percent == pytest.approx(68.5)
    assert snapshot.windows[0].label == "Weekly credits"


def test_task_packet_normalizes_unknown_worker_to_luna() -> None:
    packet = dispatcher.TaskPacket.from_mapping({"task_id": "ABC 12", "goal": "Do a thing", "worker": "wizard"})
    assert packet.task_id == "abc-12"
    assert packet.worker == "luna"


def dataclasses_replace(packet: dispatcher.TaskPacket, **changes: object) -> dispatcher.TaskPacket:
    values = {
        "task_id": packet.task_id,
        "title": packet.title,
        "goal": packet.goal,
        "worker": packet.worker,
        "risk": packet.risk,
        "allowed_paths": list(packet.allowed_paths),
        "forbidden_paths": list(packet.forbidden_paths),
        "acceptance": list(packet.acceptance),
        "validation": list(packet.validation),
        "dependencies": list(packet.dependencies),
        "architectural_intent": packet.architectural_intent,
    }
    values.update(changes)
    return dispatcher.TaskPacket(**values)  # type: ignore[arg-type]


def test_spark_router_rejects_architecture_and_large_scope(tmp_path: Path) -> None:
    cfg = dispatcher.AppConfig(state_dir=tmp_path / "state", agents=dispatcher.default_agents())
    app = dispatcher.Dispatcher(cfg)
    safe = dispatcher.TaskPacket(
        task_id="small",
        title="Add focused test",
        goal="Add one focused parser regression test",
        worker="spark",
        risk="low",
        allowed_paths=["tests/test_parser.py"],
        acceptance=["test passes"],
    )
    assert app._route_worker(safe) == "spark"

    architectural = dataclasses_replace(safe, goal="Redesign memory routing architecture")
    assert app._route_worker(architectural) == "luna"

    broad = dataclasses_replace(safe, allowed_paths=["a.py", "b.py", "c.py", "d.py"])
    assert app._route_worker(broad) == "luna"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_dirty_repo_is_rescued_into_separate_worktree_without_secrets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test User")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(source, "add", "app.py")
    git(source, "commit", "-m", "initial")
    original_head = git(source, "rev-parse", "HEAD")

    (source / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (source / "notes.txt").write_text("unfinished Sol note\n", encoding="utf-8")
    (source / ".env.local").write_text("SECRET=do-not-copy\n", encoding="utf-8")

    workspace = dispatcher.GitWorkspace(tmp_path / "dispatcher-state")
    recovery = workspace.prepare_recovery(source)

    assert recovery.source_head == original_head
    assert recovery.dirty is True
    assert "app.py" in recovery.rescued_paths
    assert "notes.txt" in recovery.rescued_paths
    assert ".env.local" in recovery.skipped_sensitive_paths
    assert recovery.integration_root != source
    assert (recovery.integration_root / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (recovery.integration_root / "notes.txt").exists()
    assert not (recovery.integration_root / ".env.local").exists()
    assert git(recovery.integration_root, "rev-parse", "HEAD") != original_head

    assert (source / ".env.local").exists()
    source_status = git(source, "status", "--porcelain")
    assert "app.py" in source_status and "notes.txt" in source_status and ".env.local" in source_status


def test_event_payload_paths_are_json_safe(tmp_path: Path) -> None:
    async def run() -> None:
        bus = dispatcher.EventBus(tmp_path / "events.jsonl")
        event = await bus.emit("system", "path", payload={"path": tmp_path})
        json.dumps(event)
        assert event["payload"]["path"] == str(tmp_path)

    asyncio.run(run())


def make_fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake_cli.py"
    script.write_text(
        r'''
import json
import pathlib
import sys

role = sys.argv[1]
args = sys.argv[2:]

def send(obj):
    print(json.dumps(obj), flush=True)

if "--version" in args:
    print(f"fake-{role} 1.0")
    raise SystemExit(0)

if "app-server" in args:
    init = json.loads(sys.stdin.readline())
    send({"id": init["id"], "result": {"userAgent": "fake"}})
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        msg = json.loads(line)
        if msg.get("method") == "account/rateLimits/read":
            send({"id": msg["id"], "result": {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 100}}}}})
            break
    raise SystemExit(0)

if "agent" in args and "stdio" in args:
    init = json.loads(sys.stdin.readline())
    send({"jsonrpc": "2.0", "id": init["id"], "result": {"protocolVersion": 1}})
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        msg = json.loads(line)
        if msg.get("method") == "x.ai/billing":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"creditUsagePercent": 20, "currentPeriod": {"type": "WEEKLY", "end": "2099-01-01T00:00:00Z"}}})
            break
    raise SystemExit(0)

if role == "grok":
    prompt_path = pathlib.Path(args[args.index("--prompt-file") + 1])
    prompt = prompt_path.read_text()
    if "Act as the accepting architect" in prompt:
        answer = {"verdict": "ACCEPT", "summary": "fake review accepted", "remaining_risks": []}
    elif '"status": "ACCEPTED"' in prompt:
        answer = {"status": "DONE", "state_summary": "fake backlog complete", "final_evidence": ["worker.txt accepted"]}
    else:
        answer = {
            "status": "TASK",
            "state_summary": "initial commit only",
            "task": {
                "task_id": "fake-one",
                "title": "Create worker marker",
                "goal": "Create worker.txt with a stable marker",
                "worker": "spark",
                "risk": "low",
                "architectural_intent": "No architecture changes",
                "allowed_paths": ["worker.txt"],
                "forbidden_paths": ["README.md"],
                "acceptance": ["worker.txt contains completed"],
                "validation": ["test -f worker.txt"],
                "dependencies": [],
            },
        }
    send({"type": "text", "data": json.dumps(answer)})
    send({"type": "end", "sessionId": "fake-grok-session", "usage": {"input_tokens": 10, "output_tokens": 5}})
    raise SystemExit(0)

if role in {"luna", "spark"} and "exec" in args:
    _ = sys.stdin.read()
    pathlib.Path("worker.txt").write_text("completed\n")
    send({"type": "thread.started", "thread_id": f"fake-{role}-thread"})
    send({"type": "item.completed", "item": {"type": "agent_message", "text": "HANDOFF: worker.txt created"}})
    send({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 6}})
    raise SystemExit(0)

print("unsupported fake invocation", role, args, file=sys.stderr)
raise SystemExit(2)
''',
        encoding="utf-8",
    )
    return script


@pytest.mark.asyncio
async def test_limit_probes_work_with_subscription_cli_protocols(tmp_path: Path) -> None:
    import sys

    fake = make_fake_cli(tmp_path)
    grok = dispatcher.default_agents()["grok"]
    grok.command = [sys.executable, str(fake), "grok"]
    codex = dispatcher.default_agents()["luna"]
    codex.command = [sys.executable, str(fake), "luna"]

    async def log(_: str) -> None:
        return

    grok_snapshot = await dispatcher.probe_grok_limits(grok, log)
    codex_snapshot = await dispatcher.probe_codex_limits(codex, log)
    assert grok_snapshot.windows[0].remaining_percent == 80
    assert codex_snapshot.windows[0].remaining_percent == 90


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
async def test_end_to_end_takeover_with_fake_logged_in_clis(tmp_path: Path) -> None:
    import sys

    source = tmp_path / "friday"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test User")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "README.md").write_text("# Fake Friday\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "initial")

    fake = make_fake_cli(tmp_path)
    agents = dispatcher.default_agents()
    agents["grok"].command = [sys.executable, str(fake), "grok"]
    agents["luna"].command = [sys.executable, str(fake), "luna"]
    agents["spark"].command = [sys.executable, str(fake), "spark"]
    cfg = dispatcher.AppConfig(
        state_dir=tmp_path / "state",
        project_root=source,
        agents=agents,
        max_tasks=4,
        process_timeout_seconds=30,
    )
    control = dispatcher.Dispatcher(cfg)
    await control.start_takeover(max_tasks=4)
    assert control._takeover_task is not None
    await asyncio.wait_for(control._takeover_task, timeout=30)

    assert control.run_state["status"] == "completed"
    assert control.run_state["accepted"] == 1
    assert control.recovery is not None
    assert (control.recovery.integration_root / "worker.txt").read_text() == "completed\n"
    assert not (source / "worker.txt").exists()
    assert control.tasks["fake-one"].status == "ACCEPTED"
