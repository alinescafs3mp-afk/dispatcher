#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLY = ROOT / ".profiles" / "apply_orchestrator.py"

source = APPLY.read_text(encoding="utf-8")
problem = 'replace_once("        self._grok_chat_session_id = None\\n", "")\n'
if source.count(problem) != 2:
    raise RuntimeError("unexpected legacy chat-session replacement count")
source = source.replace(
    problem,
    'text = text.replace("        self._grok_chat_session_id = None\\n", "")\n',
    1,
)
source = source.replace(problem, "", 1)

# These templates contain Python string literals for the generated file. Raw
# outer strings preserve their backslash escapes instead of materialising
# accidental physical newlines inside quoted strings.
for marker in ("methods = '''", "chat_method = '''"):
    if source.count(marker) != 1:
        raise RuntimeError(f"profile template marker not found exactly once: {marker}")
    source = source.replace(marker, marker.replace("= '''", "= r'''"), 1)

namespace = {"__file__": str(APPLY), "__name__": "__main__"}
exec(compile(source, str(APPLY), "exec"), namespace)

orchestrator_path = ROOT / "nightshift" / "orchestrator.py"
orchestrator = orchestrator_path.read_text(encoding="utf-8")

# Validate mission state before switching the live process into the mission's
# stored profile. A rejected resume request must not have side effects.
old = '''        row = rows[0]
        stored_profile = str(row.get("profile") or "reserve")
        try:
            stored_options = json.loads(row.get("profile_options_json") or "{}")
        except json.JSONDecodeError:
            stored_options = {}
        await self.set_profile(
            stored_profile,
            combat_grok_enabled=bool(
                stored_options.get(
                    "combat_grok_enabled",
                    self.combat_grok_enabled,
                )
            ),
            persist=True,
        )
        if row["status"] not in _RESUMABLE_MISSIONS:
            raise OrchestratorError(
                f"Mission in state {row['status']!r} cannot be resumed"
            )
'''
new = '''        row = rows[0]
        if row["status"] not in _RESUMABLE_MISSIONS:
            raise OrchestratorError(
                f"Mission in state {row['status']!r} cannot be resumed"
            )
        stored_profile = str(row.get("profile") or "reserve")
        try:
            stored_options = json.loads(row.get("profile_options_json") or "{}")
        except json.JSONDecodeError:
            stored_options = {}
        await self.set_profile(
            stored_profile,
            combat_grok_enabled=bool(
                stored_options.get(
                    "combat_grok_enabled",
                    self.combat_grok_enabled,
                )
            ),
            persist=True,
        )
'''
if orchestrator.count(old) != 1:
    raise RuntimeError("generated resume-profile block was not found")
orchestrator = orchestrator.replace(old, new, 1)

# Fail here with a compact syntax error rather than letting Ruff report a
# cascade hundreds of lines downstream.
compile(orchestrator, str(orchestrator_path), "exec")
orchestrator_path.write_text(orchestrator, encoding="utf-8", newline="\n")

# Older databases do not have the new chat columns when executescript first
# runs. Create the channel index only after the additive migration.
db_path = ROOT / "nightshift" / "db.py"
db_text = db_path.read_text(encoding="utf-8")
old_index = 'CREATE INDEX IF NOT EXISTS idx_chat_channel ON chat(profile, agent_key, seq);\n"""'
if db_text.count(old_index) != 1:
    raise RuntimeError("chat index position was not found")
db_text = db_text.replace(old_index, '"""', 1)
compile(db_text, str(db_path), "exec")
db_path.write_text(db_text, encoding="utf-8", newline="\n")

print("profile refactor, compatibility fixes, and syntax preflight applied")
