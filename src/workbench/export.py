"""`.eval` interop (#5) — round-trip a workbench branch through petri's log format.

`export_branch` writes a one-sample `.eval` whose `AuditTape` store carries
the branch's L2 subtree (`History.dump()` shape), so `inspect view` and
petri's `resample` task can read it unchanged. `import_eval` is the inverse:
`load_tape()` → `History.load()` + a `BranchMeta` recovered from the log
header. The imported `History`'s root carries the full recorded log; the
caller (`_dispatch` ``"import"``) wraps it in a fresh `Branch` and replays
it the same way `Session.load` does.
"""

from __future__ import annotations

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
from inspect_ai.util import Store
from inspect_petri._auditor import AuditTape
from inspect_petri._task.resample import load_tape
from inspect_petri.target import History

from workbench.run import BranchMeta

if TYPE_CHECKING:
    from workbench.run import Branch


def export_branch(branch: Branch, path: str | Path) -> None:
    """Write `branch` (and its descendants) as a one-sample `.eval` log.

    The sample's store holds an `AuditTape` (`trajectories` =
    `History.dump(root=branch.trajectory)` — self-contained subtree,
    `seed_instructions` = `branch.meta.seed`) so petri's `load_tape` /
    `resample` accept it verbatim. Sample metadata carries the model
    names so `import_eval` can rebuild a `BranchMeta`.
    """
    m = branch.meta
    store = Store()
    tape = AuditTape(store=store)
    tape.trajectories = branch.session.audit_history.dump(root=branch.trajectory)
    tape.seed_instructions = m.seed
    sample = EvalSample(
        id=branch.branch_id,
        epoch=1,
        input=m.seed,
        target="",
        metadata={
            "auditor_model": m.auditor_model,
            "target_model": m.target_model,
            "max_turns": m.max_turns,
            "auditor_config": m.auditor_config,
            "target_config": m.target_config,
        },
        store={k: v for k, v in store.items() if v is not None},
    )
    log = EvalLog(
        status="success",
        eval=EvalSpec(
            created=datetime.now(UTC).isoformat(),
            task="workbench/export",
            dataset=EvalDataset(samples=1),
            model=m.target_model,
            config=EvalConfig(),
        ),
        samples=[sample],
    )
    write_eval_log(log, Path(path))


def import_eval(
    path: str | Path, sample_id: int | str | None = None
) -> tuple[History, BranchMeta]:
    """Load one sample's `AuditTape` from a `.eval` and rebuild it as a `History` + `BranchMeta`.

    Models are read from the sample's ``metadata`` (workbench-exported
    logs) with a fallback to the log header's ``model_roles`` / ``model``
    (petri-produced logs).
    """
    tape = load_tape(str(path), sample_id)
    history = History.load(tape.trajectories)

    header = read_eval_log(str(path), header_only=True)
    roles = header.eval.model_roles or {}
    summaries = header.samples or []
    md: dict[str, Any] = {}
    if summaries:
        sid = sample_id if sample_id is not None else summaries[0].id
        md = next((s.metadata for s in summaries if s.id == sid), None) or {}

    def _role(name: str) -> str:
        r = roles.get(name)
        return r.model if r is not None else header.eval.model

    meta = BranchMeta(
        seed=tape.seed_instructions,
        auditor_model=md.get("auditor_model") or _role("auditor"),
        target_model=md.get("target_model") or _role("target"),
        max_turns=md.get("max_turns"),
        auditor_config=md.get("auditor_config"),
        target_config=md.get("target_config"),
    )
    return history, meta
