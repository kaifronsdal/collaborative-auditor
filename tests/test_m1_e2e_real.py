"""Thin pytest wrap over ``workbench._e2e_m1_real`` (real Anthropic API).

Deselected by default (``-m 'not real_model'`` in ``addopts``). Run on a
worker VM with::

    uv run pytest tests/test_m1_e2e_real.py -m real_model \
        --e2e-model anthropic/claude-opus-4-8 \
        --e2e-target anthropic/claude-haiku-4-5
"""

import pytest

from workbench._e2e_m1_real import _amain

pytestmark = [pytest.mark.anyio, pytest.mark.real_model]


async def test_e2e_real(pytestconfig: pytest.Config) -> None:
    await _amain(
        model=pytestconfig.getoption("--e2e-model"),
        target=pytestconfig.getoption("--e2e-target"),
        keep=False,
    )
