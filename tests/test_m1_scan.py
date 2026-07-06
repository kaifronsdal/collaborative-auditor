"""Thin pytest wrap over ``workbench._smoke_m1_scan``."""

import pytest

from workbench._smoke_m1_scan import _amain

pytestmark = pytest.mark.anyio


async def test_scan() -> None:
    await _amain()
