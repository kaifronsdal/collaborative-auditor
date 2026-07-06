"""Pytest fixtures for the M1 smoke wrappers (M1-REFACTOR.md Batch H).

The ``tests/test_m1_*.py`` files are thin wrappers over the existing
``workbench._smoke_m1_*._amain`` entrypoints (which keep their ``__main__``
blocks — dual-mode).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

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
