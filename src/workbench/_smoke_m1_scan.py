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

import shutil
import tempfile
from pathlib import Path
from typing import Any

import anyio
from inspect_ai import Task, eval_async
from inspect_ai.dataset import Sample
from inspect_ai.solver import generate

from workbench.m1.kernel import OrchestratorKernel
from workbench.m1.orchestrator import _prewarm
from workbench.m1.proposals import Gate
from workbench.m1.wb import Workbench
from workbench.m1.wire import STREAM_MIME, WB_MIME


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
    with OrchestratorKernel() as k:
        await _run_diff(k)
    await _run_branch_scan()
    await _run_live_scanner_bad_name()
    print("\n✓ M1 wb.scan smoke passed")


async def _run(k: OrchestratorKernel) -> None:
    try:
        from inspect_scout import grep_scanner
    except ImportError:
        print("- wb.scan: inspect_scout not installed, skipping")
        return

    k.shell.user_ns["wb"] = Workbench(Gate())
    k.shell.user_ns["grep_scanner"] = grep_scanner

    # seed a 4-sample mockllm .eval to scan over
    log_dir = tempfile.mkdtemp(prefix="wb-scan-src-")
    await eval_async(make_task("a"), model="mockllm/model", log_dir=log_dir)
    k.shell.user_ns["log_dir"] = log_dir

    # -- P1.8(a): scanner library + named groups --------------------------
    # A temp scanner_dir with one @scanner factory + a groups.yaml naming
    # it; patch Settings.scanner_dir; assert resolve() and wb.scan() both
    # take the group name.
    import textwrap
    from dataclasses import replace

    from workbench import config
    from workbench.m1 import scanners as scanmod

    lib_dir = tempfile.mkdtemp(prefix="wb-scanlib-")
    (Path(lib_dir) / "flags.py").write_text(
        textwrap.dedent("""
            from inspect_scout import Result, Transcript, scanner

            @scanner(messages="all")
            def has_a():
                async def scan(t: Transcript) -> Result:
                    return Result(value="a-" in (t.messages[0].text or ""))
                return scan
        """)
    )
    (Path(lib_dir) / "groups.yaml").write_text("my-group:\n  - has_a\n")
    prev_settings = config.settings
    config.settings = replace(config.settings, scanner_dir=lib_dir)
    scanmod._lib_cache = None
    try:
        lib = scanmod.load_library()
        assert "has_a" in lib, sorted(lib)
        groups = scanmod.load_groups()
        assert groups == {"my-group": ["has_a"]}, groups
        resolved = scanmod.resolve("my-group")
        assert set(resolved) == {"has_a"}, resolved
        # miss → helpful message listing available names
        try:
            scanmod.resolve("nope")
        except ValueError as e:
            assert "my-group" in str(e) and "has_a" in str(e), str(e)
        else:
            raise AssertionError("resolve('nope') should have raised")

        r = await k.run_turn(
            "sh2 = await wb.scan(log_dir, 'my-group')\n"
            "await sh2.wait()\n"
            "sh2"
        )
        assert r.success, r.text
        sh2 = k.shell.user_ns["sh2"]
        assert sh2.finished and sh2.error is None, (sh2.finished, sh2.error)
        assert sh2.per_scanner["has_a"]["scans"] == 4, sh2.per_scanner
        assert "has_a" in r.text, r.text
        print(
            f"✓ P1.8(a): resolve('my-group') → {sorted(resolved)}; "
            f"wb.scan(logs, 'my-group') → {sh2.n_done} scanned"
        )
    finally:
        config.settings = prev_settings
        scanmod._lib_cache = None
        shutil.rmtree(lib_dir, ignore_errors=True)

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
    shutil.rmtree(log_dir, ignore_errors=True)


