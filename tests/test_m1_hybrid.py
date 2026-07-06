"""Thin pytest wrap over ``workbench._smoke_m1_hybrid`` (subprocess-heavy)."""

import pytest

from workbench._smoke_m1_hybrid import _amain

pytestmark = [pytest.mark.anyio, pytest.mark.slow]


async def test_hybrid() -> None:
    await _amain()
