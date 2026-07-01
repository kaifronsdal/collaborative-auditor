"""``wb.transcript`` / ``wb.excerpt`` / ``wb.read_transcript``.

Thin wrappers over inspect's own loaders/renderers so what the model reads
here matches what the judge and inspect-view see:

- ``read_eval_log_sample`` + ``span_messages`` → the message list
- ``messages_as_str`` → the plain-text render (same as scout scanners)

``TranscriptRef`` is a *pointer* — its ``text/plain`` is one line, the
frontend renders an embedded inspect-view from the WB_MIME payload.
``Excerpt``'s ``text/plain`` *is* the window, so the model reads it inline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from inspect_ai.log import read_eval_log_sample
from inspect_ai.model import ChatMessage
from inspect_scout import MessagesPreprocessor, messages_as_str, span_messages

from workbench.m1.kernel import WB_MIME
from workbench.m1.run import RunHandle

#: Render everything — the ``at`` index is into the raw message list, so
#: dropping system messages here would desync what you asked for from what
#: you see.
_PP = MessagesPreprocessor(exclude_system=False)


def _resolve_log(log: str | RunHandle) -> str:
    if isinstance(log, RunHandle):
        loc = log.location
        if loc is None:
            raise ValueError(f"{log!r} has no log file yet")
        return loc
    return log


def _load_messages(log: str, sample_id: str) -> list[ChatMessage]:
    """One sample's messages, via the timeline where available.

    ``span_messages`` walks the event tree (handles compaction, nested
    agents) — that's the same source ``audit_judge``'s ``render_target_timeline``
    reads, so indices line up. Falls back to the flat ``.messages`` list for
    logs without events.
    """
    sample = read_eval_log_sample(log, id=sample_id, resolve_attachments=True)
    if sample.events:
        return cast("list[ChatMessage]", span_messages(sample.events))
    return list(sample.messages)


async def _render(messages: list[ChatMessage]) -> str:
    return cast("str", await messages_as_str(messages, preprocessor=_PP))


# -- dataclasses --------------------------------------------------------------


@dataclass
class TranscriptRef:
    """Handle on a loaded transcript. The model sees one line; the human
    sees an embedded inspect-view (frontend keys off ``kind:"transcript"``)."""

    log: str
    sample_id: str
    at: int | None
    messages: list[ChatMessage] = field(repr=False)

    def __len__(self) -> int:
        return len(self.messages)

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        return {
            "text/plain": f"<Transcript {self.sample_id} · {len(self.messages)} msgs>",
            WB_MIME: {
                "kind": "transcript",
                "log": self.log,
                "sample_id": self.sample_id,
                "at": self.at,
            },
        }


@dataclass
class Excerpt:
    """A rendered window. ``text/plain`` *is* the messages — this is how the
    model actually reads transcript content."""

    log: str
    sample_id: str
    at: int
    messages: list[ChatMessage] = field(repr=False)
    text: str = field(repr=False)

    def _repr_mimebundle_(
        self, include: Any = None, exclude: Any = None
    ) -> dict[str, Any]:
        return {
            "text/plain": self.text,
            WB_MIME: {
                "kind": "excerpt",
                "log": self.log,
                "sample_id": self.sample_id,
                "at": self.at,
                "messages": [m.model_dump(mode="json") for m in self.messages],
            },
        }


# -- loaders ------------------------------------------------------------------


def transcript(
    log: str | RunHandle, sample_id: str, *, at: int | None = None
) -> TranscriptRef:
    loc = _resolve_log(log)
    return TranscriptRef(
        log=loc, sample_id=sample_id, at=at, messages=_load_messages(loc, sample_id)
    )


async def excerpt(
    log: str | RunHandle, sample_id: str, *, at: int, around: int = 1
) -> Excerpt:
    loc = _resolve_log(log)
    msgs = _load_messages(loc, sample_id)
    lo = max(0, at - around)
    window = msgs[lo : at + around + 1]
    return Excerpt(
        log=loc, sample_id=sample_id, at=at, messages=window, text=await _render(window)
    )


async def read_transcript(
    log: str | RunHandle,
    sample_id: str,
    *,
    range: tuple[int, int] | None = None,  # noqa: A002
) -> str:
    """Plain-text render for the model (no rich repr, no frontend card)."""
    msgs = _load_messages(_resolve_log(log), sample_id)
    if range is not None:
        lo, hi = range
        msgs = msgs[lo:hi]
    return await _render(msgs)
