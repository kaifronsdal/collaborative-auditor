"""Branch lifecycle — petri's `run_audit` driven by our `workbench_auditor`.

`Branch.run()` sets up the channel / controller / history / audit-tape via
`audit_context()`, registers its auditor and target span ids on the owning
`Session` for routing (STREAMING.md §B), and runs petri's `run_audit()` (the
auditor/target task group). The auditor is `workbench_auditor()` — a small
loop that owns the step-gate and queued-feedback drain inline; petri owns
everything else (tools, target trajectory, replay, anchor/branch events).

The step gate is an `anyio.Event` the auditor loop awaits each turn. `step()`
sets it (released for one turn). `play()` sets a free-running flag; the loop
re-arms the gate itself after each turn while that flag holds, so play
self-perpetuates without a polling pump task. `pause()` clears the flag.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
from inspect_ai.event import ModelEvent
from inspect_ai.model import (
    ChatMessage,
    GenerateConfig,
    Model,
    ModelOutput,
    get_model,
)
from inspect_ai.log._transcript import init_transcript  # noqa: PLC2701
from inspect_ai.util import Store
from inspect_petri._auditor import audit_context, run_audit
from inspect_petri.target import (
    Channel,
    Controller,
    History,
    Step,
    Tape,
    target_agent,
)
from inspect_petri._auditor.agent import GEN_SOURCE  # noqa: PLC2701
from inspect_petri.target._history import _find_cutoff  # noqa: PLC2701
from shortuuid import uuid

from workbench.auditor import workbench_auditor
from workbench.view import Role, Status

if TYPE_CHECKING:
    from workbench.session import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BranchMeta:
    """What a branch was started with — the config a child inherits on `fork()`."""

    parent: str | None  # branch_id this was branched from
    branched_at: str | None  # anchor_id where the slice happened
    seed: str
    auditor_model: str
    target_model: str
    max_turns: int | None
    auditor_config: dict | None = None
    target_config: dict | None = None
    # Provider kwargs (e.g. mockllm `custom_outputs` for deterministic tests).
    auditor_model_args: dict | None = None
    target_model_args: dict | None = None


def slice_at(steps: list[Step], anchor_id: str) -> list[Step]:
    """Slice a level-2 audit tape at a re-sync point (footgun #1).

    Delegates the cutoff search (last matching `anchor_id`, advance past
    trailing `boundary=="out"` acks) to petri's `_find_cutoff` — the same
    routine `Trajectory` uses for level-1 branching — then applies the
    workbench-specific mid-rollback guard.

    Raises `ValueError` if `anchor_id` is not found, or if the slice would land
    mid-rollback (the last included auditor step issued `rollback_conversation`
    but no `boundary=="in"` re-sync follows in the prefix).
    """
    cutoff = _find_cutoff(steps, anchor_id)
    if cutoff is None:
        raise ValueError(
            f"anchor_id {anchor_id!r} not found in audit tape ({len(steps)} steps)"
        )
    prefix = steps[: cutoff + 1]

    # Mid-rollback guard: if the last auditor ModelOutput in the prefix carries
    # a rollback_conversation tool call, there must be a subsequent
    # boundary=="in" step (the channel re-sync) — otherwise the level-1 tree
    # would desync on resume.
    last_auditor_out: Step | None = None
    resynced_after = False
    for step in prefix:
        if step.source == "auditor:Model.generate" and isinstance(
            step.value, ModelOutput
        ):
            last_auditor_out = step
            resynced_after = False
        elif step.boundary == "in":
            resynced_after = True

    if last_auditor_out is not None and not resynced_after:
        mo = last_auditor_out.value
        assert isinstance(mo, ModelOutput)
        tool_calls = (mo.choices[0].message.tool_calls or []) if mo.choices else []
        if any(tc.function == "rollback_conversation" for tc in tool_calls):
            raise ValueError(
                f"anchor_id {anchor_id!r} lands mid-rollback: the last auditor "
                "step issued rollback_conversation but no boundary=='in' "
                "re-sync follows in the prefix. Slice at a completed turn."
            )

    return prefix


TARGET_GEN_SOURCE = "Model.generate"

# Auditor tool → the argument that becomes the target-side message body.
# Used by `locate_staging_call` to map a target user/system/tool message back
# to the auditor tool_call that produced it (WISHLIST 3a/3c).
STAGING_ARG: dict[str, str] = {
    "send_message": "message",
    "set_system_message": "system_message",
    "send_tool_call_result": "result",
}
ROLE_TO_STAGING_FN: dict[str, str] = {
    "user": "send_message",
    "system": "set_system_message",
    "tool": "send_tool_call_result",
}


def find_auditor_step(steps: list[Step], turn_index: int) -> tuple[int, Step]:
    """Index and `Step` of the `turn_index`-th (0-based) auditor generate on the
    level-2 tape. Raises `ValueError` if out of range."""
    n = -1
    for i, s in enumerate(steps):
        if s.source == GEN_SOURCE and isinstance(s.value, ModelOutput):
            n += 1
            if n == turn_index:
                return i, s
    raise ValueError(
        f"auditor turn_index {turn_index} out of range (tape has {n + 1} auditor turns)"
    )


def find_target_step(steps: list[Step], anchor_id: str) -> tuple[int, Step]:
    """Index and `Step` of the target generate with `anchor_id` on the tape.
    Raises `ValueError` if not found."""
    for i, s in enumerate(steps):
        if (
            s.source == TARGET_GEN_SOURCE
            and s.anchor_id == anchor_id
            and isinstance(s.value, ModelOutput)
        ):
            return i, s
    raise ValueError(f"target anchor_id {anchor_id!r} not found in audit tape")


def edited_auditor_step(orig: Step, mutate: Callable[[ModelOutput], None]) -> Step:
    """A fresh `Step` carrying a deep-copied `ModelOutput` from `orig`, with
    `mutate` applied and a fresh message id / anchor (footgun #3)."""
    assert isinstance(orig.value, ModelOutput)
    out: ModelOutput = copy.deepcopy(orig.value)
    mutate(out)
    new_id = uuid()
    if out.choices:
        out.choices[0].message.id = new_id
    return Step(value=out, source=orig.source, anchor_id=new_id)


def locate_staging_call(
    steps: list[Step], message_id: str, role: str, tool_call_id: str | None
) -> tuple[int, Step, str, str]:
    """Map a target-side user/system/tool message back to the auditor tool_call
    that staged it.

    Finds the marked `boundary=="in"` step with `anchor_id == message_id` (the
    `Stage` command receipt), walks back to the preceding auditor generate, and
    picks the tool_call whose function matches `role` (disambiguated by
    `tool_call_id` for tool results). Returns `(aud_idx, aud_step, call_id,
    arg_key)`. Raises `ValueError` if the message can't be located or the
    matching tool_call is ambiguous — caller surfaces that as a
    "use the auditor column's tool-call edit" hint.
    """
    mark_idx = next(
        (
            i
            for i, s in enumerate(steps)
            if s.boundary == "in" and s.anchor_id == message_id
        ),
        None,
    )
    if mark_idx is None:
        raise ValueError(f"message_id {message_id!r} not found on the audit tape")
    aud_idx = next(
        (
            i
            for i in range(mark_idx - 1, -1, -1)
            if steps[i].source == GEN_SOURCE
            and isinstance(steps[i].value, ModelOutput)
        ),
        None,
    )
    if aud_idx is None:
        raise ValueError(
            f"no auditor turn precedes message_id {message_id!r} on the tape"
        )
    aud_step = steps[aud_idx]
    assert isinstance(aud_step.value, ModelOutput)
    fn = ROLE_TO_STAGING_FN.get(role)
    if fn is None:
        raise ValueError(f"role {role!r} is not editable via edit_target_message")
    calls = aud_step.value.message.tool_calls or []
    candidates = [tc for tc in calls if tc.function == fn]
    if role == "tool" and tool_call_id is not None:
        candidates = [
            tc for tc in candidates if tc.arguments.get("tool_call_id") == tool_call_id
        ]
    if len(candidates) != 1:
        raise ValueError(
            f"cannot uniquely map {role} message to an auditor {fn} call "
            f"({len(candidates)} candidates) — edit the auditor's tool call directly"
        )
    return aud_idx, aud_step, candidates[0].id, STAGING_ARG[fn]


class Branch:
    def __init__(
        self,
        session: Session,
        branch_id: str,
        *,
        seed: str,
        auditor_model: str,
        target_model: str,
        max_turns: int | None = None,
        auditor_config: dict | None = None,
        target_config: dict | None = None,
        auditor_model_args: dict | None = None,
        target_model_args: dict | None = None,
        resume: list[Step] | None = None,
        parent_id: str | None = None,
        branched_at: str | None = None,
    ) -> None:
        self.session = session
        self.branch_id = branch_id
        self.resume = resume
        self.meta = BranchMeta(
            parent=parent_id,
            branched_at=branched_at,
            seed=seed,
            auditor_model=auditor_model,
            target_model=target_model,
            max_turns=max_turns,
            auditor_config=auditor_config,
            target_config=target_config,
            auditor_model_args=auditor_model_args,
            target_model_args=target_model_args,
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

        # step gate — `workbench_auditor` awaits `_gate.wait()` each turn then
        # replaces it (one-shot Event). `play()` sets `_free_running`; the loop
        # re-sets the gate after each turn while that flag holds, so play
        # self-perpetuates without a polling pump and `pause()` takes effect at
        # the next turn boundary.
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

    # -- run ------------------------------------------------------------------

    async def run(self) -> None:
        # Re-install the session's transcript in *this* task's context. The
        # Session set it in `__init__()`, but that ran in whichever WS
        # connection's task created the session — a `start` from a later
        # connection spawns this task in a different context that doesn't
        # inherit the var, so events would land on the default transcript
        # (no subscriber) and never reach the wire.
        init_transcript(self.session.transcript)

        # `_register_and_spawn` may have called `play()` already (autoplay);
        # only fall back to "paused" if it didn't.
        if self.status == "idle":
            self.status = "paused"
        await self.session.broadcast_status()

        # Model construction stays *outside* `audit_context()` (F2: contextvars
        # children inherit must be set in this parent context before
        # `run_audit`'s task group opens; some providers spawn during
        # `get_model`). It is *inside* the try so a bad model id surfaces as a
        # `{t:"error"}` instead of an unhandled task exception.
        try:
            auditor_model, target_model = self._build_models()
            auditor = workbench_auditor(self, max_turns=self.meta.max_turns or 10_000)

            # `audit_context()` installs every contextvar children inherit
            # (transcript was set by the Session). `store=self.store` keeps
            # `AuditTape` per-branch (footgun #5). Drain is session-owned
            # (footgun #12), so this just runs the audit.
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
                    # session's one Transcript, so the default would collide.
                    audit_name=self.branch_id,
                )
        except (asyncio.CancelledError, anyio.get_cancelled_exc_class()):
            # `_stop_running_branches` cancelled us — normal stop, not an error.
            pass
        except Exception as exc:
            # Loud (full traceback in the server log + {t:"error"} to clients)
            # but not fatal: re-raising into a detached task only delays
            # visibility until the next branch awaits it, and would kill the
            # session if we ever moved to a shared task group.
            self.error = self.error or str(exc)
            logger.exception("branch %s failed", self.branch_id)
        finally:
            self.status = "ended"
            self.generating = None
            self._free_running = False
            await self.session.broadcast_status()
            if self.error:
                await self.session.broadcast(
                    {"t": "error", "v": self.session.version, "message": self.error}
                )

    @classmethod
    def fork(
        cls,
        session: Session,
        parent: "Branch",
        *,
        resume: list[Step],
        branched_at: str,
    ) -> "Branch":
        """A child branch inheriting `parent`'s config (seed, models, turns)."""
        m = parent.meta
        return cls(
            session,
            uuid(),
            seed=m.seed,
            auditor_model=m.auditor_model,
            target_model=m.target_model,
            max_turns=m.max_turns,
            auditor_config=m.auditor_config,
            target_config=m.target_config,
            auditor_model_args=m.auditor_model_args,
            target_model_args=m.target_model_args,
            resume=resume,
            parent_id=parent.branch_id,
            branched_at=branched_at,
        )

    def _build_models(self) -> tuple[Model, Model]:
        # force streaming so provider partial-output flushes fire (default
        # "auto" only streams with reasoning or large max_tokens).
        m = self.meta
        return (
            get_model(
                m.auditor_model,
                streaming=True,
                config=GenerateConfig(**(m.auditor_config or {})),
                **(m.auditor_model_args or {}),
            ),
            get_model(
                m.target_model,
                streaming=True,
                config=GenerateConfig(**(m.target_config or {})),
                **(m.target_model_args or {}),
            ),
        )

    def _synthesize_prefix_events(
        self, auditor_model: Model, target_model: Model
    ) -> None:
        """Replay the resume prefix onto the wire as settled `ModelEvent`s.

        STREAMING.md §"Replay" — record/replay serves replayed calls from
        `pending` without emitting events, so without this the resumed branch's
        columns would be blank until the first live turn. Walks the resume
        steps and feeds one settled `ModelEvent` per recorded `ModelOutput`
        through the session, routed to this branch's column by `span_id`.

        Synthesised events carry `input=[]` — they are the *replayed* prefix
        the user already saw on the parent branch.
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
