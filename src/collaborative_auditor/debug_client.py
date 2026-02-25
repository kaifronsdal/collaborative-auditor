#!/usr/bin/env python3
"""Text-based debug client for the Collaborative Auditor WebSocket server.

Connects to the server, displays session state as ASCII art, and accepts
commands via stdin for interactive debugging.

Usage:
    uv run python src/collaborative_auditor/debug_client.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any

import jsonpatch

try:
    import websockets
    import websockets.exceptions
except ImportError:
    print("ERROR: 'websockets' package is required. Install with: uv add websockets")
    sys.exit(1)

from collaborative_auditor.debug_rendering import (
    find_branch_point,
    navigate_branch_point,
    render_branch_points,
    render_state,
    render_target,
    truncate,
    visible_messages,
)


SERVER_HOST = os.environ.get("COLLAB_SERVER_HOST", "localhost")
SERVER_PORT = int(os.environ.get("COLLAB_SERVER_PORT", "8000"))
DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"


class DebugClient:
    """WebSocket client for the Collaborative Auditor server."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.ws: Any = None
        self.view_state: dict[str, Any] | None = None
        self.connected = False
        self.server_url = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws/{self.session_id}"
        self._receive_task: asyncio.Task[None] | None = None

    async def connect(self) -> bool:
        try:
            print(f"Connecting to {self.server_url} ...")
            self.ws = await websockets.connect(self.server_url)
            self.connected = True
            print(f"Connected! Session ID: {self.session_id}")
            return True
        except (ConnectionRefusedError, OSError) as e:
            print(f"ERROR: Could not connect to server at {self.server_url}")
            print(f"  {e}")
            print("  Make sure the server is running: uv run python -m collaborative_auditor.server")
            return False

    async def disconnect(self) -> None:
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()
        self.connected = False
        print("Disconnected.")

    async def send(self, message: dict[str, Any]) -> None:
        if not self.ws or not self.connected:
            print("ERROR: Not connected.")
            return
        await self.ws.send(json.dumps(message))

    async def receive_loop(self) -> None:
        try:
            async for raw_msg in self.ws:
                data = json.loads(raw_msg)
                await self._handle_server_message(data)
        except websockets.exceptions.ConnectionClosed as e:
            print(f"\nConnection closed: {e}")
            self.connected = False
        except asyncio.CancelledError:
            pass

    async def _handle_server_message(self, data: dict[str, Any]) -> None:
        msg_type = data.get("type")

        if msg_type == "state":
            self.view_state = data["state"]
            self._redisplay()

        elif msg_type == "patch":
            if self.view_state:
                self.view_state = jsonpatch.apply_patch(self.view_state, data["ops"])
                self._redisplay()

        elif msg_type == "error":
            print(f"\n╔══ SERVER ERROR ══╗")
            print(f"║ {data.get('message', 'Unknown error')}")
            print(f"╚══════════════════╝\n")

    def _redisplay(self) -> None:
        print("\033[2J\033[H", end="")
        if self.view_state:
            print(render_state(self.view_state))
        else:
            print("No state yet. Use 'start <prompt>' to begin.")
        print("> ", end="", flush=True)


# ---------------------------------------------------------------------------
# Navigation helper
# ---------------------------------------------------------------------------


async def _handle_nav(
    client: DebugClient,
    bp_type: str,
    msg_idx: int,
    direction: str,
    tc_idx: int | None = None,
) -> None:
    """Unified handler for nav, nav_tool, and nav_target commands."""
    if client.view_state is None:
        print("No state yet.")
        return

    vis = visible_messages(client.view_state)
    if msg_idx < 1 or msg_idx > len(vis):
        print(f"Invalid message index. Valid range: 1-{len(vis)}")
        return

    msg = vis[msg_idx - 1]

    if tc_idx is not None:
        tool_calls = msg.get("tool_calls") or []
        if tc_idx < 1 or tc_idx > len(tool_calls):
            print(f"Invalid tool index. Valid range: 1-{len(tool_calls)}")
            return
        tc = tool_calls[tc_idx - 1]
        lookup_id = tc.get("id", "")
    else:
        lookup_id = msg.get("id", "")

    bp = find_branch_point(
        client.view_state, bp_type,
        msg_id=lookup_id if bp_type == "turn" else None,
        tc_id=lookup_id if bp_type != "turn" else None,
    )
    if not bp:
        print(f"No {bp_type}-level branch point at that location.")
        return

    target_branch = navigate_branch_point(bp, direction)
    if target_branch is None:
        edge = "leftmost" if direction == "left" else "rightmost"
        print(f"Already at the {edge} branch.")
        return

    await client.send({"type": "switch_branch", "branch_id": target_branch})
    print(f"Nav {bp_type} {direction} to branch {target_branch[:8]}...")


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------


