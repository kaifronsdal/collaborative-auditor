"""Non-server smoke test for the audit-workbench event-pipe backend.

Exercises the real streaming path (real models, `streaming=True`) end to end:
creates a `Session`, a `Branch`, registers a fake connection that captures every
broadcast wire message, runs the branch under `play()`, then asserts the §C
protocol (`state` / `pool` / `event` / `update`) was emitted and that the pool
wire format round-trips — resolving a ModelEvent's `input_refs` against the
accumulated pool reconstructs its real input.

Run:  uv run python -m workbench._smoke
      uv run python -m workbench._smoke --dump fixtures/smoke.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import anyio

from workbench.run import Branch
from workbench.session import Session

MODEL = "anthropic/claude-haiku-4-5-20251001"
SEED = "test seed"


class FakeConn:
    """A WebSocket-shaped sink that records every wire message it receives."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


def _expand_refs(refs: list[list[int]], pool: list[dict]) -> list[dict]:
    """Port of `expandEvents`' expandRefs: [[start, end_excl], ...] → pool slices."""
    return [pool[i] for s, e in refs for i in range(s, e)]


async def _amain(dump_path: Path | None = None) -> None:
    session = Session()
    conn = FakeConn()
    session.connections.append(conn)

    branch = Branch(
        session,
        branch_id="b0",
        seed=SEED,
        auditor_model=MODEL,
        target_model=MODEL,
        max_turns=3,
    )
    session.branches["b0"] = branch
    session.current = "b0"

    # full state on connect, then run freely.
    await session.push_full_state(conn)
    branch.play()
    await branch.run()

    # --- assertions ----------------------------------------------------------
    kinds = [m["t"] for m in conn.sent]
    n_state = kinds.count("state")
    n_pool = kinds.count("pool")
    n_event = kinds.count("event")
    n_update = kinds.count("update")

    assert n_state >= 1, "expected ≥1 state message"

    pool_msgs = [m for m in conn.sent if m["t"] == "pool"]
    assert pool_msgs, "expected ≥1 pool message"
    assert any(m["entries"] for m in pool_msgs), "expected a pool delta with entries"

    # a settled ModelEvent: input condensed to refs, input itself emptied.
    model_events = [
        m
        for m in conn.sent
        if m["t"] == "event" and m["event"]["event"] == "model"
    ]
    condensed = [
        m for m in model_events if m["event"]["input_refs"] and m["event"]["input"] == []
    ]
    assert condensed, "expected ≥1 model event with non-empty input_refs and input==[]"

    # ≥1 update for one of those uuids with a growing completion (streaming).
    streamed_uuid = None
    for m in condensed:
        uuid = m["event"]["uuid"]
        updates = [
            u
            for u in conn.sent
            if u["t"] == "update" and u["event"]["uuid"] == uuid
        ]
        lens = [len(u["event"]["output"]["completion"]) for u in updates]
        if len(lens) >= 1 and lens == sorted(lens) and lens[-1] > 0:
            streamed_uuid = uuid
            break
    assert streamed_uuid is not None, (
        "expected ≥1 update with monotonically growing output.completion length"
    )

    # --- roundtrip: resolve refs against the accumulated pool ----------------
    # rebuild the pool exactly as a client would from the pool deltas.
    pool: list[dict] = []
    for m in pool_msgs:
        assert m["from"] == len(pool), f"pool delta gap: from={m['from']} have={len(pool)}"
        pool.extend(m["entries"])

    # the last target ModelEvent (system + user prompt at minimum).
    target_span = branch.target_span_id
    last_target = None
    for m in reversed(model_events):
        sid = m["event"]["span_id"]
        # walk span ancestry to the target role span (session.span_parent).
        cur: str | None = sid
        while cur is not None and cur != target_span:
            cur = session.span_parent.get(cur)
        if cur == target_span:
            last_target = m["event"]
            break
    assert last_target is not None, "no target ModelEvent found"

    resolved = _expand_refs(last_target["input_refs"], pool)
    assert len(resolved) >= 2, f"resolved target input too short: {len(resolved)}"
    roles = {msg["role"] for msg in resolved}
    assert "system" in roles, f"no system message in resolved input: {roles}"
    assert "user" in roles, f"no user message in resolved input: {roles}"

    print(
        f"state={n_state} pool={n_pool} event={n_event} update={n_update}; "
        f"pool_size={len(pool)}; last_target_input_len={len(resolved)}"
    )
    print("✓ smoke passed")

    if dump_path is not None:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(json.dumps(conn.sent, indent=2))
        print(f"wrote {len(conn.sent)} messages → {dump_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dump",
        metavar="PATH",
        type=Path,
        default=None,
        help="write captured wire messages as JSON to PATH (e.g. fixtures/smoke.json)",
    )
    args = parser.parse_args()
    anyio.run(_amain, args.dump)


if __name__ == "__main__":
    main()
