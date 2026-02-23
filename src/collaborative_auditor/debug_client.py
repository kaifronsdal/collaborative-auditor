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
import re
import sys
import uuid
from typing import Any

try:
    import websockets
    import websockets.exceptions
except ImportError:
    print("ERROR: 'websockets' package is required. Install with: uv add websockets")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────

SERVER_HOST = os.environ.get("COLLAB_SERVER_HOST", "localhost")
SERVER_PORT = int(os.environ.get("COLLAB_SERVER_PORT", "8000"))
DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"
MAX_CONTENT_WIDTH = 80
BOX_WIDTH = 62


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def truncate(text: str, max_len: int = MAX_CONTENT_WIDTH) -> str:
    """Truncate text and add ellipsis if needed."""
    if not text:
        return ""
    # Flatten newlines for display
    text = text.replace("\n", " ").replace("\r", "")
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def extract_text_content(content: Any) -> str:
    """Extract plain text from a message content field.

    Content can be a string or a list of content parts (dicts with 'type' key).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif part.get("type") == "reasoning":
                    reasoning = part.get("reasoning", "")
                    if reasoning:
                        parts.append(f"[thinking: {truncate(reasoning, 40)}]")
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(parts)
    return str(content) if content else ""


def extract_target_response(content: str) -> str | None:
    """Extract target response text from <target_response> XML tags."""
    match = re.search(r"<target_response>(.*?)</target_response>", content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


# ──────────────────────────────────────────────────────────────────────
# Display Rendering
# ──────────────────────────────────────────────────────────────────────


class DisplayRenderer:
    """Renders the ViewState as ASCII art."""

    def __init__(self) -> None:
        self.view_state: dict[str, Any] | None = None

    def update(self, view_state: dict[str, Any]) -> None:
        self.view_state = view_state

    def render(self) -> str:
        """Render the full display."""
        if self.view_state is None:
            return "No state yet. Use 'start <prompt>' to begin.\n"

        lines: list[str] = []
        lines.append("")
        lines.extend(self._render_header())
        lines.append("")
        lines.extend(self._render_messages())
        lines.append("")
        lines.extend(self._render_target_summary())
        lines.append("")
        return "\n".join(lines)

    def _render_header(self) -> list[str]:
        vs = self.view_state
        assert vs is not None
        sid = vs["session_id"][:8] + ".."
        n_branches = len(vs.get("branches", []))
        branch_idx = vs.get("current_branch_index", 0) + 1
        auditor = vs.get("auditor_model", "?").split("/")[-1][:12]
        target = vs.get("target_model", "?").split("/")[-1][:12]
        state = vs.get("playback_state", "idle")
        generating = vs.get("is_generating", False)
        if generating:
            state += " (generating)"

        w = BOX_WIDTH
        top = "╔" + "═" * w + "╗"
        bot = "╚" + "═" * w + "╝"
        line1 = f" Session: {sid}  │ Branches: {n_branches}  │ Current: {branch_idx}/{n_branches}"
        line2 = f" Auditor: {auditor} │ Target: {target} │ State: {state}"

        return [
            top,
            "║" + line1.ljust(w) + "║",
            "║" + line2.ljust(w) + "║",
            bot,
        ]

    def _render_messages(self) -> list[str]:
        vs = self.view_state
        assert vs is not None
        branch = vs.get("current_branch", {})
        messages: list[dict[str, Any]] = branch.get("auditor_messages", [])
        branch_points: list[dict[str, Any]] = vs.get("branch_points", [])

        lines: list[str] = []
        visible_idx = 0  # 1-indexed user-facing counter

        for msg in messages:
            role = msg.get("role", "")
            # Skip tool result messages (rendered inline with their parent)
            if role == "tool":
                continue

            visible_idx += 1
            content = extract_text_content(msg.get("content", ""))
            metadata = msg.get("metadata", {}) or {}
            source = metadata.get("source", "")
            msg_id = msg.get("id", "")

            # Determine display label
            if role == "system":
                label = "System"
            elif role == "user":
                label = "Researcher" if source == "Researcher" else "System"
            elif role == "assistant":
                label = "Auditor"
            else:
                label = role.capitalize()

            # Check for turn-level branch point
            bp_indicator = self._get_branch_indicator(
                branch_points, "turn", message_id=msg_id
            )

            # Build message line
            bp_suffix = f"  {bp_indicator}" if bp_indicator else ""
            lines.append(
                f"[{visible_idx}] {label}: {truncate(content)}{bp_suffix}"
            )

            # Render tool calls for assistant messages
            if role == "assistant":
                tool_calls = msg.get("tool_calls") or []
                for tc_idx, tc in enumerate(tool_calls):
                    is_last_tc = tc_idx == len(tool_calls) - 1
                    tc_lines = self._render_tool_call(
                        tc, messages, branch_points, is_last=is_last_tc
                    )
                    lines.extend(tc_lines)

        return lines

    def _render_tool_call(
        self,
        tc: dict[str, Any],
        all_messages: list[dict[str, Any]],
        branch_points: list[dict[str, Any]],
        is_last: bool = False,
    ) -> list[str]:
        """Render a single tool call with its result."""
        lines: list[str] = []
        tc_id = tc.get("id", "")
        func_name = tc.get("function", "?")
        args = tc.get("arguments", {})

        # Format arguments
        args_str = ", ".join(f'{k}="{truncate(str(v), 30)}"' for k, v in args.items())
        call_str = f"{func_name}({args_str})"

        # Tree characters
        branch_char = "└─" if is_last else "├─"
        cont_char = "   " if is_last else "│  "

        # Check for tool-call-level branch point
        tc_bp = self._get_branch_indicator(
            branch_points, "tool_call", tool_call_id=tc_id
        )
        tc_bp_suffix = f"  {tc_bp}" if tc_bp else ""

        lines.append(f"    {branch_char} {call_str}{tc_bp_suffix}")

        # Find matching tool result
        result_msg = None
        for m in all_messages:
            if m.get("role") == "tool" and m.get("tool_call_id") == tc_id:
                result_msg = m
                break

        if result_msg:
            result_content = extract_text_content(result_msg.get("content", ""))
            error = result_msg.get("error")

            if error:
                error_msg = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
                lines.append(f"    {cont_char} → ✗ Error: {truncate(error_msg, 60)}")
            else:
                # Check for target response
                target_text = extract_target_response(result_content) if isinstance(result_content, str) else None
                if target_text:
                    # Check for target-level branch point
                    target_bp = self._get_branch_indicator(
                        branch_points, "target", tool_call_id=tc_id
                    )
                    target_bp_suffix = f"  {target_bp}" if target_bp else ""
                    lines.append(
                        f'    {cont_char} → Target Response: "{truncate(target_text, 50)}"{target_bp_suffix}'
                    )
                else:
                    lines.append(
                        f"    {cont_char} → ✓ {truncate(result_content, 60)}"
                    )

        return lines

    def _get_branch_indicator(
        self,
        branch_points: list[dict[str, Any]],
        branch_type: str,
        message_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> str:
        """Get a '< n/m > (type)' indicator for a branch point, if one exists."""
        for bp in branch_points:
            if bp.get("branch_type") != branch_type:
                continue
            if branch_type == "turn" and message_id:
                if bp.get("message_id") == message_id:
                    idx = bp.get("current_index", 0) + 1
                    total = bp.get("total_branches", 1)
                    return f"< {idx}/{total} > (turn)"
            elif branch_type == "tool_call" and tool_call_id:
                if bp.get("tool_call_id") == tool_call_id:
                    idx = bp.get("current_index", 0) + 1
                    total = bp.get("total_branches", 1)
                    return f"< {idx}/{total} > (tool_call)"
            elif branch_type == "target" and tool_call_id:
                if bp.get("tool_call_id") == tool_call_id:
                    idx = bp.get("current_index", 0) + 1
                    total = bp.get("total_branches", 1)
                    return f"< {idx}/{total} > (target)"
        return ""

    def _render_target_summary(self) -> list[str]:
        vs = self.view_state
        assert vs is not None
        branch = vs.get("current_branch", {})
        target_state = branch.get("target_state", {})
        n_msgs = len(target_state.get("messages", []))
        n_tools = len(target_state.get("tools", []))
        return [f"── Target State: {n_msgs} messages, {n_tools} tools ──"]

    def get_visible_messages(self) -> list[dict[str, Any]]:
        """Return visible (non-tool) messages for index-based commands."""
        if self.view_state is None:
            return []
        branch = self.view_state.get("current_branch", {})
        messages = branch.get("auditor_messages", [])
        return [m for m in messages if m.get("role") != "tool"]

    def get_all_messages(self) -> list[dict[str, Any]]:
        """Return all messages including tool results."""
        if self.view_state is None:
            return []
        branch = self.view_state.get("current_branch", {})
        return branch.get("auditor_messages", [])

    def get_first_branch_point(self) -> dict[str, Any] | None:
        """Get the first branch point (for left/right navigation)."""
        if self.view_state is None:
            return None
        bps = self.view_state.get("branch_points", [])
        return bps[0] if bps else None

    def render_target_state(self) -> str:
        """Render the full target state (all messages)."""
        if self.view_state is None:
            return "No state yet.\n"

        branch = self.view_state.get("current_branch", {})
        target_state = branch.get("target_state", {})
        messages = target_state.get("messages", [])
        tools = target_state.get("tools", [])

        lines: list[str] = []
        lines.append("")
        lines.append("═══ Full Target State ═══")
        lines.append(f"Tools: {len(tools)}")
        for t in tools:
            lines.append(f"  - {t.get('name', '?')}: {truncate(t.get('description', ''), 50)}")
        lines.append(f"Messages: {len(messages)}")
        lines.append("")

        for i, msg in enumerate(messages, 1):
            role = msg.get("role", "?")
            content = extract_text_content(msg.get("content", ""))
            lines.append(f"  [{i}] {role}: {truncate(content, 100)}")

            # Show tool calls
            for tc in msg.get("tool_calls", []) or []:
                func = tc.get("function", "?")
                args_str = json.dumps(tc.get("arguments", {}))
                lines.append(f"       └─ {func}({truncate(args_str, 60)})")

        lines.append("")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────


class DebugClient:
    """WebSocket client for the Collaborative Auditor server."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.ws: Any = None  # websockets connection
        self.renderer = DisplayRenderer()
        self.connected = False
        self.server_url = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws/{self.session_id}"
        self._receive_task: asyncio.Task[None] | None = None

    async def connect(self) -> bool:
        """Connect to the WebSocket server."""
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
        """Disconnect from the server."""
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
        """Send a JSON message to the server."""
        if not self.ws or not self.connected:
            print("ERROR: Not connected.")
            return
        await self.ws.send(json.dumps(message))

    async def receive_loop(self) -> None:
        """Background loop that receives and processes server messages."""
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
        """Handle a message from the server."""
        msg_type = data.get("type")

        if msg_type == "state":
            self.renderer.update(data["state"])
            self._redisplay()

        elif msg_type == "delta_turn_start":
            # Apply delta: add new assistant message
            vs = self.renderer.view_state
            if vs:
                branch = vs.get("current_branch", {})
                msgs = branch.get("auditor_messages", [])
                msgs.append(data["message"])
                self._redisplay()

        elif msg_type == "delta_tool_call":
            # Apply delta: add tool call to last assistant message
            vs = self.renderer.view_state
            if vs:
                branch = vs.get("current_branch", {})
                msgs = branch.get("auditor_messages", [])
                for m in reversed(msgs):
                    if m.get("role") == "assistant":
                        if m.get("tool_calls") is None:
                            m["tool_calls"] = []
                        m["tool_calls"].append(data["tool_call"])
                        break
                self._redisplay()

        elif msg_type == "delta_tool_result":
            # Apply delta: add tool result message and update target state
            vs = self.renderer.view_state
            if vs:
                branch = vs.get("current_branch", {})
                msgs = branch.get("auditor_messages", [])
                msgs.append(data["tool_result"])
                if "target_state" in data:
                    branch["target_state"] = data["target_state"]
                self._redisplay()

        elif msg_type == "error":
            print(f"\n╔══ SERVER ERROR ══╗")
            print(f"║ {data.get('message', 'Unknown error')}")
            print(f"╚══════════════════╝\n")

    def _redisplay(self) -> None:
        """Clear screen and redisplay the state."""
        # Use ANSI clear screen
        print("\033[2J\033[H", end="")
        print(self.renderer.render())
        # Re-show the prompt
        print("> ", end="", flush=True)


