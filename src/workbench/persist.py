"""M0 session persistence (M1-REFACTOR Batch I; sibling of `m1/persist.py`).

`save_session` writes ``{store_dir}/{session_id}/`` with ``index.json``,
``history.json`` (`audit_history.dump()`), and one ``{branch_id}.json`` per
branch. `load_session` reconstructs the `Session`, resets each persisted
trajectory's tape for replay, and spawns `Branch.run()` in pre-order so a
child's shared-prefix splice finds its parent's already-replayed events.

Replay is ungated and I/O-free (`workbench_auditor` skips the gate while
`tape.pending` is non-empty), so the transcript's events, pool, and per-role
timelines are reconstructed deterministically from the persisted L2 tape — no
live model calls.

`Session.save()`/`Session.load()` delegate here; direct callers may use these
free functions instead.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from inspect_petri.target import History, Trajectory

if TYPE_CHECKING:
    from workbench.run import Branch
    from workbench.session import Session


def save_session(session: "Session", branch: "Branch | None" = None) -> None:
    """Persist `session` under ``{store_dir}/{session_id}/``.

    Writes ``index.json`` (current/created_at/seed), ``history.json``, and
    ``{branch_id}.json`` — for every branch when `branch` is ``None``, or just
    the given one (called from `Branch.run()`'s ``finally`` so every settled
    branch lands on disk without a full save from the dispatch path). No-op if
    `session_id` or `store_dir` is unset (e.g. smoke tests).
    """
    if session.store_dir is None or session.session_id is None:
        return
    d = session.store_dir / session.session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.json").write_text(
        json.dumps(
            {
                "current": session.current,
                "created_at": session.created_at,
                "seed": session.seed,
            }
        )
    )
    (d / "history.json").write_text(json.dumps(session.audit_history.dump()))
    for b in [branch] if branch else session.branches.values():
        _write_branch(d, b)
    if session.orchestrator is not None:
        from workbench.m1.persist import save_orchestrator  # noqa: PLC0415

        save_orchestrator(session.orchestrator, session, d)


def _write_branch(d: Path, branch: "Branch") -> None:
    meta = {
        k: v
        for k, v in asdict(branch.meta).items()
        if k not in ("auditor_model_args", "target_model_args")
    }
    (d / f"{branch.branch_id}.json").write_text(
        json.dumps({"meta": meta, "status": branch.status})
    )


async def load_session(session_id: str, store_dir: Path) -> "Session":
    """Reconstruct a `Session` from ``{store_dir}/{session_id}/``.

    Rebuilds `audit_history` via `History.load`, then for each persisted
    branch resets its trajectory's tape for replay (``pending ← log``,
    ``log ← []``; `prefix_len` preserved) and spawns `Branch.run()`. Branches
    whose persisted status was ``"ended"`` are `play()`-ed so a tape that
    terminated via ``end_conversation`` runs to completion and the spawned
    task exits.
    """
    from workbench.run import Branch, BranchMeta  # noqa: PLC0415
    from workbench.session import Session  # noqa: PLC0415

    d = store_dir / session_id
    index = json.loads((d / "index.json").read_text())
    sess = Session(session_id, store_dir)
    sess.created_at = index["created_at"]
    sess.audit_history = History.load(json.loads((d / "history.json").read_text()))
    await sess.start()

    # Spawn in pre-order (parent before children) so a child's shared-
    # prefix splice in `build_auditor_timeline` finds the parent's
    # already-replayed events in `session.events`.
    def preorder(t: Trajectory) -> list[Trajectory]:
        out = [t]
        for c in t.children:
            out.extend(preorder(c))
        return out

    for traj in preorder(sess.audit_history.root):
        bf = d / f"{traj.span_id}.json"
        if not bf.exists():
            continue  # the synthetic root, or a trajectory with no Branch
        data = json.loads(bf.read_text())
        meta = BranchMeta(**data["meta"])
        # Reset the tape for replay: serve the full recorded log from
        # `pending`. `prefix_len` is unchanged so `shared_prefix_len` /
        # `branched_at_turn` / the `_on_event` splice gate behave as
        # they did in the original run; steps past `prefix_len` are
        # served on the divergent path (workbench_auditor emits their
        # `ModelEvent`s inline).
        traj.tape.rewind()
        branch = Branch(sess, trajectory=traj, **asdict(meta))
        branch.status = data["status"]
        sess.branches[branch.branch_id] = branch
        if data["status"] == "ended":
            branch.play()
        sess.branch_tasks[branch.branch_id] = asyncio.create_task(branch.run())
        with anyio.move_on_after(5.0):
            await branch._replayed.wait()  # noqa: SLF001

    sess.current = index["current"]

    if (d / "orchestrator.eval").exists():
        from workbench.m1.persist import load_orchestrator  # noqa: PLC0415

        meta = load_orchestrator(sess, d)
        await sess.start_orchestrator(**meta)

    return sess
