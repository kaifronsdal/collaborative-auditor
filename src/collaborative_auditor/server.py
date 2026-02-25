"""FastAPI server for the Collaborative Auditor Interface.

Thin WebSocket router that delegates to handler modules.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

_env_file = Path(__file__).parent.parent.parent / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
else:
    load_dotenv()

from collaborative_auditor.handlers.branching import (
    handle_branch,
    handle_edit_tool_call,
    handle_resample_target_response,
    handle_resample_turn,
    handle_switch_branch,
)
from collaborative_auditor.handlers.common import push_full_state
from collaborative_auditor.handlers.editing import (
    handle_edit_initial_prompt,
    handle_edit_message,
    handle_rewrite_tool_call,
)
from collaborative_auditor.handlers.feedback import (
    handle_feedback,
    handle_queue_feedback,
    handle_remove_queued_feedback,
)
from collaborative_auditor.handlers.playback import (
    handle_pause,
    handle_play,
    handle_step,
)
from collaborative_auditor.handlers.session import handle_start_session
from collaborative_auditor.session_manager import SessionManager

logger = logging.getLogger(__name__)

manager = SessionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Collaborative Auditor server")
    yield
    logger.info("Shutting down Collaborative Auditor server")


app = FastAPI(
    title="Collaborative Auditor Interface",
    description="Real-time collaborative audit interface for AI safety research",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()

    runtime = await manager.get_or_load(session_id)
    if runtime is not None:
        manager.register_connection(session_id, websocket)
        await push_full_state(runtime, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            await _dispatch(websocket, session_id, data)
    except (WebSocketDisconnect, ConnectionError, OSError):
        pass
    except Exception as e:
        logger.error(f"WebSocket error for session {session_id}: {e}", exc_info=True)
    finally:
        manager.unregister_connection(session_id, websocket)
        runtime = manager.get_runtime(session_id)
        if runtime is not None and not runtime.connections:
            await runtime.cancel_generation()


_ALLOWED_DURING_GENERATION = frozenset({
    "pause", "feedback", "queue_feedback", "remove_queued_feedback",
})


async def _dispatch(ws: WebSocket, session_id: str, data: dict[str, Any]) -> None:
    msg_type = data.get("type")
    if not msg_type:
        await ws.send_json({"type": "error", "message": "Missing 'type' field"})
        return

    runtime = manager.get_runtime(session_id)

    if msg_type == "start_session":
        new_runtime = await handle_start_session(manager, runtime, session_id, ws, data)
        if runtime is None and new_runtime is not None:
            manager.register_connection(session_id, ws)
        return

    if runtime is None:
        await ws.send_json({"type": "error", "message": "Session not found"})
        return

    # rewrite_tool_call is a read-only model call that doesn't mutate session
    # state — run it outside the lock to avoid blocking other operations
    if msg_type == "rewrite_tool_call":
        try:
            await handle_rewrite_tool_call(ws, runtime, data)
        except Exception as e:
            logger.error(f"Error handling rewrite_tool_call: {e}", exc_info=True)
            await ws.send_json({"type": "error", "message": f"Error: {e}"})
        return

    async with runtime.lock:
        try:
            if runtime.is_generating and msg_type not in _ALLOWED_DURING_GENERATION:
                await ws.send_json({
                    "type": "error",
                    "message": "Pause the auditor before performing this action",
                })
                return

            if msg_type == "play":
                await handle_play(runtime)
            elif msg_type == "pause":
                await handle_pause(runtime)
            elif msg_type == "step":
                await handle_step(runtime)
            elif msg_type == "feedback":
                await handle_feedback(runtime, data)
            elif msg_type == "branch":
                await handle_branch(runtime, data)
            elif msg_type == "switch_branch":
                await handle_switch_branch(runtime, data)
            elif msg_type == "edit_message":
                await handle_edit_message(runtime, data)
            elif msg_type == "edit_initial_prompt":
                await handle_edit_initial_prompt(runtime, data)
            elif msg_type == "resample_turn":
                await handle_resample_turn(runtime, data)
            elif msg_type == "edit_tool_call":
                await handle_edit_tool_call(runtime, data)
            elif msg_type == "resample_target_response":
                await handle_resample_target_response(runtime, data)
            elif msg_type == "queue_feedback":
                await handle_queue_feedback(runtime, data)
            elif msg_type == "remove_queued_feedback":
                await handle_remove_queued_feedback(runtime, data)
            else:
                await ws.send_json({"type": "error", "message": f"Unknown: {msg_type}"})
        except Exception as e:
            logger.error(f"Error handling {msg_type}: {e}", exc_info=True)
            await ws.send_json({"type": "error", "message": f"Error in {msg_type}: {e}"})


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/api/sessions")
async def list_sessions():
    return await anyio.to_thread.run_sync(manager.list_summaries)


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    try:
        exists_on_disk = manager.session_exists_on_disk(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session ID format")

    if manager.get_runtime(session_id) is None and not exists_on_disk:
        raise HTTPException(status_code=404, detail="Session not found")

    await manager.delete_session(session_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Static files (built frontend)
# ---------------------------------------------------------------------------


def _find_frontend_dist() -> Path | None:
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "frontend" / "dist",
        Path.cwd() / "frontend" / "dist",
    ]
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").exists():
            return candidate
    return None


def _mount_static_files() -> None:
    dist = _find_frontend_dist()
    if dist:
        app.mount("/", StaticFiles(directory=dist.as_posix(), html=True), name="static")
        logger.info(f"Serving frontend from {dist}")
    else:
        logger.warning("Frontend dist not found. Run 'npm run build' in frontend/.")


_mount_static_files()


def run_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