async def _run_diff(k: OrchestratorKernel) -> None:
    """P3 run diff: two ``@demo`` runs → ``wb.diff(a, b)`` in-cell.

    Seeds two mockllm ``.eval`` dirs (3 vs 4 samples so ``n_only_b == 1``),
    calls ``wb.diff`` on the paths, and asserts ``.df`` shape / ``.summary``
    counts / a ``run_diff`` payload landed in the cell's outputs.
    """
    from workbench.m1._audit_task import demo

    k.shell.user_ns["wb"] = Workbench(Gate())
    dir_a = tempfile.mkdtemp(prefix="wb-diff-a-")
    dir_b = tempfile.mkdtemp(prefix="wb-diff-b-")
    try:
        await eval_async(demo(n=3), model="mockllm/model", log_dir=dir_a)
        await eval_async(demo(n=4), model="mockllm/model", log_dir=dir_b)
        k.shell.user_ns["dir_a"] = dir_a
        k.shell.user_ns["dir_b"] = dir_b

        r = await k.run_turn("d = wb.diff(dir_a, dir_b)\ndisplay(d)\nd")
        assert r.success, r.text
        d = k.shell.user_ns["d"]
        # 3 samples in both, one only-in-b, one score_* delta column.
        assert d.df.shape[0] == 4, d.df.shape
        assert set(d.on) == {"id"}
        assert d.scorers == ["score__always_one"], d.scorers
        assert "delta__always_one" in d.df.columns, list(d.df.columns)
        assert "score__always_one_a" in d.df.columns
        s = d.summary
        assert s["n"] == 3 and s["n_only_a"] == 0 and s["n_only_b"] == 1, s
        # constant score=1 on both sides → no flips, mean delta 0.
        assert s["n_flipped"] == 0 and len(d.flipped) == 0, s
        assert s["mean_delta"]["_always_one"] == 0.0, s["mean_delta"]
        # DiffPayload emitted via WB_MIME
        payloads = [
            ev.bundle[WB_MIME]
            for ev in r.outputs
            if WB_MIME in ev.bundle and ev.bundle[WB_MIME]["kind"] == "run_diff"
        ]
        assert payloads, [ev.bundle.keys() for ev in r.outputs]
        p = payloads[0]
        assert p["n"] == 3 and p["n_flipped"] == 0, p
        assert p["summary"]["n_only_b"] == 1
        assert "<table" in p["df_head"] and "delta__always_one" in p["df_head"]
        assert "3 joined" in r.text and "0 flipped" in r.text, r.text
        print(
            f"✓ P3 wb.diff: {d.df.shape[0]}×{d.df.shape[1]} joined, "
            f"summary={s}, {len(payloads)} run_diff payload(s)"
        )
    finally:
        shutil.rmtree(dir_a, ignore_errors=True)
        shutil.rmtree(dir_b, ignore_errors=True)


