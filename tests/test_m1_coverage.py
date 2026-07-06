"""Thin pytest wrap over ``workbench._smoke_m1_coverage`` (subprocess-heavy)."""

import pytest

from workbench._smoke_m1_coverage import _amain

pytestmark = [pytest.mark.anyio, pytest.mark.slow]


async def test_coverage() -> None:
    await _amain()
