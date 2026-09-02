# Security Policy

## Threat model

Nightshift assumes the target repository and model output may contain mistakes. It does **not** assume a deliberately hostile repository is safe to execute on the same Unix account as personal credentials.

The default design reduces blast radius with:

- separate integration, architect and worker worktrees;
- no direct edits to the source checkout;
- Codex sandboxing and read-only Grok architect mode;
- no provider API keys in configuration;
- removal of credential-shaped environment variables from child processes;
- protected path filters and pre-commit secret-shaped content scanning;
- shell-free validation command execution;
- deterministic scope/risk gates before model review;
- manual approval for high and critical risk.

These controls are not an OS security boundary.

## Recommended deployment

For a trusted personal project, run on loopback under your normal account after reviewing `nightshift.toml`.

For an unknown or hostile repository:

1. create a dedicated Unix user, container or VM;
2. mount only the target repository and required CLI auth homes;
3. do not expose SSH, cloud, browser, GitHub or password-manager credentials;
4. disable network access outside provider endpoints where practical;
5. keep `unsafe_full_access = false`;
6. inspect the integration branch before merging or pushing.

## Dashboard exposure

The dashboard has no authentication. Defaults bind to `127.0.0.1`. Do not bind it to `0.0.0.0`, a LAN address or a public interface without placing it behind authenticated TLS termination.

## Subscription credentials

Nightshift uses the CLIs' existing local login state. It does not print, persist or intentionally read bearer token values. It strips common API-key variables so commands do not silently switch to metered API billing.

The CLI processes still need access to their own auth stores. Anyone who controls the same OS account may already be able to use those subscriptions, independently of Nightshift.

## Secret detection limitations

Credential-shaped scanning catches common private keys, provider tokens, auth headers and secret assignments. It can have false positives and cannot recognize every encoding or custom secret format. Protected paths should therefore be expanded for the target project.

Unsafe worker files are discarded before a Nightshift commit. The original source checkout is not cleaned or modified, so pre-existing secrets there remain the operator's responsibility.

## Validation commands

Validation uses direct argv parsing and rejects shell metacharacters, privilege escalation, package installers and arbitrary absolute executables. Nevertheless, commands such as `pytest`, `npm test`, `cargo test` or a project-local executable can run repository-controlled code. Treat them as code execution.

## Reporting

Open a private security advisory in the repository for vulnerabilities that could expose credentials or modify the source checkout unexpectedly. Do not include real tokens, session files or private repository content in a public issue.
