#!/usr/bin/env python3
"""Friday Emergency Dispatcher.

A subscription-backed local control room for Grok Build, Codex Luna and
Codex Spark. The program never calls vendor HTTP APIs directly. It drives
already authenticated local CLI binaries and isolates implementation work in
git worktrees.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import tomllib
import uuid
import webbrowser
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

VERSION = "0.1.0"
AGENT_IDS = ("grok", "luna", "spark")
REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

EMERGENCY_DIRECTIVE_CORE = r"""
You are the chief architect of an emergency replacement team for the Friday
repository. The two original Sol agents exhausted their subscription limits
without a guaranteed handoff. Your mission is not to start a new design. It is
to reconstruct exactly where their work stopped, preserve all evidence and WIP,
and continue the existing backlog to completion without architectural drift.

Operating law:
1. Evidence beats inference. Inspect current HEAD, dirty state snapshot, recent
   commits, relevant backlog/roadmap/handoff files, tests, and implementation.
2. Grok owns architecture, task selection, contracts, review, and acceptance.
3. Luna is the implementation owner for medium, investigative, cross-file, or
   integration work. Luna may not silently redesign contracts.
4. Spark is a micro-worker only: low-risk, deterministic operations normally
   limited to one to three files with explicit acceptance checks.
5. One task, one worker, one isolated worktree. Review the real diff and test
   evidence, never only the worker's summary.
6. Preserve existing contracts unless repository evidence proves they are
   obsolete. Prefer a BLOCKED result over invented architecture.
7. Do not discard interrupted work, force-push, rewrite shared history, expose
   secrets, or merge destructive/security-sensitive changes without an explicit
   risk record.
8. Keep handoffs compact: task packet, base SHA, changed paths, commit SHA,
   validation summary, assumptions, and remaining risks. Do not retransmit whole
   transcripts or giant diffs when a repository pointer is enough.
9. The finish line is the reconciled backlog, not a single green patch. Continue
   until each discovered item is accepted, explicitly obsolete/duplicate, or
   blocked with concrete evidence and a useful next action.
""".strip()

WORKER_RULES = r"""
You are an implementation worker in the Friday emergency team. Implement only
the attached task packet in the provided isolated worktree.

Rules:
- Read the relevant existing code and tests before editing.
- Preserve architecture and public contracts exactly as stated.
- Do not broaden scope, redesign adjacent systems, or perform opportunistic
  cleanup.
- Run every practical validation command from the packet and report exact
  outcomes. Add focused tests where requested.
- Do not commit. The dispatcher performs the audit commit after checking paths.
- Never create, copy, print, or commit credentials, .env files, private keys,
  tokens, browser profiles, or auth stores.
- If the task cannot be completed inside its contract, stop with BLOCKED and
  name the ambiguity plus repository evidence. A narrow honest stop is better
  than a plausible architectural invention.
