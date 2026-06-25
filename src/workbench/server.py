"""FastAPI + WebSocket server for the audit-workbench (M0 skeleton).

One `Session` per `session_id`. The WS endpoint accepts a connection, registers
it, pushes full state, then dispatches commands: `start` (create + run a Branch),
`step`, `play`, `pause` (STREAMING.md §C).
"""

from __future__ import annotations

import os

# Lower inspect's partial-output flush throttle for smoother live streaming.
# Must be set before any inspect_ai import — the constant is read at module
# load. The workbench has at most a handful of in-flight model calls, so the
# extra event churn vs. inspect's batch-eval default (0.1s) is negligible.
os.environ.setdefault("INSPECT_STREAM_FLUSH_INTERVAL", "0.025")

import asyncio
import logging

import anyio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from inspect_ai.model import ChatMessageUser, ModelOutput
from shortuuid import uuid

from inspect_petri.target import Step

from workbench.run import (
    Branch,
    edited_auditor_step,
    find_auditor_step,
    find_target_step,
    generate_rewrite,
    locate_staging_call,
)
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
            try:
                await _dispatch(session, data)
            except Exception as exc:  # noqa: BLE001
                # A malformed/unexpected command should surface to the client,
                # not crash the connection (which would orphan a running branch).
                logger.exception("dispatch failed for %r", data.get("t"))
                await session.broadcast(
                    {"t": "error", "v": session.version, "message": str(exc)}
                )
    except (WebSocketDisconnect, ConnectionError, OSError):
        pass
    finally:
        if websocket in session.connections:
            session.connections.remove(websocket)


def _parent(session: Session, data: dict) -> Branch | None:
    """Resolve the parent branch for a fork command.

    New per-message edit/resample commands name their `branch` explicitly; the
    legacy `branch`/`resample`/`edit` commands act on `session.current`.
    """
    branch_id = data.get("branch") or session.current
    if branch_id is None:
        return None
    return session.branches.get(branch_id)


async def _fork_error(session: Session, exc: Exception) -> None:
    logger.warning("fork failed: %s", exc)
    await session.broadcast({"t": "error", "v": session.version, "message": str(exc)})


