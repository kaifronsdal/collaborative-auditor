"""Step-gate mixin shared by ``Branch`` (M0) and ``Orchestrator`` (M1).

Both drive an agent loop that awaits a one-shot ``anyio.Event`` each turn.
``step()`` releases one turn; ``play()`` sets a free-running flag so the loop
re-arms the gate itself after each turn (self-perpetuating without a pump
task); ``pause()`` clears the flag so the next await blocks. The wait/re-arm
pair lives here as ``_await_gate`` / ``_rearm_if_playing`` so the two agent
loops (``workbench_auditor``, ``orchestrator_agent``) don't each carry the
``_gate = anyio.Event()`` reset + ``# noqa: SLF001`` littering.
"""

from __future__ import annotations

import anyio

from workbench.view import Status


class StepGated:
    status: Status

    def _init_gate(self) -> None:
        self._gate = anyio.Event()
        self._free_running = False

    def step(self) -> None:
        """Release one turn."""
        if self.status != "ended":
            self.status = "running"
        self._gate.set()

    def play(self) -> None:
        """Run freely: each turn re-arms the gate itself until ``pause()``."""
        self._free_running = True
        if self.status != "ended":
            self.status = "running"
        self._gate.set()

    def pause(self) -> None:
        """Stop after the current turn; the next gate wait blocks."""
        self._free_running = False
        if self.status != "ended":
            self.status = "paused"

    async def _await_gate(self) -> None:
        await self._gate.wait()
        self._gate = anyio.Event()

    def _rearm_if_playing(self) -> None:
        if self._free_running:
            self._gate.set()
