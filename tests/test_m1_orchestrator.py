"""Thin pytest wrap over ``workbench._smoke_m1_orchestrator``."""

import pytest

from workbench._smoke_m1_orchestrator import _amain

pytestmark = pytest.mark.anyio


async def test_orchestrator_wire_and_persist() -> None:
    await _amain()