async def _register_and_spawn(
    session: Session, branch: Branch, *, autoplay: bool
) -> None:
    """Common tail for start/branch/resample/edit: stop the previous branch,
    register `branch`, broadcast `state`, spawn `branch.run()`, wait for the
    replay prefix to drain (so the columns are hot before we return), then
    optionally release the gate."""
    await _stop_running_branches(session)
    session.branches[branch.branch_id] = branch
    session.current = branch.branch_id
    await session.broadcast({"t": "state", "v": session.version, **session.view()})
    task = asyncio.create_task(branch.run())
    session.branch_tasks.append(task)
    # Replay is I/O-free (cached generates, in-process channel rendezvous) so
    # this is <50ms for realistic prefixes; the timeout guards desync hangs.
    with anyio.move_on_after(5.0):
        await branch._replayed.wait()  # noqa: SLF001
    if autoplay:
        branch.play()
    await session.broadcast_status()


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
                raw_max_turns = data.get("max_turns")
                branch = Branch(
                    session,
                    uuid(),
                    seed=data["seed"],
                    auditor_model=data["auditor_model"],
                    target_model=data["target_model"],
                    max_turns=int(raw_max_turns) if raw_max_turns is not None else None,
                    auditor_config=data.get("auditor_config") or None,
                    target_config=data.get("target_config") or None,
                )
                # the branch registered its span ids in `session.span_role`;
                # `_register_and_spawn` re-broadcasts `state` so clients learn
                # the mapping. autoplay=True: a human is in the loop, so the
                # audit starts immediately; `play()` before the gate is safe
                # (the first wait finds the event already set).
                await _register_and_spawn(session, branch, autoplay=True)
        case "end":
            async with session._dispatch_lock:  # noqa: SLF001
                if session.current is None:
                    logger.warning("end before start — dropping")
                    return
                branch = session.branches[session.current]
                if branch.status == "ended":
                    return
                try:
                    await branch.channel.end_conversation()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("end_conversation raised: %r", exc)
                await _stop_running_branches(session)

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
                    await session.broadcast(
                        {
                            "t": "error",
                            "v": session.version,
                            "message": "branch has ended",
                        }
                    )
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
        case "branch":
            # Inclusive branch at a target assistant anchor: the clicked
            # target response is in the replayed prefix; the *next* auditor
            # turn goes live.
            async with session._dispatch_lock:  # noqa: SLF001
                anchor = data["at"]
                parent = _parent(session, data)
                if parent is None:
                    logger.warning("branch before start — dropping")
                    return
                try:
                    child = Branch.fork(session, parent, anchor=anchor)
                except ValueError as exc:
                    await _fork_error(session, exc)
                    return
                await _register_and_spawn(session, child, autoplay=False)

        case "resample":
            # WISHLIST 3b: regenerate the clicked *target* response. Branch
            # exclusive of that target step — the auditor turn that produced
            # it replays from `pending`, the target generate goes live.
            async with session._dispatch_lock:  # noqa: SLF001
                anchor = data["at"]
                parent = _parent(session, data)
                if parent is None:
                    logger.warning("resample before start — dropping")
                    return
                try:
                    # validate `anchor` is a target generate (so the user
                    # gets a precise error, not a desync mid-replay)
                    find_target_step(parent.audit_tape.log, anchor)
                    child = Branch.fork(
                        session, parent, anchor=anchor, inclusive=False
                    )
                except ValueError as exc:
                    await _fork_error(session, exc)
                    return
                await _register_and_spawn(session, child, autoplay=True)

        case "branch_auditor" | "resample_auditor":
            # Regenerate the auditor's `turn_index`-th response: branch
            # exclusive of that auditor step so the next live call is the
            # auditor's generate at that turn. `branch_auditor` is the same
            # backend op (matching target `branch`/`resample` parity).
            async with session._dispatch_lock:  # noqa: SLF001
                parent = _parent(session, data)
                if parent is None:
                    logger.warning("%r before start — dropping", data.get("t"))
                    return
                try:
                    _, step = find_auditor_step(
                        parent.audit_tape.log, int(data["turn_index"])
                    )
                    assert step.anchor_id is not None
                    child = Branch.fork(
                        session, parent, anchor=step.anchor_id, inclusive=False
                    )
                except ValueError as exc:
                    await _fork_error(session, exc)
                    return
                await _register_and_spawn(
                    session, child, autoplay=data["t"] == "resample_auditor"
                )

        case "edit_auditor_call":
            # WISHLIST 3a: edit an auditor tool_call's args, replay the turn.
            # Branch exclusive of the auditor step, append a copy with the
            # edited tool_call past `prefix_len`. On replay the edited output
            # is served from `pending`; `execute_tools` runs the edited args
            # live, so the target sees the new staged message / tool result.
            async with session._dispatch_lock:  # noqa: SLF001
                parent = _parent(session, data)
                if parent is None:
                    logger.warning("edit_auditor_call before start — dropping")
                    return
                call_id = data["call_id"]
                new_args = data["args"]

                def mutate(out: ModelOutput) -> None:
                    for tc in out.message.tool_calls or []:
                        if tc.id == call_id:
                            tc.arguments = dict(new_args)
                            return
                    raise ValueError(
                        f"call_id {call_id!r} not found in auditor turn "
                        f"{data['turn_index']}"
                    )

                try:
                    _, orig = find_auditor_step(
                        parent.audit_tape.log, int(data["turn_index"])
                    )
                    assert orig.anchor_id is not None
                    edited = edited_auditor_step(orig, mutate)
                    child = Branch.fork(
                        session,
                        parent,
                        anchor=orig.anchor_id,
                        inclusive=False,
                        edited=edited,
                    )
                except ValueError as exc:
                    await _fork_error(session, exc)
                    return
                await _register_and_spawn(session, child, autoplay=True)

        case "edit_target_message":
            # WISHLIST 3c (target-column convenience over 3a): a user/system/
            # tool message in the target conversation was produced by an
            # auditor staging tool_call (`send_message` / `set_system_message`
            # / `send_tool_call_result`). Locate that call via the marked
            # `Stage` step on the level-2 tape, edit its content arg, replay
            # the turn. Falls back to an error if the mapping is ambiguous
            # (e.g. two `send_message` calls in one auditor turn) — the user
            # can use `edit_auditor_call` from the auditor column instead.
            async with session._dispatch_lock:  # noqa: SLF001
                parent = _parent(session, data)
                if parent is None:
                    logger.warning("edit_target_message before start — dropping")
                    return
                content = data["content"]
                try:
                    _, orig, call_id, arg_key = locate_staging_call(
                        parent.audit_tape.log,
                        message_id=data["message_id"],
                        role=data["role"],
                        tool_call_id=data.get("tool_call_id"),
                    )
                except ValueError as exc:
                    await _fork_error(session, exc)
                    return

                def mutate(out: ModelOutput) -> None:
                    for tc in out.message.tool_calls or []:
                        if tc.id == call_id:
                            tc.arguments = {**tc.arguments, arg_key: content}
                            return
                    raise ValueError(f"call_id {call_id!r} vanished")

                assert orig.anchor_id is not None
                try:
                    edited = edited_auditor_step(orig, mutate)
                    child = Branch.fork(
                        session,
                        parent,
                        anchor=orig.anchor_id,
                        inclusive=False,
                        edited=edited,
                    )
                except ValueError as exc:
                    await _fork_error(session, exc)
                    return
                await _register_and_spawn(session, child, autoplay=True)

        case "edit":
            # Edit a *target* assistant response in place: branch exclusive
            # of that target step, append the edited `ModelOutput` past
            # `prefix_len`. The auditor turn that produced it replays from
            # `pending`; the edited output is served (`_DivergentTape` emits
            # its `ModelEvent`); the *next* auditor turn goes live.
            async with session._dispatch_lock:  # noqa: SLF001
                anchor = data["at"]
                parent = session.branches.get(session.current)  # type: ignore[arg-type]
                if parent is None:
                    logger.warning("edit before start — dropping")
                    return
                try:
                    _, orig = find_target_step(parent.audit_tape.log, anchor)
                except ValueError as exc:
                    await _fork_error(session, exc)
                    return
                edited_output = ModelOutput.model_validate(data["output"])
                # Fresh message id so downstream code never sees a stale
                # anchor ref (footgun #3); the new step's anchor is the new id.
                new_msg_id = uuid()
                if edited_output.choices:
                    edited_output.choices[0].message.id = new_msg_id
                edited_step = Step(
                    value=edited_output, source=orig.source, anchor_id=new_msg_id
                )
                try:
                    child = Branch.fork(
                        session,
                        parent,
                        anchor=anchor,
                        inclusive=False,
                        edited=edited_step,
                    )
                except ValueError as exc:
                    await _fork_error(session, exc)
                    return
                await _register_and_spawn(session, child, autoplay=True)

        case "rewrite_tool_call":
            # Stateless draft: ask the auditor model to rewrite one tool_call's
            # args per a freeform instruction. NOT a fork — no tape mutation,
            # no `_dispatch_lock` (the model call may take seconds and must not
            # block transport/branch commands). The client applies the draft
            # via `edit_auditor_call` if accepted.
            branch_id = data["branch"]
            call_id = data["call_id"]
            branch = session.branches.get(branch_id)
            if branch is None:
                await session.broadcast(
                    {
                        "t": "rewrite_draft",
                        "v": session.version,
                        "branch": branch_id,
                        "call_id": call_id,
                        "error": f"unknown branch {branch_id!r}",
                    }
                )
                return
            try:
                args, raw = await generate_rewrite(
                    branch,
                    int(data["turn_index"]),
                    call_id,
                    data["instruction"],
                    selected_text=data.get("selected_text"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("rewrite_tool_call failed: %s", exc, exc_info=True)
                await session.broadcast(
                    {
                        "t": "rewrite_draft",
                        "v": session.version,
                        "branch": branch_id,
                        "call_id": call_id,
                        "error": str(exc),
                    }
                )
                return
            await session.broadcast(
                {
                    "t": "rewrite_draft",
                    "v": session.version,
                    "branch": branch_id,
                    "call_id": call_id,
                    "args": args,
                    "raw": raw,
                }
            )

        case "rewrite_target_message":
            # Target-column counterpart to `rewrite_tool_call`: a target-side
            # user/system/tool message maps to an auditor staging call via
            # `locate_staging_call`; rewrite that call's args with the auditor
            # model, then return the staging-arg content (the field that
            # becomes the visible message text) so the client can preview it
            # and apply via `edit_target_message`. Stateless draft — no fork,
            # no `_dispatch_lock`.
            branch_id = data["branch"]
            message_id = data["message_id"]
            branch = session.branches.get(branch_id)
            if branch is None:
                await session.broadcast(
                    {
                        "t": "rewrite_draft",
                        "v": session.version,
                        "branch": branch_id,
                        "message_id": message_id,
                        "error": f"unknown branch {branch_id!r}",
                    }
                )
                return
            try:
                aud_idx, _orig, call_id, arg_key = locate_staging_call(
                    branch.audit_tape.log,
                    message_id=message_id,
                    role=data["role"],
                    tool_call_id=data.get("tool_call_id"),
                )
                turn_index = sum(
                    1
                    for s in branch.audit_tape.log[:aud_idx]
                    if isinstance(s.value, ModelOutput)
                    and s.source == "auditor:Model.generate"
                )
                args, raw = await generate_rewrite(
                    branch,
                    turn_index,
                    call_id,
                    data["instruction"],
                    selected_text=data.get("selected_text"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("rewrite_target_message failed: %s", exc, exc_info=True)
                await session.broadcast(
                    {
                        "t": "rewrite_draft",
                        "v": session.version,
                        "branch": branch_id,
                        "message_id": message_id,
                        "error": str(exc),
                    }
                )
                return
            content = args.get(arg_key)
            await session.broadcast(
                {
                    "t": "rewrite_draft",
                    "v": session.version,
                    "branch": branch_id,
                    "message_id": message_id,
                    "args": args,
                    "raw": raw,
                    "content": content if isinstance(content, str) else raw,
                }
            )

        case "switch":
            async with session._dispatch_lock:  # noqa: SLF001
                branch_id = data["branch"]
                if branch_id not in session.branches:
                    logger.warning("switch to unknown branch %r — dropping", branch_id)
                    return
                session.current = branch_id
                await session.broadcast(
                    {"t": "state", "v": session.version, **session.view()}
                )

        case other:
            logger.warning("unknown command %r", other)


def main() -> None:
    import os

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("WB_PORT", "8765")))


if __name__ == "__main__":
    main()
