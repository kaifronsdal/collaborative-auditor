"""P1.6 — render ``findings.jsonl`` as a markdown report.

The whole point of an audit is the write-up. ``export_findings_md`` reads the
durable store (P0.6) and emits a self-contained markdown document: a header
with session id / date / model, then one ``## {claim}`` section per signed
finding with its description and quote blocks. Quote citations resolve to
relative inspect-view URLs (``/logs/{log}?sample={id}``) so the exported
document links back into the workbench when served alongside it, and stays a
readable ``[sample_id · turn N](…)`` label when it isn't.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote as urlquote

from workbench.m1.proposals import Finding, Quote, load_findings

if TYPE_CHECKING:
    from workbench.session import Session


def _inspect_view_url(q: Quote) -> str:
    """Relative inspect-view link for one quote's transcript location.

    Mirrors the ``{t:"import", path: log, sample_id}`` shape the frontend
    uses (``FindingCard`` / ``ReaderCard``) — quotes reference *finished*
    samples, hence a ``.eval`` file path not a ``log_dir``. There's no
    ``/logs`` route yet; the URL degrades to a labelled link until one
    exists.
    """
    if not q.log:
        return ""
    return f"/logs/{urlquote(q.log, safe='')}?sample={urlquote(q.sample_id)}"


def _quote_md(q: Quote) -> str:
    label = f"{q.sample_id} · turn {q.at}"
    url = _inspect_view_url(q)
    cite = f"[{label}]({url})" if url else f"`{label}`"
    body = "\n".join(f"> {line}" for line in q.text.splitlines()) or "> …"
    return f"{body}\n>\n> — {q.role}, {cite}"


def _finding_md(f: Finding) -> str:
    lines = [f"## {f.claim}", ""]
    if f.description:
        lines += [f.description, ""]
    for q in f.quotes:
        lines += [_quote_md(q), ""]
    meta = " · ".join(
        s
        for s in (
            f"signed by {f.signed_by}" if f.signed_by else None,
            f.signed_at.split("T")[0] if f.signed_at else None,
        )
        if s
    )
    if meta:
        lines += [f"*{meta}*", ""]
    return "\n".join(lines)


def export_findings_md(session: Session) -> str:
    """Render every signed finding under ``session.orchestrator.session_dir``.

    Returns a stub document (header + "no findings yet") when the
    orchestrator hasn't started or nothing has been signed — the sidebar
    link is always live.
    """
    orch = session.orchestrator
    findings: list[Finding] = load_findings(orch.session_dir) if orch else []
    header = [
        "# Audit findings",
        "",
        f"- **Session:** `{session.session_id or '(unsaved)'}`",
        f"- **Date:** {session.created_at.split('T')[0]}",
    ]
    if orch is not None:
        header.append(f"- **Orchestrator model:** `{orch.model_name}`")
    header += [f"- **Findings:** {len(findings)}", ""]
    if not findings:
        return "\n".join([*header, "_No signed findings yet._", ""])
    body = "\n".join(_finding_md(f) for f in findings)
    return "\n".join(header) + "\n" + body
