"""The ``wb`` namespace — side-effects only (M1-NOTEBOOK.md v4 §Revised surface).

Constructed by ``Orchestrator`` (which has both ``session`` and ``kernel``)
and seeded into ``user_ns``. Everything that isn't a side-effect is just
Python — the agent uses ``pd``/``px``/``display()`` directly.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from inspect_ai import Task

from workbench.m1.kernel import Prompt
from workbench.m1.run import (
    AuditRunHandle,
    RunHandle,
    RunProposal,
    _launch,
    steer,
    stop,
)

if TYPE_CHECKING:
    from workbench.m1.kernel import OrchestratorKernel
    from workbench.session import Session

#: Runs with more audits than this are gated on human approval.
GATE_THRESHOLD = 8


class Workbench:
    """The ``wb.*`` surface. ~10 helpers, all side-effects.

    ``run_audits``/``run_eval``/``cite``/``ask_human`` block on a gate;
    ``steer``/``stop`` mutate running samples; ``scan``/``excerpt``/
    ``transcript``/``pin`` are compute/read helpers with a rich repr.
    """

    def __init__(self, kernel: "OrchestratorKernel", session: "Session | None") -> None:
        self._k = kernel
        self._session = session

    def __repr__(self) -> str:
        return (
            "<wb · run_audits run_eval steer stop pin ask_human "
            "scan cite excerpt transcript>"
        )

    # -- gated launchers --------------------------------------------------

    async def run_audits(
        self,
        seeds: str | Sequence[str],
        config: dict[str, Any] | None = None,
        *,
        description: str,
        n_per_seed: int = 1,
        model: str,
        auditor_model: str | None = None,
        log_dir: str | None = None,
    ) -> AuditRunHandle:
        """Launch a petri audit batch as an inspect eval.

        Gates on approval when ``n > GATE_THRESHOLD``; the human may strike
        seeds. Returns a live ``AuditRunHandle`` — the card ticks via
        ``dh.update``; ``await h.wait()`` for the result inline. Shares
        ``_launch`` with ``run_eval``; the petri specifics are the
        ``RunProposal`` seed-preview and ``AuditRunHandle`` per-audit rows.
        """
        from inspect_petri import audit  # noqa: PLC0415

        seed_list = [seeds] if isinstance(seeds, str) else list(seeds)
        cfg = dict(config or {})
        prop = RunProposal(seed_list, cfg, description, n_per_seed)

        if prop.n > GATE_THRESHOLD:
            await self._k.gate(prop)
            if prop.denied:
                # A human clicking "deny" is expected control flow, not an
                # exception — hand back a settled handle so the model reads
                # one line, not a traceback.
                reason = (prop.verdict or {}).get("reason", "denied")
                return AuditRunHandle(
                    task_name="audit",
                    log_dir="",
                    total=prop.n,
                    id=prop.id,
                    description=description,
                    finished=True,
                    error=f"denied: {reason}",
                )

        task = audit(
            seed_instructions=prop.seeds,
            **cfg,
        )
        model_roles = {"target": model, "auditor": auditor_model or model}
        h = await _launch(
            task,
            handle_cls=AuditRunHandle,
            handle_id=prop.id,
            log_dir=log_dir,
            total=prop.n,
            description=description,
            model=model,
            model_roles=model_roles,
            epochs=n_per_seed,
        )
        assert isinstance(h, AuditRunHandle)
        return h

    async def run_eval(
        self,
        task: Task,
        *,
        model: str,
        description: str = "",
        log_dir: str | None = None,
        **eval_kw: Any,
    ) -> RunHandle:
        """Launch any inspect ``Task`` — same launcher as ``run_audits``."""
        total = len(task.dataset) if task.dataset else 0
        return await _launch(
            task,
            handle_cls=RunHandle,
            log_dir=log_dir,
            total=total,
            description=description,
            model=model,
            **eval_kw,
        )

    async def ask_human(self, question: str, options: list[str] | None = None) -> str:
        return str(await self._k.gate(Prompt(question, options)))

    # -- mutate running samples ------------------------------------------

    def steer(self, ids: str | Iterable[str], message: str) -> None:
        """Queue an operator message for each sample's next turn."""
        steer([ids] if isinstance(ids, str) else ids, message)

    def stop(self, ids: str | Iterable[str], *, hard: bool = False) -> None:
        """Ask each sample to end (``hard=True`` interrupts immediately)."""
        stop([ids] if isinstance(ids, str) else ids, hard=hard)

    # -- desk bridge ------------------------------------------------------

    def pin(self, audit_id: str, *, log: str | None = None) -> None:
        """Import one audit as an M0 ``Branch`` for live pause/step/resample.

        Delegates to the existing ``server._dispatch("import")`` path so the
        desk's edit/branch/resample machinery applies unchanged. ``log``
        defaults to the most recent ``run_audits`` log dir.
        """
        raise NotImplementedError("wb.pin: M1.2 stub — wire to server import path")

    # -- stubs for M1.3 ---------------------------------------------------

    async def scan(self, logs: str, scanner: Any, **kw: Any) -> Any:
        raise NotImplementedError("wb.scan: M1.3")

    async def cite(self, claim: str, quotes: Any, **kw: Any) -> Any:
        raise NotImplementedError("wb.cite: M1.3")

    def excerpt(self, audit_id: str, at: int, around: int = 1) -> Any:
        raise NotImplementedError("wb.excerpt: M1.3")

    def transcript(self, audit_id: str, at: int | None = None) -> Any:
        raise NotImplementedError("wb.transcript: M1.3")
