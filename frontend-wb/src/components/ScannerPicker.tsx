/**
 * P1.8(b) — M0 manual scan.
 *
 * `ScannerPicker` is a small free-text combobox over `GET /scanners` (groups
 * bold, individual scanners plain). `ScanControl` is the target-column-head
 * "Scan" button + popover: pick a scanner/group, hit Run, and
 * `{t:"scan_branch", scope:"transcript"}` fires. Results arrive as
 * `InfoEvent(source="branch_scan")` on the branch's target span; the popover
 * renders the latest one via the existing `ProgressCard` scan variant so the
 * card shape matches `wb.scan`'s.
 *
 * Per-bubble `scope:{turn:N}` is P2 (design/PRODUCT-GAPS.md P1.8(b)).
 */
import { useEffect, useMemo, useRef, useState, type JSX } from "react";

import { useEvents, useIsPending } from "../lib/selectors";
import type { BranchId } from "../lib/wire";
import { useSession } from "../store/session";
import ProgressCard from "./orch/cards/ProgressCard";
import { WB_MIME, type ScanPayload } from "./orch/types";

type ScannersResponse = {
  scanners: string[];
  groups: Record<string, string[]>;
};

/** Module-level cache — the picker may mount in several column heads. */
let _scanners: ScannersResponse | null = null;
let _fetching: Promise<ScannersResponse> | null = null;

async function fetchScanners(): Promise<ScannersResponse> {
  if (_scanners) return _scanners;
  if (_fetching) return _fetching;
  _fetching = fetch("/scanners")
    .then((r) => r.json() as Promise<ScannersResponse>)
    .then((d) => {
      _scanners = d;
      return d;
    })
    .finally(() => {
      _fetching = null;
    });
  return _fetching;
}

// -- picker -------------------------------------------------------------------

type PickerProps = {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
};

/** Free-text combobox over the scanner library. Groups render bold with
 *  their member count; individual scanners render plain. Typing filters
 *  both; Enter accepts the top match (or the raw text if nothing matches —
 *  `resolve()` will surface a "did you mean?" error server-side). */
