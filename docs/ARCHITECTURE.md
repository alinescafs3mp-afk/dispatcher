# Architecture

## Purpose

Sol Link Dispatcher is a local, subscription-backed control plane for Friday development. It exposes two operating profiles over the same hardened orchestration engine:

- `combat`: normal development with Sol as architect, SolGoodman as implementation owner, and optional Grok as a bounded helper;
- `reserve`: emergency continuity with Grok as temporary architect, Luna as primary worker, and Spark as micro-worker.

The control plane separates architecture, implementation, validation, review, integration, direct operator communication, and durable recovery.

## Profile indirection

The closed loop uses three stable logical keys:

```text
grok   architect slot
luna   primary worker slot
spark  secondary worker slot
```

`nightshift.profiles` maps those keys to physical subscription-backed CLI templates:

```text
COMBAT
  grok  -> codex              -> Sol / lead architect
  luna  -> codex-solgoodman   -> SolGoodman / implementation owner
  spark -> grok-build|grok    -> optional Grok helper

RESERVE
  grok  -> grok-build|grok    -> Grok / temporary architect
  luna  -> codex-solgoodman   -> Luna / primary worker
  spark -> codex              -> Spark / micro-worker
```

This indirection preserves the tested task packet and review state machine while preventing role logic from being duplicated. Public UI metadata exposes display names and physical CLI labels, while persisted task packets retain the stable logical key.

A profile may change only when no mission or participant turn is active. The orchestrator acquires the profile lock, doctor/quota locks, and all participant locks before replacing adapters. Every mission persists its profile and profile options. Resume validates the stored repository and worktree before restoring that profile.

## Components

```text
Browser UI
   | HTTP + WebSocket
FastAPI control room
   |
NightshiftOrchestrator
   |---- Profile resolver
   |---- StateDB (SQLite WAL)
   |---- EventHub (live Sol Link events)
   |---- ForensicsScanner
   |---- MissionWorkspace
   |---- ProcessRunner
   |
   |---- GrokAdapter ----------> Grok subscription CLI
   `---- CodexAdapter ---------> codex / codex-solgoodman subscriptions
```

No provider API key is required by the normal path. Child environments strip common API-key and credential-shaped variables so persisted CLI login remains authoritative.

## Mission lifecycle

1. **Select profile**: resolve logical participants to physical CLI adapters.
2. **Create mission**: persist goal, profile, profile options, and directive.
3. **Prepare workspace**: create a dedicated integration branch/worktree and detached architect worktree.
4. **Rescue**: copy safe dirty tracked and bounded untracked files from the source checkout into integration, commit there, and leave the source untouched.
5. **Discover capabilities and quotas**: inspect enabled CLIs.
6. **Repository dossier**: record Git, worktrees, backlog material, watcher state, logs, and account-specific sessions.
7. **Profile bootstrap**:
   - combat: Sol reconciles the human goal with code and the living backlog;
   - reserve: predecessor sessions provide read-only handoffs and Grok performs Phase Zero.
8. **Task loop**: one worker receives one bounded packet in one isolated worktree.
9. **Validation and review**: deterministic scope/risk enforcement and configured commands run before the architect reviews the real diff.
10. **Integration**: accepted patches enter the integration branch only when its HEAD still equals the worker base.
11. **Final audit**: a second explicit `done` decision is required after backlog reconciliation.

## Combat backlog ownership

The combat directive makes Sol responsible for forming, correcting, prioritising, and completing the backlog. The architect worktree is read-only, so modifying a backlog file is itself a reviewed implementation task. This prevents an architect response from claiming that repository state changed when no commit exists.

SolGoodman handles substantial investigation and implementation. Optional Grok is a secondary worker for bounded tasks with decided architectural intent. It does not become a second architect and cannot work in SolGoodman's worktree.

## Direct operator channels

Each active participant has a profile-isolated direct channel:

```text
operator -> participant read-only chat -> answer
operator -> queued nudge -> participant's next model turn
```

A direct chat acquires the participant lock while the profile lock still protects participant resolution. This prevents a profile switch from changing the physical CLI between address resolution and process launch.

Nudges are stored in SQLite with profile, mission, logical agent key, delivery status, and timestamps. The next eligible turn appends queued notes to its prompt under the participant lock. Successful adapter invocation acknowledges delivery. A direct channel never silently dispatches, edits, reviews, or integrates work.

## Sol Link contracts

Model-to-model communication is a durable, bounded ledger rather than an unrestricted group chat. Principal events include:

- `CONTRACT`
- `HANDOFF`
- `REVIEW`
- `CHANGES_REQUESTED`
- `ACCEPTED`
- `BLOCKER`
- `chat.queued`
- `chat.nudges_delivered`

Events carry task IDs, base/head SHA, paths, checks, decisions, participant IDs, and profile identity. Full diffs and full console transcripts remain artifacts inspected by reference.

## Git layout

For mission `ns-...`:

```text
source checkout                        # never checked out/reset by Dispatcher
~/.local/state/sol-link-nightshift/
  missions/ns-.../
    integration/                       # nightshift/ns-.../integration
    architect/                         # detached, reset to integration HEAD
    workers/<task>/<worker>/           # isolated task branch
    forensics/
    rescue/
    patches/
