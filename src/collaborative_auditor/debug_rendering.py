"""Shared rendering utilities for debug clients.

Used by both the interactive debug_client and the one-shot debug_cmd.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_CONTENT_WIDTH = 80
BOX_WIDTH = 70


def truncate(text: str, max_len: int = MAX_CONTENT_WIDTH) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").replace("\r", "")
    return text[: max_len - 3] + "..." if len(text) > max_len else text


def extract_text(content: Any) -> str:
    """Extract plain text from a message content field.

    Content can be a string or a list of content parts (dicts with 'type' key).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
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
    match = re.search(r"<target_response[^>]*>(.*?)</target_response>", content, re.DOTALL)
    return match.group(1).strip() if match else None


def get_bp_indicator(
    branch_points: list[dict[str, Any]],
    bp_type: str,
    *,
    msg_id: str | None = None,
    tc_id: str | None = None,
) -> str:
    """Return a '< n/m > (type)' indicator for a branch point, or empty string."""
    for bp in branch_points:
        if bp.get("branch_type") != bp_type:
            continue
        matches = (
            (bp_type == "turn" and msg_id and bp.get("message_id") == msg_id)
            or (bp_type in ("tool_call", "target") and tc_id and bp.get("tool_call_id") == tc_id)
        )
        if matches:
            idx = bp["current_index"] + 1
            total = bp["total_branches"]
            return f"< {idx}/{total} > ({bp_type})"
    return ""


def visible_messages(view_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return non-tool messages from the current branch."""
    branch = view_state.get("current_branch", {})
    return [m for m in branch.get("auditor_messages", []) if m.get("role") != "tool"]


def all_messages(view_state: dict[str, Any]) -> list[dict[str, Any]]:
    branch = view_state.get("current_branch", {})
    return branch.get("auditor_messages", [])


def find_branch_point(
    view_state: dict[str, Any],
    bp_type: str,
    *,
    msg_id: str | None = None,
    tc_id: str | None = None,
) -> dict[str, Any] | None:
    """Find a branch point by type and message/tool-call ID."""
    for bp in view_state.get("branch_points", []):
        if bp.get("branch_type") != bp_type:
            continue
        if bp_type == "turn" and bp.get("message_id") == msg_id:
            return bp
        if bp_type in ("tool_call", "target") and bp.get("tool_call_id") == tc_id:
            return bp
    return None


def navigate_branch_point(
    bp: dict[str, Any],
    direction: str,
) -> str | None:
    """Return the target branch_id for a left/right navigation, or None if at edge."""
    current_idx = bp.get("current_index", 0)
    branch_ids = bp.get("branch_ids", [])
    new_idx = current_idx + (-1 if direction == "left" else 1)
    if 0 <= new_idx < len(branch_ids):
        return branch_ids[new_idx]
    return None


# ---------------------------------------------------------------------------
# Full-state rendering
# ---------------------------------------------------------------------------


def render_header(vs: dict[str, Any]) -> list[str]:
    sid = vs["session_id"][:8]
    n_br = len(vs.get("branches", []))
    cur = vs.get("current_branch_index", 0) + 1
    aud = vs.get("auditor_model", "?").split("/")[-1][:15]
    tgt = vs.get("target_model", "?").split("/")[-1][:15]
    ps = vs.get("playback_state", "idle")
    gen = vs.get("is_generating", False)
    ver = vs.get("version", 0)

    w = BOX_WIDTH
    return [
        "╔" + "═" * w + "╗",
        "║" + f" Session: {sid}  │ Branches: {n_br}  │ Current: {cur}/{n_br}  │ v{ver}".ljust(w) + "║",
        "║" + f" Auditor: {aud} │ Target: {tgt} │ {ps}{' (generating)' if gen else ''}".ljust(w) + "║",
        "╚" + "═" * w + "╝",
    ]


def render_tool_call_line(
    tc: dict[str, Any],
    all_msgs: list[dict[str, Any]],
    bps: list[dict[str, Any]],
    *,
    is_last: bool = False,
    tc_index: int = 0,
) -> list[str]:
    lines: list[str] = []
    tc_id = tc.get("id", "")
    func_name = tc.get("function", "?")
    args = tc.get("arguments", {})
    args_str = ", ".join(f'{k}="{truncate(str(v), 25)}"' for k, v in args.items())

    br = "└─" if is_last else "├─"
    cont = "   " if is_last else "│  "

    tc_bp = get_bp_indicator(bps, "tool_call", tc_id=tc_id)
    tc_suffix = f"  {tc_bp}" if tc_bp else ""
    lines.append(f"    {br} [{tc_index + 1}] {func_name}({args_str}){tc_suffix}")

    result_msg = next(
        (m for m in all_msgs if m.get("role") == "tool" and m.get("tool_call_id") == tc_id),
        None,
    )
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

    return lines


def render_messages(vs: dict[str, Any]) -> list[str]:
    all_msgs = all_messages(vs)
    bps = vs.get("branch_points", [])
    lines: list[str] = []
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

        if role == "system":
            label = "System"
        elif role == "user":
            label = "Researcher" if source == "Researcher" else "System"
        elif role == "assistant":
            label = "Auditor"
        else:
            label = role.capitalize()

        bp_str = get_bp_indicator(bps, "turn", msg_id=msg_id)
        bp_suffix = f"  {bp_str}" if bp_str else ""
        lines.append(f"[{vis_idx}] {label}: {truncate(content)}{bp_suffix}")

        for tc_i, tc in enumerate(msg.get("tool_calls") or []):
            lines.extend(render_tool_call_line(
                tc, all_msgs, bps,
                is_last=(tc_i == len(msg.get("tool_calls") or []) - 1),
                tc_index=tc_i,
            ))

    return lines


def render_state(vs: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.extend(render_header(vs))
    lines.append("")
    lines.extend(render_messages(vs))
    lines.append("")

    branch = vs.get("current_branch", {})
    ts = branch.get("target_state", {})
    lines.append(f"── Target State: {len(ts.get('messages', []))} messages, {len(ts.get('tools', []))} tools ──")

    bps = vs.get("branch_points", [])
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


def render_target(vs: dict[str, Any]) -> str:
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
            func = tc.get("function", "?")
            args_str = json.dumps(tc.get("arguments", {}))
            lines.append(f"       └─ {func}({truncate(args_str, 60)})")

    return "\n".join(lines)


def render_branch_points(vs: dict[str, Any]) -> str:
    bps = vs.get("branch_points", [])
    if not bps:
        return "No branch points."
    lines = [f"\nBranch Points ({len(bps)}):"]
    for bp in bps:
        bt = bp.get("branch_type", "?")
        ci = bp.get("current_index", 0) + 1
        tot = bp.get("total_branches", 0)
        mi = bp.get("message_id", "")[:12] if bp.get("message_id") else "-"
        ti = bp.get("tool_call_id", "")[:12] if bp.get("tool_call_id") else "-"
        bids = bp.get("branch_ids", [])
        cur_bid = bids[bp.get("current_index", 0)][:8] if bids else "?"
        lines.append(f"  [{bt}] < {ci}/{tot} > msg={mi} tc={ti} current_branch={cur_bid}")
        for i, b in enumerate(bids):
            marker = " ◀" if i == bp.get("current_index", -1) else ""
            lines.append(f"    {i}: {b[:12]}{marker}")
    return "\n".join(lines)
