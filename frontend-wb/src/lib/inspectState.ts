/**
 * In-memory `ComponentStateHooks` so any `@tsmono/inspect-components` widget
 * (TimelineSwimLanes, ExpandablePanel, popovers, …) can persist its UI state
 * without an app-specific store. Mirrors scout's adapter shape but backed by
 * a tiny standalone zustand store — nothing here needs to survive reload.
 */
import { useCallback } from "react";
import { create } from "zustand";

import type { ComponentStateHooks } from "@tsmono/react/state";

type Bag = Record<string, Record<string, unknown>>;

const useBag = create<{
  bag: Bag;
  set: (id: string, prop: string, value: unknown) => void;
  remove: (id: string, prop: string) => void;
  removeAll: (id: string) => void;
  removeByPrefix: (id: string, prefix: string) => void;
}>((set) => ({
  bag: {},
  set: (id, prop, value) =>
    set((s) => ({ bag: { ...s.bag, [id]: { ...s.bag[id], [prop]: value } } })),
  remove: (id, prop) =>
    set((s) => {
      const { [prop]: _, ...rest } = s.bag[id] ?? {};
      return { bag: { ...s.bag, [id]: rest } };
    }),
  removeAll: (id) =>
    set((s) => {
      const { [id]: _, ...rest } = s.bag;
      return { bag: rest };
    }),
  removeByPrefix: (id, prefix) =>
    set((s) => {
      const entries = s.bag[id] ?? {};
      const kept = Object.fromEntries(
        Object.entries(entries).filter(([k]) => !k.startsWith(prefix))
      );
      return { bag: { ...s.bag, [id]: kept } };
    }),
}));

export const componentStateHooks: ComponentStateHooks = {
  useValue: (id, prop, defaultValue) =>
    useBag(useCallback((s) => s.bag[id]?.[prop] ?? defaultValue, [id, prop, defaultValue])),
  useSetValue: () => useBag((s) => s.set),
  useRemoveValue: () => useBag((s) => s.remove),
  useEntries: (id) => useBag(useCallback((s) => s.bag[id], [id])),
  useRemoveAll: () => useBag((s) => s.removeAll),
  useRemoveByPrefix: () => useBag((s) => s.removeByPrefix),
};
