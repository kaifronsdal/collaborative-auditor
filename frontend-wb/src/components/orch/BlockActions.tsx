/**
 * `<BlockActions>` — hover-reveal horizontal icon row (M1-FEATURES §6).
 *
 * The claude.ai idiom: a small right-aligned row of xs icon buttons that fades
 * in when the containing block is hovered. Positioned absolute bottom-right of
 * its `.ba-host` parent, so it works on prose (no chrome), code cells (bordered
 * box), and every `<Output>` variant without per-block layout tweaks.
 *
 * `<CopyBtn>` writes `text` (or its lazy result) to the clipboard and swaps
 * `bi-clipboard` → `bi-check2` for one second.
 */
import { useState, type JSX, type ReactNode } from "react";

export function BlockActions({ children }: { children: ReactNode }): JSX.Element {
  return <div className="block-actions">{children}</div>;
}

export function CopyBtn({
  text,
  title = "copy",
}: {
  /** The text to copy, or a thunk that produces it (for expensive
   *  serialisations — DataFrame → TSV, JSON.stringify). */
  text: string | (() => string);
  title?: string;
}): JSX.Element {
  const [ok, setOk] = useState(false);
  return (
    <button
      type="button"
      title={ok ? "copied" : title}
      onClick={(e) => {
        e.stopPropagation();
        const t = typeof text === "function" ? text() : text;
        void navigator.clipboard.writeText(t).then(() => {
          setOk(true);
          setTimeout(() => setOk(false), 1000);
        });
      }}
    >
      <i className={`bi bi-${ok ? "check2" : "clipboard"}`} />
    </button>
  );
}

/** Serialise an HTML `<table>` to TSV (for pandas DataFrame copy). */
export function tableToTsv(html: string): string {
  const host = document.createElement("div");
  host.innerHTML = html;
  const table = host.querySelector("table");
  if (!table) return host.textContent ?? "";
  const lines: string[] = [];
  for (const tr of table.querySelectorAll("tr")) {
    const cells = Array.from(tr.querySelectorAll<HTMLElement>("th,td")).map((c) =>
      (c.textContent ?? "").trim().replace(/\t/g, " ")
    );
    lines.push(cells.join("\t"));
  }
  return lines.join("\n");
}
