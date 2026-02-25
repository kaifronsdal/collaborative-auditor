from __future__ import annotations

from typing import Any

from fastapi import WebSocket

from collaborative_auditor.handlers.common import push_full_state, push_view_state
from collaborative_auditor.session_manager import SessionManager, SessionRuntime


async def handle_start_session(
    manager: SessionManager,
    runtime: SessionRuntime | None,
    session_id: str,
    ws: WebSocket,
    data: dict[str, Any],
) -> SessionRuntime:
    if runtime is not None:
        manager.register_connection(session_id, ws)
        await push_full_state(runtime, ws)
        return runtime

    runtime = await manager.create_session(
        session_id=session_id,
        initial_prompt=data["initial_prompt"],
        auditor_model=data["auditor_model"],
        target_model=data["target_model"],
    )
    manager.register_connection(session_id, ws)
    await push_view_state(runtime)
    return runtime
