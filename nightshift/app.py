from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, load_settings
from .forensics import ForensicsScanner
from .orchestrator import NightshiftOrchestrator, OrchestratorError
from .prompts import directive_path

WEBSOCKET_HEARTBEAT_SECONDS = 15.0


class MissionStartRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=20_000)


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=30_000)
    recipient: str = Field(default="architect", min_length=1, max_length=128)
    delivery: Literal["auto", "chat", "nudge"] = "auto"


class ProfileRequest(BaseModel):
    profile: Literal["reserve", "combat"]
    combat_grok_enabled: bool | None = None


class HumanDecisionRequest(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=10_000)


class ScanRequest(BaseModel):
    include_quotas: bool = True


class ReasoningRequest(BaseModel):
    effort: str = Field(min_length=1, max_length=32)


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_WILDCARD_BIND_HOSTS = {"0.0.0.0", "::"}


def _default_port(scheme: str) -> int | None:
    return 443 if scheme in {"https", "wss"} else 80 if scheme in {"http", "ws"} else None


def _hostname(value: str) -> str:
    """Parse an exact Host-style value without accepting userinfo or paths."""
    raw = value.strip()
    if not raw or "://" in raw:
        return ""
    if raw.count(":") >= 2 and not raw.startswith("["):
        raw = f"[{raw}]"
    try:
        parsed = urlsplit(f"http://{raw}")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return ""
        return (parsed.hostname or "").rstrip(".").casefold()
    except ValueError:
        return ""


def _trusted_hostnames(settings: Settings) -> set[str]:
    trusted = {
        hostname
        for value in settings.server.allowed_hosts
        if (hostname := _hostname(str(value)))
    }
    configured = _hostname(settings.server.host)
    if configured in _LOOPBACK_HOSTS or configured in _WILDCARD_BIND_HOSTS:
        trusted.update(_LOOPBACK_HOSTS)
    elif configured:
        trusted.add(configured)
    return trusted


def _is_private_network_host(value: str) -> bool:
    hostname = _hostname(value)
    if not hostname:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def _host_allowed(
    host: str,
    trusted_hosts: set[str],
    *,
    allow_private_network: bool = False,
) -> bool:
    hostname = _hostname(host)
    return bool(
        hostname
        and (
            hostname in trusted_hosts
            or (
                allow_private_network
                and _is_private_network_host(hostname)
            )
        )
    )

def _origin_allowed(origin: str, host: str, scheme: str = "http") -> bool:
    """Accept non-browser clients and same-origin browser control requests."""
    if not origin:
        return True
    if origin == "null" or not host:
        return False
    try:
        parsed_origin = urlsplit(origin)
        host_scheme = "https" if scheme in {"https", "wss"} else "http"
        origin_port = parsed_origin.port or _default_port(parsed_origin.scheme)
        host_port = urlsplit(f"{host_scheme}://{host}").port or _default_port(host_scheme)
    except ValueError:
        return False
    origin_hostname = (parsed_origin.hostname or "").rstrip(".").casefold()
    return (
        parsed_origin.scheme in {"http", "https"}
        and parsed_origin.username is None
        and parsed_origin.password is None
        and bool(origin_hostname)
        and origin_hostname == _hostname(host)
        and origin_port == host_port
    )


async def _safe_warmup(orchestrator: NightshiftOrchestrator) -> None:
    try:
        await orchestrator.doctor()
    except Exception as exc:  # UI still starts when one CLI is broken
        await orchestrator._emit("system.warmup_error", {"phase": "doctor", "error": str(exc)})
    try:
        await orchestrator.refresh_quotas()
    except Exception as exc:
        await orchestrator._emit("system.warmup_error", {"phase": "quotas", "error": str(exc)})
    await orchestrator._emit(
        "system.warmup_complete",
        {"profile": orchestrator.profile_id},
    )


