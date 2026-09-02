# Security Policy

## Threat model

Sol Link Dispatcher assumes that the target repository and model output may contain mistakes. It does **not** assume that a deliberately hostile repository is safe to execute under the same Unix account as personal credentials.

The design reduces blast radius with:

- separate integration, architect and worker worktrees;
- no direct model edits to the source checkout;
- subscription CLIs instead of provider API keys in configuration;
- removal of credential-shaped environment variables from child processes;
- protected path filters and pre-commit secret-shaped content scanning;
- shell-free validation command execution;
- deterministic scope and risk gates before model review;
- manual approval for high and critical risk;
- read-only direct operator chats, even for participants whose work turns have full access.

These controls are not an OS security boundary.

Configured `project.operational_roots` may point into the operator's home directory, for example `~/.jericho`. Forensics does not crawl those roots indiscriminately: it reads only configured backlog/handoff and watcher-state patterns, rejects paths that escape through symlinks, applies protected-path filters, and redacts captured text. Declaring a root does not grant it implicit write or Git-integration authority.

## Operating profiles and full access

The requested defaults intentionally differ by profile:

- **Combat Sol** (`codex`): Ultra reasoning and Codex sandbox bypass for automated architect/review turns.
- **Combat SolGoodman** (`codex-solgoodman`): Ultra reasoning and Codex sandbox bypass for implementation turns.
- **Optional combat Grok helper**: Grok `--always-approve --sandbox off` for implementation turns.
- **Reserve Grok architect/reviewer**: Grok `--always-approve --sandbox off` for automated turns.
- **Reserve Luna** (`codex-solgoodman`): Max reasoning and Codex sandbox bypass for implementation turns.
- **Reserve Spark** (`codex`): Codex sandbox bypass for implementation turns.

Combat Sol runs inside a disposable architect worktree that is hard-reset after every turn. Persistent product changes still require a worker diff, validation, architect review and integration. This is damage reduction, not containment: a full-access process can read or alter files reachable by the OS user and can run arbitrary commands permitted to that user.

For a trusted personal Friday checkout these defaults match the intended operating posture. For any unknown, hostile or externally supplied repository, override all `*_full_access` settings to `false` and keep every `unsafe_full_access` agent setting false.

## Recommended deployment

For a trusted personal project, bind to loopback, review `nightshift.toml`, and inspect the integration branch before merging.

For an unknown or hostile repository:

1. create a dedicated Unix user, container or VM;
2. mount only the target repository and the minimum required CLI auth homes;
3. disable `reserve_grok_full_access`, `reserve_luna_full_access`, `reserve_spark_full_access`, `combat_sol_full_access`, `combat_goodman_full_access`, and `combat_grok_full_access`;
4. do not expose SSH, cloud, browser, GitHub or password-manager credentials;
5. disable network access outside required provider endpoints where practical;
6. inspect the integration branch and validation evidence before any merge or push.

## Dashboard exposure

The dashboard has no user authentication. It binds to `127.0.0.1` by default. Browser-originated state-changing HTTP requests and WebSocket connections are restricted to the dashboard origin as defence in depth, but this does not replace authentication. Do not bind it to `0.0.0.0`, a LAN address or a public interface without authenticated TLS termination.

## Subscription credentials

Dispatcher uses each CLI's existing local login state. It does not intentionally extract, print or persist bearer-token values. Common API-key variables are stripped so commands do not silently switch from a consumer subscription to metered API billing.

The CLI processes still need access to their own auth stores. Anyone controlling the same OS account may already be able to use those subscriptions independently of Dispatcher.

## Direct lines and durable nudges

A direct chat model turn is always read-only and cannot silently dispatch or integrate work. A queued nudge is stored in SQLite and attached to the participant's next work turn. It is marked delivered after a successful result or other provider-side evidence (events or a final response). A failure before any such evidence leaves the nudge queued for retry.

Do not put passwords, tokens or private-key material into chat or nudges. Redaction is defence in depth and cannot recognize every custom credential format.

## Secret detection limitations

Credential-shaped scanning catches common private keys, provider tokens, auth headers and secret assignments. It can have false positives and cannot recognize every encoding or custom secret format. Protected paths should therefore be expanded for the target project.

Unsafe worker files are discarded before a Dispatcher commit. The original source checkout is not cleaned or modified, so pre-existing secrets there remain the operator's responsibility.

## Validation commands

Validation uses direct argv parsing and rejects shell metacharacters, privilege escalation, package installers and arbitrary absolute executables. Nevertheless, commands such as `pytest`, `npm test`, `cargo test` or a project-local executable can run repository-controlled code. Treat them as code execution.

## Reporting

Open a private security advisory in the repository for vulnerabilities that could expose credentials or modify the source checkout unexpectedly. Do not include real tokens, session files or private repository content in a public issue.
