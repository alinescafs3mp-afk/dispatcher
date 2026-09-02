from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class AgentState(StrEnum):
    OFFLINE = "offline"
    IDLE = "idle"
    RECOVERING = "recovering"
    PLANNING = "planning"
    WORKING = "working"
    REVIEWING = "reviewing"
    WAITING = "waiting"
    LIMITED = "limited"
    ERROR = "error"
    STOPPED = "stopped"


class MissionState(StrEnum):
    CREATED = "created"
    RECOVERING = "recovering"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_HUMAN = "awaiting_human"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


class TaskState(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    REVISION = "revision"
    AWAITING_HUMAN = "awaiting_human"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    FAILED = "failed"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Usage(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class AgentResult(BaseModel):
    ok: bool
    returncode: int = 0
    final_text: str = ""
    session_id: str | None = None
    usage: Usage = Field(default_factory=Usage)
    limit_detected: bool = False
    error: str = ""
    raw_events: int = 0


class TaskPacket(BaseModel):
    id: str = ""
    title: str
    goal: str
    worker: Literal["luna", "spark"]
    context: str = ""
    source_ref: str = ""
    architectural_intent: str = ""
    allowed_paths: list[str] = Field(default_factory=list, min_length=1, max_length=64)
    forbidden_paths: list[str] = Field(default_factory=list, max_length=64)
    acceptance_criteria: list[str] = Field(default_factory=list, min_length=1, max_length=64)
    validation_commands: list[str] = Field(default_factory=list, max_length=32)
    stop_conditions: list[str] = Field(default_factory=list, max_length=32)
    risk: RiskLevel = RiskLevel.LOW
    max_files: int | None = Field(default=None, ge=1, le=100)

    @field_validator("allowed_paths", "forbidden_paths", mode="before")
    @classmethod
    def normalize_paths(cls, value: Any) -> list[str]:
        if value is None:
            return []
        items = [value] if isinstance(value, str) else value
        return [str(item).strip() for item in items if str(item).strip()]


class ArchitectDispatch(BaseModel):
    action: Literal["dispatch"]
    summary: str = ""
    task: TaskPacket
    backlog_remaining_estimate: int | None = None
    confidence: Literal["low", "medium", "high"] = "medium"


class ArchitectDone(BaseModel):
    action: Literal["done"]
    summary: str
    evidence: list[str] = Field(default_factory=list)
    remaining_items: list[str] = Field(default_factory=list)


class ArchitectBlocked(BaseModel):
    action: Literal["blocked", "ask_user"]
    summary: str
    blockers: list[str] = Field(default_factory=list)
    requested_input: str = ""


class ReviewDecision(BaseModel):
    action: Literal["accept", "revise", "reject", "escalate"]
    summary: str
    findings: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    residual_risk: RiskLevel = RiskLevel.LOW
    acceptance_evidence: list[str] = Field(default_factory=list)


class QuotaWindow(BaseModel):
    id: str
    label: str
    used_percent: float | None = None
    left_percent: float | None = None
    window_minutes: int | None = None
    resets_at: int | None = None
    resets_at_text: str = ""
    source: str = ""


class QuotaSnapshot(BaseModel):
    agent_id: str
    available: bool
    account: dict[str, Any] = Field(default_factory=dict)
    windows: list[QuotaWindow] = Field(default_factory=list)
    plan_type: str = ""
    reached_type: str | None = None
    message: str = ""
    fetched_at: str = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)
