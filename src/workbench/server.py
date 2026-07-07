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

import argparse
import asyncio
import json
import logging
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import anyio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from inspect_ai.model import ChatMessageUser, ModelOutput
from inspect_petri.target import Step
from shortuuid import uuid

from workbench import config
from workbench.export import export_branch, import_eval
from workbench.m1.attach import AttachedRun
from workbench.run import (
    Branch,
    edited_auditor_step,
    find_auditor_step,
    find_target_step,
    generate_rewrite,
    locate_staging_call,
)
from workbench.session import CandidateBatch, Session, _cancel_all

logger = logging.getLogger(__name__)

sessions: dict[str, Session] = {}

#: Sessions directory — sourced from `workbench.config` (P0.3) so it agrees
#: with `Orchestrator.session_dir`. Kept as a module global (rather than
#: calling ``config.sessions_dir()`` at each use-site) so smoke fixtures can
#: patch it. `main()` mutates ``config.STORE_DIR`` from ``--store-dir`` and
#: re-derives this before any session is created.
STORE_DIR: Path = config.sessions_dir()


def _save_all_sessions() -> None:
    """P0.2 — flush every live session to disk on shutdown."""
    for s in list(sessions.values()):
        try:
            s.save()
        except Exception:
            logger.exception("save on shutdown failed for %r", s.session_id)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Persist every live session on shutdown (P0.2).

    Uvicorn's own SIGINT/SIGTERM handlers set ``should_exit`` and then run
    lifespan shutdown, so this fires on Ctrl-C and ``kill`` alike. `main()`
    additionally registers ``signal.signal`` fallbacks for non-uvicorn
    embeddings; uvicorn overrides those via ``loop.add_signal_handler`` so
    they're inert in the normal path.
    """
    yield
    _save_all_sessions()


app = FastAPI(title="Audit Workbench", version="0.1.0", lifespan=lifespan)


async def _get_or_create(session_id: str) -> Session:
    sess = sessions.get(session_id)
    if sess is None:
        if (STORE_DIR / session_id / "index.json").exists():
            # `Session.load` starts the drain task and replays every
            # persisted branch before returning, so the first
            # `push_full_state` ships a fully-reconstructed view.
            sess = await Session.load(session_id, STORE_DIR)
        else:
            sess = Session(session_id, STORE_DIR)
            # start the session-owned drain task once, on first connect, so
            # the single broadcast loop is live before any branch runs
            # (STREAMING.md §B; petri footgun #12 — never one drain per branch).
            await sess.start()
        sessions[session_id] = sess
    return sess


#: Map ``read_model_info()`` org prefixes to inspect provider names where
#: they differ (P1.3). Most first-party orgs already match; ``gdm`` is the
#: YAML's Google key on some inspect versions.
_ORG_TO_PROVIDER = {"gdm": "google"}


@app.get("/models")
def list_models() -> dict[str, Any]:
    """P1.3: provider list + per-provider model-id suggestions, all offline.

    ``providers`` is every registered ``modelapi`` (built-in + entry-point
    extensions like ``narwhal``). ``suggestions`` merges our curated
    ``MODEL_ORDER`` (flagship-first) with inspect's hand-maintained
    ``read_model_info`` YAML, filtered to ids whose ``provider/`` prefix is a
    real registered provider — so every suggestion is a valid ``get_model()``
    argument. Gateway/local providers (``vllm/`` etc.) get no suggestions;
    the picker's free-text path covers them.
    """
    from inspect_ai._util.entrypoints import ensure_entry_points
    from inspect_ai._util.registry import registry_find, registry_unqualified_name

    from workbench.m1.model_palette import MODEL_ORDER

    ensure_entry_points()
    providers = sorted(
        {
            registry_unqualified_name(i)
            for i in registry_find(lambda i: i.type == "modelapi")
        }
        - {"none"}
    )
    provider_set = set(providers)

    suggestions: dict[str, list[str]] = {}

    def _add(model_id: str) -> None:
        org, _, name = model_id.partition("/")
        if not name:
            return
        prov = _ORG_TO_PROVIDER.get(org, org)
        if prov not in provider_set:
            return
        full = f"{prov}/{name}" if prov != org else model_id
        bucket = suggestions.setdefault(prov, [])
        if full not in bucket:
            bucket.append(full)

    for ids in MODEL_ORDER.values():
        for m in ids:
            _add(m)
    # inspect's private model-data YAML — fail soft to MODEL_ORDER only.
    try:
        from inspect_ai.model._model_data.model_data import read_model_info

        for m in read_model_info():
            _add(m)
    except Exception:  # noqa: BLE001
        logger.debug("read_model_info unavailable; suggestions = MODEL_ORDER only")

    return {"providers": providers, "suggestions": suggestions}


@app.get("/scanners")
def list_scanners() -> dict[str, Any]:
    """P1.8(a): scanner library + named groups for the M0 scan picker.

    ``scanners`` is every name ``wb.scan(logs, "name")`` can resolve
    (built-in registry + entry-point extensions + user
    ``scanner_dir/*.py``); ``groups`` is the parsed ``groups.yaml``.
    """
    from workbench.m1.scanners import load_groups, load_library

    return {"scanners": sorted(load_library()), "groups": load_groups()}


@app.get("/settings")
def get_settings() -> dict[str, Any]:
    """P1.7: the global `Settings` dataclass as a flat dict."""
    d = asdict(config.settings)
    d.update(d.pop("extra"))
    return d


@app.patch("/settings")
def patch_settings(body: dict[str, Any]) -> dict[str, Any]:
    """P1.7: merge ``body`` into ``config.settings``, persist, return new.

    Rebinds the module-level singleton so subsequent ``build_system_prompt``
    calls (next ``start_orchestrator``) see the new thresholds. Already-
    running orchestrators keep their rendered prompt.
    """
    config.patch_settings(body)
    d = asdict(config.settings)
    d.update(d.pop("extra"))
    return d


@app.get("/sessions")
def list_sessions() -> list[dict[str, Any]]:
    """Sidebar "Recents": one entry per persisted session under `STORE_DIR`."""
    out: list[dict[str, Any]] = []
    if not STORE_DIR.exists():
        return out
    for d in STORE_DIR.iterdir():
        idx = d / "index.json"
        if not idx.is_file():
            continue
        index = json.loads(idx.read_text())
        n_branches = sum(
            1 for f in d.glob("*.json") if f.name not in ("index.json", "history.json")
        )
        out.append(
            {
                "session_id": d.name,
                "seed": index.get("seed", ""),
                "created_at": index.get("created_at", ""),
                "n_branches": n_branches,
            }
        )
    out.sort(key=lambda e: e["created_at"], reverse=True)
    return out


@app.get("/sessions/{session_id}/findings")
async def list_findings(session_id: str) -> list[dict[str, Any]]:
    """P0.6: the durable ``findings.jsonl`` for one session, as JSON.

    Reads via the live ``Orchestrator.session_dir`` (findings land under the
    orchestrator's ``span_id`` dir, not the session-persist dir), so the
    session is loaded on demand if not already in memory.
    """
    session = await _get_or_create(session_id)
    orch = session.orchestrator
    if orch is None:
        return []
    from workbench.m1.proposals import load_findings

    return [asdict(f) for f in load_findings(orch.session_dir)]


@app.get("/sessions/{session_id}/export.md")
async def export_findings(session_id: str) -> Response:
    """P1.6: markdown write-up of every signed finding (``text/markdown``)."""
    session = await _get_or_create(session_id)
    from workbench.m1.export import export_findings_md

    return Response(
        content=export_findings_md(session),
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="findings-{session_id}.md"'
        },
    )


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
            except Exception as exc:
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
    legacy `branch`/`resample` commands act on `session.current`.
    """
    branch_id = data.get("branch") or session.current
    if branch_id is None:
        return None
    return session.branches.get(branch_id)


async def _fork_error(session: Session, exc: Exception) -> None:
    logger.warning("fork failed: %s", exc)
    await session.broadcast({"t": "error", "v": session.version, "message": str(exc)})


def _spawn(session: Session, branch: Branch) -> asyncio.Task[None]:
    """Register `branch` on `session` and detach its `run()` task."""
    session.branches[branch.branch_id] = branch
    task = asyncio.create_task(branch.run())
    session.branch_tasks[branch.branch_id] = task
    return task


async def _register_and_spawn(
    session: Session, branch: Branch, *, autoplay: bool
) -> None:
    """Common tail for start/branch/resample/edit: stop the previous branch,
    set `current`, broadcast `state`, spawn `branch.run()`, wait for the
    replay prefix to drain (so the columns are hot before we return), then
    optionally release the gate."""
    await _stop_running_branches(session)
    session.branches[branch.branch_id] = branch
    session.current = branch.branch_id
    await session.broadcast({"t": "state", "v": session.version, **session.view()})
    _spawn(session, branch)
    # Replay is I/O-free (cached generates, in-process channel rendezvous) so
    # this is <50ms for realistic prefixes; the timeout guards desync hangs.
    with anyio.move_on_after(5.0):
        await branch._replayed.wait()  # noqa: SLF001
    if autoplay:
        branch.play()
    await session.broadcast_status()


async def _stop_running_branches(
    session: Session, only: set[str] | None = None
) -> None:
    """Cancel running branch tasks (all, or a subset).

    The single-fork commands (`start`/`branch`/`resample`/`edit_*`) call
    this with ``only=None`` to stop everything before installing a new
    `current` — there's one status pill / step / play / pause target, so
    those keep one-running-branch as a *semantic* invariant. Resample-N's
    `pick_candidate` / `dismiss_candidates` pass ``only=`` to cancel just
    the unpicked candidates (RESAMPLE-N.md — concurrent branches are safe
    since #5/#12: per-branch `Store`, session-owned drain, sync
    `_on_event`, `(branch_id, role)`-keyed `by_role`).

    The cancelled `Branch` (its `audit_tape`, settled events, store)
    stays in `session.branches` — only the live coroutine stops.
    `run_audit`'s `finally` persists the tape on cancellation.
    """
    running = {
        bid: t
        for bid, t in session.branch_tasks.items()
        if not t.done() and (only is None or bid in only)
    }
    await _cancel_all(list(running.values()))
    for bid in running:
        del session.branch_tasks[bid]
        if (b := session.branches.get(bid)) is not None and b.status == "running":
            b.status = "ended"


#: `locate(parent, data) -> (anchor, inclusive, edited)` for one fork variant.
Locate = Callable[[Branch, dict], tuple[str, bool, Step | None]]


async def _fork(
    session: Session, data: dict, *, locate: Locate, autoplay: bool
) -> None:
    """Common path for the six fork-shaped commands.

    `locate` projects the command body onto a `Branch.fork()` call site:
    the L2 anchor to branch at, whether the matched step is in the
    replayed prefix, and an optional divergent edited step appended past
    `prefix_len` (`edit_*` ops). Any `ValueError` it raises surfaces to
    the client as `{t:"error"}` instead of forking.
    """
    parent = _parent(session, data)
    if parent is None:
        logger.warning("%r before start — dropping", data.get("t"))
        return
    try:
        anchor, inclusive, edited = locate(parent, data)
        child = Branch.fork(
            session, parent, anchor=anchor, inclusive=inclusive, edited=edited
        )
    except ValueError as exc:
        await _fork_error(session, exc)
        return
    await _register_and_spawn(session, child, autoplay=autoplay)


async def _step_after_replay(branch: Branch) -> None:
    """Release one auditor turn once `branch`'s prefix replay drains.

    Used by `candidates_auditor`: each candidate's auditor turn at the
    branch point is excluded, so replay parks at the gate; one `step()`
    yields exactly the divergent auditor turn (RESAMPLE-N.md §Mechanics).
    """
    await branch._replayed.wait()  # noqa: SLF001
    branch.step()


async def _candidates(
    session: Session,
    data: dict,
    *,
    locate: Locate,
    kind: Literal["target", "auditor"],
    step: bool,
) -> None:
    """Resample-N: spawn `n` background sibling forks at one anchor.

    Unlike `_fork`, candidates do NOT stop other branches and do NOT
    repoint `session.current` — they run concurrently with the parent
    until `pick_candidate` / `dismiss_candidates`. The handler returns
    after a single `state` broadcast (no `await _replayed`; model
    latency × N would trip the 5s cap — picker cards fill as events
    stream).
    """
    parent = _parent(session, data)
    if parent is None:
        logger.warning("%r before start — dropping", data.get("t"))
        return
    try:
        anchor, inclusive, _ = locate(parent, data)
    except ValueError as exc:
        await _fork_error(session, exc)
        return
    n = int(data["n"])
    batch_id = uuid()
    batch = CandidateBatch(parent=parent.branch_id, anchor=anchor, kind=kind)
    for _ in range(n):
        child = Branch.fork(
            session, parent, anchor=anchor, inclusive=inclusive, batch=batch_id
        )
        _spawn(session, child)
        batch.children.append(child.branch_id)
        if step:
            asyncio.create_task(_step_after_replay(child))  # noqa: RUF006
    session.candidate_batches[batch_id] = batch
    await session.broadcast({"t": "state", "v": session.version, **session.view()})


def _auditor_anchor(parent: Branch, turn_index: int) -> str:
    step = find_auditor_step(parent.audit_tape.log, turn_index)
    assert step.anchor_id is not None
    return step.anchor_id


def _locate_branch(parent: Branch, data: dict) -> tuple[str, bool, Step | None]:  # noqa: ARG001
    return data["at"], True, None


def _locate_resample(parent: Branch, data: dict) -> tuple[str, bool, Step | None]:
    # validate `anchor` is a target generate (so the user gets a precise
    # error, not a desync mid-replay)
    find_target_step(parent.audit_tape.log, data["at"])
    return data["at"], False, None


def _locate_auditor(parent: Branch, data: dict) -> tuple[str, bool, Step | None]:
    return _auditor_anchor(parent, int(data["turn_index"])), False, None


def _locate_edit_auditor_call(
    parent: Branch, data: dict
) -> tuple[str, bool, Step | None]:
    """`edit_auditor_call`: edit one tool_call's args, replay the turn.

    Branches exclusive of the auditor step and appends a copy with the
    edited tool_call past `prefix_len`. On replay the edited output is
    served from `pending`; `execute_tools` runs the edited args live, so
    the target sees the new staged message / tool result.
    """
    orig = find_auditor_step(parent.audit_tape.log, int(data["turn_index"]))
    assert orig.anchor_id is not None
    call_id, new_args = data["call_id"], data["args"]

    def mutate(out: ModelOutput) -> None:
        for tc in out.message.tool_calls or []:
            if tc.id == call_id:
                tc.arguments = dict(new_args)
                return
        raise ValueError(
            f"call_id {call_id!r} not found in auditor turn {data['turn_index']}"
        )

    return orig.anchor_id, False, edited_auditor_step(orig, mutate)


def _locate_edit_target_message(
    parent: Branch, data: dict
) -> tuple[str, bool, Step | None]:
    """`edit_target_message` (target-column convenience over
    `edit_auditor_call`): a user/system/tool message in the target
    conversation was produced by an auditor staging tool_call
    (`send_message` / `set_system_message` / `send_tool_call_result`).
    Locate that call via the marked `Stage` step on the level-2 tape,
    edit its content arg, replay the turn. Falls back to an error if the
    mapping is ambiguous — the user can use `edit_auditor_call` from the
    auditor column instead.
    """
    _, orig, call_id, arg_key = locate_staging_call(
        parent.audit_tape.log,
        message_id=data["message_id"],
        role=data["role"],
        tool_call_id=data.get("tool_call_id"),
    )
    assert orig.anchor_id is not None
    content = data["content"]

    def mutate(out: ModelOutput) -> None:
        for tc in out.message.tool_calls or []:
            if tc.id == call_id:
                tc.arguments = {**tc.arguments, arg_key: content}
                return
        raise ValueError(f"call_id {call_id!r} vanished")

    return orig.anchor_id, False, edited_auditor_step(orig, mutate)


async def _draft_reply(
    session: Session, data: dict, key: dict[str, str], **body: Any
) -> None:
    await session.broadcast(
        {
            "t": "rewrite_draft",
            "v": session.version,
            "branch": data["branch"],
            **key,
            **body,
        }
    )


async def _rewrite_draft(
    session: Session,
    data: dict,
    *,
    key: dict[str, str],
    turn_index: int,
    call_id: str,
    arg_key: str | None = None,
) -> None:
    """Stateless draft: ask the auditor model to rewrite one tool_call's
    args per a freeform instruction; broadcast the result as
    `{t:"rewrite_draft", …key, args, raw[, content]}` (or `error` on
    failure). `key` carries the client correlation field (`call_id` or
    `message_id`). When `arg_key` is given (target-column rewrite), the
    staging arg's value is also returned as `content` for direct
    `edit_target_message` apply.
    """
    branch = session.branches.get(data["branch"])
    if branch is None:
        await _draft_reply(
            session, data, key, error=f"unknown branch {data['branch']!r}"
        )
        return
    try:
        args, raw = await generate_rewrite(
            branch,
            turn_index,
            call_id,
            data["instruction"],
            selected_text=data.get("selected_text"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s failed: %s", data.get("t"), exc, exc_info=True)
        await _draft_reply(session, data, key, error=str(exc))
        return
    extra: dict[str, Any] = {}
    if arg_key is not None:
        content = args.get(arg_key)
        extra["content"] = content if isinstance(content, str) else raw
    await _draft_reply(session, data, key, args=args, raw=raw, **extra)


async def _import(session: Session, history: Any, meta: Any) -> None:
    """Graft a loaded ``History`` under ``session.audit_history.root`` and
    replay it as a fresh root ``Branch`` (shared tail of ``import`` /
    ``import_running``). The imported root carries the full recorded log
    (``History.load`` contract); reset the tape for replay exactly as
    ``Session.load`` does."""
    imported = history.root
    imported.span_id = uuid()
    imported.parent = session.audit_history.root
    imported.branched_from = ""
    session.audit_history.root.children.append(imported)
    imported.tape.rewind()
    branch = Branch(session, trajectory=imported, **asdict(meta))
    await _register_and_spawn(session, branch, autoplay=False)


#: Commands whose handler must NOT take `_dispatch_lock` — `rewrite_*`
#: makes a model call (seconds) and `export` does file I/O; both are
#: stateless drafts that don't touch `branches` / `current` / tape.
#: `approve`/`detach_cell` only set a Future/Event — the cell task does
#: the work; blocking dispatch on them would serialise unrelated commands
#: behind a running cell.
UNLOCKED = {
    "rewrite_tool_call",
    "rewrite_target_message",
    "export",
    "approve",
    "detach_cell",
    "cancel_cell",
    "interrupt_and_send",
    "stop_sample",
}


async def _dispatch(session: Session, data: dict) -> None:
    if data.get("t") in UNLOCKED:
        await _dispatch_locked(session, data)
        return
    async with session._dispatch_lock:  # noqa: SLF001
        await _dispatch_locked(session, data)


# -- per-command handlers ------------------------------------------------------


async def _h_start(session: Session, data: dict) -> None:
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
        auditor_model_args=data.get("auditor_model_args") or None,
        target_model_args=data.get("target_model_args") or None,
    )
    # the branch registered its span ids in `session.span_role`;
    # `_register_and_spawn` re-broadcasts `state` so clients learn
    # the mapping. autoplay=True: a human is in the loop, so the
    # audit starts immediately; `play()` before the gate is safe
    # (the first wait finds the event already set).
    await _register_and_spawn(session, branch, autoplay=True)


async def _h_end(session: Session, data: dict) -> None:  # noqa: ARG001
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


async def _h_transport(session: Session, data: dict, cmd: str) -> None:
    """``step``/``play``/``pause`` — explicit target: a branch id, ``"orch"``
    for the orchestrator, or omitted for ``session.current`` (M0 back-compat).
    Lets the human step a non-current branch without a ``switch`` round-trip."""
    tgt = data.get("target")
    obj = (
        session.orchestrator
        if tgt == "orch"
        else session.branches.get(tgt or session.current or "")
    )
    if obj is None:
        logger.warning("%r: no target %r — dropping", cmd, tgt)
        return
    getattr(obj, cmd)()
    await session.broadcast_status()


async def _h_inject(session: Session, data: dict) -> None:
    branch_id = data["branch"]
    role = data["role"]
    if branch_id not in session.branches:
        logger.warning("inject for unknown branch %r — dropping", branch_id)
        return
    if session.branches[branch_id].status == "ended":
        logger.warning("inject into ended branch %r — dropping", branch_id)
        await session.broadcast(
            {"t": "error", "v": session.version, "message": "branch has ended"}
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


async def _h_pick_candidate(session: Session, data: dict) -> None:
    batch = session.candidate_batches[data["batch"]]
    picked = data["branch"]
    await _stop_running_branches(
        session, only={c for c in batch.children if c != picked}
    )
    session.current = picked
    batch.picked = picked
    await session.broadcast({"t": "state", "v": session.version, **session.view()})
    await session.broadcast_status()


async def _h_rewrite_target_message(session: Session, data: dict) -> None:
    """Target-column counterpart to ``rewrite_tool_call``: a target-side
    user/system/tool message maps to an auditor staging call via
    ``locate_staging_call``; rewrite that call's args with the auditor model,
    then return the staging-arg content (the field that becomes the visible
    message text) so the client can preview it and apply via
    ``edit_target_message``. Stateless draft — no fork, no ``_dispatch_lock``."""
    key = {"message_id": data["message_id"]}
    branch = session.branches.get(data["branch"])
    if branch is None:
        await _draft_reply(
            session, data, key, error=f"unknown branch {data['branch']!r}"
        )
        return
    try:
        turn_index, _orig, call_id, arg_key = locate_staging_call(
            branch.audit_tape.log,
            message_id=data["message_id"],
            role=data["role"],
            tool_call_id=data.get("tool_call_id"),
        )
    except ValueError as exc:
        await _draft_reply(session, data, key, error=str(exc))
        return
    await _rewrite_draft(
        session, data, key=key, turn_index=turn_index, call_id=call_id, arg_key=arg_key
    )


async def _h_export(session: Session, data: dict) -> None:
    """Write one branch (and its descendants) as a one-sample ``.eval``.
    Stateless — no fork, no lock; runs in the WS task."""
    branch_id = data["branch"]
    branch = session.branches.get(branch_id)
    if branch is None:
        await session.broadcast(
            {
                "t": "error",
                "v": session.version,
                "message": f"unknown branch {branch_id!r}",
            }
        )
        return
    export_branch(branch, data["path"])
    logger.info("exported branch %s → %s", branch_id, data["path"])


async def _h_start_orchestrator(session: Session, data: dict) -> None:
    if session.orchestrator is not None:
        await session.broadcast(
            {
                "t": "error",
                "v": session.version,
                "message": "orchestrator already running",
            }
        )
        return
    await session.start_orchestrator(
        model=data["model"],
        system_prompt=data.get("system_prompt"),
        generate_config=data.get("config") or None,
        model_args=data.get("model_args") or None,
        audit_defaults=data.get("audit_defaults") or None,
    )


async def _h_import_running(session: Session, data: dict) -> None:
    """M1-HYBRID §Import-to-auditor: the eval is a *subprocess*, so there is no
    in-process ``ActiveSample`` to snapshot. Attach to ``log_dir``, per-sample
    ``inspect/cancel_sample`` over ACP, wait for the recorder to flush that
    sample to the ``.eval``, then ``import_eval`` it — same tail as ``import``.
    Siblings keep running.

    If ``interrupt_sample`` returns ``False`` (no ACP server — eval wasn't
    launched with ``--acp-server``, or already exited) the flush poll may
    still succeed if the sample finished on its own; otherwise the timeout
    surfaces as ``{t:"error"}`` via the outer ``_dispatch`` guard.
    """
    sample_id = str(data["sample_id"])
    h = await AttachedRun.discover(data["log_dir"])
    await h.interrupt_sample(sample_id)
    location = await h.wait_for_sample(sample_id)
    history, meta = import_eval(location, sample_id)
    await _import(session, history, meta)


async def _h_approve(session: Session, data: dict) -> None:
    """Resolve a pending gate. ``verdict`` is opaque to the server — the
    awaiting ``_gate`` coroutine interprets it (edits/denied/answer)."""
    if session.orchestrator is None:
        return
    ok = session.orchestrator.gate.resolve(data["display_id"], data.get("verdict"))
    if not ok:
        logger.warning("approve for unknown display_id %r", data["display_id"])


async def _h_interrupt_and_send(session: Session, data: dict) -> None:
    """M1-FEATURES §11: kill the running cell (tool result becomes
    ``[interrupted by user …]``), queue the human's text, release one turn so
    both reach the *same* next generate. Not ``orch.send()`` — that would
    ``detach()``, which can win the race against the cancel and background the
    cell instead."""
    orch = session.orchestrator
    if orch is None:
        return
    orch.kernel.interrupt(int(data["turn"]))
    orch.queued.append(ChatMessageUser(content=data["text"]))
    orch.step()
    await session.broadcast_status()


async def _h_stop_sample(session: Session, data: dict) -> None:  # noqa: ARG001
    """M1-FEATURES §9: per-sample stop from a ``ProgressCard`` row.
    Post-M1-HYBRID the eval is a subprocess — same ACP per-sample cancel as
    ``import_running`` (without the flush-wait/import tail)."""
    h = await AttachedRun.discover(data["log_dir"])
    ok = await h.interrupt_sample(str(data["id"]))
    if not ok:
        logger.warning(
            "stop_sample %r: no ACP server for %r", data["id"], data["log_dir"]
        )


# -- dispatch ------------------------------------------------------------------


async def _dispatch_locked(session: Session, data: dict) -> None:  # noqa: PLR0915
    match data.get("t"):
        case "start":
            await _h_start(session, data)
        case "end":
            await _h_end(session, data)
        case "step" | "play" | "pause" as cmd:
            await _h_transport(session, data, cmd)
        case "inject":
            await _h_inject(session, data)
        case "branch":
            # Inclusive branch at a target assistant anchor: the clicked
            # target response is in the replayed prefix; the *next* auditor
            # turn goes live.
            await _fork(session, data, locate=_locate_branch, autoplay=False)
        case "resample":
            # WISHLIST 3b: regenerate the clicked *target* response. Branch
            # exclusive of that target step — the auditor turn that produced
            # it replays from `pending`, the target generate goes live.
            await _fork(session, data, locate=_locate_resample, autoplay=True)
        case "branch_auditor" | "resample_auditor":
            # Regenerate the auditor's `turn_index`-th response: branch
            # exclusive of that auditor step so the next live call is the
            # auditor's generate at that turn.
            await _fork(
                session,
                data,
                locate=_locate_auditor,
                autoplay=data["t"] == "resample_auditor",
            )
        case "candidates":
            # Resample-N target: N background forks at `at`, each
            # regenerates the clicked target response during ungated
            # replay then parks at the gate. `current` is unchanged.
            await _candidates(
                session, data, locate=_locate_resample, kind="target", step=False
            )
        case "candidates_auditor":
            # Resample-N auditor: N background forks exclusive of auditor
            # turn `turn_index`; one `step()` per child after replay
            # yields exactly the divergent auditor turn.
            await _candidates(
                session, data, locate=_locate_auditor, kind="auditor", step=True
            )
        case "pick_candidate":
            await _h_pick_candidate(session, data)
        case "dismiss_candidates":
            # Original = implicit candidate #0: pick the parent.
            batch = session.candidate_batches[data["batch"]]
            await _stop_running_branches(session, only=set(batch.children))
            batch.picked = batch.parent
            await session.broadcast(
                {"t": "state", "v": session.version, **session.view()}
            )
        case "edit_auditor_call":
            await _fork(session, data, locate=_locate_edit_auditor_call, autoplay=True)
        case "edit_target_message":
            await _fork(
                session, data, locate=_locate_edit_target_message, autoplay=True
            )
        case "rewrite_tool_call":
            # Stateless draft: ask the auditor model to rewrite one tool_call's
            # args per a freeform instruction. NOT a fork — no tape mutation,
            # no `_dispatch_lock`. Client applies via `edit_auditor_call`.
            await _rewrite_draft(
                session,
                data,
                key={"call_id": data["call_id"]},
                turn_index=int(data["turn_index"]),
                call_id=data["call_id"],
            )
        case "rewrite_target_message":
            await _h_rewrite_target_message(session, data)
        case "export":
            await _h_export(session, data)
        case "import":
            # Load one sample's `AuditTape` from a `.eval`, install it as a
            # fresh root branch, and replay it.
            history, meta = import_eval(data["path"], data.get("sample_id"))
            await _import(session, history, meta)
        case "start_orchestrator":
            await _h_start_orchestrator(session, data)
        case "import_running":
            await _h_import_running(session, data)
        case "orch_send":
            if session.orchestrator is None:
                logger.warning("orch_send before start_orchestrator — dropping")
                return
            session.orchestrator.send(data["text"])
        case "approve":
            await _h_approve(session, data)
        case "detach_cell":
            if session.orchestrator is not None:
                session.orchestrator.kernel.detach()
        case "cancel_cell":
            if session.orchestrator is not None:
                session.orchestrator.kernel.cancel(int(data["turn"]))
        case "interrupt_and_send":
            await _h_interrupt_and_send(session, data)
        case "rewind":
            # M1-FEATURES §2: discard orchestrator turn N onward.
            if session.orchestrator is not None:
                await session.orchestrator.rewind(int(data["turn"]))
        case "stop_sample":
            await _h_stop_sample(session, data)
        case "switch":
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
    global STORE_DIR  # noqa: PLW0603
    parser = argparse.ArgumentParser(prog="workbench")
    parser.add_argument(
        "--store-dir",
        type=Path,
        default=config.STORE_DIR,
        help=(
            "workbench persistence root (WORKBENCH_STORE; default: "
            "~/.workbench). Sessions land under {store-dir}/sessions/."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("WB_PORT", "8765"))
    )
    args = parser.parse_args()
    # P0.3: mutate the shared config so `Orchestrator.session_dir` (and any
    # other `sessions_dir()` reader) honours the flag. Setting the env var
    # after import doesn't re-read into `config.STORE_DIR`, but propagates to
    # any subprocess the orchestrator spawns.
    config.STORE_DIR = args.store_dir.expanduser()
    os.environ["WORKBENCH_STORE"] = str(config.STORE_DIR)
    STORE_DIR = config.sessions_dir()
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    # P0.2: belt-and-suspenders — uvicorn installs its own loop-level signal
    # handlers (which run `lifespan` shutdown → `_save_all_sessions`), so
    # these are overridden in the normal path; they cover embeddings that
    # bypass uvicorn's signal setup.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _save_all_sessions())
    uvicorn.run(app, host="0.0.0.0", port=args.port)  # noqa: S104


if __name__ == "__main__":
    main()
