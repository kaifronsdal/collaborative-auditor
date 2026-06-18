"""Branch lifecycle — petri's `run_audit` + `auditor_agent`, outside `inspect eval`, step-gated.

`Branch.run()` sets up the channel / controller / history / audit-tape via
`audit_context()`, registers its auditor and target span ids on the owning
`Session` for routing (STREAMING.md §B), and runs petri's `run_audit()`
(the auditor/target task group) alongside the session's `drain()` task. The
auditor is a stock `auditor_agent()` whose per-turn model call is gated by
`_gated_generate` — the only thing the workbench interposes.

The step gate is an `anyio.Event` the auditor's generate awaits each turn.
`step()` sets it (released for one turn). `play()` sets a free-running
flag; `_gated_generate` re-arms the gate itself after each turn while that
flag holds, so play self-perpetuates without a polling pump task. `pause()`
clears the flag — the next turn waits.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
from inspect_ai.event import ModelEvent
from inspect_ai.model import (
    CachePolicy,
    ChatMessage,
    GenerateConfig,
    Model,
    ModelOutput,
    get_model,
)
from inspect_ai.tool import Tool
from inspect_ai.util import Store
from inspect_petri._auditor import audit_context, auditor_agent, run_audit
from inspect_petri.target import (
    Channel,
    Controller,
    History,
    Step,
    Tape,
    target_agent,
)
from shortuuid import uuid

from workbench.view import Role, Status

if TYPE_CHECKING:
    from workbench.session import Session


@dataclass
class BranchMeta:
    parent: str | None  # branch_id this was branched from
    branched_at: str | None  # anchor_id where the slice happened
    seed: str
    auditor_model: str
    target_model: str
    max_turns: int


def slice_at(steps: list[Step], anchor_id: str) -> list[Step]:
    """Slice a level-2 audit tape at a re-sync point (footgun #1).

    Finds the last step whose `anchor_id` matches, advances past trailing
    `boundary=="out"` steps (external send acks that belong with the turn),
    and returns the prefix up to and including that point.

    Raises `ValueError` if:
    - `anchor_id` is not found in `steps`;
    - the slice would land mid-rollback: the last included auditor step has a
      `rollback_conversation` tool call and no subsequent `boundary=="in"` step
      follows in the slice (i.e. the level-1 re-sync has not yet fired).
    """
    last_match: int | None = None
    for i, step in enumerate(steps):
        if step.anchor_id == anchor_id:
            last_match = i
    if last_match is None:
        raise ValueError(
            f"anchor_id {anchor_id!r} not found in audit tape "
            f"({len(steps)} steps)"
        )
    # Advance past trailing boundary=="out" steps (send_response acks).
    cutoff = last_match
    for i in range(last_match + 1, len(steps)):
        if steps[i].boundary != "out":
            break
        cutoff = i

    prefix = steps[: cutoff + 1]

    # Guard: detect mid-rollback slice. If the last auditor ModelOutput step in
    # the prefix contains a rollback_conversation tool call, there must be a
    # subsequent boundary=="in" step in the prefix (the channel re-sync). If
    # not, we are in the gap between the rollback command and the re-sync, and
    # the level-1 tree would desync on resume.
    last_auditor_out: Step | None = None
    last_boundary_in_after_auditor: bool = False
    for step in prefix:
        if step.source == "auditor:Model.generate" and isinstance(
            step.value, ModelOutput
        ):
            last_auditor_out = step
            last_boundary_in_after_auditor = False
        elif step.boundary == "in":
            last_boundary_in_after_auditor = True

    if last_auditor_out is not None and not last_boundary_in_after_auditor:
        # Check whether the auditor output actually issued a rollback_conversation
        # tool call. Only raise if it did (and we have no re-sync after it).
        mo = last_auditor_out.value
        assert isinstance(mo, ModelOutput)
        tool_calls = mo.choices[0].message.tool_calls if mo.choices else []
        rollback_fns = {tc.function for tc in (tool_calls or [])}
        if "rollback_conversation" in rollback_fns:
            raise ValueError(
                f"anchor_id {anchor_id!r} lands mid-rollback: the last auditor "
                "step issued rollback_conversation but no boundary=='in' re-sync "
                "follows in the prefix. Slice at a completed turn instead."
            )

    return prefix


class Branch:
    def __init__(
        self,
        session: Session,
        branch_id: str,
        *,
        seed: str,
        auditor_model: str,
        target_model: str,
        max_turns: int,
        resume: list[Step] | None = None,
        parent_id: str | None = None,
        branched_at: str | None = None,
    ) -> None:
        self.session = session
        self.branch_id = branch_id
        self.seed = seed
        self.auditor_model = auditor_model
        self.target_model = target_model
        self.max_turns = max_turns
        self.resume = resume
        self.meta = BranchMeta(
            parent=parent_id,
            branched_at=branched_at,
            seed=seed,
            auditor_model=auditor_model,
            target_model=target_model,
            max_turns=max_turns,
        )

        # per-branch Store: `AuditTape` is a StoreModel, so without a branch-
        # private store every branch in this process would read/write the
        # process-global default and cross-contaminate `config_digest`/`steps`
        # (petri footgun #5). `audit_context(store=...)` installs it per branch.
        self.store = Store()
        self.error: str | None = None

        # petri target plumbing. On resume, seed the audit tape's replay queue
        # from the parent branch's recorded steps (value-bearing only — the
        # `value is not None` filter matches petri's `_read_resume_seed`), so
        # the prefix replays from `pending` instead of re-calling the model.
        self.channel = Channel(seed_instructions=seed)
        self.controller = Controller(self.channel)
        self.history = History()
        self.audit_tape = (
            Tape(pending=deque(s for s in resume if s.value is not None))
            if resume is not None
            else Tape()
        )

        # step gate — the loop awaits `_gate.wait()` each turn then clears it.
        # `play()` sets `_free_running`; the loop re-sets the gate itself after
        # each turn while that flag holds, so play self-perpetuates without a
        # polling pump and `pause()` takes effect at the next turn boundary.
        self._gate = anyio.Event()
        self._free_running = False

        # user-injected messages awaiting the next turn boundary (STREAMING.md §B).
        self.queued: dict[Role, list[ChatMessage]] = {"auditor": [], "target": []}
        self.status: Status = "idle"
        self.generating: Role | None = None

        # span ids — registered on the session so events route to this branch.
        self.auditor_span_id = uuid()
        self.target_span_id = uuid()
        session.span_role[self.auditor_span_id] = (branch_id, "auditor")
        session.span_role[self.target_span_id] = (branch_id, "target")

    # -- step gate ------------------------------------------------------------

    def step(self) -> None:
        """Release one auditor turn."""
        if self.status != "ended":
            self.status = "running"
        self._gate.set()

    def play(self) -> None:
        """Run freely: each turn re-arms the gate itself until `pause()`."""
        self._free_running = True
        if self.status != "ended":
            self.status = "running"
        self._gate.set()

    def pause(self) -> None:
        """Stop after the current turn; the next gate wait blocks."""
        self._free_running = False
        if self.status != "ended":
            self.status = "paused"

    async def _await_turn(self) -> None:
        await self._gate.wait()
        self._gate = anyio.Event()  # anyio.Event is one-shot; replace to re-arm

    # -- run ------------------------------------------------------------------

    async def run(self) -> None:
        # models — force streaming so provider partial-output flushes fire
        # (default "auto" only streams with reasoning or large max_tokens).
        auditor_model = get_model(self.auditor_model, streaming=True)
        target_model = get_model(self.target_model, streaming=True)

        # petri's full auditor agent, with the per-turn generate gated by our
        # step gate. `auditor_agent` owns the system/user prompt, tools, the
        # turn loop, eager-resume and end_conversation; we only interpose the
        # gate + queued-message injection via the `generate=` hook (which now
        # receives petri's tape-wrapped generate, so the call still records
        # onto the audit tape).
        auditor = auditor_agent(
            generate=self._gated_generate,
            max_turns=self.max_turns,
            compaction=False,
            realism_filter=False,
            eager_resume=True,
        )

        # The auditor blocks on the step gate before its first turn, so the
        # branch is "paused" (awaiting play/step) until a turn is released — not
        # "running". play()/step() flip it to "running". Broadcast so a client
        # that connected on the `start` snapshot (which still read "idle", as
        # this detached task hadn't run yet) learns the paused state.
        self.status = "paused"
        await self.session.broadcast_status()

        # On resume, synthesise the replayed prefix's events onto the wire
        # before the live run starts, so the frontend shows the parent branch's
        # turns in this branch's columns (STREAMING.md §"Replay"). Done inside
        # the store-bearing context so any store reads behave like a real event.
        # F2: every contextvar children inherit must be set in THIS parent
        # context, before create_task_group(). `audit_context()` is petri's
        # single CM for that. The session installed `transcript` already (it
        # owns and subscribes to it across branches), so we don't pass it here.
        # `store=self.store` installs a branch-private Store so this branch's
        # `AuditTape` doesn't read/write a sibling's (petri footgun #5).
        # Drain is owned by the Session (started in `Session.start()`), not by
        # this branch (petri footgun #12), so `run()` just runs the audit.
        try:
            with audit_context(
                controller=self.controller,
                audit_tape=self.audit_tape,
                store=self.store,
                active_model=target_model,
                model_roles={"auditor": auditor_model, "target": target_model},
            ):
                if self.resume is not None:
                    self._synthesize_prefix_events(auditor_model, target_model)

                await run_audit(
                    auditor=auditor,
                    target=target_agent(),
                    channel=self.channel,
                    history=self.history,
                    audit_tape=self.audit_tape,
                    auditor_span_id=self.auditor_span_id,
                    target_span_id=self.target_span_id,
                    # name timelines per branch: multiple branches share the
                    # session's one Transcript, so the default ("target"/"auditor")
                    # collides on the second branch's `add_timeline`.
                    audit_name=self.branch_id,
                )
        except Exception as exc:
            self.error = self.error or str(exc)
            raise
        finally:
            self.status = "ended"
            self.generating = None
            self._free_running = False
            await self.session.broadcast_status()
            if self.error:
                await self.session.broadcast({"t": "error", "v": self.session.version, "message": self.error})

    def _synthesize_prefix_events(
        self, auditor_model: Model, target_model: Model
    ) -> None:
        """Replay the resume prefix onto the wire as settled `ModelEvent`s.

        STREAMING.md §"Replay" promises the workbench synthesises the prefix's
        events from `audit_tape.log` so a resumed branch's columns are not blank
        before the first live turn — but the record/replay machinery serves
        replayed calls from `pending` without emitting events (the sink never
        fires). This walks the resume steps and feeds one settled `ModelEvent`
        per recorded `ModelOutput` through the session, routed to this branch's
        auditor or target column by `span_id`.

        The synthesised events carry `input=[]` — we don't reconstruct the input
        for replayed turns, so the `ModelEventRow` input-tail is empty for them.
        That is acceptable: they are the *replayed* prefix the user already saw
        in the parent branch, not freshly generated content.
        """
        assert self.resume is not None
        for step in self.resume:
            if not isinstance(step.value, ModelOutput):
                continue
            if step.source == "auditor:Model.generate":
                span_id, role, model = self.auditor_span_id, "auditor", auditor_model
            elif step.source == "Model.generate":
                span_id, role, model = self.target_span_id, "target", target_model
            else:
                continue
            ev = ModelEvent(
                model=model.name,
                role=role,
                input=[],
                tools=[],
                tool_choice="auto",
                config=GenerateConfig(),
                output=step.value,
                pending=False,
                span_id=span_id,
            )
            self.session._on_event(ev)  # noqa: SLF001

    async def _gated_generate(
        self,
        generate: Callable[..., Awaitable[ModelOutput]],
        messages: list[ChatMessage],
        tools: list[Tool],
        cache: bool | CachePolicy,
    ) -> ModelOutput:
        """Per-turn auditor generate, gated by the step gate (STREAMING.md §B).

        Awaits the gate (one release per `step()`, self-perpetuating under
        `play()`), folds any user-injected messages in before the model sees
        them — the injected `id` survives into `ModelEvent.input` → pool so the
        frontend reconciles the ghost bubble by id — then calls petri's
        tape-wrapped `generate` so the output still lands on the audit tape.
        """
        await self._await_turn()  # ← the step gate
        messages.extend(self.queued["auditor"])
        self.queued["auditor"].clear()
        self.generating = "auditor"
        try:
            return await generate(input=messages, tools=tools, cache=cache)
        finally:
            self.generating = None
            # free-running: re-arm the gate so the next turn proceeds
            # immediately. A `pause()` between turns clears the flag and the
            # next `_await_turn` blocks.
            if self._free_running:
                self._gate.set()
