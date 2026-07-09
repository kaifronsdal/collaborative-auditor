/**
 * ModelPicker — combobox chip replacing the native <select>.
 *
 * Renders as a pill button (<button class="model-chip">) showing:
 *   <role>: <modelLabel(value)> ⌄
 * with optional param suffixes for non-default GenerateConfig values.
 *
 * Click opens an absolutely-positioned popover with:
 *   - filter input
 *   - preset list (from presets.ts, populated by `GET /models`)
 *   - recent models section (localStorage LRU, last 8)
 *   - ▸ config disclosure (reasoning_effort, temperature, max_tokens, top_p,
 *     seed, stop_sequences + a "raw JSON" textarea for any other
 *     `GenerateConfig` key — P1.5)
 *   - ▸ advanced disclosure with a `model_args` JSON textarea (`base_url`,
 *     `api_key`, arbitrary provider kwargs — P1.4)
 *
 * Closes on outside-click or Escape.
 */

import type { JSX } from "react";
import { useEffect, useRef, useState } from "react";
import { MODELS, PROVIDERS, modelLabel } from "../lib/presets";
import { Chevron, IconClose } from "./icons";

const RECENT_STORAGE_KEY = "workbench.recentModels";
const MAX_RECENT = 8;

/** P1.5 — open `GenerateConfig` subset. The six named fields keep their
 *  precise types for the dedicated UI controls; the index signature lets any
 *  other `GenerateConfig` key (`reasoning_tokens`, `parallel_tool_calls`,
 *  `extra_body`, `response_schema`, `logprobs`, …) flow through the "raw
 *  JSON" expander. Backend already `**config`'s the whole dict into
 *  `GenerateConfig(...)`. */
export type GenerateConfigDict = {
  reasoning_effort?: "none" | "low" | "medium" | "high";
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  seed?: number;
  stop_sequences?: string[];
  [key: string]: unknown;
};

/** P1.4 — provider kwargs passed to `get_model(model, **model_args)`.
 *  Free-form: `base_url`, `api_key`, or anything the provider accepts. */
export type ModelArgs = Record<string, unknown>;

/** The named-field subset of `GenerateConfigDict` that has a dedicated UI
 *  control. Everything else lives in the raw-JSON expander. */
const NAMED_CONFIG_KEYS: readonly string[] = [
  "reasoning_effort", "temperature", "max_tokens", "top_p", "seed", "stop_sequences",
];

function readRecent(): string[] {
  try {
    const raw = localStorage.getItem(RECENT_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as string[]) : [];
  } catch {
    return [];
  }
}