async def _run_branch_scan() -> None:
    """P1.8(b): ``_h_scan_branch`` on a mock branch.

    Builds a ``Branch`` (no ``run()`` — just channel + target span), connects
    a hand-built target ``[sys, u1, r1, u2, r2]`` conversation to
    ``channel.state``, fires ``scan_branch`` with a temp ``@scanner``, and
    asserts the running → done ``InfoEvent`` pair landed on the branch's
    target span with a ``ScanPayload`` shape ``ProgressCard`` will render.
    """
    import textwrap
    from dataclasses import replace

    from inspect_ai.model import (
        ChatMessageAssistant,
        ChatMessageSystem,
        ChatMessageUser,
    )

    from workbench import config
    from workbench.m1 import scanners as scanmod
    from workbench.run import Branch
    from workbench.server import _h_scan_branch
    from workbench.session import Session

    lib_dir = tempfile.mkdtemp(prefix="wb-scanlib-b-")
    (Path(lib_dir) / "flags.py").write_text(
        textwrap.dedent("""
            from inspect_scout import Result, Transcript, scanner

            @scanner(messages="all")
            def has_u1():
                async def scan(t: Transcript) -> Result:
                    hit = any("u1" in (m.text or "") for m in t.messages)
                    return Result(value=hit, explanation=f"{len(t.messages)} msgs")
                return scan
        """)
    )
    prev_settings = config.settings
    config.settings = replace(config.settings, scanner_dir=lib_dir)
    scanmod._lib_cache = None

    session = Session()
    await session.start()
    try:
        # A `Branch` without `run()` — `__init__` allocates the channel /
        # target span / registers `span_role`, which is all `_h_scan_branch`
        # needs. Populate the target conversation directly via
        # `channel.state.connect` (what `TargetContext.__init__` would do).
        base = Branch(
            session,
            "scan-base",
            seed="s",
            auditor_model="mockllm/model",
            target_model="mockllm/model",
        )
        session.branches[base.branch_id] = base
        msgs = [
            ChatMessageSystem(content="sys"),
            ChatMessageUser(content="u1"),
            ChatMessageAssistant(content="r1"),
            ChatMessageUser(content="u2"),
            ChatMessageAssistant(content="r2"),
        ]
        base.channel.state.connect(msgs, [])

        before = set(session.events)
        await _h_scan_branch(
            session,
            {"branch_id": base.branch_id, "scanner": "has_u1", "scope": "transcript"},
        )
        # running InfoEvent landed synchronously; find it
        scan_ids = [
            e["uuid"]
            for e in session.events.values()
            if e["event"] == "info"
            and e.get("source") == "branch_scan"
            and e["uuid"] not in before
        ]
        assert len(scan_ids) == 1, scan_ids
        scan_id = scan_ids[0]
        payload = session.events[scan_id]["data"]["bundle"][WB_MIME]
        assert payload["kind"] == "scan" and not payload["finished"], payload
        # routes to the branch's target column
        assert scan_id in session.by_role.get((base.branch_id, "target"), []), (
            "scan InfoEvent not routed to (branch, 'target')"
        )

        # the handler spawned a fire-and-forget task; poll until it patches
        # the same event (uuid == scan_id) with finished=True.
        for _ in range(50):
            payload = session.events[scan_id]["data"]["bundle"][WB_MIME]
            if payload["finished"]:
                break
            await anyio.sleep(0.02)
        assert payload["finished"] and payload["error"] is None, payload
        assert payload["per_scanner"]["has_u1"] == {
            "scans": 1,
            "results": 1,
            "errors": 0,
        }, payload["per_scanner"]
        assert "True" in payload["df_head"]["has_u1"], payload["df_head"]
        assert f"{len(msgs)} msgs" in payload["df_head"]["has_u1"]
        print(
            f"✓ P1.8(b): scan_branch → InfoEvent({scan_id[:8]}) on target span, "
            f"has_u1=True over {len(msgs)} messages"
        )

        # scope={"turn": 0} slices to the first message only → no "u1"
        await _h_scan_branch(
            session,
            {"branch_id": base.branch_id, "scanner": "has_u1", "scope": {"turn": 0}},
        )
        done: list[dict[str, Any]] = []
        for _ in range(50):
            done = [
                e["data"]["bundle"][WB_MIME]
                for e in session.events.values()
                if e["event"] == "info"
                and e.get("source") == "branch_scan"
                and e["uuid"] != scan_id
            ]
            if done and done[0]["finished"]:
                break
            await anyio.sleep(0.02)
        assert done and done[0]["finished"], "turn-scope scan never finished"
        assert "1 msgs" in done[0]["df_head"]["has_u1"], done[0]["df_head"]
        print("✓ P1.8(b): scope={'turn':0} sliced to 1 message")
    finally:
        await session.close()
        config.settings = prev_settings
        scanmod._lib_cache = None
        shutil.rmtree(lib_dir, ignore_errors=True)


async def _run_live_scanner_bad_name() -> None:
    """P1.8(c) resolve failure: a bad ``live_scanners`` name in ``post_turn``
    surfaces as a ``{t:"notify"}`` frame (not just a server-side log line)."""
    from workbench._smoke_util import FakeConn
    from workbench.run import Branch
    from workbench.session import Session

    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)
    try:
        b = Branch(
            session,
            "scan-bad",
            seed="s",
            auditor_model="mockllm/model",
            target_model="mockllm/model",
            live_scanners=["not-a-real-scanner"],
        )
        session.branches[b.branch_id] = b
        # ``post_turn`` short-circuits before touching ``audit_tape.log`` when
        # resolve() raises, so an unrun branch is enough.
        b.post_turn()
        with anyio.fail_after(2.0):
            while not any(m["t"] == "notify" for m in conn.sent):
                await anyio.sleep(0.01)
        notify = next(m for m in conn.sent if m["t"] == "notify")
        assert "not-a-real-scanner" in notify["text"], notify
        assert "resolve" in notify["text"], notify
        print(f"✓ P1.8(c): bad live_scanners → {{t:'notify'}}: {notify['text'][:60]!r}…")
    finally:
        await session.close()


if __name__ == "__main__":
    anyio.run(_amain)
