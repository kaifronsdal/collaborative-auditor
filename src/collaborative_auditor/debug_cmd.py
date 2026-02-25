#!/usr/bin/env python3
"""Send a single command to the Collaborative Auditor and display the text UI.

Usage:
    # First call creates a session (pass session_id as first arg):
    uv run python src/collaborative_auditor/debug_cmd.py SESSION_ID start "Test the model"

    # Subsequent calls reuse the session:
    uv run python src/collaborative_auditor/debug_cmd.py SESSION_ID step
    uv run python src/collaborative_auditor/debug_cmd.py SESSION_ID feedback "Try harder"
    uv run python src/collaborative_auditor/debug_cmd.py SESSION_ID resample 3
    uv run python src/collaborative_auditor/debug_cmd.py SESSION_ID nav 3 left
    uv run python src/collaborative_auditor/debug_cmd.py SESSION_ID state
    uv run python src/collaborative_auditor/debug_cmd.py SESSION_ID bp
    uv run python src/collaborative_auditor/debug_cmd.py SESSION_ID target
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import jsonpatch
import websockets

from collaborative_auditor.debug_rendering import (
    find_branch_point,
    navigate_branch_point,
    render_branch_points,
    render_state,
    render_target,
    visible_messages,
)

SERVER = "ws://localhost:8000"
MODEL = "anthropic/claude-haiku-4-5-20251001"

# Commands that trigger generation and need to wait for a terminal state
_GENERATION_COMMANDS = {"step", "resample", "resample_target", "play"}


def _resolve_nav(
    last_state: dict[str, Any],
    bp_type: str,
    args: list[str],
) -> dict[str, Any]:
    """Build a switch_branch message from a nav-family command."""
    msg_idx = int(args[0])
    direction: str
    tc_idx: int | None = None

    if bp_type == "turn":
        direction = args[1]
    else:
        tc_idx = int(args[1])
        direction = args[2]

    vis = visible_messages(last_state)
    if msg_idx < 1 or msg_idx > len(vis):
        print(f"ERROR: Invalid index {msg_idx}, valid: 1-{len(vis)}")
        sys.exit(1)

    msg = vis[msg_idx - 1]

    if tc_idx is not None:
        tool_calls = msg.get("tool_calls") or []
        if tc_idx < 1 or tc_idx > len(tool_calls):
            print(f"ERROR: Invalid tool index {tc_idx}, valid: 1-{len(tool_calls)}")
            sys.exit(1)
        lookup_id = tool_calls[tc_idx - 1].get("id", "")
    else:
        lookup_id = msg.get("id", "")

    bp = find_branch_point(
        last_state, bp_type,
        msg_id=lookup_id if bp_type == "turn" else None,
        tc_id=lookup_id if bp_type != "turn" else None,
    )
    if not bp:
        print(f"ERROR: No {bp_type} branch point at that location")
        sys.exit(1)

    target_branch = navigate_branch_point(bp, direction)
    if target_branch is None:
        edge = "leftmost" if direction == "left" else "rightmost"
        print(f"ERROR: Already at {edge} branch")
        sys.exit(1)

    return {"type": "switch_branch", "branch_id": target_branch}


def build_command_message(
    cmd: str,
    args: list[str],
    last_state: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the WebSocket message for a command. Returns None for display-only commands."""
    if cmd == "start":
        prompt = " ".join(args) if args else "Test the model"
        return {
            "type": "start_session",
            "initial_prompt": prompt,
            "auditor_model": MODEL,
            "target_model": MODEL,
        }

    if cmd == "step":
        return {"type": "step"}
    if cmd == "play":
        return {"type": "play"}
    if cmd == "pause":
        return {"type": "pause"}
    if cmd == "feedback":
        return {"type": "feedback", "content": " ".join(args)}
    if cmd == "switch":
        return {"type": "switch_branch", "branch_id": args[0]}

    if last_state is None:
        print("ERROR: No session state available")
        sys.exit(1)

    vis = visible_messages(last_state)

    if cmd == "resample":
        idx = int(args[0])
        if idx < 1 or idx > len(vis):
            print(f"ERROR: Invalid index {idx}, valid: 1-{len(vis)}")
            sys.exit(1)
        m = vis[idx - 1]
        turn_id = (m.get("metadata") or {}).get("turn_id")
        if not turn_id:
            print(f"ERROR: Message [{idx}] has no turn_id (role={m.get('role')})")
            sys.exit(1)
        return {"type": "resample_turn", "turn_id": turn_id}

    if cmd == "resample_target":
        idx = int(args[0])
        tc_idx = int(args[1]) if len(args) > 1 else None
        if idx < 1 or idx > len(vis):
            print(f"ERROR: Invalid index {idx}")
            sys.exit(1)
        m = vis[idx - 1]
        tcs = m.get("tool_calls") or []
        if tc_idx is not None:
            if tc_idx < 1 or tc_idx > len(tcs):
                print(f"ERROR: Invalid tool index {tc_idx}, valid: 1-{len(tcs)}")
                sys.exit(1)
            tc = tcs[tc_idx - 1]
        else:
            tc = next((t for t in tcs if t.get("function") == "query_target"), None)
            if not tc:
                print(f"ERROR: No query_target in message [{idx}]")
                sys.exit(1)
        return {"type": "resample_target_response", "tool_call_id": tc["id"]}

    if cmd == "edit":
        idx = int(args[0])
        tc_idx = int(args[1])
        json_str = " ".join(args[2:])
        m = vis[idx - 1]
        tcs = m.get("tool_calls") or []
        tc = tcs[tc_idx - 1]
        return {
            "type": "edit_tool_call",
            "tool_call_id": tc["id"],
            "new_arguments": json.loads(json_str),
        }

    if cmd in ("nav", "nav_tool", "nav_target"):
        bp_type = {"nav": "turn", "nav_tool": "tool_call", "nav_target": "target"}[cmd]
        return _resolve_nav(last_state, bp_type, args)

    if cmd == "state":
        print(render_state(last_state))
        return None
    if cmd == "bp":
        print(render_branch_points(last_state))
        return None
    if cmd == "target":
        print(render_target(last_state))
        return None

    print(f"Unknown command: {cmd}")
    sys.exit(1)


