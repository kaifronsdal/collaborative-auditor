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
import re
import sys
import time
from typing import Any

import websockets

SERVER = "ws://localhost:8000"
MODEL = "anthropic/claude-haiku-4-5-20251001"
MAX_WIDTH = 90


def truncate(text: str, max_len: int = MAX_WIDTH) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").replace("\r", "")
    return text[:max_len - 3] + "..." if len(text) > max_len else text


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif p.get("type") == "reasoning":
                    r = p.get("reasoning", "")
                    if r:
                        parts.append(f"[thinking: {truncate(r, 40)}]")
        return " ".join(parts)
    return str(content) if content else ""


def extract_target_response(content: str) -> str | None:
    m = re.search(r"<target_response[^>]*>(.*?)</target_response>", content, re.DOTALL)
    return m.group(1).strip() if m else None


def get_bp_indicator(branch_points: list, bp_type: str, 
                     msg_id: str | None = None, tc_id: str | None = None) -> str:
    for bp in branch_points:
        if bp.get("branch_type") != bp_type:
            continue
        if bp_type == "turn" and msg_id and bp.get("message_id") == msg_id:
            return f"< {bp['current_index']+1}/{bp['total_branches']} > (turn)"
        if bp_type == "tool_call" and tc_id and bp.get("tool_call_id") == tc_id:
            return f"< {bp['current_index']+1}/{bp['total_branches']} > (tool_call)"
        if bp_type == "target" and tc_id and bp.get("tool_call_id") == tc_id:
            return f"< {bp['current_index']+1}/{bp['total_branches']} > (target)"
    return ""


def render_state(vs: dict) -> str:
    lines = []
    
    # Header
    sid = vs["session_id"][:8]
    n_br = len(vs.get("branches", []))
    cur = vs.get("current_branch_index", 0) + 1
    aud = vs.get("auditor_model", "?").split("/")[-1][:15]
    tgt = vs.get("target_model", "?").split("/")[-1][:15]
    ps = vs.get("playback_state", "idle")
    gen = vs.get("is_generating", False)
    ver = vs.get("version", 0)
    
    w = 70
    lines.append("╔" + "═" * w + "╗")
    lines.append("║" + f" Session: {sid}  │ Branches: {n_br}  │ Current: {cur}/{n_br}  │ v{ver}".ljust(w) + "║")
    lines.append("║" + f" Auditor: {aud} │ Target: {tgt} │ {ps}{' (generating)' if gen else ''}".ljust(w) + "║")
    lines.append("╚" + "═" * w + "╝")
    lines.append("")
    
    # Messages
    branch = vs.get("current_branch", {})
    all_msgs = branch.get("auditor_messages", [])
    bps = vs.get("branch_points", [])
    
    vis_idx = 0
    for msg in all_msgs:
        role = msg.get("role", "")
        if role == "tool":
            continue
        
        vis_idx += 1
        content = extract_text(msg.get("content", ""))
        meta = msg.get("metadata") or {}
        source = meta.get("source", "")
        msg_id = msg.get("id", "")
        turn_id = meta.get("turn_id", "")
        
        if role == "system":
            label = "System"
        elif role == "user":
            label = "Researcher" if source == "Researcher" else "System"
        elif role == "assistant":
            label = "Auditor"
        else:
            label = role
        
        # Turn-level branch indicator
        bp_str = get_bp_indicator(bps, "turn", msg_id=msg_id)
        bp_suffix = f"  {bp_str}" if bp_str else ""
        
        lines.append(f"[{vis_idx}] {label}: {truncate(content)}{bp_suffix}")
        
        # Tool calls
        tool_calls = msg.get("tool_calls") or []
        for tc_i, tc in enumerate(tool_calls):
            is_last = tc_i == len(tool_calls) - 1
            tc_id = tc.get("id", "")
            func = tc.get("function", "?")
            args = tc.get("arguments", {})
            args_str = ", ".join(f'{k}="{truncate(str(v), 25)}"' for k, v in args.items())
            
            br = "└─" if is_last else "├─"
            cont = "   " if is_last else "│  "
            
            # Tool-call branch indicator
            tc_bp = get_bp_indicator(bps, "tool_call", tc_id=tc_id)
            tc_suffix = f"  {tc_bp}" if tc_bp else ""
            
            lines.append(f"    {br} [{tc_i+1}] {func}({args_str}){tc_suffix}")
            
            # Find tool result
            result_msg = None
            for m in all_msgs:
                if m.get("role") == "tool" and m.get("tool_call_id") == tc_id:
                    result_msg = m
                    break
            
            if result_msg:
                rc = extract_text(result_msg.get("content", ""))
                err = result_msg.get("error")
                if err:
                    err_msg = err.get("message", "?") if isinstance(err, dict) else str(err)
                    lines.append(f"    {cont} → ✗ Error: {truncate(err_msg, 60)}")
                else:
                    target_text = extract_target_response(rc) if isinstance(rc, str) else None
                    if target_text:
                        tgt_bp = get_bp_indicator(bps, "target", tc_id=tc_id)
                        tgt_suffix = f"  {tgt_bp}" if tgt_bp else ""
                        lines.append(f'    {cont} → Target Response: "{truncate(target_text, 50)}"{tgt_suffix}')
                    else:
                        lines.append(f"    {cont} → ✓ {truncate(rc, 60)}")
    
    lines.append("")
    
    # Target state summary
    ts = branch.get("target_state", {})
    lines.append(f"── Target State: {len(ts.get('messages', []))} messages, {len(ts.get('tools', []))} tools ──")
    
    # Branch points summary
    if bps:
        lines.append("")
        lines.append(f"── Branch Points ({len(bps)}) ──")
        for bp in bps:
            bt = bp.get("branch_type", "?")
            mi = bp.get("message_id", "")[:8] if bp.get("message_id") else "-"
            ti = bp.get("tool_call_id", "")[:8] if bp.get("tool_call_id") else "-"
            ci = bp.get("current_index", 0) + 1
            tot = bp.get("total_branches", 0)
            bids = [b[:8] for b in bp.get("branch_ids", [])]
            lines.append(f"  {bt}: msg={mi} tc={ti} < {ci}/{tot} > branches={bids}")
    
    return "\n".join(lines)


