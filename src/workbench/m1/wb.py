"""The ``wb`` namespace — side-effects only (M1-NOTEBOOK.md v4 §Revised surface).

Constructed by ``Orchestrator`` (which has both ``session`` and ``kernel``)
and seeded into ``user_ns``. Everything that isn't a side-effect is just
Python — the agent uses ``pd``/``px``/``display()`` directly.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from inspect_ai import Task
from inspect_ai.log._samples import active_samples  # noqa: PLC2701
from IPython.display import Markdown, display

from workbench.m1.attach import AttachedRun
from workbench.m1.cite import Finding, Quote, cite
from workbench.m1.kernel import Prompt
from workbench.m1.plots import Plots
from workbench.m1.read import (
    Excerpt,
    TranscriptRef,
    excerpt,
    read_transcript,
    transcript,
)
from workbench.m1.run import (
    AuditRunHandle,
    RunHandle,
    RunProposal,
    ScanHandle,
    steer,
    stop,
)

if TYPE_CHECKING:
    from workbench.m1.kernel import Gate
    from workbench.session import Session

#: Runs with more audits than this are gated on human approval.
GATE_THRESHOLD = 8


class Workbench:
    """The ``wb.*`` surface. ~10 helpers, all side-effects.

    ``run_audits``/``run_eval``/``cite``/``ask_human`` block on a gate;
    ``steer``/``stop`` mutate running samples; ``scan``/``excerpt``/
    ``transcript`` are compute/read helpers with a rich repr.
    """

    def __init__(self, gate: "Gate", session: "Session | None") -> None:  # noqa: ARG002
        # ``gate`` is the only kernel dependency (``run_audits``/``ask_human``/
        # ``cite`` await it); holding just the ``Gate`` keeps ``wb`` decoupled
        # from the turn-lifecycle machinery. ``session`` is unused until a
        # helper needs it.
        self._gate = gate

    def __repr__(self) -> str:
        return (
            "<wb · attach run_audits run_eval steer stop ask_human "
            "scan cite excerpt transcript read_transcript>"
        )

    #: Read-only handle on an out-of-process eval's ``log_dir`` (M1-HYBRID
    #: §``wb.attach``). Displays a live ``ProgressCard``; ``await h.wait()``
    #: for the ``.eval`` to settle; ``h.audits`` for the DataFrame.
    attach = staticmethod(AttachedRun.attach)

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
        ``RunHandle.launch`` with ``run_eval``; the petri specifics are the
        ``RunProposal`` seed-preview and ``AuditRunHandle`` per-audit rows.

        The task is built from petri's public parts (``seeds_dataset`` /
        ``audit_solver`` / ``audit_judge`` / ``audit_viewer``) with
        ``workbench_auditor(BatchHooks(), compaction=True, …)`` as the
        auditor — so ``wb.steer``/``wb.stop`` reach running samples via the
        same loop that drives the M0 desk, and we control the ``config``
        surface rather than tracking petri's ``audit()`` kwargs.
        """
        from inspect_petri import (  # noqa: PLC0415
            audit_judge,
            audit_solver,
            audit_viewer,
            seeds_dataset,
            target_agent,
        )

        from workbench.auditor import workbench_auditor  # noqa: PLC0415
        from workbench.m1.run import BatchHooks  # noqa: PLC0415

        seed_list = [seeds] if isinstance(seeds, str) else list(seeds)
        cfg = dict(config or {})
        prop = RunProposal(seed_list, cfg, description, n_per_seed, model=model)

        if prop.n > GATE_THRESHOLD:
            await self._gate(prop)
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

        max_turns = int(cfg.pop("max_turns", 30))
        auditor = workbench_auditor(
            BatchHooks(),
            max_turns=max_turns,
            compaction=cfg.pop("compaction", True),
            realism_filter=cfg.pop("realism_filter", False),
        )
        task = Task(
            dataset=seeds_dataset(prop.seeds),
            solver=audit_solver(auditor=auditor, target=target_agent()),
            scorer=audit_judge(cfg.get("judge_dimensions")),
            viewer=audit_viewer(cfg.get("judge_dimensions")),
            name=f"audit-{prop.id[:6]}",
        )
        # ``audit_judge`` resolves ``get_model(role="judge", required=True)``
        # whenever an ``auditor`` role is present — omit it and every sample
        # errors at scoring with ``Model role 'judge' is required``.
        model_roles = {
            "target": model,
            "auditor": auditor_model or model,
            "judge": auditor_model or model,
        }
        return AuditRunHandle.launch(
            task,
            id=prop.id,
            log_dir=log_dir,
            total=prop.n,
            description=description,
            model=model,
            model_roles=model_roles,
            epochs=n_per_seed,
        )

    def run_eval(
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
        return RunHandle.launch(
            task,
            log_dir=log_dir,
            total=total,
            description=description,
            model=model,
            **eval_kw,
        )

    async def ask_human(self, question: str, options: list[str] | None = None) -> str:
        return str(await self._gate(Prompt(question, options)))

    async def cite(
        self,
        claim: str,
        quotes: Sequence[Quote | dict[str, Any]],
        *,
        grades_ref: str | None = None,
        description: str,
    ) -> Finding:
        """Propose a finding for the human to sign. Always blocks; deny
        returns an unsigned ``Finding`` (no exception)."""
        return await cite(
            self._gate, claim, list(quotes), grades_ref=grades_ref, description=description
        )

    #: Plot helpers over ``px.*`` — ``link``/``annotate_top``/``paired_slope``/
    #: ``replicate_grid``/``survival`` (M1-PLOTTING.md). The ``workbench``
    #: template + ``notebook_connected`` renderer are installed at kernel init.
    plots = Plots()

    # -- mutate running samples ------------------------------------------

    def steer(self, ids: str | Iterable[str], message: str) -> None:
        """Queue an operator message for each sample's next turn.

        Always emits a receipt. Writes to ``CONTROL`` for *every* id (a
        sample may not have started yet); the running count is advisory.
        """
        want = [ids] if isinstance(ids, str) else [str(i) for i in ids]
        running = {str(s.sample.id) for s in active_samples()}
        n_matched = sum(1 for i in want if i in running)
        n_missed = len(want) - n_matched
        steer(want, message)
        note = f" ({n_missed} not running)" if n_missed else ""
        display(Markdown(f"→ steered {n_matched}/{len(want)} running samples{note}"))

    def stop(self, ids: str | Iterable[str], *, hard: bool = False) -> None:
        """Ask each sample to end (``hard=True`` interrupts immediately)."""
        stop([ids] if isinstance(ids, str) else ids, hard=hard)

    # -- read transcripts -------------------------------------------------

    def transcript(
        self, log: str | RunHandle, sample_id: str, *, at: int | None = None
    ) -> TranscriptRef:
        """Embed an inspect-view of one sample. The model sees a one-line
        summary; use ``excerpt``/``read_transcript`` to read content."""
        return transcript(log, sample_id, at=at)

    async def excerpt(
        self, log: str | RunHandle, sample_id: str, *, at: int, around: int = 1
    ) -> Excerpt:
        """Render ``messages[at-around : at+around+1]`` inline."""
        return await excerpt(log, sample_id, at=at, around=around)

    async def read_transcript(
        self,
        log: str | RunHandle,
        sample_id: str,
        *,
        range: tuple[int, int] | None = None,  # noqa: A002
    ) -> str:
        """Plain ``messages_as_str`` text — for the model, no frontend card."""
        return await read_transcript(log, sample_id, range=range)

    # -- scan (inspect_scout) ---------------------------------------------

    async def scan(
        self,
        logs: str | RunHandle | list[str],
        scanner: Any,
        *,
        description: str = "",
        model: str | None = None,
        scans_dir: str | None = None,
    ) -> ScanHandle:
        """Run scout scanners over eval logs — returns a live ``ScanHandle``.

        ``logs`` may be a ``RunHandle`` (uses ``.log_dir``), a path, or a list
        of paths. ``scanner`` may be a single ``Scanner``, a list, or a
        ``{name: Scanner}`` dict. Each call gets a fresh ``scans_dir`` so
        ``ScanHandle._poll`` can resolve the one scan location inside it via
        ``scan_list_async`` (mirrors ``RunHandle`` resolving its ``.eval``).
        """
        import tempfile  # noqa: PLC0415

        from inspect_scout import ScanJob, transcripts_from  # noqa: PLC0415
        from inspect_scout.aio import scan_async  # noqa: PLC0415

        if isinstance(logs, RunHandle):
            logs = logs.log_dir
        transcripts = transcripts_from(logs)

        if isinstance(scanner, dict):
            scanners = scanner
            names = list(scanner)
        elif isinstance(scanner, (list, tuple)):
            scanners = list(scanner)
            names = [s[0] if isinstance(s, tuple) else "?" for s in scanners]
        else:
            scanners = [scanner]
            names = ["scan"]

        scans_dir = scans_dir or tempfile.mkdtemp(prefix="wb-scan-")
        job = ScanJob(
            transcripts=transcripts,
            scanners=scanners,
            scans=scans_dir,
            model=model,
            # Scout's multiprocess strategy forks workers that each re-run
            # ``platform_init()`` (hooks banner → parent stdout, past the
            # ``_CellStream`` tee). In-kernel scans are small; keep it in-loop.
            max_processes=1,
        )
        h = ScanHandle(
            scans_dir=scans_dir,
            scanner_names=names,
            description=description,
        )
        return h._start(scan_async(job))
