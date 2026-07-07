"""The workbench's auditor agent — petri's loop, with a two-method hook seam.

Drives the M0 desk (one audit, human-in-loop). ``Branch`` implements
``TurnHooks`` — ``pre_turn`` awaits the desk gate, drains
``branch.queued["auditor"]``, and flips ``branch.generating``. M1 batch
runs live in subprocesses via the ``bash`` tool (``_audit_task.py`` →
``inspect_petri.audit``) and never enter this loop.

Everything else — divergent-serve emit, the ``if not tape.pending`` replay
burn-through, the ``TURN_END_SOURCE`` anchor — is replay mechanics, not
an M0/M1 difference. They stay unconditional.

Compaction and realism-filter are ordinary parameters defaulting off (M0's
default — a watching operator would be surprised by silent context rewrites,
and compaction summaries aren't tape-recorded so enabling it under M0's
fork/replay would desync). ``resolve_compaction(False)`` returns petri's
``CompactNone`` no-op, so the loop calls ``compact.compact_input`` /
``record_output`` unconditionally at zero cost.

The load-bearing petri machinery — model resolution, tape-wrapped
``generate``/``today``, config-digest guard, prompt templating,
compaction/approval resolve, and the eager-resume tool phase — is now a
straight import of ``auditor_prelude`` / ``run_turn_tools``. What remains
here is exactly the desk/batch-specific control flow layered on top.
"""

from __future__ import annotations

from typing import Protocol

from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.approval import ApprovalPolicy
from inspect_ai.event import AnchorEvent, ModelEvent
from inspect_ai.log import transcript
from inspect_ai.model import (
    ChatMessage,
    ChatMessageUser,
    CompactionStrategy,
    GenerateConfig,
    ModelOutput,
    get_model,
)
from inspect_petri._auditor import (
    audit_tape,
    auditor_prelude,
    auditor_tools,
    run_turn_tools,
)
from inspect_petri._auditor.agent import AUDITOR_CONTINUE_PROMPT

from workbench.sources import GEN_SOURCE, TURN_END_SOURCE


class TurnHooks(Protocol):
    """The two per-turn desk seams — implemented by ``Branch``."""

    async def pre_turn(self) -> list[ChatMessage]:
        """Called before each *live* generate (once ``tape.pending`` is drained).

        May block (the desk's step-gate). Returns messages to inject; the
        loop stops via task-cancel, never a return value.
        """
        ...

    def post_generate(self) -> None:
        """Called after the model returns, before tools execute."""
        ...

    def post_turn(self) -> None:
        """Called after tools execute — i.e. once any target reply this turn
        has landed on the L2 tape. Fire-and-forget seam for P1.8(c) live
        scanners; must not block (the desk gate is ``pre_turn``'s job)."""
        ...


def workbench_auditor(
    hooks: TurnHooks,
    *,
    max_turns: int,
    compaction: bool | int | float | CompactionStrategy = False,
    realism_filter: bool | float = False,
    approval: str | list[ApprovalPolicy] | None = None,
) -> Agent:
    """An auditor ``Agent`` for ``run_audit(auditor=…)`` / ``audit_solver``.

    ``hooks`` supplies the desk's step-gate + spinner (``Branch``);
    everything else is petri's own machinery. ``compaction``/
    ``realism_filter`` default off — a watching operator would be
    surprised by silent context rewrites.
    """
    tools = auditor_tools(prefill=True)

    @agent
    def _factory() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            model_name = get_model(role="auditor", required=True).name
            tape = audit_tape()
            assert tape is not None, "workbench_auditor requires audit_context()"
            generate, compact, approval_policies, _ = await auditor_prelude(
                state,
                max_turns=max_turns,
                tools=tools,
                compaction=compaction,
                realism_filter=realism_filter,
                approval=approval,
            )

            for turn in range(max_turns):
                # Replay turns (served from ``pending``) are deterministic and
                # I/O-free — burn through ungated. First live turn onward:
                # hand control to the hooks (which may block on the desk gate).
                live = not tape.pending
                if live:
                    state.messages.extend(await hooks.pre_turn())

                input_msgs, c_msg = await compact.compact_input(state.messages)
                if c_msg is not None:
                    state.messages.append(c_msg)

                # Divergent serve: an ``edit_*`` op appended one edited step
                # past ``prefix_len``. Inert in batch (``pending`` is always
                # empty there); stays unconditional so M0 replay works.
                divergent = bool(tape.pending) and len(tape.log) >= tape.prefix_len
                state.output = await generate(input=input_msgs, tools=tools)
                if divergent:
                    _emit_divergent(model_name, list(state.messages), state.output)
                state.messages.append(state.output.message)
                await compact.record_output(input_msgs, state.output)
                hooks.post_generate()

                if state.output.message.tool_calls:
                    if await run_turn_tools(state, tools, approval_policies, turn):
                        break
                else:
                    state.messages.append(
                        ChatMessageUser(content=AUDITOR_CONTINUE_PROMPT)
                    )

                if state.output.message.id:
                    transcript()._event(  # noqa: SLF001
                        AnchorEvent(
                            anchor_id=state.output.message.id,
                            source=TURN_END_SOURCE,
                        )
                    )

                # Live turns only: replay re-emits the parent's target
                # messages verbatim, so re-scoring them is wasted judge calls.
                if live:
                    hooks.post_turn()

            return state

        return execute

    return _factory()


def _emit_divergent(
    model_name: str, input_msgs: list[ChatMessage], output: ModelOutput
) -> None:
    """Emit the ``ModelEvent`` + ``AnchorEvent`` for a served divergent step.

    ``Tape.replayable`` serves an edited step without a ``ModelEvent``, so
    the desk's ``build_auditor_timeline`` and ``session._on_event`` splice
    gate need one emitted here — it's the only ``Step`` no other branch's
    transcript carries.
    """
    transcript()._event(  # noqa: SLF001
        ModelEvent(
            model=model_name,
            role="auditor",
            input=input_msgs,
            tools=[],
            tool_choice="auto",
            config=GenerateConfig(),
            output=output,
            pending=False,
        )
    )
    if (a := output.message.id) is not None:
        transcript()._event(AnchorEvent(anchor_id=a, source=GEN_SOURCE))  # noqa: SLF001
