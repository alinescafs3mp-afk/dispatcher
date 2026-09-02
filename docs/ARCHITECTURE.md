# Architecture

## Purpose

Sol Link Nightshift is an emergency continuity control plane. It is deliberately narrower than a general multi-agent framework. Its job is to recover an interrupted Friday development state and keep the backlog moving with one architect and two differently sized worker lanes.

## Components

```text
Browser UI
   | HTTP + WebSocket
FastAPI control room
   |
NightshiftOrchestrator
   |---- StateDB (SQLite WAL)
   |---- EventHub (live Sol Link events)
   |---- ForensicsScanner
   |---- MissionWorkspace
   |---- ProcessRunner
   |
   |---- GrokAdapter ----------> grok-build subscription CLI
   |---- CodexAdapter (Luna) --> codex-solgoodman subscription CLI
   `---- CodexAdapter (Spark) -> codex subscription CLI
```

No provider API key is required or accepted by the normal path. Child environments strip common API-key and credential-shaped variables so the persisted CLI login remains authoritative.

## Mission lifecycle

1. **Create mission**: persist goal and directive.
2. **Prepare workspace**: create a dedicated integration branch/worktree and detached architect worktree.
3. **Rescue**: copy safe dirty tracked and bounded untracked files from the source checkout into integration, commit there, and leave the source untouched.
4. **Discover capabilities and quotas**: inspect all CLIs.
5. **Forensics**: write JSON and Markdown dossiers from Git, worktrees, backlog material, watcher state, logs and account-specific sessions.
6. **Predecessor handoff**: resume matching Sol/SolGoodman sessions read-only through the fallback Codex accounts.
7. **Architect bootstrap**: Grok reconciles evidence and returns one typed action.
8. **Task loop**: one worker receives one task packet in one isolated worktree.
9. **Validation and review**: deterministic scope/risk enforcement and configured commands run before Grok reviews the real diff.
10. **Integration**: accepted patches are applied to the integration branch only when its HEAD still equals the worker base.
11. **Final audit**: a second explicit `done` decision is required after backlog reconciliation.

## Sol Link contracts

Model-to-model communication is not an unrestricted group chat. The durable ledger records compact events:

- `CONTRACT`
- `HANDOFF`
- `REVIEW`
- `CHANGES_REQUESTED`
- `ACCEPTED`
- `BLOCKER`

The repository contains source truth. Events carry task IDs, base/head SHA, paths, checks and decisions. Full diffs and full console transcripts remain artifacts and are inspected by reference.

## Git layout

For mission `ns-...`:

```text
source checkout                        # never checked out/reset by Nightshift
~/.local/state/sol-link-nightshift/
  missions/ns-.../
    integration/                       # nightshift/ns-.../integration
    architect/                         # detached, reset to integration HEAD
    workers/<task>/<worker>/           # isolated task branch
    forensics/
    rescue/
    patches/
```

Worker integration is optimistic and serial. `integrate_worker` refuses to apply a patch when integration HEAD moved since the task was created. This prevents a worker built against stale assumptions from sliding into a changed branch.

## Deterministic enforcement

Before a worker commit:

- protected paths are discarded from the disposable worker tree;
- small text files are scanned for credential-shaped content;
- unsafe files are discarded;
- commit refuses if unsafe material remains.

Before acceptance:

- changed paths must match allowed/forbidden/max-file rules;
- measured risk is raised by sensitive paths and change size;
- validation must pass;
- a model `accept` is overridden to `revise` when deterministic checks fail;
- high/critical risk is gated by the operator.

## Quota readers

### Codex

Nightshift starts the configured CLI's local app-server over stdio and performs:

```text
initialize
account/read
account/rateLimits/read
model/list
```

The multi-bucket response is normalized into CLI-like percentage bars. `model/list` supplies reasoning options when available.

### Grok

Nightshift starts Grok's ACP agent over stdio, initializes it, authenticates with the local `cached_token` method when required, and negotiates the available billing extension (`x.ai/billing` or the legacy `_x.ai/billing`). Token values are never extracted or logged.

A configurable text parser exists only as a fallback for a CLI-specific quota command.

## Persistence and restart

SQLite stores missions, agents, tasks, events, console logs, chat, usage and preferences. WAL is enabled. At startup every nonterminal mission becomes paused. Resuming performs a fresh forensic scan and asks Grok to reconcile DB state against preserved Git branches/worktrees.

The tool does not assume a dead subprocess can be safely resumed. It resumes conversation sessions where supported and reconstructs code work from Git.

## Frontend

The frontend is plain HTML/CSS/JavaScript served by FastAPI. WebSocket events trigger compact state refreshes. There is no npm toolchain. User text and logs are inserted with `textContent`, not HTML.

## Trust boundary

Nightshift is defense in depth, not a VM. A target repository's tests are code execution. Use an isolated OS identity or VM for hostile repositories, and do not expose the dashboard beyond loopback without adding authentication and TLS.
