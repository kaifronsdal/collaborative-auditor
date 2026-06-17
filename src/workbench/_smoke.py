"""Non-server smoke test for the audit-workbench M0 skeleton.

Exercises the real streaming path (real models, `streaming=True`) end to end:
creates a `Session`, a `Branch`, registers a fake connection that captures every
broadcast wire message, runs the branch under `play()`, then asserts the §C
protocol was emitted and the final view has both timelines populated.

Run:  uv run python -m workbench._smoke
"""

from __future__ import annotations

import anyio

from workbench.run import Branch
from workbench.session import Session

MODEL = "anthropic/claude-haiku-4-5-20251001"
SEED = "test seed"


class FakeConn:
    """A WebSocket-shaped sink that records every wire message it receives."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


async def _amain() -> None:
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
    n_stream = kinds.count("stream")
    n_patch = kinds.count("patch")
    print(f"wire messages: state={n_state} stream={n_stream} patch={n_patch} (total {len(kinds)})")

    assert n_state >= 1, "expected ≥1 state message"
    assert n_stream >= 1, "expected ≥1 stream message (real streaming path did not fire)"
    assert n_patch >= 1, "expected ≥1 patch message"

    view = session.view()
    bv = view.branches["b0"]
    print(
        f"final BranchView: status={bv.status} "
        f"auditor msgs={len(bv.auditor)} target msgs={len(bv.target)}"
    )
    assert bv.status == "ended", f"branch did not end: {bv.status}"
    assert bv.auditor, "auditor messages empty"
    assert bv.target, "target messages empty"

    # patch ops target the right paths
    sample_patch = next(m for m in conn.sent if m["t"] == "patch")
    assert sample_patch["ops"][0]["path"].startswith("/branches/b0/"), sample_patch

    print("✓ smoke passed")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
