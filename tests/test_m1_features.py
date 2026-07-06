"""Thin pytest wrap over ``workbench._smoke_m1_features``."""

import pytest

from workbench._smoke_m1_features import (
    _test_interrupt_and_send,
    _test_rewind_and_persist,
)

pytestmark = pytest.mark.anyio


async def test_rewind_and_persist() -> None:
    await _test_rewind_and_persist()


async def test_interrupt_and_send() -> None:
    await _test_interrupt_and_send()