""".strip()

SENSITIVE_PATTERNS = (
    re.compile(r"(^|/)\.env(?:\.|$)", re.I),
    re.compile(r"(^|/)(?:id_rsa|id_ed25519)(?:\.|$)", re.I),
    re.compile(r"\.(?:pem|key|p12|pfx|token)$", re.I),
    re.compile(r"(^|/)(?:credentials|secrets?)(?:\.|/|$)", re.I),
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def safe_slug(value: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._").lower()
    return (slug or "task")[:limit]


def is_sensitive_path(path: str | Path) -> bool:
    normalized = str(path).replace("\\", "/").lstrip("./")
    return any(pattern.search(normalized) for pattern in SENSITIVE_PATTERNS)


def compact_text(value: str, limit: int = 16_000) -> str:
    if len(value) <= limit:
        return value
    return value[: limit // 2] + f"\n...[{len(value) - limit} chars omitted]...\n" + value[-limit // 2 :]


def parse_command(value: Any, fallback: list[str]) -> list[str]:
    if value is None:
        return list(fallback)
    if isinstance(value, str):
        parsed = shlex.split(value)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        parsed = list(value)
    else:
        raise ValueError("agent command must be a string or an array of strings")
    if not parsed:
        raise ValueError("agent command cannot be empty")
    return parsed


@dataclass(slots=True)
class AgentConfig:
    agent_id: str
    display_name: str
    role: str
    kind: Literal["grok", "codex"]
    command: list[str]
    model: str
    reasoning: str
    reasoning_options: list[str] = field(default_factory=lambda: list(REASONING_LEVELS))
    max_turns: int = 80
    sandbox: str = "workspace"


@dataclass(slots=True)
class SolLinkConfig:
    enabled: bool = False
    inbox: Path | None = None
    poll_seconds: float = 1.0


@dataclass(slots=True)
class AppConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    state_dir: Path = field(default_factory=lambda: expand_path("~/.local/state/friday-dispatcher"))
    project_root: Path | None = None
    limit_poll_seconds: int = 180
    process_timeout_seconds: int = 7200
    max_tasks: int = 50
    max_review_rounds: int = 2
    keep_task_worktrees: bool = False
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    sol_link: SolLinkConfig = field(default_factory=SolLinkConfig)


def default_agents() -> dict[str, AgentConfig]:
    return {
        "grok": AgentConfig("grok", "Grok Architect", "chief architect and reviewer", "grok", ["grok-build"], "grok-4.6", "xhigh", max_turns=120, sandbox="read-only"),
        "luna": AgentConfig("luna", "Luna Goodman", "implementation owner", "codex", ["codex-solgoodman"], "gpt-5.6-luna", "high", max_turns=80, sandbox="workspace-write"),
        "spark": AgentConfig("spark", "Codex Spark", "micro-implementation worker", "codex", ["codex"], "gpt-5.3-codex-spark", "medium", max_turns=50, sandbox="workspace-write"),
    }


def load_config(path: Path | None = None) -> AppConfig:
    cfg = AppConfig(agents=default_agents())
    if path is None:
        env_path = os.getenv("FRIDAY_DISPATCHER_CONFIG")
        candidate = expand_path(env_path) if env_path else Path.cwd() / "dispatcher.toml"
        path = candidate if candidate.exists() else None
    if path is None:
        return cfg
    config_path = expand_path(path)
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    server = raw.get("server", {})
    cfg.host = str(server.get("host", cfg.host))
    cfg.port = int(server.get("port", cfg.port))
    cfg.state_dir = expand_path(server.get("state_dir", cfg.state_dir))
    project = server.get("project_root")
    cfg.project_root = expand_path(project) if project else None
    cfg.limit_poll_seconds = max(30, int(server.get("limit_poll_seconds", cfg.limit_poll_seconds)))
    cfg.process_timeout_seconds = max(60, int(server.get("process_timeout_seconds", cfg.process_timeout_seconds)))
    cfg.max_tasks = max(1, int(server.get("max_tasks", cfg.max_tasks)))
    cfg.max_review_rounds = max(0, int(server.get("max_review_rounds", cfg.max_review_rounds)))
    cfg.keep_task_worktrees = bool(server.get("keep_task_worktrees", cfg.keep_task_worktrees))
    raw_agents = raw.get("agents", {})
    for agent_id, agent_cfg in cfg.agents.items():
        overrides = raw_agents.get(agent_id, {})
        agent_cfg.display_name = str(overrides.get("display_name", agent_cfg.display_name))
        agent_cfg.role = str(overrides.get("role", agent_cfg.role))
        agent_cfg.command = parse_command(overrides.get("command"), agent_cfg.command)
        agent_cfg.model = str(overrides.get("model", agent_cfg.model))
        agent_cfg.reasoning = str(overrides.get("reasoning", agent_cfg.reasoning))
        options = overrides.get("reasoning_options")
        if isinstance(options, list) and options:
            agent_cfg.reasoning_options = [str(item) for item in options]
        agent_cfg.max_turns = max(1, int(overrides.get("max_turns", agent_cfg.max_turns)))
        agent_cfg.sandbox = str(overrides.get("sandbox", agent_cfg.sandbox))
    bridge = raw.get("sol_link", {})
    cfg.sol_link.enabled = bool(bridge.get("enabled", False))
    inbox = bridge.get("inbox")
    cfg.sol_link.inbox = expand_path(inbox) if inbox else None
    cfg.sol_link.poll_seconds = max(0.2, float(bridge.get("poll_seconds", 1.0)))
    return cfg


@dataclass(slots=True)
class LimitWindow:
    label: str
    used_percent: float
    remaining_percent: float
    resets_at: int | str | None = None
    duration_minutes: int | None = None
    limit_id: str | None = None


@dataclass(slots=True)
class LimitSnapshot:
    source: str
    windows: list[LimitWindow] = field(default_factory=list)
    account_id: str | None = None
    plan: str | None = None
    updated_at: str = field(default_factory=utc_now)
    error: str | None = None


@dataclass(slots=True)
class AgentRuntime:
    agent_id: str
    display_name: str
    role: str
    command: list[str]
    model: str
    reasoning: str
    reasoning_options: list[str]
    phase: str = "offline"
    message: str = "not checked"
    pid: int | None = None
    session_id: str | None = None
    last_seen: str | None = None
    limits: LimitSnapshot | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunResult:
    returncode: int
    text: str = ""
    session_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class RecoveryResult:
    source_root: Path
    source_head: str
    integration_root: Path
    integration_branch: str
    integration_head: str
    dirty: bool
    rescued_paths: list[str]
    skipped_sensitive_paths: list[str]
    manifest_path: Path


@dataclass(slots=True)
class TaskPacket:
    task_id: str
    title: str
    goal: str
    worker: Literal["luna", "spark"]
    risk: str = "medium"
    allowed_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    architectural_intent: str = "Preserve the existing architecture and contracts."

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "TaskPacket":
        task_id = safe_slug(str(raw.get("task_id") or raw.get("id") or uuid.uuid4().hex[:8]))
        title = str(raw.get("title") or raw.get("goal") or task_id).strip()
        goal = str(raw.get("goal") or title).strip()
        worker = str(raw.get("worker", "luna")).lower()
        if worker not in {"luna", "spark"}:
            worker = "luna"
        return cls(task_id=task_id, title=title, goal=goal, worker=worker, risk=str(raw.get("risk", "medium")).lower(), allowed_paths=[str(x) for x in raw.get("allowed_paths", [])], forbidden_paths=[str(x) for x in raw.get("forbidden_paths", [])], acceptance=[str(x) for x in raw.get("acceptance", [])], validation=[str(x) for x in raw.get("validation", [])], dependencies=[str(x) for x in raw.get("dependencies", [])], architectural_intent=str(raw.get("architectural_intent", "Preserve the existing architecture and contracts.")))


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    title: str
    worker: str
    status: str
    base_sha: str | None = None
    branch: str | None = None
    worktree: str | None = None
    commit_shas: list[str] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    updated_at: str = field(default_factory=utc_now)


class EventBus:
    def __init__(self, journal: Path, history_size: int = 1500) -> None:
        self.journal = journal
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.sequence = 0
        self._lock = asyncio.Lock()

    async def emit(self, agent: str, kind: str, text: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            self.sequence += 1
            safe_payload = json.loads(json.dumps(payload or {}, ensure_ascii=False, default=str))
            event = {"event_id": self.sequence, "ts": utc_now(), "agent": agent, "kind": kind, "text": compact_text(text), "payload": safe_payload}
            self.history.append(event)
            with self.journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            stale = []
            for queue in self.subscribers:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    stale.append(queue)
            for queue in stale:
                self.subscribers.discard(queue)
            return event

    @asynccontextmanager
    async def subscribe(self):
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self.subscribers.add(queue)
        try:
            yield queue
        finally:
            self.subscribers.discard(queue)


LineCallback = Callable[[str, str], Awaitable[None]]


class ProcessRunner:
    def __init__(self) -> None:
        self.active: dict[str, asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()

    async def run(self, name: str, argv: list[str], *, cwd: Path, input_text: str | None, timeout: int, on_line: LineCallback, env: dict[str, str] | None = None, on_pid: Callable[[int | None], Awaitable[None]] | None = None) -> tuple[int, list[str], list[str]]:
        if not argv:
            raise ValueError("empty command")
        executable = argv[0]
        if os.path.sep not in executable and shutil.which(executable) is None:
            raise FileNotFoundError(f"executable not found: {executable}")
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=process_env, **kwargs)
        async with self._lock:
            previous = self.active.get(name)
            if previous and previous.returncode is None:
                await self._terminate_process(previous)
            self.active[name] = proc
        if on_pid:
            await on_pid(proc.pid)
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        async def pump(stream: asyncio.StreamReader | None, channel: str, bucket: list[str]) -> None:
            if stream is None:
                return
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                bucket.append(line)
                await on_line(channel, line)

        stdout_task = asyncio.create_task(pump(proc.stdout, "stdout", stdout_lines))
        stderr_task = asyncio.create_task(pump(proc.stderr, "stderr", stderr_lines))
        try:
            if input_text is not None and proc.stdin is not None:
                proc.stdin.write(input_text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.CancelledError:
            await self._terminate_process(proc)
            raise
        except asyncio.TimeoutError:
            await on_line("stderr", f"dispatcher timeout after {timeout}s; terminating process")
            await self._terminate_process(proc)
            returncode = 124
        else:
            returncode = int(proc.returncode or 0)
        finally:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            async with self._lock:
                if self.active.get(name) is proc:
                    self.active.pop(name, None)
            if on_pid:
                await on_pid(None)
        return returncode, stdout_lines, stderr_lines

    async def terminate(self, name: str) -> bool:
        async with self._lock:
            proc = self.active.get(name)
        if not proc or proc.returncode is not None:
            return False
        await self._terminate_process(proc)
        return True

    async def terminate_all(self) -> None:
        async with self._lock:
            procs = list(self.active.values())
        await asyncio.gather(*(self._terminate_process(proc) for proc in procs), return_exceptions=True)

    async def _terminate_process(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=3)


def extract_json_object(text: str) -> dict[str, Any] | None:
    candidates = [text.strip()]
    candidates.extend(match.group(1) for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I))
    decoder = json.JSONDecoder()
    for candidate in candidates:
        with contextlib.suppress(json.JSONDecodeError):
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            with contextlib.suppress(json.JSONDecodeError):
                value, _ = decoder.raw_decode(candidate[index:])
                if isinstance(value, dict):
                    return value
    return None


def _duration_label(minutes: int | None) -> str:
    if minutes is None:
        return "Usage limit"
    if minutes >= 6 * 24 * 60:
        return "Weekly limit"
    if minutes >= 24 * 60:
        return f"{round(minutes / (24 * 60))}-day limit"
    if minutes >= 60:
        return f"{round(minutes / 60)}h limit"
    return f"{minutes}m limit"


def parse_codex_limits(payload: dict[str, Any]) -> LimitSnapshot:
    result = payload.get("result", payload)
    if not isinstance(result, dict):
        return LimitSnapshot(source="codex-app-server", error="unexpected rate limit response")
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        fallback = result.get("rateLimits")
        buckets = {str((fallback or {}).get("limitId") or "codex"): fallback} if isinstance(fallback, dict) else {}
    windows: list[LimitWindow] = []
    plan = None
    for limit_id, snapshot in buckets.items():
        if not isinstance(snapshot, dict):
            continue
        plan = plan or snapshot.get("planType")
        limit_name = str(snapshot.get("limitName") or limit_id or "Codex")
        for slot in ("primary", "secondary"):
            window = snapshot.get(slot)
            if not isinstance(window, dict):
                continue
            used = float(window.get("usedPercent", 0))
            duration = window.get("windowDurationMins")
            duration_int = int(duration) if isinstance(duration, (int, float)) else None
            prefix = "" if limit_id in {"codex", "default", ""} else f"{limit_name} "
            windows.append(LimitWindow(f"{prefix}{_duration_label(duration_int)}".strip(), max(0.0, min(100.0, used)), max(0.0, min(100.0, 100.0 - used)), window.get("resetsAt"), duration_int, str(limit_id)))
    return LimitSnapshot("codex-app-server", windows, result.get("accountId"), str(plan) if plan else None, error=None if windows else "no rate-limit windows returned")


def parse_grok_billing(payload: dict[str, Any]) -> LimitSnapshot:
    result: Any = payload.get("result", payload)
    for _ in range(3):
        if isinstance(result, dict) and set(result).intersection({"config", "creditUsagePercent", "error"}):
            break
        if isinstance(result, dict) and isinstance(result.get("result"), dict):
            result = result["result"]
            continue
        break
    if not isinstance(result, dict):
        return LimitSnapshot(source="grok-acp", error="unexpected billing response")
    config = result.get("config") if isinstance(result.get("config"), dict) else result
    used = config.get("creditUsagePercent") if isinstance(config, dict) else None
    windows: list[LimitWindow] = []
    if isinstance(used, (int, float)):
        period = config.get("currentPeriod") if isinstance(config, dict) else None
        label = "Weekly credits" if isinstance(period, dict) and "WEEK" in str(period.get("type", "")).upper() else "Grok credits"
        used_float = max(0.0, min(100.0, float(used)))
        windows.append(LimitWindow(label, used_float, 100.0 - used_float, period.get("end") if isinstance(period, dict) else None, limit_id="grok-build"))
    return LimitSnapshot("grok-acp", windows, plan=result.get("subscriptionTier"), error=None if windows else str(result.get("error") or "billing percentage unavailable"))


async def _write_rpc(proc: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("RPC process has no stdin")
    proc.stdin.write((json.dumps(message) + "\n").encode())
    await proc.stdin.drain()


async def _read_rpc_response(proc: asyncio.subprocess.Process, request_id: int, *, timeout: float, log: Callable[[str], Awaitable[None]]) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("RPC process has no stdout")
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"RPC response {request_id} timed out")
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        if not raw:
            raise RuntimeError("RPC process exited before responding")
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            await log(f"rpc: {line}")
            continue
        if message.get("id") == request_id:
            if message.get("error"):
                raise RuntimeError(str(message["error"]))
            return message
        await log("rpc notification: " + compact_text(json.dumps(message), 2000))


async def _probe_rpc(argv: list[str], init: dict[str, Any], request: dict[str, Any], parser: Callable[[dict[str, Any]], LimitSnapshot], source: str, log: Callable[[str], Awaitable[None]]) -> LimitSnapshot:
    kwargs: dict[str, Any] = {"stdin": asyncio.subprocess.PIPE, "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    try:
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
    except Exception as exc:
        return LimitSnapshot(source=source, error=str(exc))

    async def drain_stderr() -> None:
        if proc.stderr:
            while raw := await proc.stderr.readline():
                await log(raw.decode(errors="replace").rstrip())
    stderr_task = asyncio.create_task(drain_stderr())
    try:
        await _write_rpc(proc, init)
        await _read_rpc_response(proc, int(init["id"]), timeout=20, log=log)
        await _write_rpc(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}} if "jsonrpc" in init else {"method": "initialized", "params": {}})
        await _write_rpc(proc, request)
        return parser(await _read_rpc_response(proc, int(request["id"]), timeout=25, log=log))
    except Exception as exc:
        return LimitSnapshot(source=source, error=str(exc))
    finally:
        if proc.returncode is None:
            proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=3)
        stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)


async def probe_codex_limits(agent: AgentConfig, log: Callable[[str], Awaitable[None]]) -> LimitSnapshot:
    return await _probe_rpc([*agent.command, "app-server"], {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "friday_dispatcher", "title": "Friday Dispatcher", "version": VERSION}, "capabilities": {"experimentalApi": True}}}, {"id": 2, "method": "account/rateLimits/read", "params": None}, parse_codex_limits, "codex-app-server", log)


async def probe_grok_limits(agent: AgentConfig, log: Callable[[str], Awaitable[None]]) -> LimitSnapshot:
    argv = [*agent.command, "agent"]
    if agent.model:
        argv += ["--model", agent.model]
    if agent.reasoning:
        argv += ["--reasoning-effort", agent.reasoning]
    argv.append("stdio")
    return await _probe_rpc(argv, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": "friday-dispatcher", "title": "Friday Dispatcher", "version": VERSION}}}, {"jsonrpc": "2.0", "id": 2, "method": "x.ai/billing", "params": {}}, parse_grok_billing, "grok-acp", log)


class GrokRunner:
    def __init__(self, cfg: AgentConfig, runner: ProcessRunner, bus: EventBus, state_dir: Path, timeout: int) -> None:
        self.cfg, self.runner, self.bus, self.state_dir, self.timeout = cfg, runner, bus, state_dir, timeout
        self._run_lock = asyncio.Lock()

    async def run(self, prompt: str, cwd: Path, *, session_id: str | None = None, purpose: str = "architect") -> RunResult:
        async with self._run_lock:
            return await self._run_locked(prompt, cwd, session_id=session_id, purpose=purpose)

    async def _run_locked(self, prompt: str, cwd: Path, *, session_id: str | None, purpose: str) -> RunResult:
        prompt_dir = self.state_dir / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = prompt_dir / f"grok-{purpose}-{uuid.uuid4().hex}.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        argv = [*self.cfg.command, "--cwd", str(cwd)]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        if self.cfg.reasoning:
            argv += ["--reasoning-effort", self.cfg.reasoning]
        argv += ["--output-format", "streaming-json", "--no-auto-update", "--no-subagents", "--max-turns", str(self.cfg.max_turns), "--always-approve", "--sandbox", self.cfg.sandbox]
        if session_id:
            argv += ["--resume", session_id]
        argv += ["--prompt-file", str(prompt_file)]
        chunks: list[str] = []
        events: list[dict[str, Any]] = []
        found_session, usage = session_id, {}

        async def on_line(channel: str, line: str) -> None:
            nonlocal found_session, usage
            if channel == "stderr":
                await self.bus.emit("grok", "stderr", line)
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                chunks.append(line + "\n")
                await self.bus.emit("grok", "stdout", line)
                return
            if not isinstance(event, dict):
                return
            events.append(event)
            kind = str(event.get("type", "event"))
            if kind == "text":
                value = event.get("data") or event.get("text") or event.get("delta") or ""
                chunks.append(str(value))
                await self.bus.emit("grok", "text", str(value))
            elif kind == "thought":
                await self.bus.emit("grok", "thought", str(event.get("data") or event.get("text") or ""))
            elif kind == "end":
                found_session = event.get("sessionId") or event.get("session_id") or found_session
                usage = event.get("usage") or usage
                await self.bus.emit("grok", "end", "turn completed", event)
            else:
                await self.bus.emit("grok", kind, compact_text(line, 3000), event)

        async def on_pid(pid: int | None) -> None:
            await self.bus.emit("grok", "process", f"pid={pid}" if pid else "process exited", {"pid": pid})

        rc, stderr = 1, []
        try:
            rc, _, stderr = await self.runner.run("grok", argv, cwd=cwd, input_text=None, timeout=self.timeout, on_line=on_line, on_pid=on_pid)
        finally:
            with contextlib.suppress(OSError):
                prompt_file.unlink()
        return RunResult(rc, "".join(chunks).strip(), found_session, usage, events, "\n".join(stderr[-20:]) if rc else None)


class CodexRunner:
    def __init__(self, cfg: AgentConfig, runner: ProcessRunner, bus: EventBus, timeout: int) -> None:
        self.cfg, self.runner, self.bus, self.timeout = cfg, runner, bus, timeout

    async def run(self, prompt: str, cwd: Path, *, session_id: str | None = None) -> RunResult:
        argv = [*self.cfg.command, "exec"]
        if session_id:
            argv += ["resume", session_id]
        argv += ["--json", "--color", "never", "--skip-git-repo-check", "--sandbox", self.cfg.sandbox, "--ask-for-approval", "never"]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        if self.cfg.reasoning:
            argv += ["-c", f'model_reasoning_effort="{self.cfg.reasoning}"']
        argv.append("-")
        chunks: list[str] = []
        events: list[dict[str, Any]] = []
        found_session, usage = session_id, {}

        async def on_line(channel: str, line: str) -> None:
            nonlocal found_session, usage
            if channel == "stderr":
                await self.bus.emit(self.cfg.agent_id, "stderr", line)
                return
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                chunks.append(line + "\n")
                await self.bus.emit(self.cfg.agent_id, "stdout", line)
                return
            if not isinstance(event, dict):
                return
            events.append(event)
            kind = str(event.get("type") or event.get("event") or "event")
            if kind in {"thread.started", "thread_started"}:
                found_session = event.get("thread_id") or event.get("threadId") or (event.get("thread") or {}).get("id") or found_session
                await self.bus.emit(self.cfg.agent_id, "thread", str(found_session or "thread started"), event)
            elif kind in {"item.completed", "item_completed"}:
                item = event.get("item") if isinstance(event.get("item"), dict) else event
                item_type = str(item.get("type", "item"))
                text = item.get("text") or item.get("message") or ""
                if item_type in {"agent_message", "assistant_message", "message"} and text:
                    chunks.append(str(text))
                    await self.bus.emit(self.cfg.agent_id, "text", str(text))
                else:
                    await self.bus.emit(self.cfg.agent_id, "item", str(item.get("command") or item.get("name") or item_type), item)
            elif kind in {"turn.completed", "turn_completed"}:
                usage = event.get("usage") or (event.get("turn") or {}).get("usage") or usage
                await self.bus.emit(self.cfg.agent_id, "end", "turn completed", event)
            else:
                await self.bus.emit(self.cfg.agent_id, kind, compact_text(line, 3000), event)

        async def on_pid(pid: int | None) -> None:
            await self.bus.emit(self.cfg.agent_id, "process", f"pid={pid}" if pid else "process exited", {"pid": pid})

        rc, _, stderr = await self.runner.run(self.cfg.agent_id, argv, cwd=cwd, input_text=prompt, timeout=self.timeout, on_line=on_line, on_pid=on_pid)
        return RunResult(rc, "".join(chunks).strip(), found_session, usage, events, "\n".join(stderr[-20:]) if rc else None)


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True, input_bytes: bytes | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    completed = subprocess.run(["git", "-C", str(repo), *args], input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=process_env, check=False)
    if check and completed.returncode:
        raise GitError(f"git {' '.join(args)} failed ({completed.returncode}): " + completed.stderr.decode(errors="replace").strip())
    return completed


def _decode_z(output: bytes) -> list[str]:
    return [part.decode("utf-8", errors="surrogateescape") for part in output.split(b"\0") if part]


def _changed_paths(repo: Path) -> tuple[list[str], list[str]]:
    tracked = _decode_z(_git(repo, "diff", "--name-only", "-z", "HEAD").stdout)
    untracked = _decode_z(_git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout)
    return sorted(set(tracked)), sorted(set(untracked))


GIT_IDENTITY = {"GIT_AUTHOR_NAME": "Friday Dispatcher", "GIT_AUTHOR_EMAIL": "dispatcher@localhost", "GIT_COMMITTER_NAME": "Friday Dispatcher", "GIT_COMMITTER_EMAIL": "dispatcher@localhost"}


class GitWorkspace:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.worktrees_dir = state_dir / "worktrees"
        self.recovery_dir = state_dir / "recovery"
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        self._integration_lock = asyncio.Lock()

    def prepare_recovery(self, project: Path) -> RecoveryResult:
        project = expand_path(project)
        root = Path(_git(project, "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve()
        head = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
        tracked, untracked = _changed_paths(root)
        safe_tracked = [x for x in tracked if not is_sensitive_path(x)]
        safe_untracked = [x for x in untracked if not is_sensitive_path(x)]
        skipped = sorted(set(tracked + untracked) - set(safe_tracked + safe_untracked))
        stamp, nonce = dt.datetime.now().strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6]
        branch = f"dispatcher/emergency-{stamp}-{nonce}"
        integration_root = self.worktrees_dir / f"integration-{stamp}-{nonce}"
        _git(root, "worktree", "add", "-b", branch, str(integration_root), head)
        rescue_bundle = self.recovery_dir / f"{stamp}-{nonce}"
        rescue_bundle.mkdir(parents=True, exist_ok=True)
        rescued: list[str] = []
        if safe_tracked:
            patch = _git(root, "diff", "--binary", "HEAD", "--", *safe_tracked).stdout
            (rescue_bundle / "tracked.patch").write_bytes(patch)
            if patch:
                _git(integration_root, "apply", "--index", "--binary", "-", input_bytes=patch)
                rescued += safe_tracked
        copied_total = 0
        for relative in safe_untracked:
            source = root / relative
            if source.is_symlink() or not source.is_file():
                skipped.append(relative)
                continue
            size = source.stat().st_size
            if size > 25 * 1024 * 1024 or copied_total + size > 100 * 1024 * 1024:
                skipped.append(relative)
                continue
            destination = integration_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied_total += size
            rescued.append(relative)
        if rescued:
            _git(integration_root, "add", "-A")
            _git(integration_root, "commit", "-m", "chore(dispatcher): preserve interrupted WIP", env=GIT_IDENTITY)
        integration_head = _git(integration_root, "rev-parse", "HEAD").stdout.decode().strip()
        manifest = {"created_at": utc_now(), "source_root": str(root), "source_head": head, "integration_root": str(integration_root), "integration_branch": branch, "integration_head": integration_head, "dirty": bool(tracked or untracked), "rescued_paths": sorted(rescued), "skipped_sensitive_paths": sorted(set(skipped))}
        manifest_path = rescue_bundle / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return RecoveryResult(root, head, integration_root, branch, integration_head, bool(tracked or untracked), sorted(rescued), sorted(set(skipped)), manifest_path)

    def inventory(self, repo: Path) -> str:
        log = _git(repo, "log", "-20", "--date=iso", "--pretty=format:%h %ad %d %s", check=False).stdout.decode(errors="replace")
        status = _git(repo, "status", "--short", "--branch", check=False).stdout.decode(errors="replace")
        files = _decode_z(_git(repo, "ls-files", "-z", check=False).stdout)
        interesting = [x for x in files if re.search(r"(?:backlog|roadmap|todo|handoff|outer[_-]?sol|sol[_-]?link|plan|status)", x, re.I)][:250]
        return f"Repository: {repo}\n\nGit status:\n{status}\n\nRecent commits:\n{log}\n\nCandidate backlog / handoff / planning files:\n" + ("\n".join(interesting) if interesting else "(none found by filename; inspect the repository structure)")

    def create_task_worktree(self, integration_root: Path, task_id: str) -> tuple[Path, str, str]:
        base = _git(integration_root, "rev-parse", "HEAD").stdout.decode().strip()
        nonce = uuid.uuid4().hex[:6]
        branch = f"dispatcher/task/{safe_slug(task_id)}-{nonce}"
        root = self.worktrees_dir / f"task-{safe_slug(task_id)}-{nonce}"
        _git(integration_root, "worktree", "add", "-b", branch, str(root), base)
        return root, branch, base

    def commit_task(self, worktree: Path, task: TaskPacket) -> str | None:
        tracked, untracked = _changed_paths(worktree)
        changed = sorted(set(tracked + untracked))
        if not changed:
            return None
        sensitive = [x for x in changed if is_sensitive_path(x)]
        if sensitive:
            raise GitError("worker touched sensitive paths: " + ", ".join(sensitive))
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", f"feat(dispatcher): {task.title[:70]}", env=GIT_IDENTITY)
        return _git(worktree, "rev-parse", "HEAD").stdout.decode().strip()

    def commits_since(self, worktree: Path, base_sha: str) -> list[str]:
        return [x.strip() for x in _git(worktree, "rev-list", "--reverse", f"{base_sha}..HEAD").stdout.decode().splitlines() if x.strip()]

    async def integrate(self, integration_root: Path, commits: Iterable[str]) -> str:
        async with self._integration_lock:
            for commit in commits:
                _git(integration_root, "cherry-pick", commit)
            return _git(integration_root, "rev-parse", "HEAD").stdout.decode().strip()

    def remove_task_worktree(self, integration_root: Path, worktree: Path, branch: str) -> None:
        _git(integration_root, "worktree", "remove", "--force", str(worktree), check=False)
        _git(integration_root, "branch", "-D", branch, check=False)


class SolLinkJournal:
    def __init__(self, path: Path) -> None:
        self.path, self._lock = path, asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def append(self, event_type: str, source: str, target: str, payload: dict[str, Any]) -> None:
        event = {"id": str(uuid.uuid4()), "ts": utc_now(), "type": event_type, "from": source, "to": target, **payload}
        async with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


class LegacySolLinkBridge:
    def __init__(self, cfg: SolLinkConfig, state_dir: Path, handler: Callable[[dict[str, Any]], Awaitable[None]], bus: EventBus) -> None:
        self.cfg, self.handler, self.bus = cfg, handler, bus
        self.cursor_path = state_dir / "sol-link" / "legacy-cursor.json"
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        if not self.cfg.enabled or not self.cfg.inbox:
            return
        inbox, offset = self.cfg.inbox, 0
        with contextlib.suppress(Exception):
            offset = int(json.loads(self.cursor_path.read_text()).get("offset", 0))
        while not self._stop.is_set():
            try:
                if inbox.exists():
                    if inbox.stat().st_size < offset:
                        offset = 0
                    with inbox.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(offset)
                        while line := handle.readline():
                            offset = handle.tell()
                            with contextlib.suppress(json.JSONDecodeError):
                                event = json.loads(line)
                                if isinstance(event, dict):
                                    await self.handler(event)
                    self.cursor_path.write_text(json.dumps({"offset": offset}))
            except Exception as exc:
                await self.bus.emit("system", "sol_link_error", str(exc))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.poll_seconds)

    def stop(self) -> None:
        self._stop.set()


class Dispatcher:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.bus = EventBus(cfg.state_dir / "events.jsonl")
        self.sol_link = SolLinkJournal(cfg.state_dir / "sol-link" / "events.jsonl")
        self.processes, self.git = ProcessRunner(), GitWorkspace(cfg.state_dir)
        self.agent_states = {agent_id: AgentRuntime(agent_id, a.display_name, a.role, a.command, a.model or "account default / reserve fallback", a.reasoning, a.reasoning_options) for agent_id, a in cfg.agents.items()}
        self.grok = GrokRunner(cfg.agents["grok"], self.processes, self.bus, cfg.state_dir, cfg.process_timeout_seconds)
        self.workers = {x: CodexRunner(cfg.agents[x], self.processes, self.bus, cfg.process_timeout_seconds) for x in ("luna", "spark")}
        self.project_root, self.recovery = cfg.project_root, None
        self.tasks: dict[str, TaskRecord] = {}
        self.run_state: dict[str, Any] = {"status": "idle", "message": "No emergency run active", "started_at": None, "completed_at": None, "integration_branch": None, "integration_root": None, "source_root": str(self.project_root) if self.project_root else None, "accepted": 0, "blocked": 0}
        self.chat_session = self.architect_session = None
        self._takeover_task: asyncio.Task[None] | None = None
        self._chat_tasks: set[asyncio.Task[None]] = set()
        self._background: list[asyncio.Task[Any]] = []
        self._stop_requested = asyncio.Event()
        self.external_tasks: asyncio.Queue[TaskPacket] = asyncio.Queue()
        self.bridge = LegacySolLinkBridge(cfg.sol_link, cfg.state_dir, self.handle_sol_link_event, self.bus)
        self.state_path = cfg.state_dir / "runtime-state.json"

    async def start(self) -> None:
        for agent_id, cfg in self.cfg.agents.items():
            executable = cfg.command[0]
            available = Path(executable).exists() if os.path.sep in executable else shutil.which(executable) is not None
            state = self.agent_states[agent_id]
            state.phase, state.message, state.last_seen = ("idle" if available else "offline"), ("ready" if available else f"executable not found: {executable}"), utc_now()
        self._background.append(asyncio.create_task(self._limit_loop()))
        if self.cfg.sol_link.enabled:
            self._background.append(asyncio.create_task(self.bridge.run()))
        await self.bus.emit("system", "started", f"Friday Dispatcher {VERSION} started")
        self._save_state()

    async def shutdown(self) -> None:
        self.bridge.stop()
        await self.stop_takeover()
        for task in list(self._chat_tasks):
            task.cancel()
        await asyncio.gather(*self._chat_tasks, return_exceptions=True)
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.processes.terminate_all()
        self._save_state()

    def _save_state(self) -> None:
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.public_state(False), ensure_ascii=False, indent=2, default=str))
        temp.replace(self.state_path)

    def public_state(self, include_events: bool = True) -> dict[str, Any]:
        return {"version": VERSION, "project_root": str(self.project_root) if self.project_root else None, "agents": {k: asdict(v) for k, v in self.agent_states.items()}, "run": self.run_state, "tasks": [asdict(x) for x in self.tasks.values()], "events": list(self.bus.history)[-400:] if include_events else [], "sol_link": {"journal": str(self.sol_link.path), "legacy_enabled": self.cfg.sol_link.enabled, "legacy_inbox": str(self.cfg.sol_link.inbox) if self.cfg.sol_link.inbox else None}}

    async def set_project(self, path: str | Path) -> Path:
        project = expand_path(path)
        _git(project, "rev-parse", "--is-inside-work-tree")
        self.project_root = Path(_git(project, "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve()
        self.run_state["source_root"] = str(self.project_root)
        await self.bus.emit("system", "project", f"project selected: {self.project_root}")
        self._save_state()
        return self.project_root

    async def set_reasoning(self, agent_id: str, level: str) -> None:
        if agent_id not in self.cfg.agents or level not in self.cfg.agents[agent_id].reasoning_options:
            raise ValueError(f"unsupported agent/reasoning combination: {agent_id}/{level}")
        self.cfg.agents[agent_id].reasoning = level
        self.agent_states[agent_id].reasoning = level
        await self.bus.emit(agent_id, "reasoning", f"reasoning set to {level}")
        self._save_state()

    async def _set_phase(self, agent_id: str, phase: str, message: str = "") -> None:
        state = self.agent_states[agent_id]
        state.phase, state.message, state.last_seen = phase, message or phase, utc_now()
        await self.bus.emit(agent_id, "status", state.message, {"phase": phase})
        self._save_state()

    async def _limit_loop(self) -> None:
        while True:
            await self.refresh_limits()
            await asyncio.sleep(self.cfg.limit_poll_seconds)

    async def refresh_limits(self) -> None:
        async def one(agent_id: str) -> None:
            cfg = self.cfg.agents[agent_id]
            async def log(text: str) -> None:
                await self.bus.emit(agent_id, "limit_probe", text)
            snapshot = await (probe_grok_limits(cfg, log) if cfg.kind == "grok" else probe_codex_limits(cfg, log))
            self.agent_states[agent_id].limits = snapshot
            await self.bus.emit(agent_id, "limits", snapshot.error or f"{len(snapshot.windows)} limit window(s) refreshed", asdict(snapshot))
        await asyncio.gather(*(one(x) for x in AGENT_IDS), return_exceptions=True)
        self._save_state()

    def queue_chat(self, message: str) -> str:
        if not message.strip():
            raise ValueError("empty message")
        chat_id = uuid.uuid4().hex
        task = asyncio.create_task(self._run_chat(chat_id, message.strip()))
        self._chat_tasks.add(task)
        task.add_done_callback(self._chat_tasks.discard)
        return chat_id

    async def _run_chat(self, chat_id: str, message: str) -> None:
        await self.bus.emit("user", "chat.user", message, {"chat_id": chat_id})
        if not self.project_root:
            await self.bus.emit("grok", "chat.error", "Select a project repository first", {"chat_id": chat_id})
            return
        await self._set_phase("grok", "chatting", "answering user")
        prompt = f"You are Grok 4.6, the emergency chief architect for Friday. Speak directly to the owner. Inspect but do not edit or dispatch from chat. Preserve architecture and separate facts, inferences and risks.\n\nUser message:\n{message}"
        try:
            result = await self.grok.run(prompt, self.project_root, session_id=self.chat_session, purpose="chat")
            if result.session_id:
                self.chat_session = result.session_id
                self.agent_states["grok"].session_id = result.session_id
            await self.bus.emit("grok", "chat.error" if result.returncode else "chat.assistant", result.error if result.returncode else (result.text or "(empty response)"), {"chat_id": chat_id})
        except Exception as exc:
            await self.bus.emit("grok", "chat.error", str(exc), {"chat_id": chat_id})
        finally:
            await self._set_phase("grok", "idle", "ready")

    async def start_takeover(self, project: str | Path | None = None, max_tasks: int | None = None) -> str:
        if self._takeover_task and not self._takeover_task.done():
            raise RuntimeError("an emergency run is already active")
        if project:
            await self.set_project(project)
        if not self.project_root:
            raise RuntimeError("select a project repository first")
        self._stop_requested = asyncio.Event()
        run_id = uuid.uuid4().hex
        self._takeover_task = asyncio.create_task(self._takeover(run_id, max_tasks or self.cfg.max_tasks))
        return run_id

    async def stop_takeover(self) -> None:
        self._stop_requested.set()
        await self.processes.terminate("luna")
        await self.processes.terminate("spark")
        if self._takeover_task and not self._takeover_task.done():
            self._takeover_task.cancel()
            await asyncio.gather(self._takeover_task, return_exceptions=True)
        self._takeover_task = None

    async def stop_agent(self, agent_id: str) -> bool:
        if agent_id not in AGENT_IDS:
            raise ValueError(f"unknown agent: {agent_id}")
        return await self.processes.terminate(agent_id)

    async def handle_sol_link_event(self, event: dict[str, Any]) -> None:
        event_type, target = str(event.get("type", "")).upper(), str(event.get("to") or event.get("recipient") or "")
        await self.bus.emit("system", "sol_link.in", event_type or "event", event)
        if event_type in {"USER_CHAT", "CHAT"} and target in {"", "grok", "grok-architect"}:
            text = str(event.get("text") or event.get("message") or "").strip()
            if text:
                self.queue_chat(text)
        elif event_type == "CONTROL" and str(event.get("action")) == "start_takeover":
            with contextlib.suppress(Exception):
                await self.start_takeover(event.get("project") or event.get("project_root"))
        elif event_type == "CONTRACT":
            packet = TaskPacket.from_mapping(event.get("task") if isinstance(event.get("task"), dict) else event)
            packet.worker = "spark" if target in {"spark", "spark-worker"} else "luna"
            await self.external_tasks.put(packet)

    def _route_worker(self, packet: TaskPacket) -> str:
        architecture = re.compile(r"architecture|orchestration|security|sandbox|permission|schema migration|data migration|routing|memory", re.I)
        paths = [x for x in packet.allowed_paths if x and "*" not in x]
        safe = packet.worker == "spark" and packet.risk in {"low", "trivial"} and 0 < len(paths) <= 3 and bool(packet.acceptance) and not architecture.search(packet.goal + " " + packet.architectural_intent)
        return "spark" if safe else "luna"

    async def _grok_json(self, prompt: str, cwd: Path, purpose: str) -> dict[str, Any]:
        result = await self.grok.run(prompt, cwd, session_id=self.architect_session, purpose=purpose)
        if result.session_id:
            self.architect_session = result.session_id
        if result.returncode:
            raise RuntimeError(result.error or f"Grok exited {result.returncode}")
        parsed = extract_json_object(result.text)
        if parsed:
            return parsed
        repair = await self.grok.run("Return your previous decision as one JSON object only, no markdown.", cwd, session_id=self.architect_session, purpose=purpose + "-repair")
        parsed = extract_json_object(repair.text)
        if not parsed:
            raise RuntimeError("architect did not return valid JSON")
        return parsed

    def _ledger(self) -> str:
        return json.dumps([{"task_id": x.task_id, "title": x.title, "worker": x.worker, "status": x.status, "commits": x.commit_shas, "error": x.error} for x in self.tasks.values()], ensure_ascii=False, indent=2)

    async def _next_task(self, first: bool) -> tuple[str, TaskPacket | None, dict[str, Any]]:
        if not self.external_tasks.empty():
            packet = await self.external_tasks.get()
            return "TASK", packet, {"source": "sol-link"}
        assert self.recovery
        inventory = await asyncio.to_thread(self.git.inventory, self.recovery.integration_root)
        directive = EMERGENCY_DIRECTIVE_CORE if first else "Continue under the emergency directive already loaded in this session."
        prompt = f'''{directive}\n\nRecovery facts:\nOriginal checkout: {self.recovery.source_root}\nOriginal HEAD: {self.recovery.source_head}\nIntegration worktree: {self.recovery.integration_root}\nIntegration branch: {self.recovery.integration_branch}\nSafe WIP rescued: {self.recovery.rescued_paths}\nSensitive paths excluded: {self.recovery.skipped_sensitive_paths}\n\nRepository inventory:\n{inventory}\n\nLedger:\n{self._ledger()}\n\nInspect the actual repository. Select exactly one implementation-ready task, or declare DONE/BLOCKED. Return one JSON object only. TASK must include state_summary and task with task_id,title,goal,worker,risk,architectural_intent,allowed_paths,forbidden_paths,acceptance,validation,dependencies. Do not repeat ledger tasks. Spark is legal only for deterministic low-risk one-to-three-file work.'''
        decision = await self._grok_json(prompt, self.recovery.integration_root, "plan")
        status = str(decision.get("status", "BLOCKED")).upper()
        return status, TaskPacket.from_mapping(decision.get("task", {})) if status == "TASK" else None, decision

    def _worker_prompt(self, p: TaskPacket, base: str) -> str:
        return f"{WORKER_RULES}\n\n# Task packet\nTask ID: {p.task_id}\nTitle: {p.title}\nBase SHA: {base}\nGoal: {p.goal}\nRisk: {p.risk}\nArchitectural intent: {p.architectural_intent}\nAllowed paths: {json.dumps(p.allowed_paths)}\nForbidden: {json.dumps(p.forbidden_paths)}\nAcceptance: {json.dumps(p.acceptance)}\nValidation: {json.dumps(p.validation)}\nDependencies: {json.dumps(p.dependencies)}\n\nImplement and validate. Finish with a concise HANDOFF. Do not commit."

    async def _review(self, p: TaskPacket, worktree: Path, base: str, commits: list[str]) -> dict[str, Any]:
        prompt = f'''Act as the accepting architect. Review the real repository in {worktree}. Task: {json.dumps(asdict(p), ensure_ascii=False)}. Base: {base}. Commits: {commits}. Read git diff {base}..HEAD and neighboring contracts. Do not edit. Return JSON only: {{"verdict":"ACCEPT","summary":"...","remaining_risks":[]}} or CHANGES_REQUESTED with required_changes, or BLOCKED with blocker.'''
        return await self._grok_json(prompt, worktree, "review-" + p.task_id)

    async def _execute_task(self, p: TaskPacket) -> bool:
        assert self.recovery
        worker_id = self._route_worker(p)
        p.worker = worker_id
        worktree, branch, base = await asyncio.to_thread(self.git.create_task_worktree, self.recovery.integration_root, p.task_id)
        record = TaskRecord(p.task_id, p.title, worker_id, "IMPLEMENTING", base, branch, str(worktree))
        self.tasks[p.task_id] = record
        await self.sol_link.append("CONTRACT", "grok-architect", worker_id + "-worker", {"task_id": p.task_id, "base_sha": base, "task": asdict(p)})
        result = await self.workers[worker_id].run(self._worker_prompt(p, base), worktree)
        if result.returncode:
            record.status, record.error = "FAILED", result.error or f"worker exited {result.returncode}"
            self.run_state["blocked"] += 1
            return False
        commit = await asyncio.to_thread(self.git.commit_task, worktree, p)
        if not commit:
            record.status, record.error = "BLOCKED", "worker produced no repository change"
            self.run_state["blocked"] += 1
            return False
        record.commit_shas.append(commit)
        review_round = 0
        while review_round <= self.cfg.max_review_rounds:
            commits = await asyncio.to_thread(self.git.commits_since, worktree, base)
            review = await self._review(p, worktree, base, commits)
            record.review = review
            verdict = str(review.get("verdict", "BLOCKED")).upper()
            if verdict == "ACCEPT":
                self.recovery.integration_head = await self.git.integrate(self.recovery.integration_root, commits)
                record.status, record.commit_shas = "ACCEPTED", commits
                self.run_state["accepted"] += 1
                if not self.cfg.keep_task_worktrees:
                    await asyncio.to_thread(self.git.remove_task_worktree, self.recovery.integration_root, worktree, branch)
                return True
            if verdict == "CHANGES_REQUESTED" and review_round < self.cfg.max_review_rounds:
                followup = "Apply only these architect review corrections, rerun affected validation, and do not commit: " + json.dumps(review.get("required_changes") or [review.get("summary")])
                result = await self.workers[worker_id].run(followup, worktree, session_id=result.session_id)
                next_commit = await asyncio.to_thread(self.git.commit_task, worktree, p) if not result.returncode else None
                if not next_commit:
                    record.status, record.error = "BLOCKED", result.error or "no review-fix change"
                    break
                record.commit_shas.append(next_commit)
                review_round += 1
                continue
            record.status, record.error = "BLOCKED", str(review.get("blocker") or review.get("summary") or "architect blocked task")
            break
        self.run_state["blocked"] += 1
        return False

    async def _takeover(self, run_id: str, max_tasks: int) -> None:
        assert self.project_root
        self.run_state = {"run_id": run_id, "status": "recovering", "message": "Preserving interrupted state", "started_at": utc_now(), "completed_at": None, "integration_branch": None, "integration_root": None, "source_root": str(self.project_root), "accepted": 0, "blocked": 0}
        self.tasks.clear()
        self.architect_session = None
        try:
            self.recovery = await asyncio.to_thread(self.git.prepare_recovery, self.project_root)
            self.run_state.update(status="planning", message="Reconstructing interrupted work", integration_branch=self.recovery.integration_branch, integration_root=str(self.recovery.integration_root), recovery_manifest=str(self.recovery.manifest_path), skipped_sensitive_paths=self.recovery.skipped_sensitive_paths)
            await self.bus.emit("system", "recovery.complete", "Recovery worktree prepared", asdict(self.recovery))
            first = True
            for _ in range(max_tasks):
                if self._stop_requested.is_set():
                    self.run_state.update(status="stopped", message="Stopped by operator", completed_at=utc_now())
                    return
                status, packet, decision = await self._next_task(first)
                first = False
                if status == "DONE":
                    self.run_state.update(status="completed", message=str(decision.get("state_summary") or "Backlog completed"), completed_at=utc_now(), final_evidence=decision.get("final_evidence", []))
                    return
                if status == "BLOCKED" or not packet:
                    self.run_state.update(status="blocked", message=str(decision.get("blocker") or decision.get("state_summary") or "Architect blocked"), completed_at=utc_now(), next_action=decision.get("next_action"))
                    return
                await self._execute_task(packet)
            self.run_state.update(status="paused", message=f"Safety cap of {max_tasks} tasks reached", completed_at=utc_now())
        except asyncio.CancelledError:
            self.run_state.update(status="stopped", message="Cancelled", completed_at=utc_now())
            raise
        except Exception as exc:
            self.run_state.update(status="error", message=str(exc), completed_at=utc_now())
            await self.bus.emit("system", "takeover.error", str(exc))
        finally:
            self._save_state()


class ProjectRequest(BaseModel):
    path: str
class TakeoverRequest(BaseModel):
    project: str | None = None
    max_tasks: int | None = Field(default=None, ge=1, le=500)
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
class ReasoningRequest(BaseModel):
    agent: str
    level: str


CONTROL_ROOM_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Friday Dispatcher</title><style>:root{color-scheme:dark;--bg:#090b10;--p:#111622;--p2:#171d2b;--line:#293246;--t:#e8edf7;--m:#909bb0;--g:#44d19d;--w:#f0bd55;--b:#ff6b7a;--a:#83a8ff}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 18% -10%,#1d2c51 0,transparent 35%),var(--bg);font-family:system-ui;color:var(--t)}button,input,select,textarea{font:inherit}.shell{max-width:1700px;margin:auto;padding:18px}.top{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:14px}.brand{font-weight:800;margin-right:auto}.brand small{display:block;color:var(--m);font-weight:500}.field{background:#0b0f18;border:1px solid var(--line);border-radius:9px;color:var(--t);padding:9px 11px}#project{min-width:380px;flex:1}.btn{border:1px solid var(--line);border-radius:9px;padding:9px 13px;background:var(--p2);color:var(--t);cursor:pointer}.primary{background:#244385}.danger{background:#612936}.runbar,.card,.panel{border:1px solid var(--line);background:rgba(17,22,34,.94);border-radius:13px}.runbar{padding:12px 14px;margin-bottom:14px;display:flex;gap:14px;flex-wrap:wrap}.pill{border:1px solid var(--line);border-radius:999px;padding:4px 9px;color:var(--m)}.agents{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.card{overflow:hidden;min-width:0}.head{padding:13px 14px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}.dot{width:10px;height:10px;border-radius:50%;background:var(--m)}.dot.idle{background:var(--g)}.dot.working,.dot.planning,.dot.reviewing,.dot.chatting{background:var(--w)}.dot.error,.dot.offline{background:var(--b)}.role,.meta{font-size:12px;color:var(--m)}.controls{margin-left:auto;display:flex;gap:7px}.meta,.limits{padding:10px 14px}.limit-line{display:flex;justify-content:space-between;font-size:12px}.bar{height:8px;background:#090d15;border:1px solid var(--line);border-radius:99px;overflow:hidden}.bar span{display:block;height:100%;background:linear-gradient(90deg,#477eea,#67d8bd)}.console{height:285px;overflow:auto;background:#070a10;border-top:1px solid var(--line);padding:10px;margin:0;font:12px/1.45 monospace;white-space:pre-wrap}.lower{display:grid;grid-template-columns:1.15fr .85fr;gap:12px;margin-top:12px}.panel{overflow:hidden}.panel h2{font-size:14px;margin:0;padding:12px 14px;border-bottom:1px solid var(--line)}.chatlog{height:330px;overflow:auto;padding:12px}.msg{padding:9px 11px;border-radius:10px;margin:7px 0;white-space:pre-wrap}.msg.user{background:#1e376d;margin-left:16%}.msg.assistant{background:#192131;margin-right:10%}.msg.error{background:#4a2028}.composer{display:flex;gap:8px;padding:10px;border-top:1px solid var(--line)}.composer textarea{flex:1}.tasks{max-height:405px;overflow:auto}.task{padding:10px 13px;border-bottom:1px solid var(--line)}@media(max-width:1100px){.agents,.lower{grid-template-columns:1fr}.console{height:220px}}</style></head><body><div class="shell"><div class="top"><div class="brand">FRIDAY DISPATCHER<small>Grok architect · Luna Goodman · Codex Spark</small></div><input id="project" class="field" placeholder="/absolute/path/to/friday"><button class="btn" onclick="setProject()">Open project</button><button class="btn primary" onclick="startTakeover()">Emergency takeover</button><button class="btn danger" onclick="post('/api/takeover/stop',{})">Stop</button><button class="btn" onclick="post('/api/limits/refresh',{})">Refresh limits</button></div><div class="runbar"><span id="runStatus" class="pill">idle</span><span id="runMessage"></span><span id="integration" class="role"></span></div><div class="agents" id="agents"></div><div class="lower"><section class="panel"><h2>Architect chat</h2><div id="chatlog" class="chatlog"></div><div class="composer"><textarea id="chat" class="field"></textarea><button class="btn primary" onclick="sendChat()">Send</button></div></section><section class="panel"><h2>Backlog ledger</h2><div id="tasks" class="tasks"></div></section></div></div><script>const A=['grok','luna','spark'],logs={grok:[],luna:[],spark:[]};let state;const e=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function api(p,b){let r=await fetch(p,{method:b===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:b===undefined?null:JSON.stringify(b)}),d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);return d}function card(id,a){let ls=a.limits?.windows||[],limits=ls.length?ls.map(w=>`<div><div class="limit-line"><span>${e(w.label)}</span><span>${Math.round(w.remaining_percent)}% left${w.resets_at?' · resets '+new Date(typeof w.resets_at==='number'?w.resets_at*1000:w.resets_at).toLocaleString():''}</span></div><div class="bar"><span style="width:${w.remaining_percent}%"></span></div></div>`).join(''):`<span class="role">${e(a.limits?.error||'pending')}</span>`,opts=a.reasoning_options.map(x=>`<option ${x===a.reasoning?'selected':''}>${x}</option>`).join('');return `<article class="card"><div class="head"><i class="dot ${a.phase}"></i><div><b>${e(a.display_name)}</b><div class="role">${e(a.role)}</div></div><div class="controls"><select class="field" onchange="reason('${id}',this.value)">${opts}</select><button class="btn" onclick="post('/api/agents/${id}/stop',{})">■</button></div></div><div class="meta">${e(a.model)}<br>${e(a.command.join(' '))}<br>${e(a.phase)} · ${e(a.message)}</div><div class="limits">${limits}</div><pre class="console" id="log-${id}">${e(logs[id].join('\n'))}</pre></article>`}function render(){if(!state)return;document.querySelector('#project').value=state.project_root||document.querySelector('#project').value;runStatus.textContent=state.run.status;runMessage.textContent=state.run.message||'';integration.textContent=state.run.integration_branch?`integration: ${state.run.integration_branch} · ${state.run.integration_root}`:'';agents.innerHTML=A.map(x=>card(x,state.agents[x])).join('');tasks.innerHTML=(state.tasks||[]).map(t=>`<div class="task"><b>${e(t.task_id)} · ${e(t.title)}</b><div class="role">${e(t.worker)} · ${e(t.status)} · ${e((t.commit_shas||[]).map(x=>x.slice(0,9)).join(', '))}</div></div>`).join('')||'<div class="task role">No tasks yet.</div>'}function event(x){if(A.includes(x.agent)){logs[x.agent].push(`${x.ts?.slice(11,19)||''} [${x.kind}] ${x.text||''}`);logs[x.agent]=logs[x.agent].slice(-700);let el=document.querySelector('#log-'+x.agent);if(el){el.textContent=logs[x.agent].join('\n');el.scrollTop=el.scrollHeight}}if(x.kind.startsWith('chat.')){let cls=x.kind==='chat.user'?'user':x.kind==='chat.assistant'?'assistant':'error';chatlog.insertAdjacentHTML('beforeend',`<div class="msg ${cls}">${e(x.text)}</div>`);chatlog.scrollTop=chatlog.scrollHeight}}async function reload(){state=await api('/api/state');render()}async function post(p,b){try{await api(p,b);setTimeout(reload,200)}catch(x){alert(x.message)}}function setProject(){post('/api/project',{path:project.value})}function startTakeover(){post('/api/takeover/start',{project:project.value||null})}function reason(agent,level){post('/api/reasoning',{agent,level})}function sendChat(){let m=chat.value.trim();if(m){chat.value='';post('/api/chat',{message:m})}}reload().then(()=>{(state.events||[]).forEach(event);render();let ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);ws.onmessage=m=>{let d=JSON.parse(m.data);if(d.type==='event'){event(d.event);setTimeout(reload,250)}else{state=d.state;render()}}});setInterval(reload,5000)</script></body></html>'''


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    dispatcher = Dispatcher(cfg or load_config())
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await dispatcher.start()
        yield
        await dispatcher.shutdown()
    app = FastAPI(title="Friday Emergency Dispatcher", version=VERSION, lifespan=lifespan)
    app.state.dispatcher = dispatcher
    @app.get("/", response_class=HTMLResponse)
    async def root() -> str: return CONTROL_ROOM_HTML
    @app.get("/api/state")
    async def state() -> dict[str, Any]: return dispatcher.public_state()
    @app.post("/api/project")
    async def project(req: ProjectRequest):
        try: return {"ok": True, "project_root": str(await dispatcher.set_project(req.path))}
        except Exception as exc: raise HTTPException(400, str(exc)) from exc
    @app.post("/api/reasoning")
    async def reasoning(req: ReasoningRequest):
        try: await dispatcher.set_reasoning(req.agent, req.level); return {"ok": True}
        except Exception as exc: raise HTTPException(400, str(exc)) from exc
    @app.post("/api/chat")
    async def chat(req: ChatRequest): return {"ok": True, "chat_id": dispatcher.queue_chat(req.message)}
    @app.post("/api/takeover/start")
    async def start(req: TakeoverRequest):
        try: return {"ok": True, "run_id": await dispatcher.start_takeover(req.project, req.max_tasks)}
        except Exception as exc: raise HTTPException(409, str(exc)) from exc
    @app.post("/api/takeover/stop")
    async def stop(): await dispatcher.stop_takeover(); return {"ok": True}
    @app.post("/api/limits/refresh")
    async def limits(): asyncio.create_task(dispatcher.refresh_limits()); return {"ok": True}
    @app.post("/api/agents/{agent_id}/stop")
    async def stop_agent(agent_id: str): return {"ok": True, "stopped": await dispatcher.stop_agent(agent_id)}
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"type": "snapshot", "state": dispatcher.public_state()})
        try:
            async with dispatcher.bus.subscribe() as queue:
                while True: await websocket.send_json({"type": "event", "event": await queue.get()})
        except WebSocketDisconnect: return
    return app


async def doctor(cfg: AppConfig) -> int:
    print(f"Friday Dispatcher {VERSION}")
    failures = 0
    for agent_id, agent in cfg.agents.items():
        executable = agent.command[0]
        resolved = executable if os.path.sep in executable else shutil.which(executable)
        print(f"\n[{agent_id}] {' '.join(agent.command)}")
        if not resolved or not Path(resolved).exists():
            print("  executable: NOT FOUND"); failures += 1; continue
        print("  executable:", resolved)
        async def log(_: str) -> None: return
        snapshot = await (probe_grok_limits(agent, log) if agent.kind == "grok" else probe_codex_limits(agent, log))
        print("  limits:", snapshot.error or ", ".join(f"{x.label} {x.remaining_percent:.0f}% left" for x in snapshot.windows))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Subscription-backed Friday emergency orchestrator")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.project: cfg.project_root = expand_path(args.project)
    if args.host: cfg.host = args.host
    if args.port: cfg.port = args.port
    if args.doctor: return asyncio.run(doctor(cfg))
    if cfg.host not in {"127.0.0.1", "localhost", "::1"}: print("WARNING: no UI authentication; non-loopback binding is unsafe", file=sys.stderr)
    if args.open:
        import threading
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{cfg.host}:{cfg.port}")).start()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)
    return 0


# Apply independently auditable safety and protocol hardening.
from dispatcher_hardening import apply_hardening as _apply_hardening
_apply_hardening(sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
