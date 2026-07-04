"""M1 smoke: ``wb.scan`` / ``ScanHandle`` in-cell.

Post-M1-HYBRID step 6 this is what remains of ``_smoke_m1_run.py`` — the
in-process ``wb.run_eval``/``wb.run_audits``/``steer``/``stop``/
concurrent-``eval_async`` cells (1–5) all tested the deleted
``RunHandle.launch`` path and are subsumed by ``_smoke_m1_hybrid.py``.
Scout still runs in-process (no ``eval_async``, so no concurrent-eval
hazard), so this exercises ``ScanHandle`` against a mockllm ``.eval``:

- seed a small mockllm log via a plain ``eval_async`` (single call, no
  overlap — the fork guard revert is fine with that).
- ``wb.scan(log_dir, {'g': grep_scanner(...)})`` → card ticks, ``.df``
  populated, no scout progress leaks into stream events.

Run:  ``uv run python -m workbench._smoke_m1_scan``
"""

from __future__ import annotations

import tempfile

import anyio
from inspect_ai import Task, eval_async
from inspect_ai.dataset import Sample
from inspect_ai.solver import generate

from workbench.m1.kernel import STREAM_MIME, WB_MIME, OrchestratorKernel
from workbench.m1.orchestrator import _prewarm  # noqa: PLC2701
from workbench.m1.proposals import Gate
from workbench.m1.wb import Workbench


def make_task(tag: str, n: int = 4) -> Task:
    return Task(
        dataset=[Sample(input=f"{tag}-{i}", id=f"{tag}-{i}") for i in range(n)],
        solver=generate(),
        name=f"t-{tag}",
    )


async def _amain() -> None:
    _prewarm()
    with OrchestratorKernel() as k:
        await _run(k)
    print("\n✓ M1 wb.scan smoke passed")


async def _run(k: OrchestratorKernel) -> None:
    try:
        from inspect_scout import grep_scanner  # noqa: PLC0415
    except ImportError:
        print("- wb.scan: inspect_scout not installed, skipping")
        return

    k.shell.user_ns["wb"] = Workbench(Gate(), session=None)
    k.shell.user_ns["grep_scanner"] = grep_scanner

    # seed a 4-sample mockllm .eval to scan over
    log_dir = tempfile.mkdtemp(prefix="wb-scan-src-")
    await eval_async(make_task("a"), model="mockllm/model", log_dir=log_dir)
    k.shell.user_ns["log_dir"] = log_dir

    r = await k.run_turn(
        "sh = await wb.scan(log_dir, {'g': grep_scanner('a-')})\n"
        "await sh.wait()\n"
        "sh"
    )
    assert r.success, r.text
    sh = k.shell.user_ns["sh"]
    assert sh.finished and sh.error is None, (sh.finished, sh.error)
    assert sh.location and sh.location.startswith(sh.scans_dir)
    assert sh.per_scanner["g"]["scans"] == 4, sh.per_scanner
    assert sh.n_done == sh.total == 4
    assert "4/4" in r.text and "done" in r.text, r.text
    # .df is the ScanResultsDF.scanners mapping — one frame per scanner
    df = sh.df["g"]
    assert len(df) == 4, len(df)
    # card ticked at least once (initial + final)
    scan_evs = [ev for ev in r.outputs if ev.stable and ev.id == sh.id]
    assert len(scan_evs) >= 2 and scan_evs[-1].update
    assert scan_evs[-1].bundle[WB_MIME]["kind"] == "scan"
    # scout progress didn't leak into stream events
    assert not [ev for ev in r.outputs if STREAM_MIME in ev.bundle]
    print(
        f"✓ wb.scan: {sh.n_done}/{sh.total} via grep_scanner, "
        f"df['g'] {len(df)} rows, {len(scan_evs)} ticks"
    )


if __name__ == "__main__":
    anyio.run(_amain)