async def run() -> None:
    if len(sys.argv) < 3:
        print("Usage: debug_cmd.py SESSION_ID COMMAND [ARGS...]")
        print("Commands: start, step, play, pause, feedback, resample, resample_target,")
        print("          edit, nav, nav_tool, nav_target, state, bp, target, switch")
        sys.exit(1)

    session_id = sys.argv[1]
    cmd = sys.argv[2]
    args = sys.argv[3:]

    uri = f"{SERVER}/ws/{session_id}"

    async with websockets.connect(uri) as ws:
        last_state: dict[str, Any] | None = None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2)
            data = json.loads(raw)
            if data.get("type") == "state":
                last_state = data["state"]
        except asyncio.TimeoutError:
            pass

        msg = build_command_message(cmd, args, last_state)
        if msg is None:
            return

        await ws.send(json.dumps(msg))

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                data = json.loads(raw)
                if data.get("type") == "state":
                    last_state = data["state"]
                elif data.get("type") == "patch":
                    if last_state is not None:
                        last_state = jsonpatch.apply_patch(last_state, data["ops"])
                elif data.get("type") == "error":
                    print(f"SERVER ERROR: {data.get('message', '?')}")
                    break
                else:
                    continue

                if last_state is not None:
                    ps = last_state.get("playback_state", "")
                    if ps in ("paused", "idle") and cmd in _GENERATION_COMMANDS:
                        break
                    if cmd not in _GENERATION_COMMANDS:
                        break
            except asyncio.TimeoutError:
                break

        if last_state:
            print(render_state(last_state))
        else:
            print("No state received")


if __name__ == "__main__":
    asyncio.run(run())
