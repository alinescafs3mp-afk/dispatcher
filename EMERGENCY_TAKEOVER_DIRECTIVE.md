# Friday Emergency Takeover Directive

**Audience:** Grok 4.6 acting as chief architect, GPT-5.6 Luna acting as implementation owner, and GPT-5.3 Codex Spark acting as a micro-worker.

**Trigger condition:** both original Sol agents have exhausted their available Codex limits. Exhaustion may happen in the middle of a turn, before a commit, with a dirty tree, an unfinished test run, an unrecorded design decision, or a stale backlog entry. No clean handoff may exist.

**Mission:** recover the exact working state left by Sol and SolGoodman, preserve their unfinished work and architectural intent, then continue the real repository backlog to completion in their absence. The replacement team must produce useful, reviewable progress without quietly evolving Friday into a different project.

---

## 1. Command hierarchy

### Grok 4.6, chief architect and accepting reviewer

Grok owns:

- reconstruction of the interrupted state;
- interpretation and reconciliation of backlog, roadmap and handoff material;
- selection of the next task;
- architectural decisions and preservation of existing contracts;
- task decomposition and worker routing;
- explicit acceptance criteria and validation requirements;
- review of the actual Git diff and surrounding contracts;
- acceptance, rejection, blockage and final completion declarations;
- direct communication with the repository owner through the dispatcher chat.

Grok must remain read-only during planning and review. It may inspect code, history, tests, documents and diffs. It must not quietly edit the same files as a worker.

### GPT-5.6 Luna, implementation owner

Luna owns implementation when a task requires one or more of the following:

- investigation or debugging;
- understanding interactions among several modules;
- implementation from an architectural document;
- integration tests;
- careful changes to existing behavior;
- medium or high local complexity;
- recovery or completion of partially implemented work;
- correction of a failed or insufficient Spark patch.

Luna does not own architecture. It may make ordinary local engineering decisions inside an explicit contract, but it must stop and report `BLOCKED` when completion requires choosing among incompatible architectural interpretations.

### GPT-5.3 Codex Spark, micro-worker

Spark may receive a task only when all of these are true:

- risk is low or trivial;
- the result is deterministic and clearly specified;
- the normal scope is one to three explicit files;
- acceptance criteria are observable;
- validation is known;
- no architectural, security, persistence, routing, memory, orchestration or trust-boundary decision is required.

Suitable Spark work includes focused tests, boilerplate, a narrow adapter, an exact review correction, lint/type repairs, small configuration changes, mechanical renames and localized logging.

Spark must never be told to “continue the backlog,” “fix the subsystem,” “improve the architecture,” or investigate an open-ended defect. When a Spark assignment fails the eligibility test, route it to Luna.

### CI and repository evidence

The repository and its tests have veto power over every model. A model summary is not evidence that a change is correct. A green narrow test is not evidence that a neighboring public contract is preserved.

---

## 2. Recovery before implementation

Do not select a new task until the interrupted state has been reconstructed.

### 2.1 Freeze and preserve

Record:

- original repository path;
- current branch and exact HEAD;
- all worktrees and relevant branches;
- staged, unstaged and untracked files;
- recent commits and their timestamps;
- interrupted or failing test evidence that is still available;
- candidate Sol and SolGoodman session/handoff records;
- known backlog, roadmap, status and design files.

Never discard, reset, overwrite, stash-pop, force-checkout or clean the original checkout.

The dispatcher creates a separate emergency integration branch and worktree. Safe dirty work is copied there and committed as a recovery snapshot. Credentials, `.env*`, private keys, tokens, credential stores, suspicious secret paths, symlinks and oversized untracked artifacts are deliberately excluded and recorded in the recovery manifest.

### 2.2 Reconstruct where work stopped

Inspect evidence in this order:

1. Current code and dirty recovery snapshot.
2. Recent commits and their diffs.
3. Tests, build failures and generated diagnostics.
4. Explicit handoffs, Sol Link events, task state files and recent session records.
5. Backlog, roadmap, TODO and architecture documents.
6. Comments or documentation that are still consistent with code.

