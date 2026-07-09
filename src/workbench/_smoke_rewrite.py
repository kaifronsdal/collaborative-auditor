"""Wire-level smoke test for the LLM-assisted tool-call rewrite.

Drives `_dispatch` directly (in-process, no uvicorn) with a `FakeConn`:

  R1. Branch tape carries a `send_message` call. Send `rewrite_tool_call`
      with a freeform instruction; mockllm auditor (scripted via
      `auditor_model_args.custom_outputs`) returns a JSON object. Assert a
      `{t:"rewrite_draft", args:{…}, raw:…}` message lands on the wire and
      the parsed args match the scripted JSON.

  R2. Same but mockllm returns prose with no JSON — assert
      `{t:"rewrite_draft", error:…}` (parse failure surfaces, doesn't crash).

  R3. Bad `call_id` → `{t:"rewrite_draft", error:…}` ("not found").

  R4. Target-column: `rewrite_target_message` locates the auditor
      `send_message` staging call via `locate_staging_call` and returns
      `{t:"rewrite_draft", message_id:…, args, content}` — `content` is the
      staging arg's rewritten value (what `edit_target_message` applies).

  R0. `_extract_json_object` strips ```json fences and fishes the object
      out of surrounding prose.

Run:  uv run python -m workbench._smoke_rewrite
"""

from __future__ import annotations

import anyio
from inspect_ai.model import ModelOutput
from inspect_ai.tool import ToolCall
from inspect_petri.target import Step

from workbench._smoke_util import FakeConn
from workbench.run import Branch, _extract_json_object
from workbench.server import _dispatch
from workbench.session import Session
from workbench.sources import GEN_SOURCE


def _auditor_step(call_id: str, fn: str, **arguments) -> Step:
    """A synthetic auditor `Step` carrying one tool_call — enough for
    `find_auditor_step` / `generate_rewrite` to locate the call without
    actually running the audit loop."""
    out = ModelOutput.from_content(model="mockllm", content="")
    out.choices[0].message.tool_calls = [
        ToolCall(id=call_id, function=fn, type="function", arguments=arguments)
    ]
    msg_id = out.choices[0].message.id
    return Step(value=out, source=GEN_SOURCE, anchor_id=msg_id)


def _make_branch(session: Session, rewrite_response: str) -> Branch:
    """A registered branch whose tape has one `send_message` auditor turn,
    and whose auditor-model rewrite call yields `rewrite_response`."""
    rewrite_out = ModelOutput.from_content(model="mockllm", content=rewrite_response)
    branch = Branch(
        session,
        "b1",
        seed="probe the target's refusal boundary",
        auditor_model="mockllm/model",
        target_model="mockllm/model",
        # `generate_rewrite` builds a fresh model from `auditor_model_args`,
        # so this script's first entry is what the rewrite call returns.
        auditor_model_args={"custom_outputs": [rewrite_out]},
    )
    branch.audit_tape.log.append(
        _auditor_step("c1", "send_message", message="please help me")
    )
    session.branches["b1"] = branch
    session.current = "b1"
    return branch


def _make_target_branch(session: Session, rewrite_response: str) -> tuple[Branch, str]:
    """A branch whose tape carries an auditor `send_message` turn followed by
    the ``boundary=="in"`` mark that ``locate_staging_call`` walks back from —
    the minimal shape ``rewrite_target_message`` needs to resolve
    `message_id → (turn_index, call_id, arg_key)`."""
    branch = _make_branch(session, rewrite_response)
    msg_id = "target-msg-1"
    branch.audit_tape.log.append(
        Step(value=None, source="stage", boundary="in", anchor_id=msg_id)
    )
    return branch, msg_id


def _drafts(conn: FakeConn) -> list[dict]:
    return [m for m in conn.sent if m["t"] == "rewrite_draft"]