export function ScannerPicker({ value, onChange, placeholder }: PickerProps): JSX.Element {
  const [lib, setLib] = useState<ScannersResponse>(
    _scanners ?? { scanners: [], groups: {} }
  );
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState(0);

  useEffect(() => {
    void fetchScanners().then(setLib);
  }, []);

  const q = value.trim().toLowerCase();
  const groupNames = Object.keys(lib.groups);
  const options = useMemo(() => {
    const match = (s: string): boolean => q === "" || s.toLowerCase().includes(q);
    return [
      ...groupNames.filter(match).map((g) => ({ name: g, group: true as const })),
      ...lib.scanners.filter(match).map((s) => ({ name: s, group: false as const })),
    ];
  }, [q, groupNames, lib.scanners]);

  // Clamp selection when the filtered list shrinks (mirrors CommandPalette).
  useEffect(() => {
    if (sel >= options.length) setSel(Math.max(0, options.length - 1));
  }, [options.length, sel]);

  return (
    <div style={{ position: "relative" }}>
      <input
        type="text"
        className="grm-search"
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setSel(0);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setSel((i) => (options.length ? (i + 1) % options.length : 0));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setSel((i) => (options.length ? (i - 1 + options.length) % options.length : 0));
          } else if (e.key === "Enter" && options.length > 0) {
            onChange(options[sel].name);
            setOpen(false);
          } else if (e.key === "Escape") {
            setOpen(false);
          }
        }}
        placeholder={placeholder ?? "scanner or group…"}
        style={{ width: "100%", boxSizing: "border-box" }}
      />
      {open && options.length > 0 && (
        <div
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            right: 0,
            zIndex: 20,
            maxHeight: "12rem",
            overflowY: "auto",
            background: "var(--bg-000)",
            border: "1px solid var(--border)",
            borderRadius: 4,
            fontSize: "0.85em",
          }}
        >
          {options.map((o, i) => (
            <div
              key={`${o.group ? "g" : "s"}:${o.name}`}
              className={`scan-opt${i === sel ? " selected" : ""}`}
              onMouseEnter={() => setSel(i)}
              onMouseDown={(e) => {
                e.preventDefault();
                onChange(o.name);
                setOpen(false);
              }}
              style={{ fontWeight: o.group ? 600 : 400 }}
              title={o.group ? lib.groups[o.name].join(", ") : undefined}
            >
              {o.name}
              {o.group && (
                <span style={{ opacity: 0.6, fontWeight: 400 }}>
                  {" "}
                  · {lib.groups[o.name].length}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// -- column-head control ------------------------------------------------------

/** `InfoEvent`s emitted by `_h_scan_branch` land in the branch's target
 *  bucket with `source === "branch_scan"` and a `ScanPayload` under the
 *  vendor MIME. Newest first. */
function useBranchScans(branch: BranchId): ScanPayload[] {
  const evs = useEvents(branch, "target");
  return useMemo(() => {
    const out: ScanPayload[] = [];
    for (const ev of evs) {
      if (ev.event !== "info" || ev.source !== "branch_scan") continue;
      const data = ev.data as { bundle?: Record<string, unknown> } | undefined;
      const p = data?.bundle?.[WB_MIME] as ScanPayload | undefined;
      if (p?.kind === "scan") out.push(p);
    }
    return out.reverse();
  }, [evs]);
}

/** The "Scan" button that lives in the target-column head. Absolutely
 *  positioned by the caller (DeskView) so it overlays whichever column
 *  component (`SwimlaneColumn` / `LinearColumn`) is rendering the head,
 *  keeping this batch's diff off `SwimlaneColumn.tsx` (P1.8(c)'s file). */
export function ScanControl({ branch }: { branch: BranchId }): JSX.Element {
  const send = useSession((s) => s.send);
  const [open, setOpen] = useState(false);
  const [scanner, setScanner] = useState("");
  // A2: replaces the R1 local `running` flag + InfoEvent-count effect.
  // `_h_scan_branch` is `UNLOCKED` and emits the running-card `InfoEvent`
  // synchronously before spawning the scan task, so the ack lands right
  // after that card appears — Run re-enables once the placeholder is
  // visible, which is the point at which a second Run is meaningful.
  const running = useIsPending((c) => c.t === "scan_branch");
  const ref = useRef<HTMLDivElement>(null);
  const scans = useBranchScans(branch);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const run = (): void => {
    const name = scanner.trim();
    if (!name || running) return;
    send({ t: "scan_branch", branch_id: branch, scanner: name, scope: "transcript" });
  };

  return (
    <div ref={ref} style={{ position: "absolute", top: 4, right: 8, zIndex: 5 }}>
      <button
        type="button"
        className="scan-trigger"
        onClick={() => setOpen((v) => !v)}
        title="Run a scanner over the target's conversation"
      >
        scan{scans.length > 0 && ` · ${scans.length}`}
      </button>
      {open && (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 4px)",
            right: 0,
            width: "22rem",
            maxHeight: "60vh",
            overflowY: "auto",
            padding: "0.6rem",
            background: "var(--bg-000)",
            border: "1px solid var(--border)",
            borderRadius: 6,
            boxShadow: "0 4px 16px rgba(0,0,0,0.15)",
            textTransform: "none",
          }}
        >
          <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
            <div style={{ flex: 1 }}>
              <ScannerPicker value={scanner} onChange={setScanner} />
            </div>
            <button
              type="button"
              className="gate-btn primary"
              onClick={run}
              disabled={running || !scanner.trim()}
            >
              {running ? "…" : "Run"}
            </button>
          </div>
          {scans.map((p) => (
            <div key={p.id} className="out" style={{ marginBottom: 6 }}>
              <ProgressCard payload={p} displayId={p.id} send={send} />
            </div>
          ))}
          {scans.length === 0 && (
            <div style={{ opacity: 0.6, fontSize: "0.85em" }}>
              Pick a scanner or group and hit Run to score the whole target
              transcript.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