# ──────────────────────────────────────────────────────────────────────
# Command Handler
# ──────────────────────────────────────────────────────────────────────


async def handle_command(client: DebugClient, line: str) -> bool:
    """Handle a user command. Returns False if we should quit."""
    line = line.strip()
    if not line:
        return True

    parts = line.split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    try:
        if cmd == "quit" or cmd == "exit" or cmd == "q":
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
            visible = client.renderer.get_visible_messages()
            if msg_idx < 1 or msg_idx > len(visible):
                print(f"Invalid index. Valid range: 1-{len(visible)}")
                return True
            msg = visible[msg_idx - 1]
            turn_id = (msg.get("metadata") or {}).get("turn_id")
            if not turn_id:
                print(f"Message [{msg_idx}] has no turn_id (role={msg.get('role')}).")
                print("  resample only works on assistant messages.")
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
            visible = client.renderer.get_visible_messages()
            if msg_idx < 1 or msg_idx > len(visible):
                print(f"Invalid index. Valid range: 1-{len(visible)}")
                return True
            msg = visible[msg_idx - 1]
            tool_calls = msg.get("tool_calls") or []
            qt_tc = None
            if tool_idx is not None:
                # Use specific tool call by 1-based index
                if tool_idx < 1 or tool_idx > len(tool_calls):
                    print(f"Invalid tool index. Valid range: 1-{len(tool_calls)}")
                    return True
                qt_tc = tool_calls[tool_idx - 1]
            else:
                # Find the first query_target tool call in this message
                for tc in tool_calls:
                    if tc.get("function") == "query_target":
                        qt_tc = tc
                        break
            if not qt_tc:
                print(f"Message [{msg_idx}] has no query_target tool call.")
                return True
            await client.send({
                "type": "resample_target_response",
                "tool_call_id": qt_tc["id"],
            })
            print(f"Resampling target response for tool_call_id={qt_tc['id'][:8]}...")

        elif cmd == "edit":
            # edit <msg_index> <tool_index> <json_args>
            edit_parts = rest.split(maxsplit=2)
            if len(edit_parts) < 3:
                print("Usage: edit <msg_index> <tool_index> <json_args>")
                return True
            msg_idx = int(edit_parts[0])
            tc_idx = int(edit_parts[1])
            new_args_str = edit_parts[2]
            try:
                new_args = json.loads(new_args_str)
            except json.JSONDecodeError as e:
                print(f"Invalid JSON: {e}")
                return True

            visible = client.renderer.get_visible_messages()
            if msg_idx < 1 or msg_idx > len(visible):
                print(f"Invalid message index. Valid range: 1-{len(visible)}")
                return True
            msg = visible[msg_idx - 1]
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
            # nav <msg_index> left|right -- navigate turn-level branch point
            nav_parts = rest.split()
            if len(nav_parts) != 2 or nav_parts[1] not in ("left", "right"):
                print("Usage: nav <msg_index> left|right")
                return True
            msg_idx = int(nav_parts[0])
            direction = nav_parts[1]
            visible = client.renderer.get_visible_messages()
            if msg_idx < 1 or msg_idx > len(visible):
                print(f"Invalid index. Valid range: 1-{len(visible)}")
                return True
            msg = visible[msg_idx - 1]
            msg_id = msg.get("id", "")
            bps = (client.renderer.view_state or {}).get("branch_points", [])
            bp = None
            for b in bps:
                if b.get("branch_type") == "turn" and b.get("message_id") == msg_id:
                    bp = b
                    break
            if not bp:
                print(f"No turn-level branch point at message [{msg_idx}].")
                return True
            current_idx = bp.get("current_index", 0)
            branch_ids = bp.get("branch_ids", [])
            if direction == "left":
                if current_idx > 0:
                    target_id = branch_ids[current_idx - 1]
                    await client.send({"type": "switch_branch", "branch_id": target_id})
                    print(f"Nav left to branch {target_id[:8]}...")
                else:
                    print("Already at the leftmost branch.")
            else:
                if current_idx < len(branch_ids) - 1:
                    target_id = branch_ids[current_idx + 1]
                    await client.send({"type": "switch_branch", "branch_id": target_id})
                    print(f"Nav right to branch {target_id[:8]}...")
                else:
                    print("Already at the rightmost branch.")

        elif cmd == "nav_tool":
            # nav_tool <msg_index> <tool_index> left|right
            nt_parts = rest.split()
            if len(nt_parts) != 3 or nt_parts[2] not in ("left", "right"):
                print("Usage: nav_tool <msg_index> <tool_index> left|right")
                return True
            msg_idx = int(nt_parts[0])
            tc_idx = int(nt_parts[1])
            direction = nt_parts[2]
            visible = client.renderer.get_visible_messages()
            if msg_idx < 1 or msg_idx > len(visible):
                print(f"Invalid message index. Valid range: 1-{len(visible)}")
                return True
            msg = visible[msg_idx - 1]
            tool_calls = msg.get("tool_calls") or []
            if tc_idx < 1 or tc_idx > len(tool_calls):
                print(f"Invalid tool index. Valid range: 1-{len(tool_calls)}")
                return True
            tc = tool_calls[tc_idx - 1]
            tc_id = tc.get("id", "")
            bps = (client.renderer.view_state or {}).get("branch_points", [])
            bp = None
            for b in bps:
                if b.get("branch_type") == "tool_call" and b.get("tool_call_id") == tc_id:
                    bp = b
                    break
            if not bp:
                print(f"No tool_call-level branch point at message [{msg_idx}] tool [{tc_idx}].")
                return True
            current_idx = bp.get("current_index", 0)
            branch_ids = bp.get("branch_ids", [])
            if direction == "left":
                if current_idx > 0:
                    target_id = branch_ids[current_idx - 1]
                    await client.send({"type": "switch_branch", "branch_id": target_id})
                    print(f"Nav tool left to branch {target_id[:8]}...")
                else:
                    print("Already at the leftmost branch.")
            else:
                if current_idx < len(branch_ids) - 1:
                    target_id = branch_ids[current_idx + 1]
                    await client.send({"type": "switch_branch", "branch_id": target_id})
                    print(f"Nav tool right to branch {target_id[:8]}...")
                else:
                    print("Already at the rightmost branch.")

        elif cmd == "nav_target":
            # nav_target <msg_index> <tool_index> left|right
            ntar_parts = rest.split()
            if len(ntar_parts) != 3 or ntar_parts[2] not in ("left", "right"):
                print("Usage: nav_target <msg_index> <tool_index> left|right")
                return True
            msg_idx = int(ntar_parts[0])
            tc_idx = int(ntar_parts[1])
            direction = ntar_parts[2]
            visible = client.renderer.get_visible_messages()
            if msg_idx < 1 or msg_idx > len(visible):
                print(f"Invalid message index. Valid range: 1-{len(visible)}")
                return True
            msg = visible[msg_idx - 1]
            tool_calls = msg.get("tool_calls") or []
            if tc_idx < 1 or tc_idx > len(tool_calls):
                print(f"Invalid tool index. Valid range: 1-{len(tool_calls)}")
                return True
            tc = tool_calls[tc_idx - 1]
            tc_id = tc.get("id", "")
            bps = (client.renderer.view_state or {}).get("branch_points", [])
            bp = None
            for b in bps:
                if b.get("branch_type") == "target" and b.get("tool_call_id") == tc_id:
                    bp = b
                    break
            if not bp:
                print(f"No target-level branch point at message [{msg_idx}] tool [{tc_idx}].")
                return True
            current_idx = bp.get("current_index", 0)
            branch_ids = bp.get("branch_ids", [])
            if direction == "left":
                if current_idx > 0:
                    target_id = branch_ids[current_idx - 1]
                    await client.send({"type": "switch_branch", "branch_id": target_id})
                    print(f"Nav target left to branch {target_id[:8]}...")
                else:
                    print("Already at the leftmost branch.")
            else:
                if current_idx < len(branch_ids) - 1:
                    target_id = branch_ids[current_idx + 1]
                    await client.send({"type": "switch_branch", "branch_id": target_id})
                    print(f"Nav target right to branch {target_id[:8]}...")
                else:
                    print("Already at the rightmost branch.")

        elif cmd in ("bp", "branch_points"):
            bps = (client.renderer.view_state or {}).get("branch_points", [])
            if not bps:
                print("No branch points.")
                return True
            print(f"\nBranch Points ({len(bps)}):")
            for i, bp in enumerate(bps):
                bp_type = bp.get("branch_type", "?")
                bp_msg = bp.get("message_id", "")
                bp_tc = bp.get("tool_call_id", "")
                idx = bp.get("current_index", 0) + 1
                total = bp.get("total_branches", 0)
                branch_ids = bp.get("branch_ids", [])
                msg_str = f"msg={bp_msg[:8]}" if bp_msg else "msg=none"
                tc_str = f"tc={bp_tc[:8]}" if bp_tc else "tc=none"
                print(f"  [{i+1}] type={bp_type}, {msg_str}, {tc_str}, < {idx}/{total} >")
                for j, bid in enumerate(branch_ids):
                    marker = " ◀" if j == (idx - 1) else ""
                    print(f"       branch[{j}]: {bid[:12]}{marker}")
            print()

        elif cmd == "state":
            client._redisplay()

        elif cmd == "target":
            print(client.renderer.render_target_state())

        elif cmd == "help":
            print_help()

        else:
            print(f"Unknown command: {cmd}. Type 'help' for available commands.")

    except Exception as e:
        print(f"Error executing command '{cmd}': {e}")

    return True


def print_help() -> None:
    """Print the help text."""
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


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    """Main entry point."""
    print("╔════════════════════════════════════════╗")
    print("║  Collaborative Auditor Debug Client    ║")
    print("╚════════════════════════════════════════╝")
    print()

    client = DebugClient()

    if not await client.connect():
        return

    # Start the receive loop in the background
    client._receive_task = asyncio.create_task(client.receive_loop())

    # Give time for initial state to arrive
    await asyncio.sleep(0.5)

    print_help()
    print("> ", end="", flush=True)

    loop = asyncio.get_event_loop()

    try:
        while client.connected:
            # Read input in a thread to avoid blocking the event loop
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except EOFError:
                break
            if not line:
                # EOF
                break
            line = line.strip()
            should_continue = await handle_command(client, line)
            if not should_continue:
                break
            # Small delay so any incoming server messages can be displayed
            await asyncio.sleep(0.1)
            print("> ", end="", flush=True)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
