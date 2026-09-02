# Validation record

## Automated coverage

The repository test suite exercises the dispatcher without vendor credentials by using fake local CLI processes that implement the relevant streaming and RPC shapes.

Covered behavior:

- default command/profile mapping for `grok-build`, `codex-solgoodman`, and `codex`;
- TOML configuration and per-agent reasoning selection;
- Codex App Server parsing with multiple independent buckets, including `gpt-reserve`;
- Grok ACP billing-response parsing when the optional extension is exposed;
- JSON decision recovery from plain and fenced model output;
- sensitive-path detection;
- preservation of dirty tracked and untracked WIP in a separate integration worktree;
- preservation of the original checkout;
- Spark safety routing;
- explicit `allowed_paths` and `forbidden_paths` enforcement before commit;
- end-to-end `Grok plan -> Spark implementation -> Grok review -> cherry-pick -> DONE`;
- compact Sol Link lifecycle events: `CONTRACT`, `HANDOFF`, `REVIEW`, and `ACCEPT`.

Run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

## Host acceptance test

Vendor login state and preview CLI flags are machine-local and cannot be reproduced in CI. On the actual Friday host, run:

```bash
cp dispatcher.example.toml dispatcher.toml
# Set server.project_root first.
friday-dispatcher --config dispatcher.toml --doctor
friday-dispatcher --config dispatcher.toml --open
```

Acceptance checks on that host:

1. Grok Build reports an available executable and opens an architect chat turn.
2. The `codex-solgoodman` profile reports the expected account buckets and can execute a disposable Luna task.
3. The `codex` profile reports its own buckets and can execute a disposable Spark task.
4. Reasoning changes affect the next process invocation for each lane.
5. The UI renders every returned limit window and its reset time.
6. An emergency run creates a new `dispatcher/emergency-*` integration worktree without changing the original branch or dirty tree.
7. No production merge or deployment occurs automatically.

A failed optional Grok billing probe is not a failed Grok connection. Some Grok Build versions or subscription tiers do not expose a percentage through the ACP billing extension; chat and orchestration remain usable, and the card displays the limitation explicitly.
