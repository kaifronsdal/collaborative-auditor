"""Parametrised pytest wrappers over ``workbench._smoke_m1_*._amain``.

Each smoke keeps its ``__main__`` block (dual-mode); this file just gives
pytest one test id per smoke. ``slow``-marked entries spawn ``inspect eval``
subprocesses and are deselected by default (``addopts = -m 'not slow …'``).
"""

from __future__ import annotations

import importlib

import pytest

slow = pytest.mark.slow

SMOKES = [
    pytest.param("_smoke_m1_orchestrator", id="orchestrator"),
    pytest.param("_smoke_m1_streaming", id="streaming"),
    pytest.param("_smoke_m1_scan", id="scan"),
    pytest.param("_smoke_m1_hybrid", id="hybrid", marks=slow),
    pytest.param("_smoke_m1_coverage", id="coverage", marks=slow),
]


@pytest.mark.anyio
@pytest.mark.parametrize("mod", SMOKES)
async def test_smoke(mod: str) -> None:
    await importlib.import_module(f"workbench.{mod}")._amain()
