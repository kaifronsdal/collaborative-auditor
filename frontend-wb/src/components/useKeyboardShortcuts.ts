/**
 * P2 — global keyboard shortcuts.
 *
 * ⌘K / Ctrl+K → toggle the command palette. Fires everywhere (including
 *   inside inputs — matches VS Code / Linear).
 * ⌘Enter / Ctrl+Enter → approve the first pending orchestrator gate. Skipped
 *   when focus is in a text field (composers/rewrite panels own that chord)
 *   or when a component already handled and `preventDefault`ed the event.
 *
 * Esc-to-close is handled by `<Modal>` itself, not here.
 */
import { useEffect } from "react";

import { useSession } from "../store/session";

function isTextField(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable;
}

export function useKeyboardShortcuts({
  paletteOpen,
  setPaletteOpen,
}: {
  paletteOpen: boolean;
  setPaletteOpen: (open: boolean) => void;
}): void {
  const send = useSession((s) => s.send);
  const firstGate = useSession((s) => s.orchestrator?.pending_gates[0] ?? null);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent): void {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key === "k") {
        e.preventDefault();
        setPaletteOpen(!paletteOpen);
        return;
      }
      if (
        mod && e.key === "Enter" &&
        !paletteOpen && !e.defaultPrevented && !isTextField(e.target) &&
        firstGate != null
      ) {
        e.preventDefault();
        send({ t: "approve", display_id: firstGate, verdict: {} });
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [paletteOpen, setPaletteOpen, send, firstGate]);
}