async def handle_command(client: DebugClient, line: str) -> bool:
    """Handle a user command. Returns False if we should quit."""
    line = line.strip()
    if not line:
        return True

    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    try:
        if cmd in ("quit", "exit", "q"):
            return False

        elif cmd == "start":
            if not rest:
                print("Usage: start <prompt>")
                return True
            await client.send({
                "type": "start_session",
                "initial_prompt": rest,
                "auditor_model": DEFAULT_MODEL,
                "target_model": DEFAULT_MODEL,
            })
            print(f"Starting session with prompt: {truncate(rest, 60)}")

        elif cmd == "step":
            await client.send({"type": "step"})
            print("Stepping...")

        elif cmd == "play":
            await client.send({"type": "play"})
            print("Playing...")

        elif cmd == "pause":
            await client.send({"type": "pause"})
            print("Pausing...")

        elif cmd == "feedback":
            if not rest:
                print("Usage: feedback <text>")
                return True
            await client.send({"type": "feedback", "content": rest})
            print(f"Sent feedback: {truncate(rest, 60)}")

        elif cmd == "resample":
            if not rest:
                print("Usage: resample <msg_index>")
                return True
            msg_idx = int(rest)
            vis = visible_messages(client.view_state) if client.view_state else []
            if msg_idx < 1 or msg_idx > len(vis):
                print(f"Invalid index. Valid range: 1-{len(vis)}")
                return True
            msg = vis[msg_idx - 1]
            turn_id = (msg.get("metadata") or {}).get("turn_id")
            if not turn_id:
                print(f"Message [{msg_idx}] has no turn_id. resample only works on assistant messages.")
                return True
            await client.send({"type": "resample_turn", "turn_id": turn_id})
            print(f"Resampling turn at message [{msg_idx}], turn_id={turn_id[:8]}...")

        elif cmd == "resample_target":
            if not rest:
                print("Usage: resample_target <msg_index> [tool_index]")
                return True
            rt_parts = rest.split()
            msg_idx = int(rt_parts[0])
            tool_idx = int(rt_parts[1]) if len(rt_parts) > 1 else None
            vis = visible_messages(client.view_state) if client.view_state else []
            if msg_idx < 1 or msg_idx > len(vis):
                print(f"Invalid index. Valid range: 1-{len(vis)}")
                return True
            msg = vis[msg_idx - 1]
            tool_calls = msg.get("tool_calls") or []
            if tool_idx is not None:
                if tool_idx < 1 or tool_idx > len(tool_calls):
                    print(f"Invalid tool index. Valid range: 1-{len(tool_calls)}")
                    return True
                qt_tc = tool_calls[tool_idx - 1]
            else:
                qt_tc = next((tc for tc in tool_calls if tc.get("function") == "query_target"), None)
            if not qt_tc:
                print(f"Message [{msg_idx}] has no query_target tool call.")
                return True
            await client.send({
                "type": "resample_target_response",
                "tool_call_id": qt_tc["id"],
            })
            print(f"Resampling target response for tool_call_id={qt_tc['id'][:8]}...")

        elif cmd == "edit":
            edit_parts = rest.split(maxsplit=2)
            if len(edit_parts) < 3:
                print("Usage: edit <msg_index> <tool_index> <json_args>")
                return True
            msg_idx = int(edit_parts[0])
            tc_idx = int(edit_parts[1])
            try:
                new_args = json.loads(edit_parts[2])
            except json.JSONDecodeError as e:
                print(f"Invalid JSON: {e}")
                return True

            vis = visible_messages(client.view_state) if client.view_state else []
            if msg_idx < 1 or msg_idx > len(vis):
                print(f"Invalid message index. Valid range: 1-{len(vis)}")
                return True
            msg = vis[msg_idx - 1]
            tool_calls = msg.get("tool_calls") or []
            if tc_idx < 1 or tc_idx > len(tool_calls):
                print(f"Invalid tool index. Valid range: 1-{len(tool_calls)}")
                return True
            tc = tool_calls[tc_idx - 1]
            await client.send({
                "type": "edit_tool_call",
                "tool_call_id": tc["id"],
                "new_arguments": new_args,
            })
            print(f"Editing tool call {tc.get('function')}...")

        elif cmd == "switch":
            if not rest:
                print("Usage: switch <branch_id>")
                return True
            await client.send({"type": "switch_branch", "branch_id": rest})
            print(f"Switching to branch {rest[:8]}...")

        elif cmd == "nav":
            nav_parts = rest.split()
            if len(nav_parts) != 2 or nav_parts[1] not in ("left", "right"):
                print("Usage: nav <msg_index> left|right")
                return True
            await _handle_nav(client, "turn", int(nav_parts[0]), nav_parts[1])

        elif cmd == "nav_tool":
            nt_parts = rest.split()
            if len(nt_parts) != 3 or nt_parts[2] not in ("left", "right"):
                print("Usage: nav_tool <msg_index> <tool_index> left|right")
                return True
            await _handle_nav(client, "tool_call", int(nt_parts[0]), nt_parts[2], tc_idx=int(nt_parts[1]))

        elif cmd == "nav_target":
            nt_parts = rest.split()
            if len(nt_parts) != 3 or nt_parts[2] not in ("left", "right"):
                print("Usage: nav_target <msg_index> <tool_index> left|right")
                return True
            await _handle_nav(client, "target", int(nt_parts[0]), nt_parts[2], tc_idx=int(nt_parts[1]))

        elif cmd in ("bp", "branch_points"):
            if client.view_state:
                print(render_branch_points(client.view_state))
            else:
                print("No state yet.")

        elif cmd == "state":
            client._redisplay()

        elif cmd == "target":
            if client.view_state:
                print(render_target(client.view_state))
            else:
                print("No state yet.")

        elif cmd == "help":
            print_help()

        else:
            print(f"Unknown command: {cmd}. Type 'help' for available commands.")

    except Exception as e:
        print(f"Error executing command '{cmd}': {e}")

    return True


