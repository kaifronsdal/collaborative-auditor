"""Thin pytest wrap over ``workbench._smoke_m1_kernel``."""

import pytest

from workbench._smoke_m1_kernel import _amain, _check_short_repr

pytestmark = pytest.mark.anyio


async def test_kernel() -> None:
    await _amain()


async def test_short_repr() -> None:
    # Also exercised by ``_amain`` above, but cheap enough to run standalone
    # so a ``short_repr`` regression is reported under its own test id.
    await _check_short_repr()
