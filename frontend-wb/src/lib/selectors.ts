/**
 * React selectors over the session store (STREAMING.md §D).
 *
 * `byRole` is maintained incrementally in the reducer, so these are O(1)
 * reads. A column's array reference only changes when an event in that
 * column is added/updated, so Zustand short-circuits and only the
 * streaming column re-renders per flush.
 */
import { useMemo } from "react";
import type { ChatMessage, Event, ModelEvent } from "@tsmono/inspect-common";
import {
  computeFlatSwimlaneRows,
  computeRowLayouts,
  computeTimeMapping,
  convertServerTimeline,
  splice,
  type RowLayout,
  type SwimlaneRow,
  type Timeline,
  type TimelineSpan,
} from "@tsmono/inspect-components/transcript/timeline";

import { buildEventTree, isModelEvent, type EventNode } from "./events";
import type { BranchId, Role } from "./wire";
import { useSession } from "../store/session";

export function useEvents(branch: BranchId, role: Role): Event[] {
  return useSession((s) => s.byRole[branch]?.[role] ?? EMPTY);
}

/** The single in-flight (streaming) ModelEvent for a column, if any. */
export function usePending(branch: BranchId, role: Role): ModelEvent | null {
  return useSession((s) => {
    const events = s.byRole[branch]?.[role] ?? EMPTY;
    for (let i = events.length - 1; i >= 0; i--) {
      const ev = events[i];
      if (isModelEvent(ev) && ev.pending === true) return ev;
    }
    return null;
  });
}

export function useQueued(branch: BranchId, role: Role): ChatMessage[] {
  return useSession((s) => s.queued[branch]?.[role] ?? EMPTY_MSGS);
}

const STAGING_TOOLS: Record<string, ChatMessage["role"]> = {
  set_system_message: "system",
  send_message: "user",
  send_tool_call_result: "tool",
};

/**
 * Messages the auditor has staged for the target but the target hasn't
 * consumed yet (no `resume` → no target ModelEvent after them). Derived
 * from the auditor column's ToolEvents — no backend change needed.
 */
export function useStagedForTarget(branch: BranchId): ChatMessage[] {
  const auditorEvs = useEvents(branch, "auditor");
  const targetEvs = useEvents(branch, "target");
  return useMemo(() => {
    const lastTargetTs = [...targetEvs].reverse().find(isModelEvent)?.timestamp ?? "";
    const out: ChatMessage[] = [];
    for (const ev of auditorEvs) {
      if (ev.event !== "tool" || ev.timestamp <= lastTargetTs) continue;
      const role = STAGING_TOOLS[ev.function];
      if (!role) continue;
      const args = ev.arguments as Record<string, unknown>;
      const content =
        role === "system" ? String(args.system_message ?? "")
        : role === "user" ? String(args.message ?? "")
        : String(args.result ?? "");
      if (content) out.push({ role, content, id: ev.uuid ?? undefined } as ChatMessage);
    }
    return out;
  }, [auditorEvs, targetEvs]);
}

/**
 * inspect's display tree (`treeifyEvents`) over the current event set.
 *
 * Derived (not stored) so it can never go stale relative to `events`. Memoized
 * on the `events` Map reference, which the reducer replaces on every mutation,
 * so the tree rebuilds exactly when the underlying events change. O(n) at
 * audit scale; profile before optimizing (INSPECT-REUSE.md §3).
 */
export function useEventTree(): EventNode[] {
  const events = useSession((s) => s.events);
  return useMemo(() => buildEventTree(events.values()), [events]);
}

/**
 * inspect-view's Timeline + swimlane rows for one (branch, role) column.
 *
 * The server ships petri's `build_target_timeline()` output (event refs as
 * UUIDs); `convertServerTimeline` resolves them against the store's events
 * exactly as inspect-view does. `computeFlatSwimlaneRows({showBranches:true})`
 * then yields one row per trajectory, with `branch: true` rows for rollback
 * forks. `splice(root, span)` reconstructs any row's full conversation lineage.
 */
export type Swimlanes = {
  timeline: Timeline | null;
  rows: SwimlaneRow[];
  /** Gantt-bar layouts for inspect's `TimelineSwimLanes` (time-positioned). */
  layouts: RowLayout[];
  /** Reconstruct a row's full event lineage (ancestor prefix + own content). */
  lineage: (span: TimelineSpan) => Event[];
};

export function useSwimlanes(branch: BranchId, role: Role): Swimlanes {
  const serverTl = useSession((s) => s.timelines[branch]?.[role]);
  const events = useSession((s) => s.events);
  return useMemo(() => {
    if (!serverTl) return { timeline: null, rows: [], layouts: [], lineage: () => [] };
    const timeline = convertServerTimeline(serverTl, [...events.values()]);
    const rows = computeFlatSwimlaneRows(timeline.root, {
      includeUtility: true,
      showBranches: true,
    });
    const mapping = computeTimeMapping(timeline.root);
    const layouts = computeRowLayouts(rows, mapping, "direct", ["branch", "error"]);
    return {
      timeline,
      rows,
      layouts,
      lineage: (span) => splice(timeline.root, span),
    };
  }, [serverTl, events]);
}

/** Sibling set at a fork point, plus the index of the currently-viewed one. */
export type ForkGroup = { siblings: string[]; idx: number };

/**
 * Per-anchor fork groups over a swimlane row set (either column).
 *
 * A `SwimlaneRow` with `branch: true` carries `branchedFrom` — the assistant
 * message id it forked at. The group at that anchor is `[parentRowKey,
 * ...branchRowKeys]` (parent first, branches in `rows` order). `idx` is the
 * position of `selectedKey` within the group, resolved by key-prefix so a
 * nested-branch selection still maps to the ancestor that lives in the group.
 */
export function computeForks(
  rows: SwimlaneRow[],
  selectedKey: string | null
): Map<string, ForkGroup> {
  const groups = new Map<string, string[]>();
  for (const r of rows) {
    if (!r.branch || r.branchedFrom == null) continue;
    // Immediate parent row = longest key that is a strict prefix of this one.
    let parent: string | undefined;
    for (const p of rows) {
      if (p.key === r.key || !r.key.startsWith(`${p.key}/`)) continue;
      if (parent === undefined || p.key.length > parent.length) parent = p.key;
    }
    if (parent === undefined) continue;
    const g = groups.get(r.branchedFrom);
    if (g) g.push(r.key);
    else groups.set(r.branchedFrom, [parent, r.key]);
  }
  const out = new Map<string, ForkGroup>();
  for (const [anchor, siblings] of groups) {
    // Most-specific match wins: the parent key is a prefix of every branch
    // key, so pick the longest sibling that is `selectedKey` or an ancestor
    // of it.
    let idx = 0;
    let bestLen = -1;
    for (let i = 0; i < siblings.length; i++) {
      const k = siblings[i];
      if (
        (selectedKey === k || selectedKey?.startsWith(`${k}/`)) &&
        k.length > bestLen
      ) {
        idx = i;
        bestLen = k.length;
      }
    }
    out.set(anchor, { siblings, idx });
  }
  return out;
}

const EMPTY: Event[] = [];
const EMPTY_MSGS: ChatMessage[] = [];