async def _quota_loop(orchestrator: NightshiftOrchestrator) -> None:
    while True:
        await asyncio.sleep(300)
        try:
            await orchestrator.refresh_quotas()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await orchestrator._emit("system.quota_refresh_error", {"error": str(exc)})


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    static_dir = Path(__file__).with_name("static")
    background_tasks: set[asyncio.Task[Any]] = set()
    trusted_hosts = _trusted_hostnames(resolved)
    allow_private_network_hosts = (
        _hostname(resolved.server.host) in _WILDCARD_BIND_HOSTS
    )

    def spawn(coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        orchestrator = NightshiftOrchestrator(resolved)
        app.state.orchestrator = orchestrator
        app.state.background_tasks = background_tasks
        spawn(_safe_warmup(orchestrator))
        spawn(_quota_loop(orchestrator))
        try:
            yield
        finally:
            tasks = list(background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await orchestrator.close()

    app = FastAPI(
        title="Sol Link Dispatcher",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.middleware("http")
    async def reject_untrusted_or_cross_origin_controls(request: Request, call_next):
        request_host = request.headers.get("host", "")
        if not _host_allowed(
            request_host,
            trusted_hosts,
            allow_private_network=allow_private_network_hosts,
        ):
            return JSONResponse(
                status_code=421,
                content={"detail": "Untrusted Host header"},
            )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _origin_allowed(
            request.headers.get("origin", ""),
            request_host,
            request.url.scheme,
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-origin control request rejected"},
            )
        return await call_next(request)

    def orch() -> NightshiftOrchestrator:
        return app.state.orchestrator

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "sol-link-dispatcher",
            "profile": orch().profile_id,
        }

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return orch().snapshot()

    @app.put("/api/profile")
    async def set_profile(request: ProfileRequest) -> dict[str, Any]:
        before = (orch().profile_id, orch().combat_grok_enabled)
        try:
            result = await orch().set_profile(
                request.profile,
                combat_grok_enabled=request.combat_grok_enabled,
            )
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        after = (orch().profile_id, orch().combat_grok_enabled)
        if after != before:
            spawn(_safe_warmup(orch()))
        return result

    @app.post("/api/doctor")
    async def doctor() -> dict[str, Any]:
        return await orch().doctor()

    @app.post("/api/quotas")
    async def quotas() -> dict[str, Any]:
        return await orch().refresh_quotas()

    @app.put("/api/agents/{key}/reasoning")
    async def set_reasoning(key: str, request: ReasoningRequest) -> dict[str, Any]:
        try:
            return await orch().set_reasoning(key, request.effort)
        except OrchestratorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/recovery/scan")
    async def recovery_scan(request: ScanRequest) -> dict[str, Any]:
        active_settings = orch().settings
        runtime = active_settings.orchestrator.runtime_path
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        scan_dir = runtime / "manual-scans" / stamp
        scan_dir.mkdir(parents=True, exist_ok=False)
        if request.include_quotas:
            await orch().refresh_quotas()
        scanner = ForensicsScanner(active_settings, scan_dir, orch().codex_homes)
        report = await asyncio.to_thread(
            scanner.scan,
            include_sessions=orch().profile.recover_predecessors,
        )
        await orch()._emit(
            "recovery.manual_scan_ready",
            {
                "dossier": report["markdown_path"],
                "json": report["json_path"],
            },
        )
        return report

    @app.post("/api/missions/start")
    async def start_mission(request: MissionStartRequest) -> dict[str, str]:
        try:
            mission_id = await orch().start_mission(request.goal.strip())
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"mission_id": mission_id, "profile": orch().profile_id}

    @app.post("/api/missions/{mission_id}/resume")
    async def resume_mission(mission_id: str) -> dict[str, str]:
        try:
            await orch().resume_interrupted(mission_id)
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "mission_id": mission_id,
            "status": "resuming",
            "profile": orch().profile_id,
        }

    @app.post("/api/mission/pause")
    async def pause_mission() -> dict[str, bool]:
        try:
            await orch().pause()
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/mission/resume")
    async def continue_mission() -> dict[str, bool]:
        try:
            await orch().resume()
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/mission/stop")
    async def stop_mission() -> dict[str, bool]:
        try:
            await orch().stop()
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/decision")
    async def decide_task(task_id: str, request: HumanDecisionRequest) -> dict[str, bool]:
        try:
            await orch().approve_task(task_id, request.approved, request.note)
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/chat")
    async def chat(request: ChatRequest) -> dict[str, Any]:
        try:
            return await orch().chat(
                request.text,
                recipient=request.recipient,
                delivery=request.delivery,
            )
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/directive", include_in_schema=False)
    async def directive() -> FileResponse:
        profile_id = orch().profile_id
        path = directive_path(profile_id)
        return FileResponse(
            path,
            media_type="text/markdown; charset=utf-8",
            filename=path.name,
        )

    @app.get("/api/directive/{profile_id}", include_in_schema=False)
    async def profile_directive(profile_id: str) -> FileResponse:
        try:
            path = directive_path(profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type="text/markdown; charset=utf-8",
            filename=path.name,
        )

    @app.websocket("/ws")
    async def websocket(websocket: WebSocket) -> None:
        websocket_host = websocket.headers.get("host", "")
        if not _host_allowed(
            websocket_host,
            trusted_hosts,
            allow_private_network=allow_private_network_hosts,
        ):
            await websocket.close(code=1008, reason="untrusted websocket host")
            return
        if not _origin_allowed(
            websocket.headers.get("origin", ""),
            websocket_host,
            websocket.url.scheme,
        ):
            await websocket.close(code=1008, reason="cross-origin websocket rejected")
            return
        await websocket.accept()
        # Register before reading SQLite. Events created while the authoritative
        # snapshot is assembled remain queued and are either ignored as duplicates
        # or applied immediately after the snapshot watermark.
        queue = await orch().hub.subscribe(replay=False)
        try:
            snapshot = orch().snapshot()
            await websocket.send_json(
                {
                    "type": "state.snapshot",
                    "seq": snapshot.get("event_seq", 0),
                    "payload": snapshot,
                }
            )
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(),
                        timeout=WEBSOCKET_HEARTBEAT_SECONDS,
                    )
                except TimeoutError:
                    await websocket.send_json(
                        {
                            "type": "system.heartbeat",
                            "seq": orch().db.latest_event_seq(),
                            "created_at": datetime.now(UTC).isoformat(),
                        }
                    )
                    continue
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            await orch().hub.unsubscribe(queue)

    return app


app = create_app()
