/**
 * `<ComposerTextarea>` — the shared autosize + Enter/Shift-Enter textarea used
 * by both column composers (M0 `DeskView`, M1 `OrchColumn`). The surrounding
 * `.composer` shell and `.composer-lower` button row stay caller-owned — their
 * state machines genuinely differ (M1-REFACTOR.md §"Not doing").
 */
import { useLayoutEffect, useRef, type JSX } from "react";

import { autosizeTextarea } from "@tsmono/util";

export function ComposerTextarea({
  value,
  setValue,
  onEnter,
  placeholder,
  maxRows = 6,
}: {
  value: string;
  setValue: (v: string) => void;
  onEnter: () => void;
  placeholder: string;
  maxRows?: number;
}): JSX.Element {
  const ref = useRef<HTMLTextAreaElement>(null);
  // Resize on every value change (incl. programmatic clear-after-send), not
  // just onChange — otherwise the box stays tall after `setValue("")`.
  useLayoutEffect(() => {
    if (ref.current) autosizeTextarea(ref.current, { minRows: 1, maxRows });
  }, [value, maxRows]);
  return (
    <textarea
      ref={ref}
      className="composer-input"
      rows={1}
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          onEnter();
        }
      }}
      placeholder={placeholder}
    />
  );
}
