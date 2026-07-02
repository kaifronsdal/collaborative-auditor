"""The workbench's auditor agent — petri's loop, with a two-method hook seam.

One function serves both the M0 desk (one audit, human-in-loop) and M1 batch
runs (N audits inside ``eval_async``, unattended). The differences reduce to
where operator messages are drained from and whether a step-gate blocks:

- M0: ``Branch`` implements ``TurnHooks`` — ``pre_turn`` awaits the desk gate,
  drains ``branch.queued["auditor"]``, and flips ``branch.generating``.
- M1 batch: ``BatchHooks`` (in ``m1/run.py``) drains ``CONTROL[sample_id]``
  and never blocks.

Everything else — divergent-serve emit, the ``if not tape.pending`` replay
burn-through, the ``TURN_END_SOURCE`` anchor — is replay mechanics, not an
M0/M1 difference: batch has no forks/edits so ``tape.pending`` is always
empty and those paths are inert. They stay unconditional.

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
from inspect_petri._auditor.agent import AUDITOR_CONTINUE_PROMPT  # noqa: PLC2701

from workbench.sources import GEN_SOURCE, TURN_END_SOURCE


class TurnHooks(Protocol):
    """The two per-turn seams that differ between desk and batch."""

    async def pre_turn(self) -> tuple[list[ChatMessage], bool]:
        """Called before each *live* generate (once ``tape.pending`` is drained).

        May block (M0's step-gate). Returns ``(messages to inject, stop-now)``
        — ``stop-now`` breaks the loop cleanly (M1's ``wb.stop`` path; M0
        stops via task-cancel and never returns ``True`` here).
        """
        ...

    def post_generate(self) -> None:
        """Called after the model returns, before tools execute."""
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

    ``hooks`` supplies the two lines that differ between the M0 desk
    (``Branch``) and M1 batch (``BatchHooks``); everything else is petri's
    own machinery. ``compaction``/``realism_filter`` default off for the
    desk and are set by ``wb.run_audits`` for unattended batches.
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
                if not tape.pending:
                    injected, stop_now = await hooks.pre_turn()
                    if stop_now:
                        break
                    state.messages.extend(injected)

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
