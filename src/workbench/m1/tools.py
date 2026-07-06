"""M1 hybrid tool surface (M1-HYBRID.md §Tool surface, migration step 2).

Seven inspect ``Tool`` s the orchestrator agent gets alongside ``python``:

- ``bash`` — subprocess shell in the per-orchestrator ``session_dir``
  (extracted to :mod:`workbench.m1.bash_tool`).
- ``read_file`` / ``write_file`` / ``edit_file`` — resolved relative to
  ``session_dir``; plain string returns, no display side-effects.
- ``ask_human`` / ``review_seeds`` / ``review_finding`` — thin wrappers
  around ``orch.gate(proposal)`` (same ``Gate`` primitive as in-cell
  ``wb.ask_human`` etc., just exposed at the tool level).

All seven are closures over the ``Orchestrator`` (for ``.kernel`` and
``.span_id``); ``make_tools(orch)`` returns the configured list.
Registration on ``orchestrator_agent`` is migration step 4.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from inspect_ai.tool import Tool, tool

from workbench.m1 import proposals
from workbench.m1.bash_tool import _turn, make_bash_tool
from workbench.m1.proposals import Prompt

if TYPE_CHECKING:
    from workbench.m1.orchestrator import Orchestrator


def make_tools(orch: Orchestrator) -> list[Tool]:
    """The 7 non-``python`` hybrid tools, each a closure over ``orch``:
    ``bash`` / ``read_file`` / ``write_file`` / ``edit_file`` /
    ``ask_human`` / ``review_seeds`` / ``review_finding``. The 8th tool,
    ``python``, lives in ``orchestrator.python_tool``."""
    session_dir = orch.session_dir

    # -- file tools -----------------------------------------------------------

    def _resolve(path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else session_dir / p

    @tool
    def read_file() -> Tool:
        async def execute(path: str, offset: int = 0, limit: int = 2000) -> str:
            """Read a file as numbered lines.

            Args:
                path: Path (relative to the session directory, or absolute).
                offset: 0-based line to start from.
                limit: Maximum number of lines to return.
            """
            p = _resolve(path)
            if not p.exists():
                return f"[error: {path} not found]"
            lines = p.read_text().splitlines()
            end = offset + limit
            body = "\n".join(
                f"{i + 1:6d}\t{line}" for i, line in enumerate(lines[offset:end], offset)
            )
            more = f"\n[… {len(lines) - end} more lines]" if len(lines) > end else ""
            return body + more if body else "[empty file]"

        return execute

    @tool
    def write_file() -> Tool:
        async def execute(path: str, content: str) -> str:
            """Write ``content`` to ``path`` (overwriting).

            Args:
                path: Path (relative to the session directory, or absolute).
                content: File contents.
            """
            p = _resolve(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"[wrote {len(content.encode())} bytes → {path}]"

        return execute

    @tool
    def edit_file() -> Tool:
        async def execute(path: str, old: str, new: str) -> str:
            """Replace one exact occurrence of ``old`` with ``new`` in ``path``.

            Args:
                path: Path (relative to the session directory, or absolute).
                old: Exact substring to replace (must appear exactly once).
                new: Replacement text.
            """
            p = _resolve(path)
            if not p.exists():
                return f"[error: {path} not found]"
            src = p.read_text()
            n = src.count(old)
            if n == 0:
                return f"[error: {old!r} not found in {path}]"
            if n > 1:
                return f"[error: {old!r} appears {n} times in {path} — be more specific]"
            dst = src.replace(old, new, 1)
            p.write_text(dst)
            diff = "".join(
                difflib.unified_diff(
                    src.splitlines(keepends=True),
                    dst.splitlines(keepends=True),
                    fromfile=path,
                    tofile=path,
                    n=2,
                )
            )
            return diff or f"[edited {path} · no visible diff]"

        return execute

    # -- review tools (thin gate wrappers) ------------------------------------

    @tool
    def ask_human() -> Tool:
        async def execute(question: str, options: list[str] | None = None) -> str:
            """Ask the human operator a question and block until answered.

            Args:
                question: The question to render on the gate card.
                options: Optional fixed choices (rendered as buttons).
            """
            with _turn(orch):
                return str(await orch.gate(Prompt(question, options)))

        return execute

    @tool
    def review_seeds() -> Tool:
        async def execute(
            seeds: list[str], description: str, config: dict[str, Any] | None = None
        ) -> str:
            """Propose a seed list for human approval before launching a run.

            The human may strike seeds or deny outright. Returns the
            (possibly-trimmed) seed list and approval state; only proceed
            with the run if ``approved``.

            Args:
                seeds: Seed instructions to run.
                description: One-line rationale for the run.
                config: Run config (``model``, ``max_turns``, ``n_per_seed``, …).
            """
            with _turn(orch):
                result = await proposals.review_seeds(
                    orch.gate, seeds, description, config
                )
            # inspect's ``ToolResult`` doesn't include ``dict`` — encode.
            return json.dumps(result)

        return execute

    @tool
    def review_finding() -> Tool:
        async def execute(
            claim: str, quotes: list[dict[str, Any]], description: str
        ) -> str:
            """Propose a finding for the human to sign off on.

            Args:
                claim: The claim being made.
                quotes: Supporting transcript excerpts
                    (each ``{"sample_id","at","role","text"}``).
                description: Context for the reviewer.
            """
            with _turn(orch):
                finding = await proposals.cite(
                    orch.gate, claim, quotes, description=description
                )
            return json.dumps({
                "signed": finding.signed_by is not None,
                "by": finding.signed_by,
                "claim": finding.claim,
                "quotes": [vars(q) for q in finding.quotes],
            })

        return execute

    return [
        make_bash_tool(orch),
        read_file(),
        write_file(),
        edit_file(),
        ask_human(),
        review_seeds(),
        review_finding(),
    ]
