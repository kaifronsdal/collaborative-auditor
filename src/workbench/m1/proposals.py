"""Gated proposals — ``Gate`` + the three proposal cards + shared cores.

M1-REFACTOR.md Batch A: one file for the ``display(proposal) → await Future →
dh.update(resolved)`` shape. ``Prompt`` / ``RunProposal`` / ``CiteProposal``
are the three gate cards; ``Gate`` is the awaiter; ``review_seeds`` / ``cite``
are the shared bodies that both the in-cell ``wb.*`` helpers and the tool-
level ``review_*`` wrappers call so there is exactly one construction path per
proposal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import uuid4

from IPython.display import display

from workbench.m1.wire import (
    CiteProposalPayload,
    FindingPayload,
    PromptPayload,
    QuotePayload,
    RunProposalPayload,
    WbPayload,
    wb_bundle,
)


# -- gate ---------------------------------------------------------------------


class Proposal(Protocol):
    """A gated card: renders pending, awaits a verdict, then renders resolved."""

    id: str

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]: ...

    def resolve(self, verdict: Any) -> None: ...


class Gate:
    """``display(proposal)`` → ``await Future`` → ``dh.update(resolved)``.

    The WS handler resolves via ``.resolve()``. Owned by the ``Orchestrator``
    (not the kernel — M1-REFACTOR Batch A) so ``Workbench`` and the review
    tools reach it as ``orch.gate`` without touching turn-lifecycle machinery.
    """

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self.pending: dict[str, asyncio.Future[Any]] = {}
        #: Fired whenever ``pending`` gains or loses an entry — the
        #: ``Orchestrator`` hooks this to ``session.broadcast_status()`` so
        #: the header flips to/from ``waiting`` the moment a gate opens.
        self.on_change = on_change

    async def __call__(self, proposal: Proposal) -> Any:
        dh = display(proposal, display_id=proposal.id)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[proposal.id] = fut
        if self.on_change is not None:
            self.on_change()
        try:
            verdict = await fut
        finally:
            self.pending.pop(proposal.id, None)
            if self.on_change is not None:
                self.on_change()
        proposal.resolve(verdict)
        dh.update(proposal)
        return verdict

    def resolve(self, display_id: str, verdict: Any) -> bool:
        """Resolve a pending gate. Returns ``False`` if ``display_id`` unknown."""
        fut = self.pending.get(display_id)
        if fut is None or fut.done():
            return False
        fut.set_result(verdict)
        return True


# -- proposals ----------------------------------------------------------------


@dataclass
class BaseProposal:
    """Common ``id`` / ``verdict`` / ``pending`` / ``_bundle`` for gate cards.

    ``id`` doubles as the ``display_id`` (stable slot, updates in place on
    resolve). ``_bundle`` folds the shared ``id/pending/verdict`` keys into the
    subclass's typed payload and hands it to :func:`~workbench.m1.wire.wb_bundle`.
    """

    id: str = field(default_factory=lambda: uuid4().hex, kw_only=True)
    verdict: dict[str, Any] | None = field(default=None, kw_only=True)

    @property
    def pending(self) -> bool:
        return self.verdict is None

    def _bundle(self, text: str, payload: WbPayload) -> dict[str, Any]:
        # ``payload`` already carries ``kind`` (typed by the subclass); folding
        # the shared keys via ``**`` widens to ``dict[str, object]`` under mypy,
        # so cast back — the field set is the union of ``payload``'s TypedDict
        # and the three shared keys, which every gate payload declares.
        merged = cast(
            "WbPayload",
            {"id": self.id, "pending": self.pending, "verdict": self.verdict, **payload},
        )
        return wb_bundle(text, merged)


@dataclass
class Prompt(BaseProposal):
    """The minimal gated helper — ``wb.ask_human`` / the ``ask_human`` tool."""

    question: str
    options: list[str] | None = None
    answer: str | None = field(default=None, kw_only=True)
    answered_at: str | None = field(default=None, kw_only=True)

    def resolve(self, verdict: Any) -> None:
        self.answer = str(verdict)
        self.answered_at = datetime.now(UTC).isoformat()
        self.verdict = {"answer": self.answer}

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        text = (
            f"<Prompt {self.id[:6]} · {self.question!r} · pending>"
            if self.pending
            else f"<Prompt {self.id[:6]} → {self.answer!r}>"
        )
        payload: PromptPayload = {
            "kind": "prompt",
            "question": self.question,
            "options": self.options,
            "answer": self.answer,
            "answered_at": self.answered_at,
        }
        return self._bundle(text, payload)


@dataclass
class RunProposal(BaseProposal):
    """The pre-launch gate card for ``review_seeds``.

    ``resolve()`` receives the WS ``approve`` verdict (``{"denied": bool,
    "seeds": list[str] | None, "reason": str | None}``); the human may have
    struck seeds. The resolved bundle shows ``approved by …`` and the
    surviving count.
    """

    seeds: list[str]
    config: dict[str, Any]
    description: str
    n_per_seed: int = 1
    #: Target-model id for the config line on the gate card (UI-AUDIT §A).
    model: str | None = None

    @property
    def n(self) -> int:
        return len(self.seeds) * self.n_per_seed

    def resolve(self, verdict: Any) -> None:
        v = verdict or {}
        self.verdict = v
        if not isinstance(v, dict):
            return
        # Frontend (UI-AUDIT §A) sends ``surviving`` as a list of seed *ids*
        # (``s{i}`` — matching the ``seeds`` payload below); accept that, or
        # the older ``seeds`` shape (list of texts, or list of ``{id,text}``).
        if (surviving := v.get("surviving")) is not None:
            keep = set(surviving)
            self.seeds = [s for i, s in enumerate(self.seeds) if f"s{i}" in keep]
        elif (edited := v.get("seeds")) is not None:
            self.seeds = [
                e["text"] if isinstance(e, dict) else e for e in edited
            ]

    @property
    def denied(self) -> bool:
        return bool(self.verdict and self.verdict.get("denied"))

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        text = (
            f"<RunProposal {self.id[:6]} · {self.n} audits · {self.description!r} · pending>"
            if self.pending
            else f"<RunProposal {self.id[:6]} · "
            f"{'DENIED' if self.denied else f'approved · {self.n} audits'}>"
        )
        payload: RunProposalPayload = {
            "kind": "run_proposal",
            "description": self.description,
            "n": self.n,
            "n_per_seed": self.n_per_seed,
            "seeds": [{"id": f"s{i}", "text": s[:200]} for i, s in enumerate(self.seeds)],
            "config": {"model": self.model, "n_per_seed": self.n_per_seed, **self.config},
        }
        return self._bundle(text, payload)


# -- cite ---------------------------------------------------------------------


@dataclass
class Quote:
    """One supporting transcript excerpt: ``sample_id`` at turn ``at``.

    ``log`` is the ``.eval`` file the sample lives in, so ``FindingCard``'s
    *open* link can send ``{t:"import", path: log, sample_id}`` — quotes
    reference *finished* samples, hence a file path not a ``log_dir``.
    """

    sample_id: str
    at: int
    role: str
    text: str
    log: str = ""


def _as_quote(q: Any) -> Quote:
    """Normalise a WS-edited quote (dict) or an existing ``Quote``."""
    if isinstance(q, Quote):
        return q
    # Accept the pre-UI-AUDIT ``audit_id`` key during the transition.
    d = dict(q)
    if "sample_id" not in d and "audit_id" in d:
        d["sample_id"] = d.pop("audit_id")
    return Quote(**{k: d[k] for k in ("sample_id", "at", "role", "text", "log") if k in d})


@dataclass
class CiteProposal(BaseProposal):
    """The pre-sign gate card for ``wb.cite`` / the ``review_finding`` tool.

    ``resolve()`` receives ``{"signed": bool, "by": str, "edits":
    {"claim"?: str, "quotes"?: list}}``; the human may have reworded the
    claim or struck quotes. The resolved bundle carries ``verdict`` so the
    frontend can render the ``signed by … · claim edited`` header.
    """

    claim: str
    quotes: list[Quote]
    grades_ref: str | None
    description: str

    def resolve(self, verdict: Any) -> None:
        v = verdict if isinstance(verdict, dict) else {}
        self.verdict = v
        edits = v.get("edits") or {}
        if (c := edits.get("claim")) is not None:
            self.claim = str(c)
        if (qs := edits.get("quotes")) is not None:
            self.quotes = [_as_quote(q) for q in qs]

    @property
    def signed(self) -> bool:
        return bool(self.verdict and self.verdict.get("signed"))

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        v = self.verdict
        state = (
            "pending"
            if v is None
            else (f"signed by {v.get('by')}" if v.get("signed") else "REFUSED")
        )
        payload: CiteProposalPayload = {
            "kind": "cite_proposal",
            "claim": self.claim,
            "quotes": [cast("QuotePayload", vars(q)) for q in self.quotes],
            "grades_ref": self.grades_ref,
            "description": self.description,
        }
        return self._bundle(
            f"<CiteProposal {self.id[:6]} · {self.claim!r} · {state}>", payload
        )


@dataclass
class Finding:
    """A resolved cite — what the agent holds and the bundle exports.

    ``signed_by=None`` marks a refused cite (deny-as-return); callers test
    ``if finding.signed_by:`` rather than catching.
    """

    claim: str
    quotes: list[Quote]
    signed_by: str | None
    id: str = field(default_factory=lambda: uuid4().hex)

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        state = f"signed by {self.signed_by}" if self.signed_by else "unsigned"
        payload: FindingPayload = {
            "kind": "finding",
            "id": self.id,
            "claim": self.claim,
            "quotes": [cast("QuotePayload", vars(q)) for q in self.quotes],
            "signed_by": self.signed_by,
        }
        return wb_bundle(f"<Finding {self.id[:6]} · {self.claim!r} · {state}>", payload)


# -- shared cores (called by ``wb.*`` and ``tools.review_*``) -----------------


async def review_seeds(
    gate: Gate,
    seeds: Sequence[str],
    description: str,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Propose a seed list, block on the human's verdict, return it.

    The one construction path for ``RunProposal`` — ``wb.review_seeds`` calls
    this directly; the ``review_seeds`` tool wraps it in ``_turn(orch)`` and
    ``json.dumps``.
    """
    cfg = dict(config or {})
    prop = RunProposal(
        seeds=list(seeds),
        config=cfg,
        description=description,
        n_per_seed=int(cfg.get("n_per_seed", 1)),
        model=cfg.get("model"),
    )
    await gate(prop)
    return {
        "approved": not prop.denied,
        "seeds": prop.seeds,
        "reason": (prop.verdict or {}).get("reason"),
    }


async def cite(
    gate: Gate,
    claim: str,
    quotes: Sequence[Quote | dict[str, Any]],
    *,
    grades_ref: str | None = None,
    description: str,
) -> Finding:
    """Propose a finding, block on the human's signature, return it.

    Always gates (M1-NOTEBOOK.md: ``cite`` and ``ask_human`` are the
    always-block cases). The returned ``Finding`` gets its own ``id`` —
    the proposal's ``id`` is its ``display_id`` (stable slot, updates in
    place on resolve); reusing it here would make ``OrchTurn``'s payload-id
    dedup drop the last-expr ``FindingCard``.
    """
    prop = CiteProposal(
        claim=claim,
        quotes=[_as_quote(q) for q in quotes],
        grades_ref=grades_ref,
        description=description,
    )
    await gate(prop)
    # ``gate()`` has already called ``prop.resolve(verdict)`` — claim/quotes
    # now reflect any human edits; read the normalized ``prop.verdict``.
    signed_by = (prop.verdict.get("by") or None) if prop.signed else None
    return Finding(claim=prop.claim, quotes=prop.quotes, signed_by=signed_by)
