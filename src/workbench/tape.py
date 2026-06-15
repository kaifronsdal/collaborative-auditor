"""Tape: minimal level-2 record/replay per design/resampling.md.

Spike-1 scope: `_serve` only — enough to wrap auditor model calls and prove
τ₂.log records and JSON-roundtrips. `wrap`/`_mark` (the level composition)
land in spike 3/4.
"""

from __future__ import annotations

import copy
import functools
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from inspect_ai.model import ModelOutput

Sync = Literal["in", "out"] | None


@dataclass(slots=True)
class Step:
    value: Any | None
    source: str  # (role, qualname) joined — desync guard; role disambiguates Model.generate
    sync: Sync = None
    message_id: str | None = None

    def dump(self) -> dict[str, Any]:
        v = self.value
        if isinstance(v, ModelOutput):
            v = {"__model_output__": v.model_dump()}
        return {"value": v, "source": self.source, "sync": self.sync, "message_id": self.message_id}

    @staticmethod
    def load(d: dict[str, Any]) -> Step:
        v = d["value"]
        if isinstance(v, dict) and "__model_output__" in v:
            v = ModelOutput.model_validate(v["__model_output__"])
        return Step(value=v, source=d["source"], sync=d["sync"], message_id=d["message_id"])


def _message_id_of(v: Any) -> str | None:
    if isinstance(v, ModelOutput):
        return v.message.id
    return None


def _isolate(v: Any) -> Any:
    return copy.deepcopy(v)


class ReplayDesyncError(RuntimeError):
    pass


class Tape:
    def __init__(self, pending: Iterable[Step] = (), *, role: str = "") -> None:
        self.log: list[Step] = []
        self.pending: deque[Step] = deque(pending)
        self.role = role  # prefixed onto source for disambiguation
        self.replayable: Callable[..., Callable[..., Awaitable[Any]]] = self._serve

    def _src(self, fn: Callable[..., Any]) -> str:
        return f"{self.role}:{fn.__qualname__}" if self.role else fn.__qualname__

    def pop(self, src: str, sync: Sync) -> Step | None:
        if not self.pending:
            return None
        s = self.pending.popleft()
        if s.source != src or s.sync != sync:
            raise ReplayDesyncError(
                f"expected ({src!r}, sync={sync!r}); recorded ({s.source!r}, sync={s.sync!r})"
            )
        return s

    def _serve(self, fn: Callable[..., Awaitable[Any]], *, sync: Sync = None) -> Callable[..., Awaitable[Any]]:
        """nondet(c, k)=T at this level: serve from pending if available, else live; log full value."""
        src = self._src(fn)

        @functools.wraps(fn)
        async def w(*a: Any, **kw: Any) -> Any:
            if (s := self.pop(src, sync)) is not None:
                self.log.append(s)
                return _isolate(s.value)
            v = await fn(*a, **kw)
            self.log.append(Step(_isolate(v), src, sync, _message_id_of(v)))
            return v

        return w

    def dump(self) -> list[dict[str, Any]]:
        return [s.dump() for s in self.log]

    @staticmethod
    def load(steps: list[dict[str, Any]], *, role: str = "") -> Tape:
        return Tape((Step.load(d) for d in steps), role=role)


@dataclass
class Node:
    """Per-branch tape holder (resampling.md §Branching). Spike-1: linear (no children yet)."""

    tape: Tape
    parent: Node | None = None
    branched_from: str | None = None
    children: list[Node] = field(default_factory=list)
