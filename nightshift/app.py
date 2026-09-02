from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .forensics import ForensicsScanner
from .orchestrator import NightshiftOrchestrator, OrchestratorError
from .prompts import directive_path


class MissionStartRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=20_000)


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=30_000)


class HumanDecisionRequest(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=10_000)


class ScanRequest(BaseModel):
    include_quotas: bool = True


class ReasoningRequest(BaseModel):
    effort: str = Field(min_length=1, max_length=32)


async def _safe_warmup(orchestrator: NightshiftOrchestrator) -> None:
    try:
        await orchestrator.doctor()
    except Exception as exc:  # UI still starts when one CLI is broken
        await orchestrator._emit("system.warmup_error", {"phase": "doctor", "error": str(exc)})
    try:
        await orchestrator.refresh_quotas()
    except Exception as exc:
        await orchestrator._emit("system.warmup_error", {"phase": "quotas", "error": str(exc)})


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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        orchestrator = NightshiftOrchestrator(resolved)
        app.state.orchestrator = orchestrator
        warmup = asyncio.create_task(_safe_warmup(orchestrator))
        quota_loop = asyncio.create_task(_quota_loop(orchestrator))
        try:
            yield
        finally:
            warmup.cancel()
            quota_loop.cancel()
            with suppress(asyncio.CancelledError):
                await warmup
            with suppress(asyncio.CancelledError):
                await quota_loop
            await orchestrator.close()

    app = FastAPI(
        title="Sol Link Nightshift",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    def orch() -> NightshiftOrchestrator:
        return app.state.orchestrator

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "sol-link-nightshift"}

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return orch().snapshot()

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
        runtime = resolved.orchestrator.runtime_path
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        scan_dir = runtime / "manual-scans" / stamp
        scan_dir.mkdir(parents=True, exist_ok=False)
        if request.include_quotas:
            await orch().refresh_quotas()
        scanner = ForensicsScanner(resolved, scan_dir, orch().codex_homes)
        report = await asyncio.to_thread(scanner.scan)
        await orch()._emit("recovery.manual_scan_ready", {
            "dossier": report["markdown_path"],
            "json": report["json_path"],
        })
        return report

    @app.post("/api/missions/start")
    async def start_mission(request: MissionStartRequest) -> dict[str, str]:
        try:
            mission_id = await orch().start_mission(request.goal.strip())
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"mission_id": mission_id}

    @app.post("/api/missions/{mission_id}/resume")
    async def resume_mission(mission_id: str) -> dict[str, str]:
        try:
            await orch().resume_interrupted(mission_id)
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"mission_id": mission_id, "status": "resuming"}

    @app.post("/api/mission/pause")
    async def pause_mission() -> dict[str, bool]:
        await orch().pause()
        return {"ok": True}

    @app.post("/api/mission/resume")
    async def continue_mission() -> dict[str, bool]:
        await orch().resume()
        return {"ok": True}

    @app.post("/api/mission/stop")
    async def stop_mission() -> dict[str, bool]:
        await orch().stop()
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/decision")
    async def decide_task(task_id: str, request: HumanDecisionRequest) -> dict[str, bool]:
        try:
            await orch().approve_task(task_id, request.approved, request.note)
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/chat")
    async def chat(request: ChatRequest) -> dict[str, str]:
        try:
            answer = await orch().chat(request.text)
        except OrchestratorError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"answer": answer}

    @app.get("/api/directive", include_in_schema=False)
    async def directive() -> FileResponse:
        return FileResponse(
            directive_path(),
            media_type="text/markdown; charset=utf-8",
            filename="EMERGENCY_TAKEOVER_DIRECTIVE.md",
        )

    @app.websocket("/ws")
    async def websocket(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = await orch().hub.subscribe(replay=False)
        try:
            await websocket.send_json({"type": "state.snapshot", "payload": orch().snapshot()})
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await orch().hub.unsubscribe(queue)

    return app


app = create_app()
