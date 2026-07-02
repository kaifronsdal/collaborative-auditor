"""M1 — ``wb.cite`` (M1-NOTEBOOK.md §Revised surface, scenario-e).

A ``cite`` is a hard-gated finding: the orchestrator proposes a *claim*
backed by transcript ``Quote`` s (and optionally a grades parquet); the
human signs, edits, or refuses. Same ``display → await Future → update``
shape as ``Prompt`` / ``RunProposal`` — the resolved ``Finding`` is what the
agent binds and what lands in the sidebar bundle.

Deny is a return, not an exception (matches ``run_audits``): a refused
cite comes back as a ``Finding`` with ``signed_by=None`` so the cell keeps
running and the agent can react in-turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from workbench.m1.kernel import WB_MIME, Gate


# -- data ---------------------------------------------------------------------


@dataclass
class Quote:
    """One supporting transcript excerpt: ``sample_id`` at turn ``at``."""

    sample_id: str
    at: int
    role: str
    text: str


def _as_quote(q: Any) -> Quote:
    """Normalise a WS-edited quote (dict) or an existing ``Quote``."""
    if isinstance(q, Quote):
        return q
    # Accept the pre-UI-AUDIT ``audit_id`` key during the transition.
    d = dict(q)
    if "sample_id" not in d and "audit_id" in d:
        d["sample_id"] = d.pop("audit_id")
    return Quote(**d)


# -- proposal (gated card) ----------------------------------------------------


@dataclass
class CiteProposal:
    """The pre-sign gate card for ``wb.cite``.

    ``resolve()`` receives ``{"signed": bool, "by": str, "edits":
    {"claim"?: str, "quotes"?: list}}``; the human may have reworded the
    claim or struck quotes. The resolved bundle carries ``verdict`` so the
    frontend can render the ``signed by … · claim edited`` header
    (scenario-e turn 3).
    """

    claim: str
    quotes: list[Quote]
    grades_ref: str | None
    description: str
    id: str = field(default_factory=lambda: uuid4().hex)
    verdict: dict[str, Any] | None = None

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
        pending = v is None
        state = (
            "pending"
            if v is None
            else (f"signed by {v.get('by')}" if v.get("signed") else "REFUSED")
        )
        text = f"<CiteProposal {self.id[:6]} · {self.claim!r} · {state}>"
        return {
            "text/plain": text,
            WB_MIME: {
                "kind": "cite_proposal",
                "id": self.id,
                "claim": self.claim,
                "quotes": [vars(q) for q in self.quotes],
                "grades_ref": self.grades_ref,
                "description": self.description,
                "pending": pending,
                "verdict": self.verdict,
            },
        }


# -- finding (resolved form) --------------------------------------------------


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
        return {
            "text/plain": f"<Finding {self.id[:6]} · {self.claim!r} · {state}>",
            WB_MIME: {
                "kind": "finding",
                "id": self.id,
                "claim": self.claim,
                "quotes": [vars(q) for q in self.quotes],
                "signed_by": self.signed_by,
            },
        }


# -- helper -------------------------------------------------------------------


async def cite(
    gate: "Gate",
    claim: str,
    quotes: list[Quote],
    *,
    grades_ref: str | None = None,
    description: str,
) -> Finding:
    """Propose a finding, block on the human's signature, return it.

    Always gates (M1-NOTEBOOK.md: ``cite`` and ``ask_human`` are the
    always-block cases). The returned ``Finding`` reuses the proposal's
    ``id`` so a later ``display(finding, display_id=finding.id)`` updates
    the same card slot.
    """
    quotes = [_as_quote(q) for q in quotes]
    prop = CiteProposal(
        claim=claim,
        quotes=quotes,
        grades_ref=grades_ref,
        description=description,
    )
    await gate(prop)
    # ``gate()`` has already called ``prop.resolve(verdict)`` — claim/quotes
    # now reflect any human edits; read the normalized ``prop.verdict``.
    signed_by = (prop.verdict.get("by") or None) if prop.signed else None
    return Finding(
        claim=prop.claim,
        quotes=prop.quotes,
        signed_by=signed_by,
        id=prop.id,
    )
