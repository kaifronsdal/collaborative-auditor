"""The workbench's own auditor agent — petri's loop, minus the unattended-run
machinery, plus the step-gate and queued-feedback drain inline.

Why a custom auditor instead of `auditor_agent(generate=hook)`:

- The workbench is human-in-the-loop. Compaction, realism-filter and approval
  policies are for unattended runs and would surprise a watching operator
  (silent context rewrites, auto-rejections). eager_resume is kept — it
  corrects the auditor's "staged but forgot resume" mistake; without it the
  human sees a staged message and nothing happens, which is more confusing.
- The step-gate and queued-message drain want to live *in* the loop, not
  threaded through a `generate=` hook whose signature couples to petri's.
- Owning the loop means the gate wait/re-arm and the model call are three
  consecutive lines in one task — the µs re-arm race the hook had goes away.

What we still take from petri (the load-bearing parts):

- `auditor_tools()` — tool implementations (send_message, rollback, …)
- `audit_tape()` / `Tape.replayable` — record/replay; `AnchorEvent`/`BranchEvent`
- `AuditTape` / `config_digest` — resume validation
- The system/user prompt templates (we just render them simpler)
- `run_audit()` itself — the auditor+target task-group orchestration; this
  module returns an `Agent` that `run_audit(auditor=…)` accepts unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.event import AnchorEvent
from inspect_ai.log._transcript import transcript  # noqa: PLC2701
from inspect_ai.model import (
    ChatMessageSystem,
    ChatMessageUser,
    execute_tools,
    get_model,
)
from inspect_petri._auditor import AuditTape, audit_tape, auditor_tools
from inspect_petri._auditor._audit_tape import (  # noqa: PLC2701
    TODAY_SOURCE,
    config_digest,
    today,
)
from inspect_petri._auditor.agent import (  # noqa: PLC2701
    AUDITOR_CONTINUE_PROMPT,
    AUDITOR_SYSTEM_MESSAGE,
    AUDITOR_USER_MESSAGE,
    GEN_SOURCE,
    _eager_resume_inject,
    _eager_resume_strip_on_error,
)
from inspect_petri.target import controller

if TYPE_CHECKING:
    from workbench.run import Branch


def workbench_auditor(branch: "Branch", *, max_turns: int) -> Agent:
    """An auditor `Agent` for `run_audit(auditor=…)` with the workbench's
    step-gate and queued-feedback drain inline in the turn loop."""
    tools = auditor_tools()

    @agent
    def _factory() -> Agent:
        async def execute(state: AgentState) -> AgentState:
            agent_model = get_model(role="auditor", required=True)
            target_model = get_model(role="target", required=True)
            tape = audit_tape()
            assert tape is not None, "workbench_auditor requires audit_context()"
            generate = tape.replayable(agent_model.generate, source=GEN_SOURCE)
            get_today = tape.replayable(today, source=TODAY_SOURCE)
            today_date = await get_today()

            # config_digest: same role as petri's — fail loudly on resume if the
            # settings that shape the recorded call sequence changed. Compaction
            # / realism / eager_resume are fixed-off here, so the digest is just
            # models + prompts.
            store = AuditTape()
            if not store.seed_instructions:
                store.seed_instructions = controller().state.seed_instructions
            digest = config_digest(
                auditor_model=agent_model.name,
                target_model=target_model.name,
                system_message=AUDITOR_SYSTEM_MESSAGE,
                user_message=AUDITOR_USER_MESSAGE,
            )
            if store.config_digest and store.config_digest != digest:
                raise ValueError(
                    f"Resumed audit config does not match the recording "
                    f"({store.config_digest} vs {digest}); replay would desync."
                )
            store.config_digest = digest

            seed = controller().state.seed_instructions
            template_vars = {
                **controller().state.metadata,
                "max_turns": max_turns,
                "target_model": target_model.name,
                "seed_instructions": seed,
                "today_date": today_date,
                "skills_prompt": "",
                "tools_prompt": "",
            }
            state.messages[:] = [
                ChatMessageSystem(
                    content=AUDITOR_SYSTEM_MESSAGE.format_map(template_vars)
                ),
                ChatMessageUser(content=AUDITOR_USER_MESSAGE.format_map(template_vars)),
            ]

            from workbench.run import TURN_END_SOURCE  # noqa: PLC0415

            for turn in range(max_turns):
                # ── shared-prefix replay flag (splice model) ──────────────────
                # While the tape's replayed prefix is still inside the part
                # shared verbatim with the parent, the session drops every
                # auditor-role event (the parent supplies them via splice).
                # Past it — the divergent suffix (an edited step) and live
                # turns — events flow normally.
                if (
                    branch._replaying_shared  # noqa: SLF001
                    and len(tape.log) >= branch.shared_prefix_len
                ):
                    branch._replaying_shared = False  # noqa: SLF001

                # ── step-gate + queued-feedback drain, inline ─────────────────
                # Replay turns (served from `pending`) are deterministic and
                # I/O-free — burn through them ungated so the prefix events
                # land before the dispatch handler returns. Gate, drain
                # queued feedback, and flip `generating` only on *live*
                # turns.
                if not tape.pending:
                    if not branch._replayed.is_set():  # noqa: SLF001
                        branch._replayed.set()  # noqa: SLF001
                    await branch._gate.wait()  # noqa: SLF001
                    branch._gate = anyio.Event()  # noqa: SLF001
                    state.messages.extend(branch.queued["auditor"])
                    branch.queued["auditor"].clear()
                    branch.generating = "auditor"
                # ──────────────────────────────────────────────────────────────

                state.output = await generate(input=state.messages, tools=tools)
                state.messages.append(state.output.message)
                branch.generating = None
                if branch._free_running:  # noqa: SLF001
                    branch._gate.set()  # noqa: SLF001

                if state.output.message.tool_calls:
                    # eager_resume: petri's stage-without-resume correction. The
                    # injection mutates after `tape.replayable`'s isolate() copy,
                    # so the tape records the pre-injection output and replay
                    # reproduces the same injection deterministically.
                    injected = _eager_resume_inject(state.output, turn)
                    messages, exec_output = await execute_tools(
                        messages=state.messages, tools=tools
                    )
                    if injected is not None:
                        messages, state.output = _eager_resume_strip_on_error(
                            injected, messages, state.output
                        )
                    if exec_output is not None:
                        state.output = exec_output
                    state.messages.extend(messages)
                    if any(
                        m.role == "tool" and m.function == "end_conversation"
                        for m in messages
                    ):
                        break
                else:
                    state.messages.append(
                        ChatMessageUser(content=AUDITOR_CONTINUE_PROMPT)
                    )

                # End-of-turn anchor for the auditor timeline. petri's own
                # `AnchorEvent` (from `Tape.replayable`) lands *before*
                # `execute_tools`, so `splice()` on it would drop this
                # turn's `ToolEvent`s; this one comes *after* them.
                # `build_auditor_timeline` keeps only `TURN_END_SOURCE`
                # anchors, so `splice()`'s `findIndex` resolves to this.
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
