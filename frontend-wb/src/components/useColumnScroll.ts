/**
 * Shared scroll machinery for {@link LinearColumn} and {@link SwimlaneColumn}:
 * tail-follow, per-row element registry, timestamp bisection, sync-jump
 * highlight, and rAF-coalesced linked-scroll emit. The hook installs the
 * `ColumnHandle` on `ref` itself so callers just forward it.
 *
 * Behaviours:
 *  - Tail-follow: `stick` starts true; any user scroll >80px above the bottom
 *    releases it. `useLayoutEffect` re-pins on every render while stuck (which
 *    catches streaming partials that mutate the last event in place).
 *  - `linked`: when set, every scroll frame emits `onSync(centeredTimestamp())`
 *    (rAF-coalesced). When unset, `onScroll` degrades to the plain stick check.
 *  - `scrollToTimestamp`: bisect `turns`, scroll the nearest registered row to
 *    center, pulse `highlighted` for 1.5s. Releases `stick`.
 */
import {
  type ForwardedRef,
  type RefObject,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { bisectTurns, type Turn } from "../lib/events";
import type { ColumnHandle } from "./Column";

export function useColumnScroll(
  turns: Turn[],
  { linked, onSync }: { linked?: boolean; onSync?: (ts: string) => void },
  ref: ForwardedRef<ColumnHandle>
): {
  scrollRef: RefObject<HTMLDivElement | null>;
  onScroll: () => void;
  rowRef: (uuid: string) => (el: HTMLElement | null) => void;
  highlighted: string | null;
} {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const rowEls = useRef(new Map<string, HTMLElement>());

  // Transient highlight: pulse the row a sync-jump landed on, then clear.
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const hlTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (hlTimer.current) clearTimeout(hlTimer.current); }, []);

  // Tail-follow on every render (catches streaming partials, not just new
  // events — `assignByRole` clones the array on every reducer update).
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  const centeredTimestamp = (): string | null => {
    const sc = scrollRef.current;
    if (!sc) return null;
    const mid = sc.getBoundingClientRect().top + sc.clientHeight / 2;
    let best: { d: number; ts: string } | null = null;
    for (const turn of turns) {
      const el = rowEls.current.get(turn.ev.uuid!);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      const d = Math.abs((r.top + r.bottom) / 2 - mid);
      if (best == null || d < best.d) best = { d, ts: turn.ev.timestamp };
    }
    return best?.ts ?? null;
  };

  useImperativeHandle(
    ref,
    () => ({
      scrollToTimestamp(ts) {
        let i = bisectTurns(turns, ts);
        while (i >= 0 && !rowEls.current.has(turns[i].ev.uuid!)) i--;
        const el = i >= 0 ? rowEls.current.get(turns[i].ev.uuid!) : undefined;
        if (!el) return;
        stick.current = false;
        el.scrollIntoView({ block: "center", behavior: "auto" });
        setHighlighted(turns[i].ev.uuid!);
        if (hlTimer.current) clearTimeout(hlTimer.current);
        hlTimer.current = setTimeout(() => setHighlighted(null), 1500);
      },
      centeredTimestamp,
    }),
    [turns]
  );

  // Linked-scroll: lockstep — emit on every scroll frame (rAF-coalesced so we
  // don't thrash on high-frequency wheel events, but no perceptible delay).
  const syncRaf = useRef<number | null>(null);
  useEffect(() => () => { if (syncRaf.current) cancelAnimationFrame(syncRaf.current); }, []);

  const onScroll = (): void => {
    const el = scrollRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (!linked || !onSync || syncRaf.current != null) return;
    syncRaf.current = requestAnimationFrame(() => {
      syncRaf.current = null;
      const ts = centeredTimestamp();
      if (ts) onSync(ts);
    });
  };

  const rowRef = (uuid: string) => (el: HTMLElement | null): void => {
    if (el) rowEls.current.set(uuid, el);
    else rowEls.current.delete(uuid);
  };

  return { scrollRef, onScroll, rowRef, highlighted };
}
