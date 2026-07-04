"""M1-HYBRID real-model end-to-end: opus-4-8 drives the full tool surface.

Unlike ``_smoke_m1_hybrid.py`` (mockllm, scripted) this hands a real model the
M1-HYBRID system prompt and a single user instruction, then asserts it walks
the intended path unprompted:

    write_file(seeds.json)
      → review_seeds(...)               ← auto-resolved {"surviving":["s0","s1","s2"]}
      → bash("inspect eval …@audit …")  ← subprocess; {"wb":…} → eval_run card
      → python("h = wb.attach(...)")    ← AttachedRun; h.n_done > 0

plus, opportunistically, ``AttachedRun.interrupt_sample(id)`` on one still-
running sample over the subprocess's ACP socket.

This hits the real Anthropic API and spawns a real ``inspect eval`` subprocess
against ``--target`` — run it on a worker VM, not in CI.

Run:  ``uv run python -m workbench._e2e_m1_real --target anthropic/claude-haiku-4-5``
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

import anyio

from workbench.m1.attach import AttachedRun
from workbench.m1.kernel import WB_MIME
from workbench.m1.orchestrator import ORCH_SOURCE
from workbench.session import Session

TIMEOUT_S = 600
POLL_S = 2.0


async def _amain(*, model: str, target: str, keep: bool) -> None:  # noqa: PLR0912, PLR0915
    t0 = time.monotonic()
    session = Session()
    await session.start()

    await session.start_orchestrator(model=model, max_turns=12)
    orch = session.orchestrator
    assert orch is not None
    session_dir: Path = orch.session_dir

    # Tap ``Gate.on_change`` so we can assert ``status == "waiting"`` the
    # moment ``review_seeds`` opens (the frontend hangs off
    # ``session.broadcast_status()`` which this hook drives).
    on_change_statuses: list[str] = []
    orig_on_change = orch.gate.on_change

    def _tap() -> None:
        on_change_statuses.append(orch.status)
        if orig_on_change is not None:
            orig_on_change()

    orch.gate.on_change = _tap

    # The prompt (m1/prompt.py) says: seeds → review_seeds → bash(inspect
    # eval) → python(wb.attach). "Get my sign-off" makes the review_seeds
    # call unconditional (it's otherwise gated on the agent's own >$5/>20
    # heuristic). ``--log-buffer 1`` is in the prompt's example already.
    orch.send(
        f"Run 3 short audits (max_turns=8) against {target} using seeds "
        f"about system-prompt extraction. Use {target} for the auditor and "
        f"judge roles too. Get my sign-off on the seed list before "
        f"launching, then attach to the results."
    )
    orch.play()

    # ---- poll until parked / ended / timeout -------------------------------
    # Auto-resolve gates so the e2e doesn't hang on a human card:
    # ``run_proposal`` → keep the first 3 seeds; anything else → {}.
    await asyncio.sleep(2.0)
    deadline = t0 + TIMEOUT_S
    last_turn_count = -1
    resolved_run_proposal: str | None = None
    interrupt_ok: bool | None = None
    while time.monotonic() < deadline:
        for gid in list(orch.gate.pending):
            wb = _find_wb_payload(session, gid)
            if wb and wb.get("kind") == "run_proposal":
                seeds = wb.get("seeds") or []
                verdict = {"surviving": [s["id"] for s in seeds[:3]]}
                resolved_run_proposal = gid
                print(
                    f"  auto-resolving review_seeds {gid[:8]} → surviving="
                    f"{verdict['surviving']} (of {len(seeds)})"
                )
            else:
                verdict = {}
                print(f"  auto-resolving pending gate {gid[:8]} → {{}}")
            orch.gate.resolve(gid, verdict)
        # Opportunistic: once an ``AttachedRun`` handle surfaces in the
        # kernel namespace with in-flight samples, interrupt one over ACP.
        if interrupt_ok is None:
            for h in _handles(orch):
                if h.running_ids:
                    target_id = h.running_ids[0]
                    interrupt_ok = await h.interrupt_sample(target_id)
                    print(
                        f"  interrupt_sample({target_id!r}) → {interrupt_ok} "
                        f"(acp={h._acp!r})"  # noqa: SLF001
                    )
                    break
        turns = _n_assistant_turns(session)
        if turns != last_turn_count:
            print(
                f"  [{time.monotonic() - t0:6.1f}s] status={orch.status} "
                f"turns={turns} events={len(session.events)}"
            )
            last_turn_count = turns
        if orch.status == "ended" or (orch.status == "paused" and turns > 0):
            break
        await asyncio.sleep(POLL_S)
    else:
        print(f"WARN: timed out after {TIMEOUT_S}s (status={orch.status})")

    elapsed = time.monotonic() - t0

    # ---- collect ------------------------------------------------------------
    tool_evs = _tool_events(session)
    review_evs = [e for e in tool_evs if e.get("function") == "review_seeds"]
    bash_evs = [e for e in tool_evs if e.get("function") == "bash"]
    python_evs = [e for e in tool_evs if e.get("function") == "python"]
    eval_bash = [
        e for e in bash_evs if "inspect eval" in str(e.get("arguments", {}).get("cmd", ""))
    ]
    attach_py = [
        e for e in python_evs if "wb.attach" in str(e.get("arguments", {}).get("code", ""))
    ]
    run_cards = [
        d for d in _wb_events(session)
        if d["bundle"][WB_MIME].get("kind") == "eval_run"
    ]
    tool_errors = [
        e for e in tool_evs if e.get("error") or "Traceback" in _tool_text(e)
    ]
    eval_files = sorted((session_dir / "runs").rglob("*.eval"))
    handles = _handles(orch)

    # ---- assert -------------------------------------------------------------
    try:
        assert resolved_run_proposal is not None, (
            f"no review_seeds gate opened ({len(review_evs)} review_seeds tool "
            f"call(s), on_change fired {len(on_change_statuses)}×)"
        )
        assert "waiting" in on_change_statuses, (
            f"Gate.on_change never observed status='waiting': {on_change_statuses}"
        )
        print(
            f"✓ review_seeds gated → status='waiting' broadcast; "
            f"resolved {resolved_run_proposal[:8]} with 3 seeds"
        )

        assert eval_bash, (
            f"no bash tool call with 'inspect eval' in cmd "
            f"({len(bash_evs)} bash call(s) total)"
        )
        for e in eval_bash:
            r = _tool_text(e)
            assert '{"wb":' not in r, "wb-protocol lines leaked into model text"
        print(f"✓ bash('inspect eval …') called ({len(eval_bash)}×)")

        assert run_cards, (
            f"no WB_MIME kind='eval_run' card in session.events "
            f"({len(_wb_events(session))} WB_MIME events total)"
        )
        print(
            f"✓ {len(run_cards)} eval_run card(s) at turn(s) "
            f"{sorted({d['turn'] for d in run_cards})}"
        )

        assert eval_files, (
            f"no .eval written under {session_dir / 'runs'} — subprocess never "
            f"ran or crashed. bash results:\n"
            + "\n".join(_tool_text(e)[-400:] for e in eval_bash)
        )
        print(f"✓ subprocess wrote .eval: {[p.name for p in eval_files]}")

        assert attach_py, (
            f"no python tool call with 'wb.attach' in code "
            f"({len(python_evs)} python call(s) total)"
        )
        assert handles, "no AttachedRun instance found in kernel.shell.user_ns"
        n_done = max(h.n_done for h in handles)
        assert n_done > 0, (
            f"AttachedRun.n_done == 0 for all handles "
            f"({[(h.log_dir, h.n_done, h.total, h._status) for h in handles]})"  # noqa: SLF001
        )
        print(f"✓ wb.attach → AttachedRun, n_done={n_done}")

        if interrupt_ok is True:
            print("✓ AttachedRun.interrupt_sample → True over ACP")
        elif interrupt_ok is False:
            print("WARN: interrupt_sample returned False (no ACP socket / sample gone)")
        else:
            print("WARN: no running sample observed in time to try interrupt_sample")

        # ---- summary --------------------------------------------------------
        print("\n" + "=" * 72)
        print("E2E SUMMARY")
        print("=" * 72)
        print(f"  elapsed            : {elapsed:.1f}s")
        print(f"  final status       : {orch.status}")
        print(f"  orchestrator turns : {_n_assistant_turns(session)}")
        print(f"  tool calls         : {len(tool_evs)} "
              f"(bash={len(bash_evs)} python={len(python_evs)} review={len(review_evs)})")
        print(f"  cell tracebacks    : {len(tool_errors)}")
        for e in tool_errors:
            print(f"    - {e.get('function')}: {_tool_text(e).splitlines()[-1][:100]}")
        print(f"  eval_run cards     : {len(run_cards)}")
        print(f"  session_dir        : {session_dir}")
        print(f"  .eval files        : {[str(p) for p in eval_files]}")
        print(f"  AttachedRun n_done : {[h.n_done for h in handles]}")
        print(f"  review_seeds gate  : {resolved_run_proposal}")
        print(f"  on_change statuses : {on_change_statuses}")
        print(f"  interrupt_sample   : {interrupt_ok}")
        print("=" * 72)

    finally:
        # ---- turn trace ----------------------------------------------------
        # Always emitted so assertion failures above still show what the
        # orchestrator actually did.
        print("\nTURN TRACE")
        print("=" * 72)
        ordered = sorted(session.events.values(), key=lambda e: e.get("timestamp", ""))
        n = 0
        for e in ordered:
            if (
                e["event"] == "model"
                and not e.get("pending")
                and session._resolve(e.get("span_id")) == ("orch", "orch")  # noqa: SLF001
            ):
                out = e.get("output") or {}
                choices = out.get("choices") or []
                content = choices[0]["message"]["content"] if choices else []
                prose = "".join(
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ).strip()
                if prose:
                    print(f"\n--- assistant prose ---\n{prose}\n")
            if e["event"] == "tool" and not e.get("pending"):
                n += 1
                fn = e.get("function")
                args = e.get("arguments") or {}
                body = args.get("code") or args.get("cmd") or args
                print(f"\n=== TOOL {n}: {fn} ===")
                print(body if isinstance(body, str) else str(body)[:400])
                print(f"--- result ({fn}) ---")
                print(_tool_text(e) or "<empty>")
        print("=" * 72)

        await session.close()
        if not keep:
            shutil.rmtree(session_dir, ignore_errors=True)


# -- helpers -----------------------------------------------------------------


def _n_assistant_turns(session: Session) -> int:
    """Completed orchestrator ``ModelEvent``s (retry attempts excluded)."""
    return sum(
        1
        for e in session.events.values()
        if e["event"] == "model"
        and not e.get("pending")
        and not e.get("error")
        and session._resolve(e.get("span_id")) == ("orch", "orch")  # noqa: SLF001
    )


def _tool_events(session: Session) -> list[dict[str, Any]]:
    return sorted(
        (
            e
            for e in session.events.values()
            if e["event"] == "tool" and not e.get("pending")
        ),
        key=lambda e: e.get("timestamp", ""),
    )


def _wb_events(session: Session) -> list[dict[str, Any]]:
    """All orchestrator ``InfoEvent.data`` payloads carrying a WB_MIME bundle."""
    out = []
    for e in session.events.values():
        if e["event"] == "info" and e.get("source") == ORCH_SOURCE:
            b = e["data"].get("bundle") or {}
            if WB_MIME in b:
                out.append(e["data"])
    return out


def _find_wb_payload(session: Session, display_id: str) -> dict[str, Any] | None:
    """Latest ``WB_MIME`` payload for ``display_id`` across orch InfoEvents."""
    for e in reversed(list(session.events.values())):
        if e["event"] == "info" and e.get("source") == ORCH_SOURCE:
            wb = e["data"].get("bundle", {}).get(WB_MIME)
            if wb and wb.get("id") == display_id:
                return wb
    return None


def _handles(orch: Any) -> list[AttachedRun]:
    """Every ``AttachedRun`` bound in the kernel's ``user_ns``."""
    seen: set[int] = set()
    out: list[AttachedRun] = []
    for v in orch.kernel.shell.user_ns.values():
        if isinstance(v, AttachedRun) and id(v) not in seen:
            seen.add(id(v))
            out.append(v)
    return out


def _tool_text(e: dict[str, Any]) -> str:
    r = e.get("result")
    if isinstance(r, list):
        return "".join(c.get("text", "") for c in r if isinstance(c, dict))
    return str(r or "")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--model",
        default="anthropic/claude-opus-4-8",
        help="orchestrator model (drives the tool surface)",
    )
    p.add_argument(
        "--target",
        default="anthropic/claude-haiku-4-5",
        help="target/auditor/judge model for the subprocess audits",
    )
    p.add_argument(
        "--keep",
        action="store_true",
        help="don't rm the session_dir on exit",
    )
    args = p.parse_args()
    anyio.run(lambda: _amain(model=args.model, target=args.target, keep=args.keep))
