# Friday Emergency Dispatcher

A local, subscription-backed control room for continuing development of **Friday** after both original Sol agents hit their Codex limits unexpectedly.

The dispatcher uses the CLI sessions that are already authenticated on the machine. It does **not** require OpenAI or xAI API keys.

| Lane | Default executable | Model | Responsibility |
|---|---|---|---|
| Chief architect | `grok-build` | Grok 4.6 | recovery, backlog selection, task contracts, diff review, acceptance, operator chat |
| Implementation owner | `codex-solgoodman` | GPT-5.6 Luna | investigative, multi-file, integration and medium-risk work |
| Micro-worker | `codex` | GPT-5.3 Codex Spark | deterministic, low-risk, one-to-three-file operations |

The defaults match the requested local command/profile mapping:

- `codex` is the former SolGoodman account and is routed to Spark.
- `codex-solgoodman` is the former Sol architect account and is routed to Luna Reserve.
- `grok-build` is the logged-in Grok Build client.

## What is included

- A FastAPI control room with three live console panes.
- Per-model reasoning selectors.
- Codex limit discovery through `codex app-server` and `account/rateLimits/read`, including separate buckets such as `gpt-reserve` when the account exposes them.
- A best-effort Grok credit probe through the Grok ACP `x.ai/billing` extension.
- A direct operator chat with the acting Grok architect.
- Emergency takeover mode that reconstructs interrupted work and repeatedly runs `Grok plan -> Luna/Spark implementation -> Grok review`.
- Dirty-tree rescue without altering the original checkout.
- Isolated Git worktrees and branches for every worker task.
- Secret-path guards and no automatic merge into the owner's original branch.
- A compact append-only Sol Link journal plus an optional JSONL bridge for existing watcher scripts.
- A repository-root emergency directive in [`EMERGENCY_TAKEOVER_DIRECTIVE.md`](EMERGENCY_TAKEOVER_DIRECTIVE.md).

## Install

Python 3.11 or newer and Git are required.

```bash
git clone https://github.com/alinescafs3mp-afk/dispatcher.git
cd dispatcher
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
cp dispatcher.example.toml dispatcher.toml
```

Edit `dispatcher.toml` and set the Friday repository path. Then check the three logged-in CLI profiles:

```bash
friday-dispatcher --config dispatcher.toml --doctor
```

Start the control room:

```bash
friday-dispatcher --config dispatcher.toml --open
```

Open `http://127.0.0.1:8765` when `--open` is not used.

The server has no authentication and therefore binds to loopback by default. Do not expose it to a LAN or the internet without putting an authenticated reverse proxy in front of it.

## First emergency run

1. Enter the absolute path to Friday and press **Open project**.
2. Confirm the three cards show the expected executables and accounts.
3. Refresh limits. Codex should show the same buckets returned by the CLI account, including Luna Reserve when available.
4. Select reasoning levels. The conservative defaults are Grok `xhigh`, Luna `high`, Spark `medium`.
5. Press **Emergency takeover**.

The dispatcher then:

1. Resolves the actual Git root and records the original HEAD.
2. Detects tracked and untracked WIP left by the interrupted Sol session.
3. Creates a separate `dispatcher/emergency-*` integration branch and worktree.
4. Applies safe tracked changes and copies safe untracked files into that worktree, then commits the rescued WIP there.
5. Excludes `.env*`, keys, tokens, credential paths and oversized untracked files. Exclusions are shown in the UI and recovery manifest.
6. Gives Grok the repository inventory, recent commits, backlog/handoff candidates, rescued paths and execution ledger.
7. Grok selects one implementation-ready task and emits a strict task packet.
8. The router downgrades unsafe Spark assignments to Luna automatically.
9. The worker edits an isolated task worktree and runs the requested validation.
10. The dispatcher rejects sensitive paths, creates an audit commit, and asks Grok to review the actual `base..HEAD` diff.
11. Only an `ACCEPT` verdict allows commits to be cherry-picked into the emergency integration branch.
12. Grok re-inspects the current repository and selects the next item until the backlog is complete, blocked with evidence, stopped by the operator, or the task safety cap is reached.

