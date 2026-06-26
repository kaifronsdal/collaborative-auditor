"""Persistence + `.eval` interop smoke test (#4/#5).

Builds a deterministic 3-turn base + one fork in a session, `save()`s it
to a temp dir, then in a *fresh* `Session` (new transcript, empty pool,
empty events) `load()`s it back and replays. Asserts:

(a) `normalize(loaded, bid) == normalize(orig, bid)` for every branch —
    the load-bearing check that replay-on-load reconstructs exactly the
    recorded auditor + target turns.
(b) `loaded.current` / `created_at` round-trip via `index.json`.
(c) `GET /sessions` lists the saved session with the right `n_branches`.
(d) `export_branch` → `import_eval` round-trips a branch's L2 tape and
    `BranchMeta` through a one-sample `.eval`.

Run:  uv run python -m workbench._smoke_persist
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import anyio

from workbench import server
from workbench._smoke_fixtures import (
    SCRIPT3,
    _nth_target_anchor,
    auditor_by_turn,
    make_base,
    normalize,
    run_child,
    run_suite,
    target_by_last_user,
)
from workbench.export import export_branch, import_eval
from workbench.run import Branch
from workbench.server import list_sessions
from workbench.session import Session


async def _build_session(store_dir: Path) -> tuple[Session, str, str]:
    """A two-branch session: 3-turn base + one inclusive fork at r1."""
    sess = Session("persist-test", store_dir)
    await sess.start()
    aud = auditor_by_turn(SCRIPT3)
    tgt = target_by_last_user({"u1": "r1", "u2": "r2"})
    base = await make_base(
        sess, auditor_outputs=aud, target_outputs=tgt, max_turns=3
    )
    anchor = _nth_target_anchor(base.audit_tape.log, 0)
    child = Branch.fork(sess, base, anchor=anchor, branch_id="child")
    child.meta = child.meta.__class__(**{**child.meta.__dict__, "max_turns": 3})
    sess.branches[child.branch_id] = child
    sess.current = child.branch_id
    sess.branch_tasks[child.branch_id] = asyncio.create_task(child.run())
    _, child = await run_child(sess)
    return sess, base.branch_id, child.branch_id


async def test_save_load_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        orig, base_id, child_id = await _build_session(store)
        orig.save()
        captured = {bid: normalize(orig, bid) for bid in orig.branches}
        await orig.close()

        # ── fresh "process": new Session, empty transcript/pool/events ───────
        loaded = await Session.load("persist-test", store)
        # let any branch whose run() ends naturally settle
        for t in loaded.branch_tasks.values():
            with anyio.move_on_after(5.0):
                await t

        assert set(loaded.branches) == set(captured), (
            f"branch ids mismatch: {set(loaded.branches)} vs {set(captured)}"
        )
        for bid, expect in captured.items():
            got = normalize(loaded, bid)
            assert got == expect, (
                f"\nbranch {bid!r} diverged after load:\n"
                f"  loaded   = {got}\n"
                f"  original = {expect}"
            )
        assert loaded.current == orig.current, (
            f"current: {loaded.current!r} != {orig.current!r}"
        )
        assert loaded.created_at == orig.created_at
        # the loaded child's tree position is recovered from the History
        assert loaded.branches[child_id].parent_id == base_id
        await loaded.close()


async def test_sessions_endpoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        sess, _, _ = await _build_session(store)
        sess.save()
        await sess.close()

        prev = server.STORE_DIR
        server.STORE_DIR = store
        try:
            entries = list_sessions()
        finally:
            server.STORE_DIR = prev
        assert len(entries) == 1, entries
        e = entries[0]
        assert e["session_id"] == "persist-test"
        assert e["n_branches"] == 2, e
        assert e["seed"]  # non-empty


async def test_eval_export_import() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp)
        sess, base_id, _ = await _build_session(store)
        base = sess.branches[base_id]
        out = store / "exported.eval"
        export_branch(base, out)
        assert out.exists(), "export_branch did not write the .eval"

        history, meta = import_eval(out)
        assert meta.seed == base.meta.seed
        assert meta.auditor_model == base.meta.auditor_model
        assert meta.target_model == base.meta.target_model
        # root carries the full recorded log; child subtree present
        root_steps = history.root.tape.log
        assert len(root_steps) == len(base.audit_tape.log), (
            f"{len(root_steps)} != {len(base.audit_tape.log)}"
        )
        assert len(history.root.children) == len(base.trajectory.children)
        await sess.close()


def main() -> int:
    return anyio.run(
        run_suite,
        [
            ("save → load → normalize() ==", test_save_load_roundtrip),
            ("GET /sessions", test_sessions_endpoint),
            (".eval export → import", test_eval_export_import),
        ],
    )


if __name__ == "__main__":
    sys.exit(main())
