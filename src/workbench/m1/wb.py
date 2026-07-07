"""The ``wb`` namespace — read/analyze/present + review gates (M1-HYBRID §wb.*).

Constructed by ``Orchestrator`` (which has both ``session`` and ``kernel``)
and seeded into ``user_ns``. No launching, no steer/stop — evals run in
subprocesses via the ``bash`` tool; ``wb.attach`` observes them. Everything
that isn't a side-effect is just Python — the agent uses ``pd``/``px``/
``display()`` directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from workbench.m1 import proposals
from workbench.m1.attach import AttachedRun
from workbench.m1.handles import ScanHandle
from workbench.m1.plots import Plots
from workbench.m1.proposals import Finding, Gate, Prompt, Quote, load_findings
from workbench.m1.read import (
    Excerpt,
    TranscriptRef,
    excerpt,
    read_transcript,
    transcript,
)


class Workbench:
    """The ``wb.*`` surface — read helpers + review gates.

    ``ask_human``/``review_seeds``/``cite`` block on a gate; ``attach``/
    ``scan``/``excerpt``/``transcript`` are compute/read helpers with a
    rich repr.
    """

    def __init__(
        self,
        gate: Gate,
        *,
        session_dir: str | None = None,
        session_id: str = "",
    ) -> None:
        # ``gate`` is the only kernel dependency (``ask_human``/``review_seeds``/
        # ``cite`` await it); holding just the ``Gate`` keeps ``wb`` decoupled
        # from the turn-lifecycle machinery. ``session_dir`` is the ``bash``
        # tool's cwd — threaded to ``attach`` so relative ``log_dir``s resolve
        # there, and to ``cite``/``findings`` for the durable ``findings.jsonl``
        # store (P0.6). ``session_id`` is stamped onto each persisted ``Finding``.
        self._gate = gate
        self._session_dir = session_dir
        self._session_id = session_id
        #: P1.2 — audit-role defaults from ``OrchStartCard`` (target/auditor/
        #: judge/max_turns/…). Populated by ``Orchestrator.__init__``; the
        #: same values are interpolated into the system prompt. Cells read
        #: e.g. ``wb.DEFAULTS["target"]`` when composing ``bash("inspect eval …")``.
        self.DEFAULTS: dict[str, Any] = {}
        #: P1.8(a) — scanner library + named groups from
        #: ``STORE_DIR/scanners/{*.py,groups.yaml}``. Populated by
        #: ``Orchestrator.__init__`` so cells can introspect what
        #: ``wb.scan(logs, "name")`` will resolve to.
        self.SCANNERS: list[str] = []
        self.SCANNER_GROUPS: dict[str, list[str]] = {}

    def __repr__(self) -> str:
        return (
            "<wb · attach ask_human review_seeds cite findings scan "
            "excerpt transcript read_transcript plots DEFAULTS>"
        )

    def attach(self, log_dir: str) -> AttachedRun:
        """Read-only handle on an out-of-process eval's ``log_dir``
        (M1-HYBRID §``wb.attach``). Relative paths resolve against the
        orchestrator's session dir — the same cwd the ``bash`` tool runs
        in — so ``bash("inspect eval … --log-dir runs/r1")`` and
        ``wb.attach("runs/r1")`` agree. Displays a live ``ProgressCard``;
        ``await h.wait()`` for the ``.eval`` to settle; ``h.audits`` for
        the DataFrame."""
        return AttachedRun.attach(log_dir, session_dir=self._session_dir)

    # -- review gates (in-cell aliases of the review tools) ---------------

    async def ask_human(self, question: str, options: list[str] | None = None) -> str:
        return str(await self._gate(Prompt(question, options)))

    async def review_seeds(
        self,
        seeds: Sequence[str],
        description: str,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Propose a seed list for human approval (in-cell alias of the
        ``review_seeds`` tool). The human may strike seeds or deny outright;
        returns ``{"approved": bool, "seeds": list[str], "reason": str|None}``."""
        return await proposals.review_seeds(self._gate, seeds, description, config)

    async def cite(
        self,
        claim: str,
        quotes: Sequence[Quote | dict[str, Any]],
        *,
        description: str,
    ) -> Finding:
        """Propose a finding for the human to sign. Always blocks; deny
        returns an unsigned ``Finding`` (no exception). Signed findings are
        appended to ``{session_dir}/findings.jsonl`` (P0.6)."""
        return await proposals.cite(
            self._gate,
            claim,
            quotes,
            description=description,
            session_dir=self._session_dir,
            session_id=self._session_id,
        )

    def findings(self) -> list[Finding]:
        """Every signed ``Finding`` persisted so far (reads ``findings.jsonl``)."""
        if self._session_dir is None:
            return []
        return load_findings(self._session_dir)

    #: Plot helpers over ``px.*`` — ``link``/``annotate_top``/``paired_slope``/
    #: ``replicate_grid``/``survival`` (M1-PLOTTING.md). The ``workbench``
    #: template + ``notebook_connected`` renderer are installed at kernel init.
    plots = Plots()

    # -- read transcripts -------------------------------------------------

    def transcript(
        self, log: str | AttachedRun, sample_id: str, *, at: int | None = None
    ) -> TranscriptRef:
        """Embed an inspect-view of one sample. The model sees a one-line
        summary; use ``excerpt``/``read_transcript`` to read content."""
        return transcript(log, sample_id, at=at)

    async def excerpt(
        self, log: str | AttachedRun, sample_id: str, *, at: int, around: int = 1
    ) -> Excerpt:
        """Render ``messages[at-around : at+around+1]`` inline."""
        return await excerpt(log, sample_id, at=at, around=around)

    async def read_transcript(
        self,
        log: str | AttachedRun,
        sample_id: str,
        *,
        range: tuple[int, int] | None = None,  # noqa: A002
    ) -> str:
        """Plain ``messages_as_str`` text — for the model, no frontend card."""
        return await read_transcript(log, sample_id, range=range)

    # -- scan (inspect_scout) ---------------------------------------------

    async def scan(
        self,
        logs: str | AttachedRun | list[str],
        scanner: Any,
        *,
        description: str = "",
        model: str | None = None,
        scans_dir: str | None = None,
    ) -> ScanHandle:
        """Run scout scanners over eval logs — returns a live ``ScanHandle``.

        ``logs`` may be an ``AttachedRun`` (uses ``.log_dir``), a path, or a
        list of paths. ``scanner`` may be a name (group or registry/library —
        see :mod:`.scanners`), a raw ``Scanner``, a list, or a
        ``{name: Scanner}`` dict. Each call gets a fresh ``scans_dir`` so
        ``ScanHandle._poll`` can resolve the one scan location inside it via
        ``scan_list_async``.
        """
        import tempfile

        from inspect_scout import ScanJob, transcripts_from
        from inspect_scout.aio import scan_async

        from workbench.m1.scanners import resolve

        if isinstance(logs, AttachedRun):
            logs = logs.log_dir
        transcripts = transcripts_from(logs)

        scanners = resolve(scanner)
        names = list(scanners)

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
        return h._start(scan_async(job))  # noqa: SLF001
