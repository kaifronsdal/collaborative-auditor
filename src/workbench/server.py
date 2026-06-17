"""FastAPI + WebSocket server for the audit-workbench (M0 skeleton).

One `Session` per `session_id`. The WS endpoint accepts a connection, registers
it, pushes full state, then dispatches commands: `start` (create + run a Branch),
`step`, `play`, `pause` (STREAMING.md §C).
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from shortuuid import uuid

from workbench.run import Branch
from workbench.session import Session

logger = logging.getLogger(__name__)

app = FastAPI(title="Audit Workbench", version="0.1.0")

sessions: dict[str, Session] = {}


def _get_or_create(session_id: str) -> Session:
    sess = sessions.get(session_id)
    if sess is None:
        sess = Session()
        sessions[session_id] = sess
    return sess


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    session = _get_or_create(session_id)
    session.connections.append(websocket)
    await session.push_full_state(websocket)

    try:
        while True:
            data = await websocket.receive_json()
            await _dispatch(session, data)
    except (WebSocketDisconnect, ConnectionError, OSError):
        pass
    finally:
        if websocket in session.connections:
            session.connections.remove(websocket)


async def _dispatch(session: Session, data: dict) -> None:
    match data.get("t"):
        case "start":
            branch_id = uuid()
            branch = Branch(
                session,
                branch_id,
                seed=data["seed"],
                auditor_model=data["auditor_model"],
                target_model=data["target_model"],
                max_turns=data.get("max_turns", 6),
            )
            session.branches[branch_id] = branch
            session.current = branch_id
            # run the branch as a detached task; keep a reference so it isn't
            # garbage-collected mid-run (branch.run owns its own task group).
            task = asyncio.create_task(branch.run())
            session.branch_tasks.append(task)
        case "step":
            session.branches[session.current].step()
        case "play":
            session.branches[session.current].play()
        case "pause":
            session.branches[session.current].pause()
        case other:
            logger.warning("unknown command %r", other)


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8765)


if __name__ == "__main__":
    main()
