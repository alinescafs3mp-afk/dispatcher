#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace(path: str, old: str, new: str, expected: int = 1) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{path}: expected {expected} matches, found {count}: {old[:80]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


replace(
    "nightshift/orchestrator.py",
    '''        prompt = chat_prompt(text) + "

Current compact mission ledger:
" + compact_text(
''',
    '''        prompt = chat_prompt(text) + "\\n\\nCurrent compact mission ledger:\\n" + compact_text(
''',
)
replace(
    "nightshift/orchestrator.py",
    '''                        "error": (result.error + f"
Architect worktree reset failed: {exc}").strip(),
''',
    '''                        "error": (result.error + f"\\nArchitect worktree reset failed: {exc}").strip(),
''',
    expected=2,
)
replace(
    "nightshift/orchestrator.py",
    '''                    "Nightshift ledger, and the prompt below. The emergency directive remains binding.

"
''',
    '''                    "Nightshift ledger, and the prompt below. The emergency directive remains binding.\\n\\n"
''',
)
replace(
    "nightshift/static/app.js",
    ").join('\n');",
    ").join('\\n');",
)
replace(
    "nightshift/db.py",
    "tuple(value for _, value in pairs) + (agent_id,)",
    "(*tuple(value for _, value in pairs), agent_id)",
)
replace(
    "nightshift/db.py",
    "tuple(value for _, value in pairs) + (mission_id,)",
    "(*tuple(value for _, value in pairs), mission_id)",
)
replace(
    "nightshift/db.py",
    "tuple(value for _, value in pairs) + (task_id,)",
    "(*tuple(value for _, value in pairs), task_id)",
)
replace(
    "nightshift/quota.py",
    "data, billing_method, next_id = await billing_any(2)",
    "data, billing_method, _next_id = await billing_any(2)",
)
replace(
    "nightshift/sessions.py",
    '''        elif subtype in {"agent_message", "assistant_message", "assistant"} or role == "assistant":
            if text:
                assistants.append(text)
''',
    '''        elif text and (
            subtype in {"agent_message", "assistant_message", "assistant"}
            or role == "assistant"
        ):
            assistants.append(text)
''',
)
replace(
    "nightshift/validation.py",
    '''        async def on_line(stream: str, line: str) -> None:
            lines.append(f"[{stream}] {line}")
            await event("log", {"stream": f"validation-{stream}", "text": line})
''',
    '''        async def on_line(
            stream: str, line: str, captured: list[str] = lines
        ) -> None:
            captured.append(f"[{stream}] {line}")
            await event("log", {"stream": f"validation-{stream}", "text": line})
''',
)

print("Final audit corrections applied")
