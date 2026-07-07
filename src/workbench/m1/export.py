"""P1.6 / P3 — session exports (markdown findings report + `.ipynb` notebook).

``export_findings_md`` reads the durable ``findings.jsonl`` (P0.6) and emits a
self-contained markdown document: a header with session id / date / model,
then one ``## {claim}`` section per signed finding with its description and
quote blocks. Quote citations resolve to relative inspect-view URLs so the
exported document links back into the workbench when served alongside it.

``export_ipynb`` (P3) walks the live orchestrator's ``state.messages`` and
emits an nbformat-v4 notebook: assistant prose → markdown cells; ``python``
tool calls → code cells whose ``outputs`` are the turn's ``DisplayEvent``
bundles (streams / display_data / execute_result / error, per Jupyter's
schema); ``bash`` → ``%%bash`` code cells; file/gate tools → collapsed
``<details>`` markdown blocks; user messages → blockquotes. Rich outputs
(plots, DataFrames, WB_MIME cards) survive because the kernel already speaks
IPython MIME bundles — the export is mostly a re-labelling.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING, Any
from urllib.parse import quote as urlquote
from uuid import uuid4

from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
)

from workbench.m1.proposals import Finding, Quote, load_findings
from workbench.m1.wire import STREAM_MIME, WB_MIME, DisplayEvent

if TYPE_CHECKING:
    from inspect_ai.tool import ToolCall

    from workbench.m1.orchestrator import Orchestrator
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
    if f.file_hashes:
        # P3 — anchor the finding to the exact seed/prompt version that
        # produced it: one ``path: sha`` line per ``write_file`` at cite time.
        rows = "\n".join(f"- `{p}`: `{h}`" for p, h in sorted(f.file_hashes.items()))
        lines += [
            "<details><summary>versions</summary>",
            "",
            rows,
            "",
            "</details>",
            "",
        ]
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


# -- P3: .ipynb export --------------------------------------------------------

#: Tools whose call+result render as a collapsed ``<details>`` markdown block
#: rather than a code cell — they have no meaningful executable ``source``.
_DETAILS_TOOLS = frozenset(
    {"read_file", "write_file", "edit_file", "ask_human", "review_seeds", "review_finding"}
)


def _md_cell(source: str) -> dict[str, Any]:
    return {"cell_type": "markdown", "id": uuid4().hex[:12], "metadata": {}, "source": source}


def _code_cell(source: str, outputs: list[dict[str, Any]], n: int) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "id": uuid4().hex[:12],
        "metadata": {},
        "execution_count": n,
        "source": source,
        "outputs": outputs,
    }


def _nb_output(ev: DisplayEvent, n: int) -> dict[str, Any]:
    """Map one kernel ``DisplayEvent`` to one nbformat output entry.

    The kernel already emits IPython MIME bundles, so this is mostly a
    re-labelling: ``STREAM_MIME`` → ``stream``; ``WB_MIME`` traceback →
    ``error``; anything with ``WB_MIME`` / ``text/html`` / an image →
    ``display_data``; a bare ``text/plain`` → ``execute_result``. ``STREAM_MIME``
    is stripped from ``data`` since it isn't a Jupyter-schema MIME key.
    """
    b = ev.bundle
    if (s := b.get(STREAM_MIME)) is not None:
        return {"output_type": "stream", "name": s["name"], "text": s["text"]}
    wb = b.get(WB_MIME) or {}
    if wb.get("kind") == "traceback":
        return {
            "output_type": "error",
            "ename": str(wb.get("ename", "Error")),
            "evalue": str(wb.get("evalue", "")),
            "traceback": str(wb.get("text", "")).splitlines() or [str(wb.get("evalue", ""))],
        }
    data = {k: v for k, v in b.items() if k != STREAM_MIME}
    if WB_MIME in data or "text/html" in data or any(k.startswith("image/") for k in data):
        return {"output_type": "display_data", "data": data, "metadata": ev.meta or {}}
    return {
        "output_type": "execute_result",
        "execution_count": n,
        "data": data or {"text/plain": ""},
        "metadata": ev.meta or {},
    }


def _turn_outputs(events: list[DisplayEvent], n: int) -> list[dict[str, Any]]:
    """nbformat ``outputs`` for one turn, with stable-id updates collapsed.

    Same collapsing rule as ``kernel._render_outputs``: a ``stable`` id
    appears once, at its first position, carrying its *last* bundle — so an
    ``AttachedRun`` that ticked 20× lands as one ``display_data`` with the
    final counters.
    """
    latest = {ev.id: ev for ev in events if ev.stable}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ev in events:
        if ev.stable:
            if ev.id in seen:
                continue
            seen.add(ev.id)
            out.append(_nb_output(latest[ev.id], n))
        else:
            out.append(_nb_output(ev, n))
    return out


def _details_cell(tc: ToolCall, result: str) -> dict[str, Any]:
    args = json.dumps(tc.arguments, indent=2, default=str)
    body = (
        f"<details><summary><code>{tc.function}</code></summary>\n\n"
        f"```json\n{args}\n```\n\n```\n{result or '(no output)'}\n```\n\n</details>"
    )
    return _md_cell(body)


def export_ipynb(orch: Orchestrator) -> dict[str, Any]:
    """Render the orchestrator conversation as an nbformat-v4 notebook dict.

    Walks ``orch.state.messages`` in order. Each assistant message
    contributes a markdown cell for its prose (if any) plus one cell per
    tool call: ``python`` → a code cell whose ``outputs`` are that turn's
    ``kernel.outputs`` bundles; ``bash`` → a ``%%bash`` code cell with the
    turn's stream/card outputs; everything else → a ``<details>`` markdown
    block with the call args and tool result. User messages become
    blockquotes; the system prompt is skipped.

    Turn-ids for a given assistant are recovered from ``orch._turn_msg``
    (each tool call allocated one via ``kernel.turn()`` / ``run_turn``).
    On a resumed session ``_turn_msg`` / ``kernel.outputs`` are empty for
    pre-restart turns; those cells fall back to the tool-result text as a
    single ``stream`` output so the code is still there, just without rich
    displays.
    """
    session = orch.session
    findings = load_findings(orch.session_dir)
    header = "\n".join(
        [
            "# Orchestrator session",
            "",
            f"- **Session:** `{session.session_id or '(unsaved)'}`",
            f"- **Model:** `{orch.model_name}`",
            f"- **Date:** {session.created_at.split('T')[0]}",
            f"- **Findings:** {len(findings)}",
        ]
    )
    cells: list[dict[str, Any]] = [_md_cell(header)]

    messages = list(orch.state.messages) if orch.state is not None else []
    tool_results: dict[str | None, ChatMessageTool] = {
        m.tool_call_id: m for m in messages if isinstance(m, ChatMessageTool)
    }
    # ``_turn_msg`` is ``{turn_id: assistant_msg_id}``; invert so each
    # assistant's tool_calls can be zipped against its allocated turn ids.
    msg_turns: dict[str, list[int]] = defaultdict(list)
    for tid, mid in sorted(orch._turn_msg.items()):  # noqa: SLF001
        msg_turns[mid].append(tid)

    exec_n = 0
    for m in messages:
        if isinstance(m, ChatMessageSystem):
            continue
        if isinstance(m, ChatMessageUser):
            quoted = "\n".join(f"> {line}" for line in m.text.splitlines()) or "> …"
            cells.append(_md_cell(f"**user:**\n{quoted}"))
            continue
        if not isinstance(m, ChatMessageAssistant):
            continue
        if m.text.strip():
            cells.append(_md_cell(m.text))
        tids = iter(msg_turns.get(m.id or "", []))
        for tc in m.tool_calls or []:
            tid = next(tids, None)
            result = tool_results.get(tc.id)
            result_text = result.text if result is not None else ""
            if tc.function == "python":
                exec_n += 1
                evs = orch.kernel.outputs.get(tid, []) if tid is not None else []
                outs = (
                    _turn_outputs(evs, exec_n)
                    if evs
                    else [{"output_type": "stream", "name": "stdout", "text": result_text}]
                )
                cells.append(_code_cell(str(tc.arguments.get("code", "")), outs, exec_n))
            elif tc.function == "bash":
                exec_n += 1
                evs = orch.kernel.outputs.get(tid, []) if tid is not None else []
                outs = (
                    _turn_outputs(evs, exec_n)
                    if evs
                    else [{"output_type": "stream", "name": "stdout", "text": result_text}]
                )
                src = "%%bash\n" + str(tc.arguments.get("cmd", ""))
                cells.append(_code_cell(src, outs, exec_n))
            elif tc.function in _DETAILS_TOOLS:
                cells.append(_details_cell(tc, result_text))
            else:
                cells.append(_details_cell(tc, result_text))

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
            "language_info": {"name": "python"},
            "workbench": {"session_id": session.session_id, "model": orch.model_name},
        },
        "cells": cells,
    }