The original checkout is never reset, stashed, switched or force-updated. Finished work remains on the integration branch displayed at the top of the UI. Review and merge it manually when the original Sol agents return.

## Luna model selection

The example explicitly selects `gpt-5.6-luna`:

```toml
[agents.luna]
model = "gpt-5.6-luna"
```

Some Codex rollouts expose Luna only as an automatic reserve fallback after the normal weekly limit is exhausted. In that case, set the model to an empty string so the account chooses its available fallback:

```toml
model = ""
```

The card then displays `account default / reserve fallback`.

## Reasoning controls

Reasoning changes in the UI affect subsequent processes and turns. They do not mutate vendor account settings. A currently running turn keeps the level with which it was launched.

The supported lists are configurable per agent because preview clients sometimes expose different subsets:

```toml
reasoning_options = ["low", "medium", "high", "xhigh", "max"]
```

## Limits

### Codex

Each Codex profile is probed through its own executable:

```text
<profile command> app-server
initialize
account/rateLimits/read
```

The response may contain multiple independent limit IDs and primary/secondary windows. The UI renders every returned window rather than assuming one five-hour and one weekly bucket.

### Grok

The dispatcher attempts:

```text
grok-build agent ... stdio
initialize
x.ai/billing
```

Grok Build versions and subscription rollouts do not all expose the same billing extension. Failure of this optional probe is shown as `billing percentage unavailable` and does not disable chat or orchestration.

## Sol Link and old watcher scripts

The dispatcher always writes compact protocol events to:

```text
~/.local/state/friday-dispatcher/sol-link/events.jsonl
```

Events use the control vocabulary:

```text
CONTRACT -> HANDOFF -> REVIEW -> ACCEPT/BLOCKER -> COMPLETE
```

They contain task IDs, SHAs, paths, validation summaries and risk records, not full transcripts or repeated diffs.

To consume an existing watcher's JSONL output, enable the bridge:

```toml
[sol_link]
enabled = true
inbox = "/absolute/path/to/legacy/sol-link/events.jsonl"
poll_seconds = 1.0
```

Recognized inbound events:

- `CHAT` or `USER_CHAT` addressed to `grok` / `grok-architect`.
- `CONTROL` with `action = "start_takeover"`.
- `CONTRACT` addressed to `luna-goodman` or `spark-worker` with a task packet.

Each watcher still needs its own cursor, session ID, process lock and worktree. Sharing the Python watcher engine is fine; sharing mutable watcher state between Luna and Spark is not.

## Configuration

All CLI commands are arrays, so wrappers with fixed flags can be configured without shell interpolation:

```toml
command = ["/home/user/bin/codex-solgoodman"]
```

Shell aliases are not visible to subprocesses. Convert an alias into a small executable wrapper when necessary.

Runtime state, recovery manifests, event logs, prompt scratch files and worktrees live under `server.state_dir`. Keep that directory outside the Friday repository.

## Operational safety

This is an integration-branch machine, not an unattended production deployer.

- It does not push, merge into the original branch, force-update refs, run database migrations on external systems, or deploy services.
- Worker CLIs receive autonomous workspace-write permission only inside disposable task worktrees.
- Grok runs read-only as architect/reviewer.
- A worker touching a sensitive path causes the task commit to be refused.
- High-risk security, privilege, sandbox, destructive migration and trust-boundary work can be prepared and reviewed, but should remain visibly flagged for final Sol/operator review.
- Keep the UI on loopback.

## Tests

```bash
pip install -e '.[dev]'
pytest -q
```

The suite covers configuration, JSON recovery, Codex/Grok limit parsing, Spark safety routing, sensitive-path detection, dirty Git recovery, and a complete fake-CLI takeover cycle through planning, implementation, commit, Grok review, cherry-pick and `DONE`.

## Repository layout

The application is deliberately kept in one auditable Python module. This makes it easy to transplant pieces from older watcher prototypes and avoids a frontend build chain during an emergency.

```text
dispatcher.py                       application, adapters, Git safety and UI
EMERGENCY_TAKEOVER_DIRECTIVE.md    authoritative team directive
dispatcher.example.toml            machine/profile mapping
pyproject.toml                     packaging and dependencies
tests/test_dispatcher.py           regression and fake-CLI integration tests
```