function writeRecent(id: string): void {
  const current = readRecent().filter((m) => m !== id);
  const next = [id, ...current].slice(0, MAX_RECENT);
  try {
    localStorage.setItem(RECENT_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // storage quota — ignore
  }
}

function configStorageKey(role: string): string {
  return `workbench.config.${role}`;
}

export function readStoredConfig(role: string): GenerateConfigDict {
  try {
    const raw = localStorage.getItem(configStorageKey(role));
    if (!raw) return {};
    return JSON.parse(raw) as GenerateConfigDict;
  } catch {
    return {};
  }
}

function writeStoredConfig(role: string, config: GenerateConfigDict): void {
  try {
    localStorage.setItem(configStorageKey(role), JSON.stringify(config));
  } catch {
    // ignore
  }
}

/** Build param suffix string for non-default config values. */
function configSuffix(config: GenerateConfigDict): string {
  const parts: string[] = [];
  if (config.reasoning_effort != null) parts.push(String(config.reasoning_effort));
  if (config.temperature != null) parts.push(`t=${config.temperature}`);
  if (config.max_tokens != null) parts.push(`tok=${config.max_tokens}`);
  if (config.top_p != null) parts.push(`p=${config.top_p}`);
  if (config.seed != null) parts.push(`seed=${config.seed}`);
  if (config.stop_sequences?.length) parts.push(`stop=${config.stop_sequences.length}`);
  const extra = Object.keys(config).filter((k) => !NAMED_CONFIG_KEYS.includes(k));
  if (extra.length) parts.push(`+${extra.length}`);
  return parts.length > 0 ? " · " + parts.join(" · ") : "";
}

/** Every `ModelPicker` role in the workbench (P1.1 extends the original
 *  auditor/target pair to cover the orchestrator itself and the judge). */
export type PickerRole = "auditor" | "target" | "orchestrator" | "judge";

type Props = {
  role: PickerRole;
  value: string;
  config: GenerateConfigDict;
  /** P1.4 — provider kwargs. Optional so existing call sites (Sidebar,
   *  DeskStartCard) needn't change; when omitted the "advanced" expander
   *  still edits a local dict and reports it via `onChange`'s third arg. */
  modelArgs?: ModelArgs;
  onChange: (value: string, config: GenerateConfigDict, modelArgs: ModelArgs) => void;
  compact?: boolean;
};

export function ModelPicker({
  role, value, config, modelArgs = {}, onChange, compact = false,
}: Props): JSX.Element {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [configOpen, setConfigOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [advOpen, setAdvOpen] = useState(false);
  const [recent, setRecent] = useState<string[]>(() => readRecent());

  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Close on outside-click
  useEffect(() => {
    if (!open) return;
    function handle(e: MouseEvent): void {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
        setFilter("");
      }
    }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, [open]);

  // Close on Escape. K1: `stopPropagation` so a picker opened *inside* a modal
  // (SettingsModal, StartView card) closes only itself — the parent Modal's
  // own Esc handler would otherwise fire on the same event and close both.
  useEffect(() => {
    if (!open) return;
    function handle(e: KeyboardEvent): void {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        setFilter("");
      }
    }
    document.addEventListener("keydown", handle);
    return () => document.removeEventListener("keydown", handle);
  }, [open]);

  // Focus input when opened
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  function select(modelId: string): void {
    writeRecent(modelId);
    setRecent(readRecent());
    writeStoredConfig(role, config);
    onChange(modelId, config, modelArgs);
    setOpen(false);
    setFilter("");
  }

  function updateConfig(patch: Partial<GenerateConfigDict>): void {
    const next = { ...config, ...patch };
    // remove undefined/null values
    for (const k of Object.keys(next)) {
      if (next[k] == null) delete next[k];
    }
    writeStoredConfig(role, next);
    onChange(value, next, modelArgs);
  }

  function updateModelArgs(next: ModelArgs): void {
    onChange(value, config, next);
  }

  const lowerFilter = filter.toLowerCase();
  const matchingPresets = filter
    ? MODELS.filter(
        (m) =>
          m.toLowerCase().includes(lowerFilter) ||
          modelLabel(m).toLowerCase().includes(lowerFilter),
      )
    : MODELS;

  // If the filter matches a bare provider prefix (e.g. "vllm/"), offer it as
  // a custom row so gateway/local providers with no suggestions are still
  // one-click reachable.
  const providerHit =
    !filter.includes("/") &&
    PROVIDERS.find((p) => p.toLowerCase().startsWith(lowerFilter) && lowerFilter.length > 0);

  // Show "Use custom" row when filter has "/" and no preset matches
  const showCustomRow =
    filter.includes("/") && !matchingPresets.some((m) => m === filter);

  const recentToShow = recent.filter(
    (m) =>
      !filter ||
      m.toLowerCase().includes(lowerFilter) ||
      modelLabel(m).toLowerCase().includes(lowerFilter),
  );

  const chipLabel = compact
    ? modelLabel(value)
    : `${role}: ${modelLabel(value)}${configSuffix(config)}`;

  /** One `.picker-row` — Models and Recent lists render the same row. */
  const row = (m: string, keyPrefix = ""): JSX.Element => (
    <button
      key={`${keyPrefix}${m}`}
      className={`picker-row${value === m ? " selected" : ""}`}
      onClick={() => select(m)}
      type="button"
    >
      <span className="picker-bullet">{value === m ? "●" : ""}</span>
      {modelLabel(m)}
      <span className="picker-model-id">{m}</span>
    </button>
  );

  type NumField = "max_tokens" | "top_p" | "seed";
  /** Label + `<input type="number">` bound to one numeric config field. */
  const NumRow = ({
    field, label, parse, ...attrs
  }: {
    field: NumField; label: string; parse: (s: string) => number;
  } & Pick<JSX.IntrinsicElements["input"], "min" | "max" | "step">): JSX.Element => (
    <div className="picker-cfg-row">
      <span className="picker-cfg-label">{label}</span>
      <input
        type="number"
        className="picker-cfg-number"
        placeholder="unset"
        value={(config[field] as number | undefined) ?? ""}
        onChange={(e) => {
          const v = e.target.value;
          updateConfig({ [field]: v === "" ? undefined : parse(v) });
        }}
        {...attrs}
      />
    </div>
  );

  return (
    <div ref={containerRef} className="model-picker-wrap">
      <button
        className={`model-chip${open ? " open" : ""}${compact ? " compact" : ""}`}
        onClick={() => setOpen((v) => !v)}
        type="button"
      >
        {!compact && <span className="chip-label">{role}</span>}
        <span className="chip-model">{compact ? chipLabel : `${modelLabel(value)}${configSuffix(config)}`}</span>
        <i className="bi bi-chevron-down chip-caret" />
      </button>

      {open && (
        <div className="picker-popover">
          {/* Filter input */}
          <div className="picker-filter-row">
            <input
              ref={inputRef}
              className="picker-filter"
              type="text"
              placeholder="Filter or type provider/model…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && filter.includes("/")) {
                  select(filter);
                }
              }}
            />
          </div>

          {/* Custom model row */}
          {showCustomRow && (
            <button
              className={`picker-row${value === filter ? " selected" : ""}`}
              onClick={() => select(filter)}
              type="button"
            >
              <span className="picker-custom-hint">↵ Use "{filter}"</span>
            </button>
          )}
          {providerHit && !filter.includes("/") && (
            <button
              className="picker-row"
              onClick={() => setFilter(`${providerHit}/`)}
              type="button"
            >
              <span className="picker-custom-hint">{providerHit}/…</span>
            </button>
          )}

          {/* Presets */}
          {matchingPresets.length > 0 && (
            <>
              <div className="picker-section-label">Models</div>
              {matchingPresets.map((m) => row(m))}
            </>
          )}

          {/* Recent */}
          {recentToShow.length > 0 && (
            <>
              <div className="picker-section-label">Recent</div>
              {recentToShow.map((m) => row(m, "recent-"))}
            </>
          )}

          {/* Config disclosure */}
          <div className="picker-config">
            <button
              className="picker-config-toggle"
              onClick={() => setConfigOpen((v) => !v)}
              type="button"
            >
              <Chevron open={configOpen} size={10} className="picker-config-arrow" />
              {" config"}
            </button>

            {configOpen && (
              <div className="picker-config-body">
                {/* reasoning_effort */}
                <div className="picker-cfg-row">
                  <span className="picker-cfg-label">reasoning</span>
                  <div className="picker-cfg-radios">
                    {(["none", "low", "medium", "high"] as const).map((v) => (
                      <label key={v} className="picker-radio-label">
                        <input
                          type="radio"
                          name={`${role}-reasoning`}
                          value={v}
                          checked={config.reasoning_effort === v}
                          onChange={() => updateConfig({ reasoning_effort: v })}
                        />
                        {v}
                      </label>
                    ))}
                    {config.reasoning_effort != null && (
                      <button
                        className="picker-cfg-clear"
                        onClick={() => updateConfig({ reasoning_effort: undefined })}
                        type="button"
                      >
                        <IconClose size={10} />
                      </button>
                    )}
                  </div>
                </div>

                {/* temperature */}
                <div className="picker-cfg-row">
                  <span className="picker-cfg-label">temperature</span>
                  <div className="picker-cfg-slider-row">
                    <input
                      type="range"
                      min={0}
                      max={2}
                      step={0.05}
                      value={config.temperature ?? 1}
                      onChange={(e) => updateConfig({ temperature: parseFloat(e.target.value) })}
                      className="picker-cfg-range"
                    />
                    <span className="picker-cfg-num">{(config.temperature ?? 1).toFixed(2)}</span>
                    {config.temperature != null && (
                      <button
                        className="picker-cfg-clear"
                        onClick={() => updateConfig({ temperature: undefined })}
                        type="button"
                      >
                        <IconClose size={10} />
                      </button>
                    )}
                  </div>
                </div>

                <NumRow field="max_tokens" label="max_tokens" min={1} parse={(v) => parseInt(v, 10)} />

                {/* more… */}
                <button
                  className="picker-more-toggle"
                  onClick={() => setMoreOpen((v) => !v)}
                  type="button"
                >
                  {moreOpen ? "less…" : "more…"}
                </button>

                {moreOpen && (
                  <>
                    <NumRow field="top_p" label="top_p" min={0} max={1} step={0.01} parse={parseFloat} />
                    <NumRow field="seed" label="seed" parse={(v) => parseInt(v, 10)} />

                    {/* stop_sequences */}
                    <div className="picker-cfg-row picker-cfg-row--col">
                      <span className="picker-cfg-label">stop_sequences</span>
                      <textarea
                        className="picker-cfg-textarea"
                        rows={3}
                        placeholder="one per line"
                        value={(config.stop_sequences ?? []).join("\n")}
                        onChange={(e) => {
                          const lines = e.target.value
                            .split("\n")
                            .filter((l) => l.length > 0);
                          updateConfig({ stop_sequences: lines.length > 0 ? lines : undefined });
                        }}
                      />
                    </div>

                    {/* P1.5 — raw GenerateConfig JSON: any key not covered above.
                        Merged over the named fields on parse; the named fields
                        keep their dedicated controls. */}
                    <JsonRow
                      label="raw config (JSON)"
                      placeholder='{"reasoning_tokens": 8192, "parallel_tool_calls": false}'
                      value={Object.fromEntries(
                        Object.entries(config).filter(([k]) => !NAMED_CONFIG_KEYS.includes(k)),
                      )}
                      onChange={(extra) => {
                        const named = Object.fromEntries(
                          Object.entries(config).filter(([k]) => NAMED_CONFIG_KEYS.includes(k)),
                        );
                        const next = { ...named, ...extra };
                        writeStoredConfig(role, next);
                        onChange(value, next, modelArgs);
                      }}
                    />
                  </>
                )}
              </div>
            )}
          </div>

          {/* P1.4 — advanced: model_args (provider kwargs). */}
          <div className="picker-config">
            <button
              className="picker-config-toggle"
              onClick={() => setAdvOpen((v) => !v)}
              type="button"
            >
              <Chevron open={advOpen} size={10} className="picker-config-arrow" />
              {" advanced"}
              {Object.keys(modelArgs).length > 0 && (
                <span className="picker-cfg-num"> · {Object.keys(modelArgs).length}</span>
              )}
            </button>
            {advOpen && (
              <div className="picker-config-body">
                <JsonRow
                  label="model_args (JSON)"
                  placeholder='{"base_url": "http://localhost:8000/v1", "api_key": "…"}'
                  value={modelArgs}
                  onChange={updateModelArgs}
                />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** A labelled JSON `<textarea>` bound to a `Record<string, unknown>`. Local
 *  text state so intermediate invalid JSON doesn't thrash the parent; the
 *  border goes `--danger` while unparseable and `onChange` fires only on a
 *  successful parse. Shared by the raw-config (P1.5) and model_args (P1.4)
 *  expanders. */
function JsonRow({
  label, placeholder, value, onChange,
}: {
  label: string;
  placeholder: string;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
}): JSX.Element {
  const [text, setText] = useState(() =>
    Object.keys(value).length ? JSON.stringify(value, null, 2) : "",
  );
  const [err, setErr] = useState<string | null>(null);
  return (
    <div className="picker-cfg-row picker-cfg-row--col">
      <span className="picker-cfg-label">
        {label}
        {err && <span className="picker-cfg-err"> — {err}</span>}
      </span>
      <textarea
        className={`picker-cfg-textarea${err ? " invalid" : ""}`}
        rows={3}
        placeholder={placeholder}
        value={text}
        onChange={(e) => {
          const t = e.target.value;
          setText(t);
          if (t.trim() === "") {
            setErr(null);
            onChange({});
            return;
          }
          try {
            const parsed = JSON.parse(t);
            if (parsed == null || typeof parsed !== "object" || Array.isArray(parsed)) {
              throw new Error("must be an object");
            }
            setErr(null);
            onChange(parsed as Record<string, unknown>);
          } catch (ex) {
            setErr(ex instanceof Error ? ex.message : "invalid JSON");
          }
        }}
      />
    </div>
  );
}
