/**
 * Debug bridge: exposes a text-based UI rendering and command interface
 * on `window` for automated testing. Reads from the SAME Zustand store
 * that the React components use, so the text output reflects exactly
 * what the real UI shows.
 *
 * Usage from browser console or automation:
 *   window.__textUI()           → returns text rendering of current state
 *   window.__cmd("step")        → sends a command
 *   window.__cmd("feedback", "try harder")
 *   window.__cmd("resample", 3) → resample visible message 3
 *   window.__cmd("nav", 3, "left")
 *   window.__state()            → returns raw ViewState JSON
 *   window.__bp()               → returns branch points summary
 */

import type { ViewState, ChatMessage, ClientMessage, ToolCall, ComputedBranchPoint, TargetState } from "./types";

// ─── Text Rendering ───────────────────────────────────────────────

const MAX_WIDTH = 90;

function truncate(text: string, maxLen = MAX_WIDTH): string {
  if (!text) return "";
  const flat = text.replace(/\n/g, " ").replace(/\r/g, "");
  return flat.length > maxLen ? flat.slice(0, maxLen - 3) + "..." : flat;
}

function extractText(content: string | unknown[] | undefined): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return content ? String(content) : "";
  return content
    .map((p: unknown) => {
      const part = p as Record<string, unknown>;
      if (part.type === "text") return (part.text as string) || "";
      if (part.type === "reasoning") {
        const r = (part.reasoning as string) || "";
        return r ? `[thinking: ${truncate(r, 40)}]` : "";
      }
      return "";
    })
    .filter(Boolean)
    .join(" ");
}

function extractTargetResponse(content: string): string | null {
  const m = content.match(/<target_response[^>]*>([\s\S]*?)<\/target_response>/);
  return m ? m[1].trim() : null;
}

function getBpIndicator(
  bps: ComputedBranchPoint[],
  bpType: string,
  msgId?: string,
  tcId?: string,
): string {
  for (const bp of bps) {
    if (bp.branch_type !== bpType) continue;
    if (bpType === "turn" && msgId && bp.message_id === msgId) {
      return `< ${bp.current_index + 1}/${bp.total_branches} > (turn)`;
    }
    if (bpType === "tool_call" && tcId && bp.tool_call_id === tcId) {
      return `< ${bp.current_index + 1}/${bp.total_branches} > (tool_call)`;
    }
    if (bpType === "target" && tcId && bp.tool_call_id === tcId) {
      return `< ${bp.current_index + 1}/${bp.total_branches} > (target)`;
    }
  }
  return "";
}