```

Worker integration is optimistic and serial. `integrate_worker` refuses a patch when integration HEAD moved since task creation.

## Deterministic enforcement

Before a worker commit:

- protected paths are discarded from the disposable worker tree;
- small text files are scanned for credential-shaped content;
- unsafe files are discarded;
- commit refuses if unsafe material remains.

Before acceptance:

- `allowed_paths`, `forbidden_paths`, and `max_files` are enforced;
- measured risk is raised by sensitive paths and change size;
- configured validation must pass;
- a model `accept` is overridden to `revise` when deterministic checks fail;
- high/critical risk is gated by the operator.

## Quota readers

### Codex

Dispatcher starts the configured Codex CLI app-server over stdio and performs:

```text
initialize
account/read
account/rateLimits/read
model/list
```

The multi-bucket response becomes percentage bars. `model/list` supplies reasoning options when available.

### Grok

Dispatcher starts the Grok ACP agent over stdio, initializes it, authenticates with the local `cached_token` method when required, and negotiates `x.ai/billing` or legacy `_x.ai/billing`. Token values are never extracted or logged.

A configurable text parser remains a fallback for CLI-specific quota commands.

## Persistence and restart

SQLite stores missions, profiles, agents, tasks, events, console logs, addressed chat, queued nudges, usage, and preferences. WAL and a busy timeout are enabled. At startup every nonterminal mission becomes paused and open tasks become blocked with an interruption reason.

Resume verifies mission status, repository identity, integration worktree, branch, and runtime directory before restoring the mission's profile. It then performs a fresh dossier scan and asks the active architect to reconcile database state against Git evidence.

The dispatcher does not assume a dead subprocess can be resumed. Conversation sessions are supplementary evidence; code continuity comes from Git.

## Realtime frontend

The frontend is plain HTML/CSS/JavaScript served by FastAPI. It receives an authoritative snapshot containing `event_seq`, then listens for persisted events over WebSocket.

The browser maintains separate applied and observed event sequence numbers. A gap, heartbeat indicating unseen events, stale socket, visibility restoration, or online transition triggers a single-flight snapshot resync. Request clocks prevent a late HTTP response from overwriting a newer WebSocket snapshot. The server sends application-level heartbeat frames every 15 seconds. Polling remains a fallback.

User text and logs are inserted with `textContent`, never as HTML.

## Trust boundary

Dispatcher is defence in depth, not a VM. Repository tests are code execution. Use an isolated OS identity or VM for hostile repositories, and do not expose the dashboard beyond loopback without authentication and TLS.