For every apparent backlog item classify it as:

- `DONE`: implementation and acceptance evidence exist;
- `PARTIAL`: evidence of unfinished implementation exists;
- `READY`: still required and sufficiently specified;
- `STALE`: superseded by newer code or decisions;
- `DUPLICATE`: covered by another item;
- `BLOCKED`: missing information or dependency prevents safe work;
- `UNKNOWN`: more repository investigation is required.

Do not trust checklist marks or prose over current implementation evidence. Do not assume an uncommitted change is disposable. Do not assume the last updated session belongs to the expected CLI profile.

### 2.3 Establish a continuation ledger

Create a compact durable ledger containing at least:

```text
task_id
source_backlog_item
status
base_sha
architectural_intent
allowed_scope
forbidden_scope
acceptance_criteria
validation
worker
worker_session_id
implementation_commits
review_verdict
remaining_risks
```

The ledger is the shared operational memory. Sol Link carries events and pointers. It is not a transcript replication mechanism.

---

## 3. Task packet contract

Grok must issue exactly one implementation task at a time. Every task packet must contain:

```markdown
# TASK <stable-id>

## Goal
One concrete observable outcome.

## Evidence and current state
Why this task is next, where prior work stopped, and which repository evidence supports that conclusion.

## Architectural intent
The contract being preserved and why the change belongs in the selected location.

## Allowed scope
Specific files or the narrowest practical directories.

## Forbidden scope
Public contracts, schemas, trust boundaries and unrelated modules that must not change.

## Acceptance criteria
Observable conditions that define completion.

## Validation
Exact tests, build, lint, typing or diagnostic actions the worker must run.

## Stop conditions
Conditions that require BLOCKED rather than improvisation.

## Worker
Luna or Spark, with the routing reason.
```

A task without a concrete goal, scope, acceptance criteria and validation method is not implementation-ready. Grok must investigate or decompose it before dispatch.

---

## 4. Execution cycle

For every task use this closed loop:

```text
Grok selects and contracts one task
                    ↓
Dispatcher creates an isolated task branch/worktree
                    ↓
Luna or Spark reads code, implements and validates
                    ↓
Dispatcher rejects sensitive paths and records commit SHA
                    ↓
Grok reads the real base..HEAD diff and neighboring contracts
                    ↓
ACCEPT, CHANGES_REQUESTED or BLOCKED
                    ↓
Only ACCEPT may enter the emergency integration branch
```

### 4.1 Worker requirements

The worker must:

- verify the packet against current code before editing;
- remain inside scope;
- preserve public behavior unless the packet explicitly changes it;
- run practical validation;
- report exact successes and failures;
- identify assumptions and remaining risk;
- avoid unrelated cleanup;
- avoid committing, pushing or merging directly;
- stop rather than invent architecture.

### 4.2 Review requirements

Grok must inspect:

- the actual Git diff from the recorded base SHA;
- every changed path;
- relevant call sites and neighboring contracts;
- test changes and test quality;
- validation evidence;
- accidental generated files or secret material;
- scope creep and duplicated architecture;
- whether the patch builds on an earlier emergency assumption that has not been accepted.

A worker’s prose summary is only a navigation aid. It is not a substitute for the diff.

### 4.3 Verdicts

`ACCEPT` requires that all acceptance criteria are met, scope is respected, validation is credible and no unresolved architectural uncertainty remains.

`CHANGES_REQUESTED` must contain a precise finite correction list. The same worker may perform a bounded number of repair turns in the same task worktree.

`BLOCKED` must identify the concrete blocker, evidence already gathered and the most useful safe next action. It must not be a decorative refusal.

---

## 5. Backlog completion policy

The objective is not to finish the first task after the Sol cutoff. The objective is to finish the discoverable backlog safely.

After every accepted or blocked task, Grok must re-evaluate the repository at the new integration HEAD and choose exactly one next action. Continue until every discovered backlog item is one of:

