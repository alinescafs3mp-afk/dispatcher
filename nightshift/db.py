from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import utc_now
from .redaction import redact, redact_value


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    base_sha TEXT NOT NULL DEFAULT '',
    integration_branch TEXT NOT NULL DEFAULT '',
    integration_path TEXT NOT NULL DEFAULT '',
    directive_path TEXT NOT NULL DEFAULT '',
    forensics_path TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    state TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    binary TEXT NOT NULL DEFAULT '',
    current_task TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    title TEXT NOT NULL,
    worker TEXT NOT NULL,
    status TEXT NOT NULL,
    risk TEXT NOT NULL,
    packet_json TEXT NOT NULL,
    base_sha TEXT NOT NULL DEFAULT '',
    worker_head TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 0,
    review_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(mission_id) REFERENCES missions(id)
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    stream TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_mission ON tasks(mission_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_mission ON events(mission_id, seq);
CREATE INDEX IF NOT EXISTS idx_logs_agent ON logs(agent_id, seq);
"""


class StateDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def upsert_agent(self, agent_id: str, role: str, state: str, model: str = "",
                     binary: str = "", current_task: str = "", session_id: str = "",
                     last_error: str = "", metadata: dict[str, Any] | None = None) -> None:
        now = utc_now()
        self.execute(
            """INSERT INTO agents(id,role,state,model,binary,current_task,session_id,last_error,metadata_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET role=excluded.role,state=excluded.state,
                 model=excluded.model,binary=excluded.binary,current_task=excluded.current_task,
                 session_id=CASE WHEN excluded.session_id='' THEN agents.session_id ELSE excluded.session_id END,
                 last_error=excluded.last_error,metadata_json=excluded.metadata_json,
                 updated_at=excluded.updated_at""",
            (agent_id, role, state, model, binary, current_task, session_id,
             redact(last_error), json.dumps(redact_value(metadata or {}), ensure_ascii=False), now),
        )

    def update_agent(self, agent_id: str, **fields: Any) -> None:
        allowed = {"state", "model", "binary", "current_task", "session_id", "last_error", "metadata_json"}
        pairs: list[tuple[str, Any]] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "last_error" and isinstance(value, str):
                value = redact(value)
            elif key == "metadata_json" and not isinstance(value, str):
                value = json.dumps(redact_value(value), ensure_ascii=False)
            pairs.append((key, value))
        if not pairs:
            return
        pairs.append(("updated_at", utc_now()))
        clause = ",".join(f"{key}=?" for key, _ in pairs)
        self.execute(f"UPDATE agents SET {clause} WHERE id=?", tuple(value for _, value in pairs) + (agent_id,))

    def create_mission(self, mission_id: str, repo: str, goal: str, status: str,
                       directive_path: str = "") -> None:
        now = utc_now()
        self.execute(
            "INSERT INTO missions(id,repo,goal,status,directive_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (mission_id, repo, redact(goal), status, directive_path, now, now),
        )

    def update_mission(self, mission_id: str, **fields: Any) -> None:
        allowed = {"status", "base_sha", "integration_branch", "integration_path",
                   "directive_path", "forensics_path", "summary"}
        pairs: list[tuple[str, Any]] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "summary" and isinstance(value, str):
                value = redact(value)
            pairs.append((key, value))
        if not pairs:
            return
        pairs.append(("updated_at", utc_now()))
        clause = ",".join(f"{key}=?" for key, _ in pairs)
        self.execute(f"UPDATE missions SET {clause} WHERE id=?",
                     tuple(value for _, value in pairs) + (mission_id,))

    def create_task(self, mission_id: str, task_id: str, packet: dict[str, Any],
                    status: str, base_sha: str = "") -> None:
        now = utc_now()
        self.execute(
            """INSERT INTO tasks(id,mission_id,title,worker,status,risk,packet_json,base_sha,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (task_id, mission_id, packet.get("title", task_id), packet.get("worker", ""), status,
             packet.get("risk", "low"), json.dumps(redact_value(packet), ensure_ascii=False), base_sha, now, now),
        )

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "base_sha", "worker_head", "attempt", "review_json", "result_json"}
        pairs: list[tuple[str, Any]] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key.endswith("_json"):
                if isinstance(value, str):
                    value = redact(value)
                else:
                    value = json.dumps(redact_value(value), ensure_ascii=False)
            pairs.append((key, value))
        if not pairs:
            return
        pairs.append(("updated_at", utc_now()))
        clause = ",".join(f"{key}=?" for key, _ in pairs)
        self.execute(f"UPDATE tasks SET {clause} WHERE id=?",
                     tuple(value for _, value in pairs) + (task_id,))

    def add_event(self, sender: str, recipient: str, event_type: str,
                  payload: dict[str, Any], mission_id: str = "", task_id: str = "") -> int:
        cur = self.execute(
            "INSERT INTO events(mission_id,task_id,sender,recipient,type,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (mission_id, task_id, sender, recipient, event_type,
             json.dumps(redact_value(payload), ensure_ascii=False), utc_now()),
        )
        return int(cur.lastrowid)

    def add_log(self, agent_id: str, text: str, stream: str = "stdout", task_id: str = "") -> int:
        cur = self.execute(
            "INSERT INTO logs(agent_id,task_id,stream,text,created_at) VALUES(?,?,?,?,?)",
            (agent_id, task_id, stream, redact(text), utc_now()),
        )
        return int(cur.lastrowid)

    def add_chat(self, role: str, text: str) -> int:
        cur = self.execute("INSERT INTO chat(role,text,created_at) VALUES(?,?,?)",
                           (role, redact(text), utc_now()))
        return int(cur.lastrowid)

    def set_preference(self, key: str, value: Any) -> None:
        self.execute(
            """INSERT INTO preferences(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                 updated_at=excluded.updated_at""",
            (key, json.dumps(redact_value(value), ensure_ascii=False), utc_now()),
        )

    def get_preference(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value_json FROM preferences WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value_json"])
        except (json.JSONDecodeError, TypeError):
            return default

    def add_usage(self, agent_id: str, task_id: str, usage: dict[str, int]) -> None:
        self.execute(
            """INSERT INTO usage(agent_id,task_id,input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (agent_id, task_id, int(usage.get("input_tokens", 0)),
             int(usage.get("cached_input_tokens", 0)), int(usage.get("output_tokens", 0)),
             int(usage.get("reasoning_tokens", 0)), utc_now()),
        )

    def snapshot(self, log_tail: int = 500) -> dict[str, Any]:
        missions = self.query("SELECT * FROM missions ORDER BY created_at DESC LIMIT 20")
        tasks = self.query("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 500")
        agents = self.query("SELECT * FROM agents ORDER BY id")
        chat = self.query("SELECT * FROM chat ORDER BY seq DESC LIMIT 200")
        chat.reverse()
        usage = self.query(
            """SELECT agent_id, SUM(input_tokens) input_tokens, SUM(cached_input_tokens) cached_input_tokens,
                      SUM(output_tokens) output_tokens, SUM(reasoning_tokens) reasoning_tokens
               FROM usage GROUP BY agent_id"""
        )
        logs: dict[str, list[dict[str, Any]]] = {}
        for agent in agents:
            rows = self.query("SELECT * FROM logs WHERE agent_id=? ORDER BY seq DESC LIMIT ?",
                              (agent["id"], log_tail))
            rows.reverse()
            logs[agent["id"]] = rows
        for row in tasks:
            for key in ("packet_json", "review_json", "result_json"):
                try:
                    row[key[:-5]] = json.loads(row.get(key) or "{}")
                except json.JSONDecodeError:
                    row[key[:-5]] = {}
        for agent in agents:
            try:
                agent["metadata"] = json.loads(agent.get("metadata_json") or "{}")
            except json.JSONDecodeError:
                agent["metadata"] = {}
        return {"missions": missions, "tasks": tasks, "agents": agents,
                "chat": chat, "usage": usage, "logs": logs}
