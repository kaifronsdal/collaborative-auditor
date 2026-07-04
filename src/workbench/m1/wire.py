"""M1 wire contract — MIME constants, ``DisplayEvent``, and per-``kind`` payloads.

M1-REFACTOR.md Batch C: the shared vocabulary between the Python side that
*builds* ``bundle["application/vnd.workbench.v1+json"]`` payloads and the
frontend's ``types.ts`` that *renders* them. One ``TypedDict`` per ``kind``
literal, one ``WbPayload`` union, one ``wb_bundle(text, payload)`` builder.

Extracted from ``kernel.py`` so ``hooks.py`` (which needs ``DisplayEvent`` /
``STREAM_MIME``) no longer imports the kernel — that import was circular
(``kernel.__enter__`` imports ``hooks`` for ``install_workbench_hooks``).

All payload TypedDicts are ``total=False``: fields document what *may* be
present, not what *must* be. mypy still catches typos and wrong value types
at construction sites; ``types.ts`` mirrors the same shapes with optional
fields so a backend field addition doesn't break the frontend build.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict, get_args, get_type_hints

WB_MIME = "application/vnd.workbench.v1+json"
STREAM_MIME = "application/vnd.jupyter.stream+json"


def _finite(v: Any) -> Any:
    """Map non-finite floats (``nan`` / ``inf``) to ``None``, recursively.

    ``jsonable_python`` leaves them as-is and stdlib ``json.dumps`` then
    emits bare ``NaN`` / ``Infinity`` — invalid JSON that breaks the
    frontend's ``JSON.parse``. Applied to score values before they reach
    ``WB_MIME`` payloads (mockllm + petri judge yields ``float('nan')``).
    """
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite(x) for x in v]
    return v


# -- DisplayEvent -------------------------------------------------------------


@dataclass(slots=True)
class DisplayEvent:
    """One kernel output, in emission order.

    ``bundle`` is the IPython MIME dict (``text/plain`` is what the model
    reads; ``application/vnd.workbench.v1+json`` is what ``<Output>``
    renders). ``update=True`` means "patch the earlier event with this
    ``id``" — the proposal→live and progress-tick cases.
    """

    id: str
    bundle: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)
    update: bool = False
    #: caller passed ``display_id=`` — later ``update=True`` events with the
    #: same id replace this one in the model-facing render.
    stable: bool = False
    turn_id: int = -1

    @property
    def text(self) -> str:
        """The model-facing rendering of this output.

        Picks the first available of ``text/markdown`` → ``text/latex`` →
        ``text/plain`` and caps length — the safety net for rich objects
        (e.g. a ``go.Figure`` whose ``text/plain`` we forgot to register a
        compact formatter for) so one plot can't blow the tool result.
        """
        if (s := self.bundle.get(STREAM_MIME)) is not None:
            return str(s["text"])
        for mime in _MODEL_MIME_PREF:
            if t := self.bundle.get(mime):
                return _truncate(str(t), _MODEL_TEXT_CAP)
        return ""


#: MIME preference for the model-facing rendering. ``text/markdown`` first so
#: ``display(Markdown(f"…"))`` (the ``wb.report`` replacement) shows the
#: computed prose, not ``<IPython.core.display.Markdown object>``.
_MODEL_MIME_PREF = ("text/markdown", "text/latex", "text/plain")
_MODEL_TEXT_CAP = 4000


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# -- supporting shapes --------------------------------------------------------


class SampleRowPayload(TypedDict, total=False):
    """One sample row inside ``EvalRunPayload["rows"]`` — ``vars(SampleRow)``.

    Not a top-level ``kind`` (no card of its own); nested under
    ``rows.running`` / ``rows.done``. ``epoch`` is absent on rows folded from
    ``wb_display`` protocol lines (``tools._fold_eval``).
    """

    id: str
    status: Literal["running", "done", "error", "stopped"]
    epoch: int
    input: str
    turns: int | None
    scores: dict[str, Any]
    error: str | None


class QuotePayload(TypedDict, total=False):
    """``vars(Quote)`` — nested under ``CiteProposalPayload`` / ``FindingPayload``."""

    sample_id: str
    at: int
    role: str
    text: str
    log: str


class TracebackFrame(TypedDict, total=False):
    file: str
    lineno: int | None
    line: str | None


# -- gate cards ---------------------------------------------------------------


class PromptPayload(TypedDict, total=False):
    kind: Literal["prompt"]
    id: str
    pending: bool
    verdict: dict[str, Any] | None
    question: str
    options: list[str] | None
    answer: str | None
    answered_at: str | None


class RunProposalPayload(TypedDict, total=False):
    kind: Literal["run_proposal"]
    id: str
    pending: bool
    verdict: dict[str, Any] | None
    description: str
    n: int
    n_per_seed: int
    #: ``[{"id": f"s{i}", "text": seed[:200]}, …]``
    seeds: list[dict[str, str]]
    config: dict[str, Any]


class CiteProposalPayload(TypedDict, total=False):
    kind: Literal["cite_proposal"]
    id: str
    pending: bool
    verdict: dict[str, Any] | None
    claim: str
    quotes: list[QuotePayload]
    grades_ref: str | None
    description: str


class FindingPayload(TypedDict, total=False):
    kind: Literal["finding"]
    id: str
    claim: str
    quotes: list[QuotePayload]
    signed_by: str | None


# -- progress cards -----------------------------------------------------------


class EvalRunPayload(TypedDict, total=False):
    """``AttachedRun._repr_mimebundle_`` and ``tools._fold_eval`` both build this.

    ``elapsed`` and ``scores`` are set once available (``elapsed`` on every
    ``AttachedRun`` tick and after the first ``eval_progress`` line; ``scores``
    only once ``finished``). ``_fold_eval`` folds per-sample scores into
    ``scores`` on ``eval_done`` so both producers converge on the same shape
    and ``ProgressCard``'s §8 histogram works for bash-driven runs too.
    """

    kind: Literal["eval_run"]
    id: str
    task: str
    description: str
    log_dir: str
    log: str | None
    total: int
    done: int
    finished: bool
    error: str | None
    elapsed: str
    rows: dict[str, list[SampleRowPayload]]
    scores: list[float | None]


class ScanPayload(TypedDict, total=False):
    kind: Literal["scan"]
    id: str
    description: str
    scans_dir: str
    location: str | None
    done: int
    total: int
    finished: bool
    error: str | None
    per_scanner: dict[str, dict[str, int]]
    #: name → 3-row HTML table; only present once ``finished``.
    df_head: dict[str, str]


# -- reader cards -------------------------------------------------------------


class TranscriptPayload(TypedDict, total=False):
    kind: Literal["transcript"]
    log: str
    sample_id: str
    at: int | None
    n_messages: int
    #: ``ChatMessage.model_dump(mode="json")`` — tail 3 (or ±1 around ``at``).
    preview: list[dict[str, Any]]


class ExcerptPayload(TypedDict, total=False):
    kind: Literal["excerpt"]
    log: str
    sample_id: str
    at: int
    at_idx: int
    messages: list[dict[str, Any]]


# -- kernel-emitted -----------------------------------------------------------


class TracebackPayload(TypedDict, total=False):
    kind: Literal["traceback"]
    ename: str
    evalue: str
    frames: list[TracebackFrame]
    text: str


class CellDonePayload(TypedDict, total=False):
    kind: Literal["cell_done"]
    turn: int
    duration: float
    new_names: list[str]
    interrupted: bool
    ns: dict[str, str]


class BgDonePayload(TypedDict, total=False):
    kind: Literal["bg_done"]
    id: str
    pid: int
    exit: int


class RewindMarkerPayload(TypedDict, total=False):
    """Carried in ``InfoEvent.data`` directly (not under ``bundle[WB_MIME]``)
    — the frontend's ``OrchColumn`` reads it off ``ev.data.kind``. Included in
    ``WbPayload`` so ``types.ts`` gets the shape; excluded from ``WB_KINDS``
    below since it never lands in a MIME bundle."""

    kind: Literal["rewind_marker"]
    to_turn: int


# -- union + builder ----------------------------------------------------------


WbPayload = (
    PromptPayload
    | RunProposalPayload
    | CiteProposalPayload
    | FindingPayload
    | EvalRunPayload
    | ScanPayload
    | TranscriptPayload
    | ExcerptPayload
    | TracebackPayload
    | CellDonePayload
    | BgDonePayload
    | RewindMarkerPayload
)


def wb_bundle(text: str, payload: WbPayload) -> dict[str, Any]:
    """The one ``{text/plain, WB_MIME}`` builder every ``_repr_mimebundle_`` calls."""
    return {"text/plain": text, WB_MIME: payload}


def _kind_literals() -> set[str]:
    """Extract the ``Literal["…"]`` from each union member's ``kind`` annotation."""
    out: set[str] = set()
    for member in get_args(WbPayload):
        hints = get_type_hints(member)
        out.update(get_args(hints["kind"]))
    return out


#: Every ``kind`` literal that may appear in ``bundle[WB_MIME]`` (i.e.
#: ``WbPayload`` minus ``rewind_marker``, which rides ``InfoEvent.data``).
#: Asserted against in ``_smoke_m1_kernel`` — an unregistered kind is a
#: forgotten TypedDict + missing ``types.ts`` mirror.
WB_KINDS: frozenset[str] = frozenset(_kind_literals() - {"rewind_marker"})
