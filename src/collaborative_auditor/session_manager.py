"""Session runtime state and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import anyio

from collaborative_auditor.auditor import initialize_auditor_messages
from collaborative_auditor.models import Branch, Session, create_branch, create_session
from collaborative_auditor.session_store import SessionStore

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger(__name__)


class PlaybackState(StrEnum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    STEPPING = "stepping"


@dataclass
class SessionRuntime:
    session: Session
    manager: SessionManager
    connections: list[WebSocket] = field(default_factory=list)
    playback_state: PlaybackState = PlaybackState.IDLE
    current_branch_index: int = 0
    version: int = 0
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    generation_scope: anyio.CancelScope | None = None
    generation_task: asyncio.Task[Any] | None = None
    pending_feedback: list[str] = field(default_factory=list)
    last_sent_view_state: dict[str, Any] | None = None

    @property
    def current_branch(self) -> Branch:
        return self.session.branches[self.current_branch_index]

    @property
    def is_generating(self) -> bool:
        return self.generation_scope is not None

    def fork_to_new_branch(self, fork_event_id: str) -> Branch:
        """Create a new branch at *fork_event_id* and switch to it.

        Returns the newly created branch.
        """
        new_branch = create_branch(self.session, fork_event_id)
        self.current_branch_index = len(self.session.branches) - 1
        return new_branch

    async def cancel_generation(self) -> None:
        """Cancel any in-progress generation on this runtime."""
        if self.generation_scope is not None:
            self.generation_scope.cancel()
        if self.generation_task is not None:
            try:
                await self.generation_task
            except (asyncio.CancelledError, Exception):
                pass
            self.generation_task = None
        self.generation_scope = None

    async def save(self) -> None:
        """Persist this session to disk."""
        await anyio.to_thread.run_sync(self.manager._store.save, self.session)

    def launch_generation(self, coro: Coroutine[Any, Any, None]) -> None:
        """Start a background generation task from a coroutine."""
        self.generation_task = asyncio.create_task(coro)


class SessionManager:
    """Registry and factory for session runtimes.

    Owns the runtime dict and the persistence store.  Lifecycle operations
    that need an ID lookup (e.g. WebSocket disconnect cleanup) live here;
    everything else lives on SessionRuntime.
    """

    def __init__(self, store: SessionStore | None = None) -> None:
        self._runtimes: dict[str, SessionRuntime] = {}
        self._store = store or SessionStore()

    def get_runtime(self, session_id: str) -> SessionRuntime | None:
        return self._runtimes.get(session_id)

    async def get_or_load(self, session_id: str) -> SessionRuntime | None:
        runtime = self._runtimes.get(session_id)
        if runtime is not None:
            return runtime
        if not self._store.exists(session_id):
            return None
        session = await anyio.to_thread.run_sync(self._store.load, session_id)
        runtime = SessionRuntime(session=session, manager=self)
        self._runtimes[session_id] = runtime
        return runtime

    async def create_session(
        self,
        session_id: str,
        initial_prompt: str,
        auditor_model: str,
        target_model: str,
    ) -> SessionRuntime:
        session = create_session(
            initial_prompt=initial_prompt,
            auditor_model=auditor_model,
            target_model=target_model,
            session_id=session_id,
        )
        initialize_auditor_messages(session, session.branches[0])
        runtime = SessionRuntime(session=session, manager=self)
        self._runtimes[session_id] = runtime
        return runtime

    async def delete_session(self, session_id: str) -> None:
        runtime = self._runtimes.pop(session_id, None)
        if runtime is not None:
            await runtime.cancel_generation()
            for ws in list(runtime.connections):
                try:
                    await ws.close()
                except Exception:
                    pass
            runtime.connections.clear()
        await anyio.to_thread.run_sync(self._store.delete, session_id)

    def register_connection(self, session_id: str, ws: WebSocket) -> None:
        runtime = self._runtimes.get(session_id)
        if runtime is not None and ws not in runtime.connections:
            runtime.connections.append(ws)

    def unregister_connection(self, session_id: str, ws: WebSocket) -> None:
        runtime = self._runtimes.get(session_id)
        if runtime is not None:
            try:
                runtime.connections.remove(ws)
            except ValueError:
                pass

    def list_summaries(self) -> list:
        """Return persisted session summaries (delegates to store)."""
        return self._store.list_summaries()

    def session_exists_on_disk(self, session_id: str) -> bool:
        """Check whether a session file exists on disk.

        Raises ValueError for invalid session ID formats.
        """
        return self._store.exists(session_id)
