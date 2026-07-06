"""Pytest fixtures for the M1 smoke wrappers (M1-REFACTOR.md Batch H).

The ``tests/test_m1_*.py`` files are thin wrappers over the existing
``workbench._smoke_m1_*._amain`` entrypoints (which keep their ``__main__``
blocks — dual-mode). Fixtures here are for future finer-grained tests that
want a shared kernel or a parametrised ``mock_orch_session`` without paying
the ``_prewarm``/``InteractiveShell`` startup per test.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest

from workbench.m1._fixtures import FakeConn, mock_orch_session
from workbench.m1.kernel import OrchestratorKernel
from workbench.m1.orchestrator import Orchestrator, _prewarm
from workbench.session import Session

# ── anyio plugin ────────────────────────────────────────────────────────────
# The smokes use ``asyncio`` directly (``asyncio.create_task``,
# ``asyncio.get_running_loop``), so pin the backend rather than letting anyio
# parametrise over trio.


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


# ── cwd guard ───────────────────────────────────────────────────────────────
# ``Orchestrator.__init__`` does ``os.chdir(session_dir)``. ``mock_orch_session``
# restores it, but a couple of ``_amain``s (``Session.load`` in the persistence
# checks, ``_check_server_handlers``) construct an ``Orchestrator`` outside that
# context manager. Snapshot/restore per test so one smoke can't strand the next
# in a deleted directory.


@pytest.fixture(autouse=True)
def _restore_cwd() -> Iterator[None]:
    orig = os.getcwd()
    try:
        yield
    finally:
        os.chdir(orig)


# ── shared kernel ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def kernel() -> Iterator[OrchestratorKernel]:
    """A prewarmed ``OrchestratorKernel`` shared across a test module.

    ``_prewarm`` silences inspect/scout progress + pays the aisitools banner
    once; ``OrchestratorKernel.__enter__`` builds the ``InteractiveShell`` and
    installs the display hooks. Both are expensive enough (~0.5s) to be worth
    module scope for a suite that splits one smoke into per-check tests.
    """
    _prewarm()
    with OrchestratorKernel() as k:
        yield k


# ── scripted orchestrator session ───────────────────────────────────────────


@pytest.fixture
async def orch_session(
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[Session, Orchestrator, FakeConn]]:
    """``mock_orch_session`` parametrised indirectly on the turn script.

    Use as::

        @pytest.mark.parametrize("orch_session", [TURNS], indirect=True)
        async def test_x(orch_session):
            session, orch, conn = orch_session

    ``request.param`` may be the ``turns`` list/callable directly, or a
    ``dict`` of ``mock_orch_session`` kwargs (``turns``, ``max_turns``, …).
    """
    param = getattr(request, "param", [])
    kw = param if isinstance(param, dict) else {"turns": param}
    async with mock_orch_session(**kw) as triple:
        yield triple


# ── e2e CLI options ─────────────────────────────────────────────────────────


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-model",
        default="anthropic/claude-opus-4-8",
        help="orchestrator model for test_m1_e2e_real",
    )
    parser.addoption(
        "--e2e-target",
        default="anthropic/claude-haiku-4-5",
        help="target/auditor/judge model for the subprocess audits",
    )
