"""M1.3 — orchestrator persistence across ``Session.save()``/``load()``.

Two files under ``{store_dir}/{session_id}/``:

- ``orchestrator.eval`` — a one-sample `.eval` whose ``EvalSample.messages`` is
  the orchestrator agent's chat history (``orch.state.messages``) and whose
  ``metadata`` carries ``{model, system_prompt, span_id, run_log_dirs}``. This
  is the *resume* state: ``Session.load`` passes it back through
  ``start_orchestrator(resume_messages=…, span_id=…)``.

- ``orchestrator_events.json`` — the already-dumped ``session.events`` entries
  for ``("orch","orch")``, in wire order. On load these are merged straight
  back into ``session.events``/``_by_role``/``span_role`` so the M1 display
  column (``InfoEvent`` cards) survives without replay. Sidecar rather than
  ``EvalSample.events`` because the dumped ``ModelEvent``s carry ``input_refs``
  into the (unpersisted) session pool — round-tripping them through
  ``.eval``'s event union would need pool expansion; the sidecar path ships
  them frontend-ready as-is (M1.3; pool-backed ``.eval`` is M1.4).

The kernel's ``user_ns`` is *not* persisted (arbitrary Python objects — same
limitation as a Jupyter kernel restart). The resumed agent gets a
``[kernel restarted …]`` note pointing at any ``RunHandle`` log dirs seen in
the pre-save event stream so it can re-read results via ``audits_df``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalLog,
    EvalSample,
    EvalSpec,
    read_eval_log,
    write_eval_log,
)

from workbench.m1.kernel import WB_MIME
from workbench.m1.orchestrator import ORCH_SOURCE

if TYPE_CHECKING:
    from workbench.m1.orchestrator import Orchestrator
    from workbench.session import Session


ORCH_EVAL = "orchestrator.eval"
ORCH_EVENTS = "orchestrator_events.json"

_RUN_KINDS = frozenset({"audit_run", "eval_run"})


def _collect_run_log_dirs(session: "Session") -> list[str]:
    """``log_dir`` from every ``RunHandle`` display card seen so far."""
    out: list[str] = []
    for ev in session.events.values():
        if ev.get("event") != "info" or ev.get("source") != ORCH_SOURCE:
            continue
        wb = (ev.get("data") or {}).get("bundle", {}).get(WB_MIME)
        if wb and wb.get("kind") in _RUN_KINDS:
            d = wb.get("log_dir")
            if d and d not in out:
                out.append(d)
    return out


def save_orchestrator(orch: "Orchestrator", session: "Session", d: Path) -> None:
    """Write ``{d}/orchestrator.eval`` + ``{d}/orchestrator_events.json``."""
    messages = list(orch.state.messages) if orch.state is not None else []
    run_log_dirs = _collect_run_log_dirs(session)

    sample = EvalSample(
        id="orch",
        epoch=1,
        input="",
        target="",
        messages=messages,
        metadata={
            "model": orch.model_name,
            "system_prompt": orch.system_prompt,
            "span_id": orch.span_id,
            "run_log_dirs": run_log_dirs,
        },
    )
    log = EvalLog(
        status="success",
        eval=EvalSpec(
            created=datetime.now(UTC).isoformat(),
            task="workbench/orchestrator",
            dataset=EvalDataset(samples=1),
            model=orch.model_name,
            config=EvalConfig(),
        ),
        samples=[sample],
    )
    write_eval_log(log, d / ORCH_EVAL)

    orch_uuids = session._by_role.get(("orch", "orch"), [])  # noqa: SLF001
    events = [session.events[u] for u in orch_uuids if u in session.events]
    (d / ORCH_EVENTS).write_text(
        json.dumps({"span_id": orch.span_id, "events": events})
    )


def load_orchestrator(session: "Session", d: Path) -> dict[str, Any]:
    """Read both files, merge events into ``session``, return resume kwargs.

    The returned dict is passed as ``**meta`` to
    ``Session.start_orchestrator`` — ``{model, system_prompt, span_id,
    resume_messages, run_log_dirs}``.
    """
    log = read_eval_log(d / ORCH_EVAL)
    assert log.samples, f"{ORCH_EVAL} has no samples"
    sample = log.samples[0]
    md = sample.metadata or {}
    span_id: str = md["span_id"]

    sidecar = json.loads((d / ORCH_EVENTS).read_text())
    role_key = ("orch", "orch")
    session.span_role[span_id] = role_key
    by_role = session._by_role.setdefault(role_key, [])  # noqa: SLF001
    for ev in sidecar["events"]:
        session.events[ev["uuid"]] = ev
        by_role.append(ev["uuid"])
    session.version += 1

    return {
        "model": md["model"],
        "system_prompt": md.get("system_prompt") or "",
        "span_id": span_id,
        "resume_messages": list(sample.messages),
        "run_log_dirs": list(md.get("run_log_dirs") or []),
    }
