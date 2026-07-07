"""M1.3 — orchestrator persistence across ``Session.save()``/``load()``.

One file under ``{store_dir}/{session_id}/``:

- ``orchestrator.eval`` — a one-sample `.eval` whose ``EvalSample.messages`` is
  the orchestrator agent's chat history (``orch.state.messages``), whose
  ``EvalSample.events`` is every event under the ``("orch","orch")`` span
  (display ``InfoEvent``s, ``ModelEvent``s, ``ToolEvent``s, span markers), and
  whose ``metadata`` carries ``{model, system_prompt, span_id, run_log_dirs}``.

Why pure ``.eval`` and not a JSON sidecar of ``session.events``: the live
``session.events`` entries are *condensed* — ``ModelEvent.input_refs`` index
into ``session.pool``. On ``Session.load`` the pool is rebuilt by M0 branch
replay with different indices, so persisted refs would dangle or resolve to
garbage. Instead, ``save_orchestrator`` expands ``input_refs`` against the
*current* pool back to full ``input`` and lets ``write_eval_log`` do its own
per-sample pooling; ``read_eval_log`` returns fully-expanded events, and
``load_orchestrator`` re-interns them into the *new* session's pool via
``session._condense`` — so the frontend's ``expandEvents`` sees consistent
refs regardless of load order.

The kernel's ``user_ns`` is *not* persisted (arbitrary Python objects — same
limitation as a Jupyter kernel restart). The resumed agent gets a
``[kernel restarted …]`` note pointing at any ``eval_run`` log dirs seen in
the pre-save event stream so it can re-read results via ``wb.attach``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from inspect_ai.event import Event, SpanBeginEvent
from inspect_ai.event._pool import _expand_refs
from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalLog,
    EvalSample,
    EvalSpec,
    read_eval_log,
    write_eval_log,
)
from inspect_ai.model import ChatMessage
from pydantic import TypeAdapter

from workbench.m1.orchestrator import ORCH_SOURCE, _restored_file_hashes
from workbench.m1.wire import WB_MIME

if TYPE_CHECKING:
    from workbench.m1.orchestrator import Orchestrator
    from workbench.session import Session


ORCH_EVAL = "orchestrator.eval"

_EVENTS = TypeAdapter(list[Event])
_MESSAGES = TypeAdapter(list[ChatMessage])


def _collect_run_log_dirs(session: Session) -> list[str]:
    """``log_dir`` from every ``eval_run`` display card seen so far."""
    out: list[str] = []
    for ev in session.events.values():
        if ev.get("event") != "info" or ev.get("source") != ORCH_SOURCE:
            continue
        wb = (ev.get("data") or {}).get("bundle", {}).get(WB_MIME)
        if wb and wb.get("kind") == "eval_run":
            log_dir = wb.get("log_dir")
            if log_dir and log_dir not in out:
                out.append(log_dir)
    return out


def save_orchestrator(orch: Orchestrator, session: Session, d: Path) -> None:
    """Write ``{d}/orchestrator.eval`` — messages + expanded events + metadata."""
    messages = orch.messages_for_save()
    run_log_dirs = _collect_run_log_dirs(session)

    # Expand condensed ModelEvent.input_refs against the CURRENT pool so the
    # events are self-contained; write_eval_log then re-pools them per-sample.
    dumped: list[dict[str, Any]] = []
    rewound_uuids: list[str] = []
    for u in session.by_role.get(("orch", "orch"), []):
        if u not in session.events:
            continue
        ev = dict(session.events[u])
        # ``rewound`` is a wire-only top-level key on the *dumped* dict
        # (``Session.mark_rewound``); it is not an ``Event`` field, so the
        # ``_EVENTS.validate_python`` round-trip below drops it. Carry the
        # uuids in ``metadata`` and re-mark on load.
        if ev.get("rewound"):
            rewound_uuids.append(u)
        if ev.get("event") == "model" and ev.get("input_refs"):
            ev["input"] = [
                m.model_dump(mode="json")
                for m in _expand_refs(ev["input_refs"], session.pool)
            ]
            ev["input_refs"] = None
        dumped.append(ev)
    events = _EVENTS.validate_python(dumped)

    sample = EvalSample(
        id="orch",
        epoch=1,
        input="",
        target="",
        messages=messages,
        events=events,
        metadata={
            "model": orch.model_name,
            "system_prompt": orch.system_prompt,
            "model_args": orch.model_args,
            "generate_config": orch.generate_config,
            "audit_defaults": orch.audit_defaults,
            "span_id": orch.span_id,
            "run_log_dirs": run_log_dirs,
            "rewound_uuids": rewound_uuids,
            # P2-persist: user messages queued for the next generate.
            "queued": [m.model_dump(mode="json") for m in orch.queued],
            # P3: ``write_file`` content hashes (prompt/seed versioning).
            "file_hashes": dict(orch.file_hashes),
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


def load_orchestrator(session: Session, d: Path) -> dict[str, Any]:
    """Read ``orchestrator.eval``, merge events into ``session``, return resume kwargs.

    ``read_eval_log`` returns events with fully-expanded ``ModelEvent.input``
    (inspect resolves ``events_data`` on read); ``session._condense`` then
    re-interns each into *this* session's pool so ``input_refs`` point at
    valid indices post-load. The returned dict is passed as ``**meta`` to
    ``Session.start_orchestrator``.
    """
    log = read_eval_log(d / ORCH_EVAL)
    assert log.samples, f"{ORCH_EVAL} has no samples"
    sample = log.samples[0]
    md = sample.metadata or {}
    span_id: str = md["span_id"]

    role_key = ("orch", "orch")
    session.span_role[span_id] = role_key
    session.span_parent[span_id] = None
    by_role = session.by_role.setdefault(role_key, [])
    rewound = set(md.get("rewound_uuids") or [])
    for ev in sample.events or []:
        assert ev.uuid is not None
        if isinstance(ev, SpanBeginEvent):
            session.span_parent[ev.id] = ev.parent_id
        d_ev = session._condense(ev)  # noqa: SLF001
        if ev.uuid in rewound:
            d_ev["rewound"] = True
        session.events[ev.uuid] = d_ev
        by_role.append(ev.uuid)
    session.version += 1

    # P3: ``file_hashes`` isn't a ``start_orchestrator`` kwarg — hand it to
    # ``Orchestrator.__init__`` via the module-level map (popped by span_id).
    _restored_file_hashes[span_id] = dict(md.get("file_hashes") or {})

    return {
        "model": md["model"],
        "system_prompt": md.get("system_prompt") or "",
        "model_args": md.get("model_args") or None,
        "generate_config": md.get("generate_config") or None,
        "audit_defaults": md.get("audit_defaults") or None,
        "span_id": span_id,
        "resume_messages": list(sample.messages),
        "run_log_dirs": list(md.get("run_log_dirs") or []),
        # Popped by ``persist.load_session`` and applied post-construction.
        "queued": _MESSAGES.validate_python(md.get("queued") or []),
    }