export function renderTextUI(vs: ViewState): string {
  const lines: string[] = [];

  // Header
  const sid = vs.session_id.slice(0, 8);
  const nBr = vs.branches.length;
  const cur = vs.current_branch_index + 1;
  const aud = vs.auditor_model.split("/").pop()?.slice(0, 15) ?? "?";
  const tgt = vs.target_model.split("/").pop()?.slice(0, 15) ?? "?";
  const ps = vs.playback_state;
  const gen = vs.is_generating;
  const ver = vs.version;
  const w = 70;

  lines.push("╔" + "═".repeat(w) + "╗");
  lines.push("║" + ` Session: ${sid}  │ Branches: ${nBr}  │ Current: ${cur}/${nBr}  │ v${ver}`.padEnd(w) + "║");
  lines.push("║" + ` Auditor: ${aud} │ Target: ${tgt} │ ${ps}${gen ? " (generating)" : ""}`.padEnd(w) + "║");
  lines.push("╚" + "═".repeat(w) + "╝");
  lines.push("");

  // Messages
  const branch = vs.current_branch;
  const allMsgs = branch.auditor_messages;
  const bps = vs.branch_points;

  let visIdx = 0;
  for (const msg of allMsgs) {
    if (msg.role === "tool") continue;
    visIdx++;

    const content = extractText(msg.content);
    const meta = msg.metadata ?? {};
    const source = meta.source ?? "";
    const msgId = msg.id ?? "";
    const turnId = meta.turn_id ?? "";

    let label: string;
    if (msg.role === "system") label = "System";
    else if (msg.role === "user") label = source === "Researcher" ? "Researcher" : "System";
    else if (msg.role === "assistant") label = "Auditor";
    else label = msg.role;

    // Turn-level branch indicator
    const bpStr = getBpIndicator(bps, "turn", msgId);
    const bpSuffix = bpStr ? `  ${bpStr}` : "";

    lines.push(`[${visIdx}] ${label}: ${truncate(content)}${bpSuffix}`);

    // Tool calls
    const toolCalls = msg.tool_calls ?? [];
    for (let tcI = 0; tcI < toolCalls.length; tcI++) {
      const tc = toolCalls[tcI];
      const isLast = tcI === toolCalls.length - 1;
      const br = isLast ? "└─" : "├─";
      const cont = isLast ? "   " : "│  ";

      const func = tc.function;
      const args = tc.arguments ?? {};
      const argsStr = Object.entries(args)
        .map(([k, v]) => `${k}="${truncate(String(v), 25)}"`)
        .join(", ");

      // Tool-call branch indicator
      const tcBp = getBpIndicator(bps, "tool_call", undefined, tc.id);
      const tcSuffix = tcBp ? `  ${tcBp}` : "";

      lines.push(`    ${br} [${tcI + 1}] ${func}(${argsStr})${tcSuffix}`);

      // Find tool result
      const resultMsg = allMsgs.find(
        (m) => m.role === "tool" && m.tool_call_id === tc.id
      );

      if (resultMsg) {
        const rc = extractText(resultMsg.content);
        const err = resultMsg.error;
        if (err) {
          const errMsg = typeof err === "object" ? (err as Record<string, string>).message ?? "?" : String(err);
          lines.push(`    ${cont} → ✗ Error: ${truncate(errMsg, 60)}`);
        } else {
          const targetText = typeof rc === "string" ? extractTargetResponse(rc) : null;
          if (targetText) {
            const tgtBp = getBpIndicator(bps, "target", undefined, tc.id);
            const tgtSuffix = tgtBp ? `  ${tgtBp}` : "";
            lines.push(`    ${cont} → Target Response: "${truncate(targetText, 50)}"${tgtSuffix}`);
          } else {
            lines.push(`    ${cont} → ✓ ${truncate(rc, 60)}`);
          }
        }
      }
    }
  }

  lines.push("");

  // Target state summary
  const ts = branch.target_state;
  lines.push(`── Target State: ${ts.messages.length} messages, ${ts.tools.length} tools ──`);

  // Branch points summary
  if (bps.length > 0) {
    lines.push("");
    lines.push(`── Branch Points (${bps.length}) ──`);
    for (const bp of bps) {
      const mi = bp.message_id ? bp.message_id.slice(0, 8) : "-";
      const ti = bp.tool_call_id ? bp.tool_call_id.slice(0, 8) : "-";
      const ci = bp.current_index + 1;
      const tot = bp.total_branches;
      const bids = bp.branch_ids.map((b) => b.slice(0, 8));
      lines.push(`  ${bp.branch_type}: msg=${mi} tc=${ti} < ${ci}/${tot} > branches=[${bids.join(", ")}]`);
    }
  }

  return lines.join("\n");
}

export function renderBranchPoints(vs: ViewState): string {
  const bps = vs.branch_points;
  const lines = [`Branch Points (${bps.length}):`];
  for (const bp of bps) {
    const ci = bp.current_index + 1;
    const tot = bp.total_branches;
    const mi = bp.message_id ? bp.message_id.slice(0, 12) : "-";
    const ti = bp.tool_call_id ? bp.tool_call_id.slice(0, 12) : "-";
    lines.push(`  [${bp.branch_type}] < ${ci}/${tot} > msg=${mi} tc=${ti}`);
    for (let i = 0; i < bp.branch_ids.length; i++) {
      const marker = i === bp.current_index ? " ◀" : "";
      lines.push(`    ${i}: ${bp.branch_ids[i].slice(0, 12)}${marker}`);
    }
  }
  return lines.join("\n");
}

export function renderTargetState(vs: ViewState): string {
  const ts = vs.current_branch.target_state;
  const lines = ["═══ Full Target State ═══"];
  lines.push(`Tools (${ts.tools.length}):`);
  for (const t of ts.tools) {
    lines.push(`  - ${t.name}: ${truncate(t.description, 50)}`);
  }
  lines.push(`Messages (${ts.messages.length}):`);
  for (let i = 0; i < ts.messages.length; i++) {
    const m = ts.messages[i];
    const content = extractText(m.content);
    lines.push(`  [${i + 1}] ${m.role}: ${truncate(content, 100)}`);
  }
  return lines.join("\n");
}

// ─── Command Interface ────────────────────────────────────────────

type StoreType = {
  subscribe: (listener: (state: { viewState: ViewState | null }) => void) => () => void;
  getState: () => {
    viewState: ViewState | null;
    send: (msg: ClientMessage) => void;
    step: () => void;
    play: () => void;
    pause: () => void;
    sendFeedback: (content: string) => void;
    switchBranch: (branchId: string) => void;
    branchAtMessage: (messageId: string) => void;
    resampleTurn: (turnId: string) => void;
    editToolCall: (toolCallId: string, newArguments: Record<string, unknown>) => void;
    resampleTargetResponse: (targetMessageId: string, toolCallId?: string) => void;
  };
};

