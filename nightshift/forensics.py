from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import Settings
from .git import GitError, git, matches_any, safe_git_diff
from .protocol import compact_text
from .redaction import redact, redact_value
from .sessions import discover_sessions


class ForensicsScanner:
    def __init__(self, settings: Settings, mission_dir: Path,
                 codex_homes: dict[str, str] | None = None) -> None:
        self.settings = settings
        self.repo = settings.project.repo_path
        self.mission_dir = mission_dir
        self.codex_homes = codex_homes or {}
        self.out_dir = mission_dir / "forensics"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.working_roots = self._resolve_working_roots()

    def _resolve_working_roots(self) -> list[tuple[str, str, Path]]:
        roots: list[tuple[str, str, Path]] = [
            ("repository", self.settings.project.repo or str(self.repo), self.repo)
        ]
        seen = {self.repo}
        for configured, resolved in self.settings.project.operational_root_entries:
            if resolved in seen:
                continue
            seen.add(resolved)
            roots.append(("operational", str(configured), resolved))
        return roots

    def scan(self, *, include_sessions: bool = True) -> dict[str, Any]:
        report: dict[str, Any] = {
            "repo": str(self.repo),
            "head": self._safe_git("rev-parse", "HEAD").strip(),
            "branch": self._safe_git("rev-parse", "--abbrev-ref", "HEAD").strip(),
            "status": self._safe_git("status", "--short", "--branch"),
            "recent_commits": self._safe_git(
                "log", "-30", "--date=iso-strict", "--pretty=format:%H%x09%ad%x09%an%x09%s"
            ),
            "branches": self._safe_git(
                "for-each-ref", "--sort=-committerdate",
                "--format=%(refname:short)%09%(objectname:short)%09%(committerdate:iso-strict)%09%(subject)",
                "refs/heads/"
            ),
            "stashes": self._safe_git("stash", "list", "--date=iso"),
            "worktrees": [],
            "backlog_files": [],
            "watcher_state": [],
            "working_roots": [
                {
                    "kind": kind,
                    "configured": configured,
                    "path": str(root),
                    "available": root.is_dir(),
                }
                for kind, configured, root in self.working_roots
            ],
            "sessions": {},
            "sessions_scanned": include_sessions,
        }
        report["worktrees"] = self._scan_worktrees()
        report["backlog_files"] = self._scan_backlog_files()
        report["watcher_state"] = self._scan_watcher_state()
        report["sessions"] = self._scan_sessions() if include_sessions else {}
        report = redact_value(report)
        json_path = self.out_dir / "recovery.json"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        markdown_path = self.out_dir / "RECOVERY_DOSSIER.md"
        markdown_path.write_text(self._render_markdown(report), encoding="utf-8")
        report["json_path"] = str(json_path)
        report["markdown_path"] = str(markdown_path)
        return report

    def _safe_git(self, *args: str, cwd: Path | None = None) -> str:
        try:
            return redact(git(cwd or self.repo, *args).strip())
        except GitError as exc:
            return f"<git error: {exc}>"

    def _scan_worktrees(self) -> list[dict[str, Any]]:
        raw = self._safe_git("worktree", "list", "--porcelain")
        blocks = [block for block in raw.split("\n\n") if block.strip()]
        result: list[dict[str, Any]] = []
        patches = self.out_dir / "worktree-patches"
        patches.mkdir(parents=True, exist_ok=True)
        for index, block in enumerate(blocks):
            item: dict[str, Any] = {}
            for line in block.splitlines():
                key, _, value = line.partition(" ")
                item[key] = value
            path = Path(item.get("worktree", ""))
            if path.is_dir():
                item["status"] = self._safe_git("status", "--short", "--branch", cwd=path)
                item["head_subject"] = self._safe_git("log", "-1", "--pretty=format:%H %s", cwd=path)
                patch = safe_git_diff(path, self.settings.project.protected_paths, "HEAD")
                untracked = self._safe_git("ls-files", "--others", "--exclude-standard", cwd=path)
                if patch.strip() or untracked.strip():
                    patch_path = patches / f"worktree-{index}.safe.patch"
                    patch_path.write_text(patch, encoding="utf-8", errors="surrogateescape", newline="\n")
                    item["dirty_patch"] = str(patch_path)
                    item["diff_excerpt"] = compact_text(redact(patch), 16_000)
                    item["untracked"] = [
                        path for path in untracked.splitlines()
                        if not matches_any(path, self.settings.project.protected_paths)
                    ][:200]
            result.append(item)
        return result

    @staticmethod
    def _display_context_path(kind: str, configured: str, relative: Path) -> str:
        if kind == "repository":
            return relative.as_posix()
        return f"{configured.rstrip('/')}/{relative.as_posix()}"

    @staticmethod
    def _iter_root_matches(root: Path, pattern: str) -> Iterator[Path]:
        try:
            yield from root.glob(pattern)
        except OSError:
            return

    def _safe_context_candidate(
        self,
        root: Path,
        path: Path,
    ) -> tuple[Path, Path] | None:
        try:
            resolved_root = root.resolve()
            resolved_path = path.resolve()
            relative = resolved_path.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            return None
        relative_text = relative.as_posix()
        if (
            ".git" in relative.parts
            or not resolved_path.is_file()
            or matches_any(relative_text, self.settings.project.protected_paths)
        ):
            return None
        return resolved_path, relative

    def _scan_backlog_files(self) -> list[dict[str, Any]]:
        found: dict[Path, tuple[str, str, Path, Path]] = {}
        for kind, configured, root in self.working_roots:
            if not root.is_dir():
                continue
            for pattern in self.settings.project.backlog_globs:
                for path in self._iter_root_matches(root, pattern):
                    candidate = self._safe_context_candidate(root, path)
                    if candidate is None:
                        continue
                    resolved_path, relative = candidate
                    found.setdefault(
                        resolved_path,
                        (kind, configured, resolved_path, relative),
                    )
        result: list[dict[str, Any]] = []
        budget = 180_000
        ordered = sorted(
            found.values(),
            key=lambda item: self._display_context_path(item[0], item[1], item[3]),
        )
        for kind, configured, path, relative in ordered[:80]:
            display_path = self._display_context_path(kind, configured, relative)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                modified_at = path.stat().st_mtime
            except OSError as exc:
                result.append({"path": display_path, "error": str(exc)})
                continue
            excerpt = compact_text(redact(text), min(18_000, budget))
            budget -= len(excerpt)
            result.append(
                {
                    "path": display_path,
                    "source_root": configured,
                    "resolved_path": str(path),
                    "modified_at": modified_at,
                    "excerpt": excerpt,
                }
            )
            if budget <= 0:
                break
        return result

    def _scan_watcher_state(self) -> list[dict[str, Any]]:
        patterns = [
            "**/.task_watch_state.json",
            "**/*watch*state*.json",
            ".sol-link/**/*.json",
            "runtime/sol-link/**/*.json",
            "handoffs/**/*.json",
        ]
        found: dict[Path, tuple[str, str, Path, Path]] = {}
        for kind, configured, root in self.working_roots:
            if not root.is_dir():
                continue
            for pattern in patterns:
                for path in self._iter_root_matches(root, pattern):
                    candidate = self._safe_context_candidate(root, path)
                    if candidate is None:
                        continue
                    resolved_path, relative = candidate
                    found.setdefault(
                        resolved_path,
                        (kind, configured, resolved_path, relative),
                    )
        result: list[dict[str, Any]] = []
        ordered = sorted(
            found.values(),
            key=lambda item: self._display_context_path(item[0], item[1], item[3]),
        )
        for kind, configured, path, relative in ordered[:60]:
            display_path = self._display_context_path(kind, configured, relative)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                text = f"<read error: {exc}>"
            result.append(
                {
                    "path": display_path,
                    "source_root": configured,
                    "resolved_path": str(path),
                    "content": compact_text(redact(text), 8000),
                }
            )
        return result

    def _scan_sessions(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        configured = list(self.settings.project.session_search_roots)
        for agent, home in self.codex_homes.items():
            summaries = []
            if home:
                # Keep account/profile provenance intact. Mixing every configured
                # Codex home before ranking can resume Sol's session with Spark or
                # SolGoodman's session with Luna when timestamps are close.
                summaries = discover_sessions(
                    [str(Path(home).expanduser() / "sessions")],
                    self.repo,
                    limit=10,
                    working_roots=self.settings.project.known_working_paths,
                )
            if not summaries:
                summaries = discover_sessions(
                    configured,
                    self.repo,
                    limit=10,
                    working_roots=self.settings.project.known_working_paths,
                )
            result[agent] = [summary.to_dict() for summary in summaries]
        if not result:
            summaries = discover_sessions(
                configured,
                self.repo,
                limit=12,
                working_roots=self.settings.project.known_working_paths,
            )
            result["unassigned"] = [summary.to_dict() for summary in summaries]
        return result

    def _render_markdown(self, report: dict[str, Any]) -> str:
        lines = [
            "# Emergency Recovery Dossier",
            "",
            f"Repository: `{report['repo']}`",
            f"Branch: `{report['branch']}`",
            f"HEAD: `{report['head']}`",
            "",
            "## Known working roots",
        ]
        for item in report.get("working_roots", []):
            availability = "available" if item.get("available") else "missing"
            lines.append(
                f"- {item.get('kind', 'operational')}: "
                f"`{item.get('configured', '')}` -> `{item.get('path', '')}` ({availability})"
            )
        lines += [
            "",
            "Configured operational roots are continuity evidence sources. They do not "
            "become implicit Git write or integration scopes.",
            "",
            "## Source working tree",
            "```text",
            report["status"] or "clean",
            "```",
            "",
            "## Recent commits",
            "```text",
            report["recent_commits"] or "none",
            "```",
            "",
            "## Local branches",
            "```text",
            report["branches"] or "none",
            "```",
            "",
            "## Stashes",
            "```text",
            report["stashes"] or "none",
            "```",
            "",
            "## Worktrees",
        ]
        for item in report["worktrees"]:
            lines += [
                f"### `{item.get('worktree', '?')}`",
                f"Branch: `{item.get('branch', '')}`  Commit: `{item.get('HEAD', '')}`",
                "```text",
                item.get("status", "") or "clean",
                "```",
            ]
            if item.get("dirty_patch"):
                lines.append(f"Dirty safe patch saved at `{item['dirty_patch']}`")
            if item.get("diff_excerpt"):
                lines += ["```diff", item["diff_excerpt"], "```"]
            if item.get("untracked"):
                lines.append("Untracked: " + ", ".join(f"`{x}`" for x in item["untracked"][:30]))
        lines += ["", "## Backlog and handoff material"]
        for item in report["backlog_files"]:
            lines += [f"### `{item['path']}`", "```markdown", item.get("excerpt", ""), "```"]
        lines += ["", "## Watcher and Sol Link state"]
        for item in report["watcher_state"]:
            lines += [f"### `{item['path']}`", "```json", item.get("content", ""), "```"]
        lines += ["", "## Candidate predecessor Codex sessions"]
        if not report.get("sessions_scanned", True):
            lines.append("Session discovery skipped by the active operating profile.")
        for agent, sessions in report["sessions"].items():
            lines.append(f"### {agent}")
            for session in sessions:
                lines += [
                    f"- Session `{session.get('session_id', '')}` | score {session.get('score', 0)} | "
                    f"cwd `{session.get('cwd', '')}` | "
                    f"matched root `{session.get('matched_root', '')}` | "
                    f"file `{session.get('path', '')}`",
                    f"  - Last user: {compact_text(session.get('last_user', ''), 800)}",
                    f"  - Last assistant: {compact_text(session.get('last_assistant', ''), 1200)}",
                ]
        lines += [
            "",
            "## Recovery rule",
            "Treat code, tests, git state, and executable evidence as authoritative. Treat prose backlog and model summaries as hypotheses until reconciled with the repository.",
        ]
        return "\n".join(lines) + "\n"
