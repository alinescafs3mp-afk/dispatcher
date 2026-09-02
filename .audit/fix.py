#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace(path: str, old: str, new: str, expected: int = 1) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{path}: expected {expected} matches, found {count}: {old[:100]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


replace(
    "nightshift/orchestrator.py",
    'raise OrchestratorError(f"Architect output repair failed: {repair.error}")',
    'raise OrchestratorError(f"Architect output repair failed: {repair.error}") from first_error',
)
replace(
    "nightshift/orchestrator.py",
    'raise OrchestratorError("Review repair failed")',
    'raise OrchestratorError("Review repair failed") from exc',
)
replace(
    "nightshift/orchestrator.py",
    'latest_review.required_changes + ["Make configured validation pass or provide an explicit safe replacement command"]',
    '[*latest_review.required_changes, "Make configured validation pass or provide an explicit safe replacement command"]',
)

print("Manual lint corrections applied")
