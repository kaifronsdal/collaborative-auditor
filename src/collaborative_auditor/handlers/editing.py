"""Message editing and tool-call rewriting handlers."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import WebSocket
from inspect_ai.model import (
    ChatMessageUser,
    GenerateConfig,
    get_model,
)

from collaborative_auditor.auditor import AUDITOR_SYSTEM_PROMPT, AUDITOR_USER_MESSAGE
from collaborative_auditor.models import (
    EventType,
    find_event_on_path,
    generate_id,
    get_branch_path,
    reconstruct_at_event,
    track_state_changes,
)
from collaborative_auditor.tools import create_cache_policy, format_content
from collaborative_auditor.handlers.common import (
    find_message_event_id,
    find_tool_call,
    push_view_state,
)
from collaborative_auditor.session_manager import SessionRuntime

logger = logging.getLogger(__name__)


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(cleaned[start : end + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Rewrite model did not return a valid JSON object")


async def _generate_rewritten_arguments(
    session_model: str,
    session_id: str,
    context_messages: list[Any],
    tool_name: str,
    original_arguments: dict[str, Any],
    instruction: str,
    selected_text: str | None = None,
    target_field: str | None = None,
) -> dict[str, Any]:
    rewrite_model = get_model(session_model)
    cache_policy = create_cache_policy(
        session_id=session_id,
        resample_id=generate_id(),
        expiry="1D",
    )

    selected_block = (
        f"\nSelected text from current tool call:\n{selected_text}\n"
        if selected_text and selected_text.strip()
        else ""
    )
    field_block = (
        f"\nPreferred field to edit first: {target_field}\n" if target_field else ""
    )
    prompt = (
        "Rewrite the arguments for an existing auditor tool call.\n"
        "Return ONLY a JSON object with the rewritten arguments "
        "(no markdown, no code fences).\n"
        "Preserve the same top-level schema and required fields for this tool.\n\n"
        f"Tool name: {tool_name}\n"
        f"Original arguments JSON:\n{json.dumps(original_arguments, indent=2)}\n"
        f"{field_block}"
        f"{selected_block}"
        f"\nRewrite instruction:\n{instruction.strip()}\n"
    )

    response = await rewrite_model.generate(
        input=[
            *context_messages,
            ChatMessageUser(
                id=generate_id(),
                content=prompt,
                metadata={"source": "Researcher"},
            ),
        ],
        config=GenerateConfig(cache=cache_policy, max_tokens=4096),
    )

    content_text = format_content(response.message.content)
    if not content_text:
        raise ValueError("Rewrite model returned empty content")

    return _extract_json_object(content_text)


async def handle_edit_message(
    runtime: SessionRuntime,
    data: dict[str, Any],
) -> None:
    session = runtime.session
    message_id = data["message_id"]
    new_content = data["new_content"]

    event_id = find_message_event_id(runtime, message_id)
    event = session.events[event_id]

    if event.parent_id is None:
        raise ValueError("Cannot edit message at root event")

    new_branch = runtime.fork_to_new_branch(event.parent_id)

    with track_state_changes(
        session, new_branch, event_type=EventType.RESEARCHER_MESSAGE
    ):
        new_branch.auditor_messages.append(
            ChatMessageUser(
                id=generate_id(),
                content=new_content,
                metadata={"source": "Researcher", "edited": True},
            )
        )

    await push_view_state(runtime)


async def handle_edit_initial_prompt(
    runtime: SessionRuntime,
    data: dict[str, Any],
) -> None:
    session = runtime.session
    new_prompt = data["new_content"]

    path = get_branch_path(session, runtime.current_branch)
    root_event_id = path[0]

    new_branch = runtime.fork_to_new_branch(root_event_id)

    session.initial_prompt = new_prompt

    sub_vars = dict(
        initial_prompt=new_prompt,
        today_date=datetime.now().strftime("%Y-%m-%d"),
        target_model=session.target_model,
    )
    system_prompt = AUDITOR_SYSTEM_PROMPT.safe_substitute(**sub_vars)
    user_message = AUDITOR_USER_MESSAGE.safe_substitute(**sub_vars)

    with track_state_changes(session, new_branch):
        new_branch.auditor_messages[0].content = system_prompt
        new_branch.auditor_messages[1].content = user_message

    await push_view_state(runtime)


async def handle_rewrite_tool_call(
    ws: WebSocket,
    runtime: SessionRuntime,
    data: dict[str, Any],
) -> None:
    """Generate rewritten tool-call arguments without applying them.
    Sends result directly to the requesting WebSocket.
    """
    request_id = data["request_id"]
    tool_call_id = data["tool_call_id"]
    instruction = data["instruction"]
    selected_text = data.get("selected_text")
    target_field = data.get("target_field")

    session = runtime.session
    branch = runtime.current_branch

    if not instruction or not instruction.strip():
        await ws.send_json({
            "type": "rewrite_tool_call_result",
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "error": "Rewrite instruction cannot be empty",
        })
        return

    tool_call_event = find_event_on_path(
        session, branch,
        event_type=EventType.TOOL_CALL_ADDED,
        tool_call_id=tool_call_id,
    )
    if tool_call_event is None:
        await ws.send_json({
            "type": "rewrite_tool_call_result",
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "error": f"Tool call not found: {tool_call_id}",
        })
        return

    try:
        original_tool_call = find_tool_call(branch, tool_call_id)
    except ValueError:
        await ws.send_json({
            "type": "rewrite_tool_call_result",
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "error": f"Tool call {tool_call_id} not found in branch messages",
        })
        return

    try:
        if tool_call_event.parent_id is None:
            raise ValueError("Cannot get context: tool call has no parent event")

        context_messages, _ = reconstruct_at_event(session, tool_call_event.parent_id)

        rewritten = await _generate_rewritten_arguments(
            session_model=session.auditor_model,
            session_id=session.id,
            context_messages=context_messages,
            tool_name=original_tool_call.function,
            original_arguments=original_tool_call.arguments,
            instruction=instruction,
            selected_text=selected_text,
            target_field=target_field,
        )
    except (ValueError, RuntimeError, json.JSONDecodeError) as e:
        logger.error("rewrite_tool_call failed: %s", e, exc_info=True)
        await ws.send_json({
            "type": "rewrite_tool_call_result",
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "error": str(e),
        })
        return

    await ws.send_json({
        "type": "rewrite_tool_call_result",
        "request_id": request_id,
        "tool_call_id": tool_call_id,
        "rewritten_arguments": rewritten,
    })
