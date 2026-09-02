from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from .app import create_app
from .config import load_settings, render_example
from .forensics import ForensicsScanner
from .git import is_git_repo
from .orchestrator import NightshiftOrchestrator
from .prompts import directive_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nightshift",
        description="Emergency subscription-backed orchestrator for Grok, Codex Luna, and Codex Spark.",
    )
    parser.add_argument("--config", default=os.getenv("NIGHTSHIFT_CONFIG", "nightshift.toml"))
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a starter nightshift.toml")
    init.add_argument("--repo", required=True, help="path to the Friday Git repository")
    init.add_argument("--force", action="store_true")

    sub.add_parser("doctor", help="check Git and all three authenticated CLIs")
    sub.add_parser("quotas", help="read Codex quota windows and configured Grok quota output")

    scan = sub.add_parser("scan", help="create a read-only emergency recovery dossier")
    scan.add_argument("--output", default="")

    serve = sub.add_parser("serve", help="start the local web control room")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--no-browser", action="store_true")

    directive = sub.add_parser("directive", help="copy the emergency directive to a chosen path")
    directive.add_argument("output", nargs="?", default="EMERGENCY_TAKEOVER_DIRECTIVE.md")
    return parser


async def _with_orchestrator(settings, action):
    orchestrator = NightshiftOrchestrator(settings)
    try:
        return await action(orchestrator)
    finally:
        await orchestrator.close()


def _require_repo(settings) -> None:
    repo = settings.project.repo_path
    if not settings.project.repo:
        raise SystemExit("project.repo is empty. Run `nightshift init --repo /path/to/friday`.")
    if not repo.exists():
        raise SystemExit(f"Repository does not exist: {repo}")
    if not is_git_repo(repo):
        raise SystemExit(f"Not a Git repository: {repo}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()

    if args.command == "init":
        if config_path.exists() and not args.force:
            raise SystemExit(f"Refusing to overwrite {config_path}; add --force if intended.")
        repo = str(Path(args.repo).expanduser().resolve()).replace("\\", "\\\\").replace('"', '\\"')
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(render_example(repo), encoding="utf-8")
        print(config_path)
        return 0

    if args.command == "directive":
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(directive_path(), output)
        print(output)
        return 0

    settings = load_settings(config_path)
    _require_repo(settings)

    if args.command == "doctor":
        async def action(orchestrator):
            result = await orchestrator.doctor()
            return result
        print(json.dumps(asyncio.run(_with_orchestrator(settings, action)), indent=2, ensure_ascii=False))
        return 0

    if args.command == "quotas":
        async def action(orchestrator):
            return await orchestrator.refresh_quotas()
        print(json.dumps(asyncio.run(_with_orchestrator(settings, action)), indent=2, ensure_ascii=False))
        return 0

    if args.command == "scan":
        async def action(orchestrator):
            await orchestrator.refresh_quotas()
            if args.output:
                output = Path(args.output).expanduser().resolve()
            else:
                stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
                output = settings.orchestrator.runtime_path / "manual-scans" / stamp
            output.mkdir(parents=True, exist_ok=True)
            scanner = ForensicsScanner(settings, output, orchestrator.codex_homes)
            return await asyncio.to_thread(scanner.scan)
        result = asyncio.run(_with_orchestrator(settings, action))
        print(result["markdown_path"])
        return 0

    if args.command == "serve":
        host = args.host or settings.server.host
        port = args.port or settings.server.port
        should_open = settings.server.open_browser and not args.no_browser
        url = f"http://{host}:{port}"
        if should_open and host in {"127.0.0.1", "localhost"}:
            # Uvicorn starts immediately after this; browser retries while the socket opens.
            import threading
            timer = threading.Timer(0.8, lambda: webbrowser.open(url))
            timer.daemon = True
            timer.start()
        uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
