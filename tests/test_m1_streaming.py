"""Thin pytest wrap over ``workbench._smoke_m1_streaming``."""

import pytest

from workbench._smoke_m1_streaming import _amain

pytestmark = pytest.mark.anyio


async def test_streaming() -> None:
    await _amain()