function getVisibleMessages(vs: ViewState): ChatMessage[] {
  return vs.current_branch.auditor_messages.filter((m) => m.role !== "tool");
}

export function installDebugBridge(store: StoreType): void {
  const w = window as unknown as Record<string, unknown>;

  // Text UI rendering
  w.__textUI = () => {
    const vs = store.getState().viewState;
    if (!vs) return "No state yet.";
    return renderTextUI(vs);
  };

  // Raw state
  w.__state = () => {
    return store.getState().viewState;
  };

  // Branch points
  w.__bp = () => {
    const vs = store.getState().viewState;
    if (!vs) return "No state.";
    return renderBranchPoints(vs);
  };

  // Target state
  w.__target = () => {
    const vs = store.getState().viewState;
    if (!vs) return "No state.";
    return renderTargetState(vs);
  };

  // Command interface
  w.__cmd = (cmd: string, ...args: unknown[]) => {
    const state = store.getState();
    const vs = state.viewState;

    switch (cmd) {
      case "step":
        state.step();
        return "Stepping...";

      case "play":
        state.play();
        return "Playing...";

      case "pause":
        state.pause();
        return "Pausing...";

      case "feedback": {
        const text = String(args[0] ?? "");
        if (!text) return "Usage: __cmd('feedback', 'text')";
        state.sendFeedback(text);
        return `Sent feedback: ${text.slice(0, 50)}`;
      }

      case "resample": {
        if (!vs) return "No state";
        const idx = Number(args[0]);
        const visible = getVisibleMessages(vs);
        if (idx < 1 || idx > visible.length) return `Invalid index. Range: 1-${visible.length}`;
        const msg = visible[idx - 1];
        const turnId = msg.metadata?.turn_id;
        if (!turnId) return `Message [${idx}] has no turn_id (role=${msg.role})`;
        state.resampleTurn(turnId);
        return `Resampling turn at [${idx}]`;
      }

      case "resample_target": {
        if (!vs) return "No state";
        const msgIdx = Number(args[0]);
        const tcIdx = args[1] !== undefined ? Number(args[1]) : null;
        const visible = getVisibleMessages(vs);
        if (msgIdx < 1 || msgIdx > visible.length) return `Invalid index`;
        const msg = visible[msgIdx - 1];
        const tcs = msg.tool_calls ?? [];
        let tc: ToolCall | undefined;
        if (tcIdx !== null) {
          if (tcIdx < 1 || tcIdx > tcs.length) return `Invalid tool index`;
          tc = tcs[tcIdx - 1];
        } else {
          tc = tcs.find((t) => t.function === "query_target");
          if (!tc) return `No query_target in message [${msgIdx}]`;
        }
        state.resampleTargetResponse(tc!.id, tc!.id);
        return `Resampling target response`;
      }

      case "edit": {
        if (!vs) return "No state";
        const mIdx = Number(args[0]);
        const tIdx = Number(args[1]);
        const newArgs = args[2] as Record<string, unknown>;
        if (!newArgs) return "Usage: __cmd('edit', msgIdx, tcIdx, {args})";
        const visible = getVisibleMessages(vs);
        if (mIdx < 1 || mIdx > visible.length) return `Invalid index`;
        const msg = visible[mIdx - 1];
        const tcs = msg.tool_calls ?? [];
        if (tIdx < 1 || tIdx > tcs.length) return `Invalid tool index`;
        const tc = tcs[tIdx - 1];
        state.editToolCall(tc.id, newArgs);
        return `Editing tool call ${tc.function}`;
      }

      case "nav": {
        if (!vs) return "No state";
        const nIdx = Number(args[0]);
        const dir = String(args[1]);
        const visible = getVisibleMessages(vs);
        if (nIdx < 1 || nIdx > visible.length) return `Invalid index`;
        const msg = visible[nIdx - 1];
        const bp = vs.branch_points.find(
          (b) => b.branch_type === "turn" && b.message_id === msg.id
        );
        if (!bp) return `No turn branch point on message [${nIdx}]`;
        const ci = bp.current_index;
        const newIdx = dir === "left" ? ci - 1 : ci + 1;
        if (newIdx < 0 || newIdx >= bp.branch_ids.length) return `Already at ${dir}most`;
        state.switchBranch(bp.branch_ids[newIdx]);
        return `Navigating ${dir}`;
      }

      case "nav_tool": {
        if (!vs) return "No state";
        const mI = Number(args[0]);
        const tI = Number(args[1]);
        const d = String(args[2]);
        const visible = getVisibleMessages(vs);
        const msg = visible[mI - 1];
        const tc = (msg.tool_calls ?? [])[tI - 1];
        if (!tc) return `Invalid tool index`;
        const bp = vs.branch_points.find(
          (b) => b.branch_type === "tool_call" && b.tool_call_id === tc.id
        );
        if (!bp) return `No tool_call branch point`;
        const ci = bp.current_index;
        const ni = d === "left" ? ci - 1 : ci + 1;
        if (ni < 0 || ni >= bp.branch_ids.length) return `Already at ${d}most`;
        state.switchBranch(bp.branch_ids[ni]);
        return `Navigating ${d}`;
      }

      case "nav_target": {
        if (!vs) return "No state";
        const mI = Number(args[0]);
        const tI = Number(args[1]);
        const d = String(args[2]);
        const visible = getVisibleMessages(vs);
        const msg = visible[mI - 1];
        const tc = (msg.tool_calls ?? [])[tI - 1];
        if (!tc) return `Invalid tool index`;
        const bp = vs.branch_points.find(
          (b) => b.branch_type === "target" && b.tool_call_id === tc.id
        );
        if (!bp) return `No target branch point`;
        const ci = bp.current_index;
        const ni = d === "left" ? ci - 1 : ci + 1;
        if (ni < 0 || ni >= bp.branch_ids.length) return `Already at ${d}most`;
        state.switchBranch(bp.branch_ids[ni]);
        return `Navigating ${d}`;
      }

      default:
        return `Unknown command: ${cmd}. Available: step, play, pause, feedback, resample, resample_target, edit, nav, nav_tool, nav_target`;
    }
  };

  // Enhanced __cmd that logs result automatically
  const originalCmd = w.__cmd as (...args: unknown[]) => string;
  w.__cmd = (...args: unknown[]) => {
    const result = originalCmd(...args);
    console.log(`[CMD] ${args[0]}(${args.slice(1).map(a => JSON.stringify(a)).join(", ")}) → ${result}`);
    return result;
  };

  // Create hidden debug panel with textarea for text UI output and input for commands
  const panel = document.createElement("div");
  panel.id = "debug-bridge-panel";
  panel.style.cssText = "position:fixed;bottom:0;left:0;right:0;z-index:99999;background:#111;border-top:2px solid #4CAF50;padding:4px;display:flex;gap:4px;height:40px;opacity:0;pointer-events:none;";
  
  const cmdInput = document.createElement("input");
  cmdInput.id = "debug-cmd-input";
  cmdInput.placeholder = "Type command: step, feedback hello, resample 3, nav 3 left...";
  cmdInput.style.cssText = "flex:1;background:#222;color:#eee;border:1px solid #555;padding:4px 8px;font-family:monospace;font-size:12px;";
  cmdInput.setAttribute("aria-label", "Debug command input");
  
  const resultOutput = document.createElement("textarea");
  resultOutput.id = "debug-text-ui";
  resultOutput.readOnly = true;
  resultOutput.style.cssText = "position:fixed;bottom:40px;left:0;right:0;height:0;overflow:hidden;background:#111;color:#eee;font-family:monospace;font-size:11px;white-space:pre;padding:0;border:none;z-index:99998;";
  resultOutput.setAttribute("aria-label", "Debug text UI output");

  // Process command on Enter
  cmdInput.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const line = cmdInput.value.trim();
    if (!line) return;
    cmdInput.value = "";

    const parts = line.split(/\s+/);
    const cmd = parts[0];
    let args: unknown[] = parts.slice(1);

    // Parse JSON argument for edit command
    if (cmd === "edit" && parts.length >= 4) {
      args = [Number(parts[1]), Number(parts[2]), JSON.parse(parts.slice(3).join(" "))];
    } else {
      // Convert numeric args
      args = args.map((a) => {
        const n = Number(a);
        return isNaN(n) ? a : n;
      });
    }

    const result = (w.__cmd as Function)(cmd, ...args);
    console.log(`[CMD] ${cmd}(${args.map(a => JSON.stringify(a)).join(", ")}) → ${result}`);
  });

  panel.appendChild(cmdInput);
  document.body.appendChild(resultOutput);
  document.body.appendChild(panel);

  // Update the textarea whenever state changes
  store.subscribe((state: { viewState: ViewState | null }) => {
    if (state.viewState) {
      const text = renderTextUI(state.viewState);
      resultOutput.value = text;
      console.log("[TextUI]\n" + text);
    }
  });

  // Initial render
  const initialState = store.getState().viewState;
  if (initialState) {
    resultOutput.value = renderTextUI(initialState);
  }

  console.log(
    "%c[Debug Bridge] Installed. Use window.__textUI(), window.__cmd('step'), etc.",
    "color: #4CAF50; font-weight: bold"
  );
}
