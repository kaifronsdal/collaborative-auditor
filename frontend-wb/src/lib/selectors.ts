/**
 * React selectors over the session store (STREAMING.md §D).
 *
 * `byRole` is maintained incrementally in the reducer, so these are O(1)
 * reads. A column's array reference only changes when an event in that
 * column is added/updated, so Zustand short-circuits and only the
 * streaming column re-renders per flush.
 */
import { useMemo } from "react";
import type { ChatMessage, Event } from "@tsmono/inspect-common";
import {
  computeBranchMappings,
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

import { isModelEvent } from "./events";
import type { BranchId, Role, Up } from "./wire";
import { useSession } from "../store/session";
import { ORCH_SOURCE, WB_MIME, type DisplayInfoEvent } from "../components/orch/types";

/** A2 (ARCHITECTURE-RACES.md) + OVERNIGHT-SWEEP W-B: fork-shaped commands.
 *  Any one in-flight means every fork button is disabled (chaos s4
 *  double-branch) — they all repoint `current` (or, for the orch/import
 *  set, tear down / recreate branches), so a second fork mid-flight would
 *  race the first regardless of which button fired it. The first six flow
 *  through `_pendingChild` → `_fork` → `_register_and_spawn`; the rest are
 *  the C2b additions (import row-click, resample-N, orch fork). */
export const FORK_KINDS: ReadonlySet<Up["t"]> = new Set([
  "branch",
  "resample",
  "branch_auditor",
  "resample_auditor",
  "edit_auditor_call",
  "edit_target_message",
  "import",
  "import_running",
  "candidates",
  "candidates_auditor",
  "pick_candidate",
  "fork_orchestrator",
]);

/**
 * A2: is any in-flight command matching `pred` awaiting its `{t:"ack"}`?
 *
 * One hook replaces R1's nine per-component `useState` guards. The
 * predicate picks the granularity: `c => c.t === "start"` for the launch
 * button, `c => FORK_KINDS.has(c.t)` for the branch/resample family,
 * `c => c.t === "dismiss_candidates" && c.batch === batchId` per-batch.
 * Returns a boolean, so Zustand's `Object.is` short-circuits on unrelated
 * `pending` churn.
 */
export function useIsPending(pred: (c: Up) => boolean): boolean {
  return useSession((s) => s.pending.some(pred));
}

export function useEvents(branch: BranchId, role: Role): Event[] {
  return useSession((s) => s.byRole[branch]?.[role] ?? EMPTY);
}

/**
 * F3: the anchor id of the in-flight fork, or `null`. `_pendingChild` ships
 * `at:` on every fork cmd (even variants whose `Up` type doesn't declare it)
 * so this reads uniformly across all six `FORK_KINDS`. Drives the derived
 * optimistic truncation that replaced the imperative `PENDING_BRANCH`
 * snapshot in `byRole`.
 */
export function usePendingForkAnchor(): string | null {
  return useSession((s) => {
    for (const c of s.pending) {
      if (FORK_KINDS.has(c.t)) return (c as { at?: string }).at ?? null;
    }
    return null;
  });
}

/**
 * F3: `useEvents` cut at the in-flight fork's anchor — the derived
 * replacement for `_truncateByRole`'s imperative `byRole[PENDING_BRANCH]`
 * snapshot. When no fork is pending (the common case) this is `useEvents`
 * with one extra `useMemo`. The anchor lives in exactly one role's column;
 * if it isn't in `events`, the full array is returned unchanged (matches
 * the old cross-role behaviour).
 */
export function useTruncatedEvents(branch: BranchId, role: Role): Event[] {
  const events = useEvents(branch, role);
  const anchor = usePendingForkAnchor();
  return useMemo(() => {
    if (anchor == null) return events;
    const idx = events.findIndex(
      (ev) => isModelEvent(ev) && ev.output.choices[0]?.message.id === anchor
    );
    return idx >= 0 ? events.slice(0, idx + 1) : events;
  }, [events, anchor]);
}

/** A4-partial: one open orchestrator gate card. */
export type PendingGate = { id: string; kind: string; desc: string };

/**
 * A4-partial (ARCHITECTURE-RACES.md): the orchestrator's open gate cards,
 * folded from the live `("orch","orch")` event stream.
 *
 * Lifted from `OrchColumn` so `useKeyboardShortcuts` (⌘Enter → approve
 * first) reads the same source instead of the stale wire-shipped
 * `orchestrator.pending_gates` — that field only refreshed on full
 * `{t:"state"}` and is now dropped from `Orchestrator.view()`. Gate cards
 * land as `InfoEvent`s with `bundle[WB_MIME].pending === true` and flip to
 * `false` on `dh.update()` when resolved, so the fold is always current.
 * Rewound turns (§2) are filtered so a gate the user restarted past
 * doesn't re-appear as approvable.
 */
export function usePendingGates(): PendingGate[] {
  const events = useEvents("orch", "orch");
  const rewound = useSession((s) => s.rewound);
  return useMemo(() => {
    const out: PendingGate[] = [];
    for (const ev of events) {
      if (ev.uuid != null && rewound.has(ev.uuid)) continue;
      if ((ev as { rewound?: unknown }).rewound === true) continue;
      if (ev.event !== "info" || ev.source !== ORCH_SOURCE) continue;
      const data = (ev as DisplayInfoEvent).data;
      const wb = data?.bundle?.[WB_MIME];
      if (wb == null || !("pending" in wb) || !wb.pending) continue;
      const desc =
        wb.kind === "prompt"
          ? wb.question
          : wb.kind === "run_proposal"
            ? wb.description
            : wb.kind === "cite_proposal"
              ? wb.claim
              : null;
      if (desc == null) continue;
      out.push({ id: data.id, kind: wb.kind, desc: desc || data.id.slice(0, 8) });
    }
    return out;
  }, [events, rewound]);
}

export function useQueued(branch: BranchId, role: Role): ChatMessage[] {
  // The queued map only tracks auditor/target (orch input goes via `orch_send`).
  return useSession((s) =>
    role === "orch" ? EMPTY_MSGS : (s.queued[branch]?.[role] ?? EMPTY_MSGS)
  );
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
  // OVERNIGHT-SWEEP P16: subscribe to `eventsRev` (a number — `Object.is`
  // short-circuits on every streaming `{t:"update"}`) and read the mutable
  // `events` Map imperatively inside the memo. Pre-C2 this subscribed to
  // `s.events` directly, which (with the P13 mutable Map) required a shim
  // that re-minted the Map on structural change; now `eventsRev` is the
  // sole recompute key and the Map ref is connection-stable.
  const eventsRev = useSession((s) => s.eventsRev);
  return useMemo(() => {
    if (!serverTl) return { timeline: null, rows: [], layouts: [], lineage: () => [] };
    const events = useSession.getState().events;
    const timeline = convertServerTimeline(serverTl, [...events.values()]);
    const rows = computeFlatSwimlaneRows(timeline.root, {
      includeUtility: true,
      showBranches: true,
    });
    const mapping = computeTimeMapping(timeline.root);
    // Fork-relative: each branch row's bar starts at its parent's fork point
    // and is sized by its own duration, not absolute wall-clock.
    const branchMappings = computeBranchMappings(rows, mapping);
    const layouts = computeRowLayouts(rows, mapping, "direct", ["branch", "error"], branchMappings);
    return {
      timeline,
      rows,
      layouts,
      lineage: (span) => splice(timeline.root, span),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverTl, eventsRev]);
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