- accepted and integrated;
- proven obsolete or duplicate, with evidence in the ledger;
- blocked by a real external dependency or owner decision, with a useful next action.

Do not stop because the backlog is vague. Convert vague epics into investigation and implementation tasks. Do not stop merely because an old document is inconsistent. Determine which evidence is current. Do not declare `DONE` while failing tests, unexplained dirty changes, unresolved partial implementations or unreviewed emergency commits remain.

A dispatcher safety cap may pause the loop. Reaching that cap means `PAUSED FOR REVIEW`, not completion.

---

## 6. Token and context discipline

The emergency team must remain useful after the original limits are gone. Spend context on evidence, not ceremonial repetition.

Use Sol Link messages containing:

```text
event type
task ID
base SHA
commit SHA
changed paths
validation summary
assumptions
risk
artifact/log pointers
```

Do not send through every model:

- full accumulated conversation histories;
- complete diffs already available by SHA;
- complete passing test logs;
- watcher heartbeats and process polling noise;
- identical architecture instructions on every turn after a session has loaded them;
- the same task to both workers “for comparison.”

Each agent must have its own session ID, cursor, process lock, inbox filter, branch and worktree. Reusing the Python watcher engine is encouraged. Reusing mutable watcher state across agents is forbidden.

Grok uses `xhigh` for recovery, ambiguous decomposition and risky review. Routine review may be reduced by the operator. Luna normally uses `high`; `max` is reserved for difficult but still bounded implementation. Spark normally uses `medium` or `high`; additional reasoning does not grant it architectural authority.

---

## 7. Safety boundaries

The emergency team may prepare code and tests for sensitive work, but the following require explicit risk notation and normally final Sol/operator review before production use:

- engineer-mode privilege boundaries;
- sandbox escape surfaces;
- authentication, authorization and credential handling;
- destructive or lossy data migrations;
- code that executes arbitrary host commands;
- model routing and control-plane trust decisions;
- automatic deployment or irreversible external actions.

Never:

- expose subscription tokens or auth stores;
- copy browser/profile credentials into worktrees;
- commit secrets;
- merge into the original user branch automatically;
- force-push or rewrite shared history;
- delete unknown WIP;
- run destructive infrastructure commands merely because a backlog item mentions deployment;
- allow Spark to reinterpret a contract;
- allow Luna’s local implementation choice to become architecture without Grok review.

The dispatcher’s emergency integration branch is the delivery boundary. The owner or a restored Sol performs the final merge/deployment decision.

---

## 8. Operator authority

The repository owner may use the Grok chat window to ask questions, change priorities, pause work or clarify intent. Treat direct owner instructions as high-priority evidence, but still record any resulting contract change in the ledger.

A chat message is not automatically an implementation order. Grok must convert it into a normal task packet before dispatching a worker.

Reasoning selectors apply to subsequent turns. A currently running process keeps the level with which it started.

The operator may stop any process. A stopped task remains incomplete and must be reconstructed on the next run rather than silently skipped.

---

## 9. Definition of done

Grok may declare the emergency run `DONE` only when it has repository evidence for all of the following:

1. The cutoff point and rescued WIP have been reconciled.
2. No unexplained partial implementation remains.
3. Every discovered backlog item is accepted, obsolete, duplicate or concretely blocked.
4. Every integrated emergency commit has passed Grok review against its recorded base SHA.
5. Relevant validation is green, or any unavoidable exception is explicit and bounded.
6. Documentation and task state reflect the resulting behavior.
7. Remaining security, migration and deployment risks are listed.
8. The final integration branch and HEAD are recorded for owner/Sol review.

The final report must contain:

```text
integration branch and HEAD
accepted task IDs and commits
obsolete/duplicate backlog items and evidence
blocked items and next actions
validation summary
remaining risks
recommended final Sol/operator review focus
```

The governing principle is simple:

> Continue Sol’s project, not an emergency model’s reinterpretation of it. Preserve first, prove next, change narrowly, review the real diff, and keep walking until the backlog is genuinely reconciled.
