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
from inspect_ai.model import ChatMessageUser, ModelOutput
from shortuuid import uuid

from inspect_petri.target import Step

from workbench.run import Branch, slice_at
from workbench.session import Session

logger = logging.getLogger(__name__)

app = FastAPI(title="Audit Workbench", version="0.1.0")

sessions: dict[str, Session] = {}


async def _get_or_create(session_id: str) -> Session:
    sess = sessions.get(session_id)
    if sess is None:
        sess = Session()
        # start the session-owned drain task once, on first connect, so the
        # single broadcast loop is live before any branch runs (STREAMING.md
        # §B; petri footgun #12 — never one drain per branch).
        await sess.start()
        sessions[session_id] = sess
    return sess


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    await websocket.accept()
    session = await _get_or_create(session_id)
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


async def _stop_running_branches(session: Session) -> None:
    """Enforce one-running-branch-per-session before a fresh `start`.

    Branches within a session share one transcript / sync `_on_event` /
    pool — concurrent runs race on those (footgun #2). Parallel work uses
    separate sessions (separate tabs), each with its own `Session`. So
    `start` cancels any still-running branch task in *this* session; the
    previous `Branch` (its `audit_tape`, settled events, store) stays in
    `session.branches` for later viewing/resume — only the live coroutine
    stops. `run_audit`'s `finally` persists the tape on cancellation.
    """
    running = [t for t in session.branch_tasks if not t.done()]
    for t in running:
        t.cancel()
    for t in running:
        try:
            await t
        except asyncio.CancelledError as exc:
            logger.debug("previous branch task ended on start: %r", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("branch task raised on cancel: %r", exc, exc_info=True)
    session.branch_tasks = [t for t in session.branch_tasks if not t.done()]
    for b in session.branches.values():
        if b.status == "running":
            b.status = "ended"


async def _dispatch(session: Session, data: dict) -> None:
    match data.get("t"):
        case "start":
            async with session._dispatch_lock:  # noqa: SLF001
                await _stop_running_branches(session)
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
                # the branch just registered its auditor/target span ids in
                # `session.span_role` and we set `current` — clients connected
                # before this got an empty `span_role`/`current` in their initial
                # `state` and would otherwise never learn the new mapping (every
                # event then fails `resolveRole` and renders nowhere). Re-broadcast.
                await session.broadcast({"t": "state", "v": session.version, **session.view()})
                # run the branch as a detached task; keep a reference so it isn't
                # garbage-collected mid-run (branch.run owns its own task group).
                task = asyncio.create_task(branch.run())
                session.branch_tasks.append(task)
        case "step" | "play" | "pause" as cmd:
            async with session._dispatch_lock:  # noqa: SLF001
                if session.current is None:
                    logger.warning("%r before start — dropping", cmd)
                    return
                getattr(session.branches[session.current], cmd)()
                await session.broadcast_status()
        case "inject":
            async with session._dispatch_lock:  # noqa: SLF001
                branch_id = data["branch"]
                role = data["role"]
                if branch_id not in session.branches:
                    logger.warning("inject for unknown branch %r — dropping", branch_id)
                    return
                if session.branches[branch_id].status == "ended":
                    logger.warning("inject into ended branch %r — dropping", branch_id)
                    await session.broadcast({"t": "error", "v": session.version, "message": "branch has ended"})
                    return
                # preserve the client-generated id so the frontend reconciles the
                # ghost bubble once the id appears in the next ModelEvent.input.
                msg = ChatMessageUser.model_validate(data["message"])
                session.branches[branch_id].queued[role].append(msg)
                await session.broadcast(
                    {
                        "t": "queued",
                        "v": session.version,
                        "branch": branch_id,
                        "role": role,
                        "message": data["message"],
                    }
                )
        case "branch" | "resample":
            async with session._dispatch_lock:  # noqa: SLF001
                anchor = data["at"]
                parent = session.branches.get(session.current)  # type: ignore[arg-type]
                if parent is None:
                    logger.warning("%r before start — dropping", data.get("t"))
                    return
                try:
                    prefix = slice_at(parent.audit_tape.log, anchor)
                except ValueError as exc:
                    logger.warning("slice_at failed for %r: %s", anchor, exc)
                    await session.broadcast({"t": "error", "v": session.version, "message": str(exc)})
                    return
                new_id = uuid()
                new_branch = Branch(
                    session,
                    new_id,
                    seed=parent.seed,
                    auditor_model=parent.auditor_model,
                    target_model=parent.target_model,
                    max_turns=parent.max_turns,
                    resume=prefix,
                    parent_id=session.current,
                    branched_at=anchor,
                )
                await _stop_running_branches(session)
                session.branches[new_id] = new_branch
                session.current = new_id
                await session.broadcast({"t": "state", "v": session.version, **session.view()})
                task = asyncio.create_task(new_branch.run())
                session.branch_tasks.append(task)

        case "edit":
            async with session._dispatch_lock:  # noqa: SLF001
                anchor = data["at"]
                parent = session.branches.get(session.current)  # type: ignore[arg-type]
                if parent is None:
                    logger.warning("edit before start — dropping")
                    return
                try:
                    prefix = slice_at(parent.audit_tape.log, anchor)
                except ValueError as exc:
                    logger.warning("slice_at failed for edit %r: %s", anchor, exc)
                    await session.broadcast({"t": "error", "v": session.version, "message": str(exc)})
                    return
                # Find the original anchor step so we can match its source. The
                # anchor is the last value-bearing step with a matching anchor_id.
                orig_step = next(
                    (s for s in reversed(prefix) if s.anchor_id == anchor),
                    None,
                )
                if orig_step is None:
                    logger.warning("edit: anchor step not found after slice — dropping")
                    return
                if orig_step.source != "Model.generate":
                    logger.warning("edit: non-target step anchor %r (source=%r) — dropping", anchor, orig_step.source)
                    await session.broadcast({"t": "error", "v": session.version, "message": "edit anchor is not a target step"})
                    return
                edited_output = ModelOutput.model_validate(data["output"])
                # Use a fresh message id for the edited step so downstream code
                # never sees a stale anchor ref (footgun #3). The anchor_id of the
                # new step is the *new* message id.
                new_msg_id = uuid()
                # Patch the message id on the edited output's first choice message.
                if edited_output.choices:
                    edited_output.choices[0].message.id = new_msg_id
                edited_step = Step(
                    value=edited_output,
                    source=orig_step.source,
                    anchor_id=new_msg_id,
                )
                # Truncate at the anchor (exclusive) then append the edited step.
                idx = next(i for i, s in enumerate(prefix) if s is orig_step)
                prefix_excl = prefix[:idx]
                new_id = uuid()
                new_branch = Branch(
                    session,
                    new_id,
                    seed=parent.seed,
                    auditor_model=parent.auditor_model,
                    target_model=parent.target_model,
                    max_turns=parent.max_turns,
                    resume=prefix_excl + [edited_step],
                    parent_id=session.current,
                    branched_at=anchor,
                )
                await _stop_running_branches(session)
                session.branches[new_id] = new_branch
                session.current = new_id
                await session.broadcast({"t": "state", "v": session.version, **session.view()})
                task = asyncio.create_task(new_branch.run())
                session.branch_tasks.append(task)

        case "switch":
            async with session._dispatch_lock:  # noqa: SLF001
                branch_id = data["branch"]
                if branch_id not in session.branches:
                    logger.warning("switch to unknown branch %r — dropping", branch_id)
                    return
                session.current = branch_id
                await session.broadcast({"t": "state", "v": session.version, **session.view()})

        case other:
            logger.warning("unknown command %r", other)


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8765)


if __name__ == "__main__":
    main()