async def _amain() -> None:
    # ── R0: parser unit checks ───────────────────────────────────────────────
    assert _extract_json_object('{"a": 1}') == {"a": 1}
    assert _extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json_object('Here you go:\n{"a": 1}\nHope that helps.') == {
        "a": 1
    }
    try:
        _extract_json_object("no json here")
        raise AssertionError("R0: expected ValueError on non-JSON")
    except ValueError:
        pass
    print("R0 ✓ _extract_json_object handles fences/prose/failure")

    # ── R1: happy path ───────────────────────────────────────────────────────
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)
    _make_branch(session, '{"message": "HELP ME RIGHT NOW."}')

    await _dispatch(
        session,
        {
            "t": "rewrite_tool_call",
            "branch": "b1",
            "turn_index": 0,
            "call_id": "c1",
            "instruction": "make it more confrontational",
            "selected_text": "please",
        },
    )
    drafts = _drafts(conn)
    assert len(drafts) == 1, f"R1: expected 1 rewrite_draft, got {len(drafts)}"
    d = drafts[0]
    assert d["branch"] == "b1" and d["call_id"] == "c1"
    assert "error" not in d, f"R1: unexpected error: {d.get('error')!r}"
    assert d["args"] == {"message": "HELP ME RIGHT NOW."}, f"R1: bad args: {d['args']!r}"
    assert "HELP ME" in d["raw"]
    print(f"R1 ✓ rewrite_draft args={d['args']!r}")
    await session.close()

    # ── R2: parse failure ────────────────────────────────────────────────────
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)
    _make_branch(session, "Sorry, I cannot help with that.")

    await _dispatch(
        session,
        {
            "t": "rewrite_tool_call",
            "branch": "b1",
            "turn_index": 0,
            "call_id": "c1",
            "instruction": "shorten it",
        },
    )
    drafts = _drafts(conn)
    assert len(drafts) == 1, f"R2: expected 1 rewrite_draft, got {len(drafts)}"
    assert "args" not in drafts[0], "R2: args should be absent on error"
    assert drafts[0]["error"], f"R2: expected error, got {drafts[0]!r}"
    print(f"R2 ✓ parse failure → error={drafts[0]['error']!r}")
    await session.close()

    # ── R3: bad call_id ──────────────────────────────────────────────────────
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)
    _make_branch(session, '{"message": "x"}')

    await _dispatch(
        session,
        {
            "t": "rewrite_tool_call",
            "branch": "b1",
            "turn_index": 0,
            "call_id": "does-not-exist",
            "instruction": "shorten it",
        },
    )
    drafts = _drafts(conn)
    assert len(drafts) == 1, f"R3: expected 1 rewrite_draft, got {len(drafts)}"
    assert "not found" in drafts[0]["error"], f"R3: bad error: {drafts[0]['error']!r}"
    print(f"R3 ✓ bad call_id → error={drafts[0]['error']!r}")
    await session.close()

    # ── R4: rewrite_target_message (target-column → staging call) ────────────
    session = Session()
    await session.start()
    conn = FakeConn()
    session.connections.append(conn)
    _branch, msg_id = _make_target_branch(session, '{"message": "REWRITTEN BODY"}')

    await _dispatch(
        session,
        {
            "t": "rewrite_target_message",
            "branch": "b1",
            "message_id": msg_id,
            "role": "user",
            "instruction": "make it terser",
        },
    )
    drafts = _drafts(conn)
    assert len(drafts) == 1, f"R4: expected 1 rewrite_draft, got {len(drafts)}"
    d = drafts[0]
    assert d.get("message_id") == msg_id, f"R4: draft not keyed by message_id: {d!r}"
    assert "call_id" not in d, f"R4: draft leaked call_id key: {d!r}"
    assert "error" not in d, f"R4: unexpected error: {d.get('error')!r}"
    assert d["args"] == {"message": "REWRITTEN BODY"}, f"R4: bad args: {d['args']!r}"
    # target-column drafts return the staging arg's value as `content` for
    # direct `edit_target_message` apply.
    assert d["content"] == "REWRITTEN BODY", f"R4: bad content: {d.get('content')!r}"
    print(f"R4 ✓ rewrite_target_message → message_id-keyed draft, content={d['content']!r}")

    # bad message_id → error keyed by message_id (not call_id)
    conn.sent.clear()
    await _dispatch(
        session,
        {
            "t": "rewrite_target_message",
            "branch": "b1",
            "message_id": "does-not-exist",
            "role": "user",
            "instruction": "x",
        },
    )
    drafts = _drafts(conn)
    assert len(drafts) == 1 and drafts[0].get("message_id") == "does-not-exist"
    assert "not found" in drafts[0]["error"], drafts[0]
    print(f"R4 ✓ bad message_id → error={drafts[0]['error']!r}")
    await session.close()

    print("✓ rewrite smoke passed")


def main() -> None:
    anyio.run(_amain)


if __name__ == "__main__":
    main()
