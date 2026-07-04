"""Subprocess entrypoint for M1-hybrid audit runs.

The orchestrator's ``bash`` tool runs::

    WORKBENCH_DISPLAY=1 inspect eval \
        <workbench>/src/workbench/m1/_audit_task.py@audit \
        -T seeds_file=seeds.json -T config='{"max_turns":30}' \
        --model <target> --model-role target=<target> \
        --model-role auditor=<m> --model-role judge=<m> \
        --log-dir runs/<name>

which is petri's own ``inspect_petri.audit`` task with the seeds/config
threaded through. The subprocess has no live channel back to the kernel,
so no per-turn hooks — petri's auditor writes ``AuditTape.trajectories``
into the sample store, which is exactly what ``import_eval`` reads. Model
roles come from ``--model-role`` CLI flags, not the task.

``demo`` is a trivial task for verifying the ``WorkbenchDisplay`` driver
without petri (petri's auditor tools reject ``mockllm`` output before any
sample completes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, Solver, TaskState, solver

# Belt-and-suspenders: entry-point loading happens inside ``eval_async``
# (model-provider lookup), which is *after* the task module is imported by
# ``resolve_tasks`` — but an editable install may not have the entry point
# yet. Importing here installs the display either way.
from workbench.m1.wb_display import register

register()


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
    import inspect_petri  # noqa: PLC0415

    cfg: dict[str, Any] = (
        json.loads(config) if isinstance(config, str) else dict(config)
    )
    return inspect_petri.audit(
        seed_instructions=json.loads(Path(seeds_file).read_text()),
        max_turns=int(cfg.get("max_turns", 30)),
        compaction=cfg.get("compaction", True),
        realism_filter=cfg.get("realism_filter", False),
        judge_dimensions=cfg.get("judge_dimensions"),
    )


# ---------------------------------------------------------------------------
# Display-driver smoke — runs under mockllm.
# ---------------------------------------------------------------------------


@solver
def _slow(turns: int = 1, turn_sleep: float = 0.0, fail_on: str = "") -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        if fail_on and str(state.sample_id) == fail_on:
            raise RuntimeError(f"deliberate failure on {fail_on}")
        for _ in range(turns):
            if turn_sleep:
                await anyio.sleep(turn_sleep)
            state = await generate(state)
        return state

    return solve


@scorer(metrics=[accuracy()])
def _always_one() -> Any:
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value=1)

    return score


@task
def demo(n: int = 3, turns: int = 1, turn_sleep: float = 0.0, fail_on: str = "") -> Task:
    """Trivial task: N samples, ``turns`` generates each with an optional
    per-turn sleep, constant score. With ``turn_sleep=0`` (default) it
    exercises ``eval_start`` / ``eval_progress`` / ``eval_sample_done`` /
    ``eval_done`` under ``mockllm/model``; with ``turns>1, turn_sleep>0``
    it holds samples running long enough for the ACP-interrupt smoke.
    ``fail_on="s1"`` makes that one sample raise (erroring-sample coverage)."""
    return Task(
        dataset=[Sample(input=f"seed {i}", id=f"s{i}") for i in range(n)],
        solver=_slow(turns, turn_sleep, fail_on),
        scorer=_always_one(),
    )
