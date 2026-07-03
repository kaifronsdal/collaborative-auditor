"""Subprocess entrypoint for M1-hybrid audit runs.

The orchestrator's ``bash`` tool runs::

    WORKBENCH_DISPLAY=1 inspect eval \
        <workbench>/src/workbench/m1/_audit_task.py@audit \
        -T seeds_file=seeds.json -T config='{"max_turns":30}' \
        --model <target> --model-role target=<target> \
        --model-role auditor=<m> --model-role judge=<m> \
        --log-dir runs/<name>

which builds the same ``Task`` that ``wb.run_audits`` used to launch
in-process (``seeds_dataset`` / ``audit_solver(workbench_auditor(...))`` /
``audit_judge`` / ``audit_viewer``), minus the ``BatchHooks`` steer/stop
plumbing — the subprocess has no live channel back to the kernel, so the
auditor's per-turn hooks are inert. Model roles come from ``--model-role``
CLI flags, not the task.

``demo`` is a trivial task for verifying the ``WorkbenchDisplay`` driver
without petri (petri's auditor tools reject ``mockllm`` output before any
sample completes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import shortuuid
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessage
from inspect_ai.scorer import Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, Solver, TaskState, solver

from workbench.auditor import TurnHooks, workbench_auditor

# Belt-and-suspenders: entry-point loading happens inside ``eval_async``
# (model-provider lookup), which is *after* the task module is imported by
# ``resolve_tasks`` — but an editable install may not have the entry point
# yet. Importing here installs the display either way.
from workbench.m1.wb_display import register

register()


class _NoHooks(TurnHooks):
    """Inert per-turn hooks: no gate, no injected messages, never stop."""

    async def pre_turn(self) -> tuple[list[ChatMessage], bool]:
        return [], False

    def post_generate(self) -> None:
        pass


@task
def audit(seeds_file: str, config: str | dict[str, Any] = "{}") -> Task:
    """Petri audit batch — the subprocess replacement for ``wb.run_audits``.

    Args:
        seeds_file: Path to a JSON file containing ``list[str]`` seed
            instructions.
        config: ``dict`` (or JSON-encoded ``dict`` — inspect's ``-T`` parser
            YAML-decodes the CLI value so ``{"k":v}`` arrives as a dict
            already). Keys: ``max_turns`` / ``compaction`` /
            ``realism_filter`` / ``judge_dimensions`` (same as
            ``wb.run_audits`` accepted).
    """
    from inspect_petri import (  # noqa: PLC0415
        audit_judge,
        audit_solver,
        audit_viewer,
        seeds_dataset,
        target_agent,
    )

    seeds: list[str] = json.loads(Path(seeds_file).read_text())
    cfg: dict[str, Any] = (
        json.loads(config) if isinstance(config, str) else dict(config)
    )

    auditor = workbench_auditor(
        _NoHooks(),
        max_turns=int(cfg.pop("max_turns", 30)),
        compaction=cfg.pop("compaction", True),
        realism_filter=cfg.pop("realism_filter", False),
    )
    return Task(
        dataset=seeds_dataset(seeds),
        solver=audit_solver(auditor=auditor, target=target_agent()),
        scorer=audit_judge(cfg.get("judge_dimensions")),
        viewer=audit_viewer(cfg.get("judge_dimensions")),
        name=f"audit-{shortuuid.uuid()[:6]}",
    )


# ---------------------------------------------------------------------------
# Display-driver smoke — runs under mockllm.
# ---------------------------------------------------------------------------


@solver
def _echo() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        return await generate(state)

    return solve


@scorer(metrics=[accuracy()])
def _always_one() -> Any:
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value=1)

    return score


@task
def demo(n: int = 3) -> Task:
    """Trivial task: N samples, one generate, constant score. Enough to
    exercise ``eval_start`` / ``eval_progress`` / ``eval_sample_done`` /
    ``eval_done`` under ``mockllm/model``."""
    return Task(
        dataset=[Sample(input=f"seed {i}", id=f"s{i}") for i in range(n)],
        solver=_echo(),
        scorer=_always_one(),
    )