def render_target(vs: dict) -> str:
    branch = vs.get("current_branch", {})
    ts = branch.get("target_state", {})
    msgs = ts.get("messages", [])
    tools = ts.get("tools", [])
    
    lines = ["", "═══ Full Target State ═══"]
    lines.append(f"Tools ({len(tools)}):")
    for t in tools:
        lines.append(f"  - {t.get('name', '?')}: {truncate(t.get('description', ''), 50)}")
    lines.append(f"Messages ({len(msgs)}):")
    for i, m in enumerate(msgs, 1):
        role = m.get("role", "?")
        content = extract_text(m.get("content", ""))
        lines.append(f"  [{i}] {role}: {truncate(content, 100)}")
        for tc in m.get("tool_calls") or []:
            lines.append(f"       └─ {tc.get('function', '?')}(...)")
    return "\n".join(lines)


async def run():
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
        # Receive initial state if session exists
        last_state = None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2)
            data = json.loads(raw)
            if data.get("type") == "state":
                last_state = data["state"]
        except asyncio.TimeoutError:
            pass
        
        # Build and send the command message
        msg = None
        
        if cmd == "start":
            prompt = " ".join(args) if args else "Test the model"
            msg = {
                "type": "start_session",
                "initial_prompt": prompt,
                "auditor_model": MODEL,
                "target_model": MODEL,
            }
        elif cmd == "step":
            msg = {"type": "step"}
        elif cmd == "play":
            msg = {"type": "play"}
        elif cmd == "pause":
            msg = {"type": "pause"}
        elif cmd == "feedback":
            msg = {"type": "feedback", "content": " ".join(args)}
        elif cmd == "resample":
            idx = int(args[0])
            visible = [m for m in (last_state or {}).get("current_branch", {}).get("auditor_messages", []) if m.get("role") != "tool"]
            if idx < 1 or idx > len(visible):
                print(f"ERROR: Invalid index {idx}, valid: 1-{len(visible)}")
                sys.exit(1)
            m = visible[idx - 1]
            turn_id = (m.get("metadata") or {}).get("turn_id")
            if not turn_id:
                print(f"ERROR: Message [{idx}] has no turn_id (role={m.get('role')})")
                sys.exit(1)
            msg = {"type": "resample_turn", "turn_id": turn_id}
        elif cmd == "resample_target":
            idx = int(args[0])
            tc_idx = int(args[1]) if len(args) > 1 else None
            visible = [m for m in (last_state or {}).get("current_branch", {}).get("auditor_messages", []) if m.get("role") != "tool"]
            if idx < 1 or idx > len(visible):
                print(f"ERROR: Invalid index {idx}")
                sys.exit(1)
            m = visible[idx - 1]
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
            msg = {"type": "resample_target_response", "tool_call_id": tc["id"]}
        elif cmd == "edit":
            idx = int(args[0])
            tc_idx = int(args[1])
            json_str = " ".join(args[2:])
            visible = [m for m in (last_state or {}).get("current_branch", {}).get("auditor_messages", []) if m.get("role") != "tool"]
            m = visible[idx - 1]
            tcs = m.get("tool_calls") or []
            tc = tcs[tc_idx - 1]
            msg = {"type": "edit_tool_call", "tool_call_id": tc["id"], "new_arguments": json.loads(json_str)}
        elif cmd == "nav":
            idx = int(args[0])
            direction = args[1]  # "left" or "right"
            visible = [m for m in (last_state or {}).get("current_branch", {}).get("auditor_messages", []) if m.get("role") != "tool"]
            m = visible[idx - 1]
            bp = next((b for b in (last_state or {}).get("branch_points", []) 
                       if b.get("branch_type") == "turn" and b.get("message_id") == m.get("id")), None)
            if not bp:
                print(f"ERROR: No turn branch point on message [{idx}]")
                sys.exit(1)
            ci = bp["current_index"]
            bids = bp["branch_ids"]
            new_idx = ci - 1 if direction == "left" else ci + 1
            if new_idx < 0 or new_idx >= len(bids):
                print(f"ERROR: Already at {'leftmost' if direction == 'left' else 'rightmost'} branch")
                sys.exit(1)
            msg = {"type": "switch_branch", "branch_id": bids[new_idx]}
        elif cmd == "nav_tool":
            idx = int(args[0])
            tc_idx = int(args[1])
            direction = args[2]
            visible = [m for m in (last_state or {}).get("current_branch", {}).get("auditor_messages", []) if m.get("role") != "tool"]
            m = visible[idx - 1]
            tc = (m.get("tool_calls") or [])[tc_idx - 1]
            bp = next((b for b in (last_state or {}).get("branch_points", [])
                       if b.get("branch_type") == "tool_call" and b.get("tool_call_id") == tc["id"]), None)
            if not bp:
                print(f"ERROR: No tool_call branch point on tool [{tc_idx}] of message [{idx}]")
                sys.exit(1)
            ci = bp["current_index"]
            bids = bp["branch_ids"]
            new_idx = ci - 1 if direction == "left" else ci + 1
            if new_idx < 0 or new_idx >= len(bids):
                print(f"ERROR: Already at edge")
                sys.exit(1)
            msg = {"type": "switch_branch", "branch_id": bids[new_idx]}
        elif cmd == "nav_target":
            idx = int(args[0])
            tc_idx = int(args[1])
            direction = args[2]
            visible = [m for m in (last_state or {}).get("current_branch", {}).get("auditor_messages", []) if m.get("role") != "tool"]
            m = visible[idx - 1]
            tc = (m.get("tool_calls") or [])[tc_idx - 1]
            bp = next((b for b in (last_state or {}).get("branch_points", [])
                       if b.get("branch_type") == "target" and b.get("tool_call_id") == tc["id"]), None)
            if not bp:
                print(f"ERROR: No target branch point on tool [{tc_idx}] of message [{idx}]")
                sys.exit(1)
            ci = bp["current_index"]
            bids = bp["branch_ids"]
            new_idx = ci - 1 if direction == "left" else ci + 1
            if new_idx < 0 or new_idx >= len(bids):
                print(f"ERROR: Already at edge")
                sys.exit(1)
            msg = {"type": "switch_branch", "branch_id": bids[new_idx]}
        elif cmd == "state":
            if last_state:
                print(render_state(last_state))
            else:
                print("No state available")
            return
        elif cmd == "bp":
            if last_state:
                bps = last_state.get("branch_points", [])
                print(f"\nBranch Points ({len(bps)}):")
                for bp in bps:
                    bt = bp.get("branch_type", "?")
                    ci = bp.get("current_index", 0) + 1
                    tot = bp.get("total_branches", 0)
                    mi = bp.get("message_id", "")[:12] if bp.get("message_id") else "-"
                    ti = bp.get("tool_call_id", "")[:12] if bp.get("tool_call_id") else "-"
                    bids = bp.get("branch_ids", [])
                    cur_bid = bids[bp.get("current_index", 0)][:8] if bids else "?"
                    print(f"  [{bt}] < {ci}/{tot} > msg={mi} tc={ti} current_branch={cur_bid}")
                    for i, b in enumerate(bids):
                        marker = " ◀" if i == bp.get("current_index", -1) else ""
                        print(f"    {i}: {b[:12]}{marker}")
            return
        elif cmd == "target":
            if last_state:
                print(render_target(last_state))
            return
        elif cmd == "switch":
            msg = {"type": "switch_branch", "branch_id": args[0]}
        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)
        
        if msg:
            await ws.send(json.dumps(msg))
        
        # Collect responses
        deadline = time.monotonic() + 120  # 2 min max wait
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                data = json.loads(raw)
                if data.get("type") == "state":
                    last_state = data["state"]
                    ps = last_state.get("playback_state", "")
                    if ps in ("paused", "idle") and cmd in ("step", "resample", "resample_target", "play"):
                        break
                    if cmd not in ("step", "resample", "resample_target", "play"):
                        break
                elif data.get("type") == "error":
                    print(f"SERVER ERROR: {data.get('message', '?')}")
                    break
                elif data.get("type", "").startswith("delta_"):
                    continue  # Wait for final state
            except asyncio.TimeoutError:
                break
        
        if last_state:
            print(render_state(last_state))
        else:
            print("No state received")


if __name__ == "__main__":
    asyncio.run(run())
