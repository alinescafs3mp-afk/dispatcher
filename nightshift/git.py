from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import Settings
from .models import RiskLevel
from .redaction import redact, secret_findings


class GitError(RuntimeError):
    pass


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="surrogateescape")


def git(cwd: Path, *args: str, timeout: int = 180, check: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out") from exc
    output = _decode(proc.stdout) + _decode(proc.stderr)
    if check and proc.returncode != 0:
        raise GitError(output.strip() or f"git exited with {proc.returncode}")
    return _decode(proc.stdout)


def is_git_repo(path: Path) -> bool:
    try:
        return git(path, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False


def _clean_relative(value: str) -> str:
    normalized = value.replace(os.sep, "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def _matches(path: str, pattern: str) -> bool:
    normalized = _clean_relative(path)
    pat = _clean_relative(pattern)
    if not normalized or not pat:
        return normalized == pat
    if pat.endswith("/**"):
        root = pat[:-3].rstrip("/")
        if normalized == root or normalized.startswith(root + "/"):
            return True
    # A plain directory-like pattern is useful in hand-written task packets.
    if not any(char in pat for char in "*?[") and (
        normalized == pat or normalized.startswith(pat + "/")
    ):
        return True
    return fnmatch.fnmatch(normalized, pat) or PurePosixPath(normalized).match(pat)


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(_matches(path, pattern) for pattern in patterns)


def changed_tracked_names(cwd: Path, base: str = "HEAD") -> list[str]:
    """Return both sides of renames so rescue patches never leave the old path behind."""
    raw = git(
        cwd, "diff", "--name-only", "--no-renames", "-z", base,
        check=False,
    )
    return [name for name in raw.split("\0") if name]


def safe_git_diff(cwd: Path, protected: list[str], base: str = "HEAD") -> str:
    """Create a binary patch while excluding configured credential paths."""
    safe_names = [
        name for name in changed_tracked_names(cwd, base)
        if not matches_any(name, protected)
    ]
    if not safe_names:
        return ""
    patch = git(
        cwd, "diff", "--binary", "--full-index", "--no-renames",
        base, "--", *safe_names, check=False,
    )
    return redact(patch)


def file_secret_findings(path: Path, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
    """Inspect a small regular text file without following symlinks."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
            return []
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\0" in raw[:8192]:
        return []
    return secret_findings(raw.decode("utf-8", errors="replace"))


def path_violations(paths: Iterable[str], allowed: list[str], forbidden: list[str],
                    protected: list[str], max_files: int | None = None) -> list[str]:
    normalized = sorted({_clean_relative(path) for path in paths if path})
    violations: list[str] = []
    if max_files is not None and len(normalized) > max_files:
        violations.append(f"changed {len(normalized)} files, maximum is {max_files}")
    for path in normalized:
        if any(_matches(path, pattern) for pattern in protected):
            violations.append(f"protected path changed: {path}")
        if any(_matches(path, pattern) for pattern in forbidden):
            violations.append(f"forbidden path changed: {path}")
        if allowed and not any(_matches(path, pattern) for pattern in allowed):
            violations.append(f"outside allowed scope: {path}")
    return violations


def assess_risk(paths: Iterable[str], diff_text: str, declared: RiskLevel,
                high_risk_patterns: list[str]) -> RiskLevel:
    levels = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
    score = levels.index(declared)
    normalized = list(paths)
    if any(any(_matches(path, pattern) for pattern in high_risk_patterns) for path in normalized):
        score = max(score, levels.index(RiskLevel.HIGH))
    additions = deletions = 0
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    if len(normalized) > 12 or additions + deletions > 1200:
        score = max(score, levels.index(RiskLevel.HIGH))
    elif len(normalized) > 5 or additions + deletions > 400:
        score = max(score, levels.index(RiskLevel.MEDIUM))
    if deletions > 1000:
        score = max(score, levels.index(RiskLevel.CRITICAL))
    return levels[score]


@dataclass(slots=True)
class WorkerTree:
    task_id: str
    worker: str
    path: Path
    branch: str
    base_sha: str


class MissionWorkspace:
    """Owns isolated worktrees and an integration branch; never checks out the source tree."""

    def __init__(self, settings: Settings, mission_id: str, mission_dir: Path) -> None:
        self.settings = settings
        self.repo = settings.project.repo_path
        self.mission_id = mission_id
        self.mission_dir = mission_dir
        self.integration_path = mission_dir / "integration"
        self.integration_branch = f"nightshift/{mission_id}/integration"
        self.architect_path = mission_dir / "architect"
        self.base_sha = ""
        self.worker_trees: dict[str, WorkerTree] = {}

    def prepare(self) -> dict[str, object]:
        if not is_git_repo(self.repo):
            raise GitError(f"Not a git repository: {self.repo}")
        self.mission_dir.mkdir(parents=True, exist_ok=True)
        self.base_sha = git(self.repo, "rev-parse", "HEAD").strip()
        if self.integration_path.exists():
            raise GitError(f"Integration path already exists: {self.integration_path}")
        git(self.repo, "worktree", "add", "-b", self.integration_branch,
            str(self.integration_path), self.base_sha)
        rescue = self._rescue_source_worktree()
        self.create_architect_worktree()
        return {
            "base_sha": self.base_sha,
            "integration_path": str(self.integration_path),
            "integration_branch": self.integration_branch,
            "rescue": rescue,
        }

    def _rescue_source_worktree(self) -> dict[str, object]:
        """Snapshot an interrupted source checkout without touching it.

        We deliberately copy the current tracked-file contents instead of
        blindly applying a patch. This also preserves conflict-marker files
        from an interrupted merge and lets us omit protected credential paths.
        A binary patch and status record are still saved as forensic evidence.
        """
        status = git(self.repo, "status", "--porcelain=v1", "-z")
        if not status:
            return {"dirty": False, "copied_tracked": [], "copied_untracked": [],
                    "skipped_tracked": [], "skipped_untracked": []}

        rescue_dir = self.mission_dir / "rescue"
        rescue_dir.mkdir(parents=True, exist_ok=True)
        (rescue_dir / "source-status.porcelain-z").write_bytes(
            status.encode("utf-8", errors="surrogateescape")
        )
        patch = safe_git_diff(self.repo, self.settings.project.protected_paths, "HEAD")
        patch_path = rescue_dir / "source-working-tree.safe.patch"
        patch_path.write_text(patch, encoding="utf-8", errors="surrogateescape", newline="\n")

        copied_tracked: list[str] = []
        skipped_tracked: list[str] = []
        tracked = changed_tracked_names(self.repo, "HEAD")
        for relative in tracked:
            if matches_any(relative, self.settings.project.protected_paths):
                skipped_tracked.append(f"{relative} (protected path; content excluded from rescue and forensic patch)")
                continue
            source = self.repo / relative
            target = self.integration_path / relative
            findings = file_secret_findings(source)
            if findings:
                skipped_tracked.append(
                    f"{relative} (credential-shaped content: {', '.join(findings)})"
                )
                continue
            try:
                resolved_parent = source.parent.resolve()
                resolved_parent.relative_to(self.repo)
            except (OSError, ValueError):
                skipped_tracked.append(f"{relative} (outside repository)")
                continue
            if source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.unlink(missing_ok=True)
                target.symlink_to(os.readlink(source))
                copied_tracked.append(relative)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied_tracked.append(relative)
            elif not source.exists():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
                copied_tracked.append(relative)
            else:
                skipped_tracked.append(f"{relative} (unsupported file type)")

        untracked_raw = git(self.repo, "ls-files", "--others", "--exclude-standard", "-z")
        untracked = [item for item in untracked_raw.split("\0") if item]
        copied: list[str] = []
        skipped: list[str] = []
        total = 0
        max_file = self.settings.orchestrator.copy_untracked_max_file_mb * 1024 * 1024
        max_total = self.settings.orchestrator.copy_untracked_total_mb * 1024 * 1024
        for relative in untracked:
            if matches_any(relative, self.settings.project.protected_paths):
                skipped.append(f"{relative} (protected path)")
                continue
            source = self.repo / relative
            target = self.integration_path / relative
            findings = file_secret_findings(source)
            if findings:
                skipped.append(
                    f"{relative} (credential-shaped content: {', '.join(findings)})"
                )
                continue
            try:
                source.parent.resolve().relative_to(self.repo)
            except (OSError, ValueError):
                skipped.append(f"{relative} (outside repository)")
                continue
            if source.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.unlink(missing_ok=True)
                target.symlink_to(os.readlink(source))
                copied.append(relative)
                continue
            if not source.is_file():
                continue
            size = source.stat().st_size
            if size > max_file or total + size > max_total:
                skipped.append(f"{relative} ({size} bytes, rescue cap)")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(relative)
            total += size

        git(self.integration_path, "add", "-A")
        has_snapshot = bool(git(self.integration_path, "status", "--porcelain").strip())
        rescue_head = self.integration_head()
        if has_snapshot:
            self._ensure_identity(self.integration_path)
            git(self.integration_path, "commit", "-m", "nightshift: rescue interrupted working tree")
            rescue_head = self.integration_head()
        return {
            "dirty": True,
            "patch": str(patch_path),
            "copied_tracked": copied_tracked,
            "skipped_tracked": skipped_tracked,
            "copied_untracked": copied,
            "skipped_untracked": skipped,
            "snapshot_committed": has_snapshot,
            "rescue_head": rescue_head,
        }

    @classmethod
    def reopen(cls, settings: Settings, mission_id: str, mission_dir: Path,
               integration_path: Path, integration_branch: str, base_sha: str) -> MissionWorkspace:
        obj = cls(settings, mission_id, mission_dir)
        obj.integration_path = integration_path
        obj.integration_branch = integration_branch
        obj.base_sha = base_sha
        obj.architect_path = mission_dir / "architect"
        if not obj.architect_path.exists():
            obj.create_architect_worktree()
        return obj

    def integration_head(self) -> str:
        return git(self.integration_path, "rev-parse", "HEAD").strip()

    def create_architect_worktree(self) -> Path:
        if self.architect_path.exists():
            return self.architect_path
        self.architect_path.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "--detach", str(self.architect_path), self.integration_head())
        return self.architect_path

    def sync_architect_worktree(self) -> Path:
        self.create_architect_worktree()
        git(self.architect_path, "reset", "--hard", self.integration_head())
        git(self.architect_path, "clean", "-fd", check=False)
        return self.architect_path

    def create_worker(self, task_id: str, worker: str) -> WorkerTree:
        base = self.integration_head()
        safe_task = "".join(char if char.isalnum() or char in "-_" else "-" for char in task_id)[:64]
        branch = f"nightshift/{self.mission_id}/{safe_task}-{worker}"
        path = self.mission_dir / "workers" / safe_task / worker
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "-b", branch, str(path), base)
        tree = WorkerTree(task_id=task_id, worker=worker, path=path, branch=branch, base_sha=base)
        self.worker_trees[task_id] = tree
        return tree

    def worker_dirty_files(self, tree: WorkerTree) -> list[str]:
        tracked = changed_tracked_names(tree.path, "HEAD")
        untracked_raw = git(
            tree.path, "ls-files", "--others", "--exclude-standard", "-z",
            check=False,
        )
        return sorted(set(tracked + [item for item in untracked_raw.split("\0") if item]))

    def discard_worker_paths(self, tree: WorkerTree, paths: Iterable[str]) -> None:
        """Discard unsafe changes only inside the disposable worker worktree."""
        for relative in sorted(set(paths)):
            normalized = _clean_relative(relative)
            if not normalized:
                continue
            tracked = bool(git(
                tree.path, "ls-files", "--error-unmatch", "--", normalized,
                check=False,
            ).strip())
            if tracked:
                git(
                    tree.path, "restore", "--source=HEAD", "--staged", "--worktree",
                    "--", normalized, check=False,
                )
                continue
            target = tree.path / normalized
            try:
                target.parent.resolve().relative_to(tree.path.resolve())
            except (OSError, ValueError):
                continue
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
            elif target.is_dir():
                shutil.rmtree(target)

    def sanitize_worker_changes(self, tree: WorkerTree) -> list[str]:
        """Remove credential paths/material before anything enters Git object storage."""
        violations: list[str] = []
        dirty = self.worker_dirty_files(tree)
        protected = [
            path for path in dirty
            if matches_any(path, self.settings.project.protected_paths)
        ]
        if protected:
            violations.extend(f"protected path changed and was discarded: {path}" for path in protected)
            self.discard_worker_paths(tree, protected)

        secret_paths: list[str] = []
        for relative in self.worker_dirty_files(tree):
            findings = file_secret_findings(tree.path / relative)
            if findings:
                secret_paths.append(relative)
                violations.append(
                    f"credential-shaped content detected and discarded: {relative} "
                    f"({', '.join(findings)})"
                )
        if secret_paths:
            self.discard_worker_paths(tree, secret_paths)
        return violations

    def worker_status(self, tree: WorkerTree) -> str:
        return git(tree.path, "status", "--short", "--branch")

    def commit_worker(self, tree: WorkerTree, message: str) -> str:
        unsafe = [
            path for path in self.worker_dirty_files(tree)
            if matches_any(path, self.settings.project.protected_paths)
            or file_secret_findings(tree.path / path)
        ]
        if unsafe:
            raise GitError(
                "Refusing to commit protected or credential-bearing paths: "
                + ", ".join(unsafe)
            )
        git(tree.path, "add", "-A")
        if not git(tree.path, "status", "--porcelain").strip():
            return git(tree.path, "rev-parse", "HEAD").strip()
        self._ensure_identity(tree.path)
        git(tree.path, "commit", "-m", message)
        return git(tree.path, "rev-parse", "HEAD").strip()

    def worker_changed_files(self, tree: WorkerTree) -> list[str]:
        out = git(tree.path, "diff", "--name-only", "-z", f"{tree.base_sha}..HEAD")
        return [name for name in out.split("\0") if name]

    def worker_diff(self, tree: WorkerTree) -> str:
        return git(tree.path, "diff", "--binary", f"{tree.base_sha}..HEAD")

    def integrate_worker(self, tree: WorkerTree, message: str) -> str:
        current = self.integration_head()
        if current != tree.base_sha:
            raise GitError(
                f"Integration HEAD moved from {tree.base_sha} to {current}; task must be rebased/replanned"
            )
        patch = self.worker_diff(tree)
        if not patch.strip():
            raise GitError("Worker produced no diff")
        patch_path = self.mission_dir / "patches" / f"{tree.task_id}.patch"
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(patch, encoding="utf-8", errors="surrogateescape", newline="\n")
        proc = subprocess.run(
            ["git", "apply", "--index", "--3way", str(patch_path)],
            cwd=str(self.integration_path), capture_output=True,
        )
        if proc.returncode != 0:
            raise GitError((_decode(proc.stderr) or _decode(proc.stdout)).strip())
        self._ensure_identity(self.integration_path)
        git(self.integration_path, "commit", "-m", message)
        return self.integration_head()

    def remove_worker_worktree(self, tree: WorkerTree, keep_branch: bool = True) -> None:
        git(self.repo, "worktree", "remove", "--force", str(tree.path), check=False)
        git(self.repo, "worktree", "prune", check=False)
        if not keep_branch:
            git(self.repo, "branch", "-D", tree.branch, check=False)

    def cleanup(self, keep_integration: bool = True) -> None:
        for tree in list(self.worker_trees.values()):
            self.remove_worker_worktree(tree, keep_branch=True)
        if self.architect_path.exists():
            git(self.repo, "worktree", "remove", "--force", str(self.architect_path), check=False)
        if not keep_integration and self.integration_path.exists():
            git(self.repo, "worktree", "remove", "--force", str(self.integration_path), check=False)
        git(self.repo, "worktree", "prune", check=False)

    @staticmethod
    def _ensure_identity(path: Path) -> None:
        if not git(path, "config", "user.email", check=False).strip():
            git(path, "config", "user.email", "nightshift@local")
        if not git(path, "config", "user.name", check=False).strip():
            git(path, "config", "user.name", "Sol Link Nightshift")