def print_help() -> None:
    print("""
Commands:
  start <prompt>                            Start a new session
  step                                      Execute one auditor turn
  play                                      Start continuous generation
  pause                                     Pause generation
  feedback <text>                           Send researcher feedback
  resample <msg_index>                      Resample auditor turn at message N
  resample_target <msg_index> [tool_index]  Resample target response
                                             (tool_index is 1-based, optional)
  edit <msg_idx> <tc_idx> <json>            Edit tool call arguments

Navigation:
  nav <msg_index> left|right                Navigate turn-level branch point
  nav_tool <msg_idx> <tc_idx> left|right    Navigate tool-call-level branch point
  nav_target <msg_idx> <tc_idx> left|right  Navigate target-response-level branch point
  switch <branch_id>                        Switch to a branch by ID (escape hatch)
  bp / branch_points                        List all branch points

Display:
  state                                     Re-display current state
  target                                    Show full target state
  help                                      Show this help
  quit                                      Exit
""")


async def main() -> None:
    print("╔════════════════════════════════════════╗")
    print("║  Collaborative Auditor Debug Client    ║")
    print("╚════════════════════════════════════════╝")
    print()

    client = DebugClient()

    if not await client.connect():
        return

    client._receive_task = asyncio.create_task(client.receive_loop())

    await asyncio.sleep(0.5)

    print_help()
    print("> ", end="", flush=True)

    loop = asyncio.get_event_loop()

    try:
        while client.connected:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except EOFError:
                break
            if not line:
                break
            line = line.strip()
            should_continue = await handle_command(client, line)
            if not should_continue:
                break
            await asyncio.sleep(0.1)
            print("> ", end="", flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
