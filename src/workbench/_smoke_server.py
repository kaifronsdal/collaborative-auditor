"""Server-level smoke test — real uvicorn, real WS clients.

Covers the multi-connection / context-boundary scenarios the in-process smoke
tests can't see (they create `Session` and run `Branch` in one coroutine, so
contextvar inheritance is trivially correct):

  S1. **Cross-connection start.** Conn-A connects (creates the Session) and
      disconnects. Conn-B connects and sends `start`. Asserts conn-B receives
      ≥1 `{t:"event"}` — i.e. `Branch.run()`'s task has the session's
      transcript even though the Session was constructed in a different task.

  S2. **Reconnect mid-generate.** Conn-B disconnects while the branch is
      running; conn-C connects. Asserts conn-C's `state` snapshot has the
      running branch and ≥1 event, and that further events keep arriving.

  S3. **Malformed message.** Conn-C sends `{t:"start"}` with no `seed`.
      Asserts the connection stays open and a `{t:"error"}` arrives — the
      `_dispatch` exception handler caught it instead of crashing the WS.

Run:  uv run python -m workbench._smoke_server
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import websockets

from workbench._smoke_fixtures import _backend, _free_port

MODEL = "anthropic/claude-haiku-4-5-20251001"


async def _recv_until(ws, pred, *, timeout: float = 60.0) -> dict[str, Any]:
    """Receive until `pred(msg)` is true; return that message."""
    with anyio.fail_after(timeout):
        while True:
            msg = json.loads(await ws.recv())
            if pred(msg):
                return msg


async def _amain() -> None:
    port = _free_port()
    async with _backend(port):
        url = f"ws://127.0.0.1:{port}/ws/smoke-server"

        # ── S1: cross-connection start ───────────────────────────────────────
        async with websockets.connect(url) as conn_a:
            state = json.loads(await conn_a.recv())
            assert state["t"] == "state", f"S1: first msg was {state['t']!r}"
        # conn_a closed → session persists in `sessions` dict.

        async with websockets.connect(url) as conn_b:
            json.loads(await conn_b.recv())  # initial state
            await conn_b.send(
                json.dumps(
                    {
                        "t": "start",
                        "seed": "say hello to the target",
                        "auditor_model": MODEL,
                        "target_model": MODEL,
                        "max_turns": 2,
                    }
                )
            )
            # A3-batch: `{t:"event"}` now arrives inside a `{t:"batch"}` frame.
            batch = await _recv_until(conn_b, lambda m: m["t"] == "batch")
            ev = next(op for op in batch["ops"] if op["t"] == "event")
            assert ev["event"]["uuid"], "S1: event missing uuid"
            print(
                f"S1 ✓ cross-conn start: first event "
                f"{ev['event']['event']!r} v={batch['v']}"
            )

            # leave the branch running for S2
            # ── S2: reconnect mid-generate ───────────────────────────────────
        # conn_b closed; branch still running detached.

        async with websockets.connect(url) as conn_c:
            state = json.loads(await conn_c.recv())
            assert state["t"] == "state"
            assert state["current"] is not None, "S2: no current branch on reconnect"
            assert len(state["events"]) >= 1, (
                f"S2: snapshot has {len(state['events'])} events"
            )
            assert len(state["branches"]) == 1, (
                f"S2: expected 1 branch, got {len(state['branches'])}"
            )
            # further events keep flowing (or the branch ends — either proves
            # the wire is live).
            nxt = await _recv_until(
                conn_c, lambda m: m["t"] in ("batch", "status")
            )
            print(
                f"S2 ✓ reconnect mid-generate: snapshot had "
                f"{len(state['events'])} events; next wire msg t={nxt['t']!r}"
            )

            # ── S3: malformed message ────────────────────────────────────────
            await conn_c.send(json.dumps({"t": "start"}))  # missing seed/models
            err = await _recv_until(conn_c, lambda m: m["t"] == "error")
            assert "seed" in err["message"] or "KeyError" in err["message"], (
                f"S3: unexpected error message: {err['message']!r}"
            )
            # connection still open: send a valid command and get a response.
            await conn_c.send(json.dumps({"t": "pause"}))
            await _recv_until(conn_c, lambda m: m["t"] == "status", timeout=10)
            print("S3 ✓ malformed msg → {t:'error'}; connection survived")

    print("✓ all server smoke scenarios passed")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
