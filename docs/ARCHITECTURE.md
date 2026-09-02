# Architecture

## Purpose

Sol Link Dispatcher is a narrow local control plane for one Git repository, declared operational context roots, and one subscription-backed model team. It exposes two operating profiles over the same durable mission engine:

- **combat** for normal development with Sol, SolGoodman and optional Grok;
- **reserve** for emergency takeover with Grok, Luna and Spark.

It is not a generic free-form group chat. One logical architect serializes task selection, one worker owns each packet, deterministic checks outrank model prose, and accepted changes land only in an isolated integration branch.

### Repository and operational roots

`project.repo` is the Git-backed product root and the only implicit integration scope. `project.operational_roots` lists additional Jericho work surfaces, such as `~/.jericho`, that may contain predecessor session cwd evidence, handoffs, watcher state, backlog fragments, or other continuity artifacts. The active profile prompt names all configured roots, session ranking accepts cwd values under any of them, and the forensic scanner reads only configured backlog/watcher patterns while applying protected-path filters and redaction.

Full-access participants may inspect or maintain an operational root when the mission explicitly requires it. Operational roots do not become automatic Git integration scopes: persistent product changes still pass through mission worktrees, deterministic scope enforcement, review, and integration, while host-side operational effects remain operator-managed.

## Stable logical lanes and physical CLIs

The orchestration loop keeps three stable logical keys so profile switching does not duplicate the state machine:

```text
logical grok   = active architect
logical luna   = primary implementation owner
logical spark  = secondary or micro worker
```

The profile resolver maps those keys to physical CLIs:

```text
COMBAT
  grok  -> codex             -> Sol, lead architect
  luna  -> codex-solgoodman  -> SolGoodman, implementation owner
  spark -> grok-build/grok   -> optional Grok helper

RESERVE
  grok  -> grok-build/grok   -> temporary architect
  luna  -> codex-solgoodman  -> Luna, primary worker
  spark -> codex             -> Spark, micro worker
```

Agent IDs differ by profile. The browser receives only active-profile agent rows, usage and logs, while missions and chat retain their profile provenance in SQLite.

## Permission posture

Permissions are resolved together with the profile, not inferred from a display name:

- combat Sol: Ultra reasoning and Codex full-access bypass for automated architect/review turns;
- combat SolGoodman: Ultra reasoning and Codex full-access bypass for implementation turns;
- optional combat Grok helper: Grok `--always-approve --sandbox off` for implementation turns;
- reserve Grok architect/reviewer: Grok `--always-approve --sandbox off` for automated turns;
- reserve Luna: Max reasoning and Codex full-access bypass for implementation turns;
- reserve Spark: Codex full-access bypass for implementation turns.

Every direct operator chat is read-only, including chats with full-access participants. Combat Sol's automated full-access turns run in the detached architect worktree, which is hard-reset to the integration HEAD before and after each turn. Persistent product changes still flow through worker branches and review.

## Components

```text
Browser UI
   | HTTP + WebSocket
FastAPI control room
   |
NightshiftOrchestrator
   |---- profile resolver and adapter remapping
   |---- StateDB (SQLite WAL + additive migrations)
   |---- EventHub (live Sol Link events)
   |---- ForensicsScanner
   |---- MissionWorkspace
   |---- ProcessRunner
   |
   |---- CodexAdapter -------> codex / codex-solgoodman
   `---- GrokAdapter --------> grok-build / grok
```

No provider API key is required by the normal path. Child environments strip common API-key and credential-shaped variables so the persisted consumer CLI login remains authoritative.

## Mission lifecycle

1. **Select profile**: resolve roles, binaries, reasoning, permissions and optional participants.
2. **Create mission**: persist goal, profile, profile options and the matching directive.
3. **Prepare workspace**: create dedicated integration and detached architect worktrees.
4. **Rescue**: copy safe dirty tracked and bounded untracked files from the source checkout into integration, commit there and leave the source untouched.
5. **Discover capabilities and quotas**: inspect every active CLI under per-agent locks.
6. **Forensics**: write JSON and Markdown dossiers from Git, worktrees, and bounded backlog/watcher material across the repository and declared operational roots. Reserve also scans account-specific predecessor sessions; combat deliberately skips that emergency-only history.
7. **Profile bootstrap**:
   - reserve resumes matching predecessor sessions read-only and reconstructs the interruption;
   - combat reconciles and improves the live backlog without cross-resuming reserve identities.
8. **Task loop**: the architect emits one typed task packet for one worker.
9. **Validation and review**: scope/risk enforcement and configured commands run before the architect reviews the real diff.
10. **Integration**: an accepted patch is applied only when integration HEAD still equals the worker base.
11. **Final audit**: a second explicit `done` decision is required after backlog reconciliation.

## Direct lines and durable nudges

Each logical participant has its own chat channel. A direct message acquires that lane briefly and performs a read-only model turn with an independently persisted chat session.

When a lane is busy, `auto` delivery becomes a nudge. The prompt is prepared under the participant lock. The row remains `queued` until the turn succeeds or provider-side events/final text prove that the note reached a model turn. A failure before any such evidence emits a deferred event and leaves the note queued, providing at-least-once delivery semantics.

Chat history stores profile, logical key, physical agent ID, mission, kind, status and delivery timestamp. This prevents messages from another profile from appearing in the wrong direct line.

## Sol Link contracts

Model-to-model communication uses a compact durable ledger:

- `CONTRACT`
- `HANDOFF`
- `REVIEW`
- `CHANGES_REQUESTED`
- `ACCEPTED`
- `BLOCKER`

Events carry task IDs, base/head SHA, paths, checks and decisions. The repository remains source truth. Full diffs and complete logs remain artifacts inspected by reference.

## Git layout

For mission `ns-...`:

```text
source checkout                        # never reset by Dispatcher
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

- changed paths must satisfy allowed, forbidden and max-file rules;
- measured risk is raised by sensitive paths and change size;
- validation must pass;
- a model `accept` is overridden to `revise` when deterministic checks fail;
- high and critical risk is gated by the operator.

Full CLI permissions do not disable any of these post-turn controls.

## Persistence and restart

SQLite stores missions, profile options, agents, tasks, events, logs, per-participant chat, nudges, usage and preferences. WAL and a busy timeout are enabled. Migrations are additive for databases created by pre-profile releases.

At startup every interrupted mission becomes paused and open tasks become blocked with interruption evidence. Resume restores the mission's original profile and optional Grok setting, validates the preserved worktree, performs fresh forensics, and asks the active architect to reconcile the ledger against Git.

## Realtime frontend

The frontend is plain HTML/CSS/JavaScript served by FastAPI. The server registers each WebSocket subscriber before taking the initial authoritative snapshot, then includes the SQLite event watermark. The browser ignores duplicate events, detects sequence gaps, resynchronises on heartbeat drift, rejects stale HTTP snapshots, reconnects with jittered backoff, and performs an immediate refresh after tab wake or network recovery. Ten-second polling is a fallback rather than the primary update channel.

The UI renders mission state, participants, access posture, reasoning, quotas, logs, task ledger, human gates, direct messages and nudge status without page reloads. User-provided text is inserted with `textContent`, not HTML.

## Trust boundary

Dispatcher is defence in depth, not a VM. Full-access Codex turns and repository-controlled tests are code execution. Use an isolated OS identity, container or VM for hostile repositories, disable all profile full-access settings, and do not expose the unauthenticated dashboard beyond loopback without authenticated TLS termination.
