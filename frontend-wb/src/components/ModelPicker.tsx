/**
 * ModelPicker — combobox chip replacing the native <select>.
 *
 * Renders as a pill button (<button class="model-chip">) showing:
 *   <role>: <modelLabel(value)> ⌄
 * with optional param suffixes for non-default GenerateConfig values.
 *
 * Click opens an absolutely-positioned popover with:
 *   - filter input
 *   - preset list (from presets.ts)
 *   - recent models section (localStorage LRU, last 8)
 *   - ▸ config disclosure (reasoning_effort, temperature, max_tokens, top_p, seed, stop_sequences)
 *
 * Closes on outside-click or Escape.
 */

import type { JSX } from "react";
import { useEffect, useRef, useState } from "react";
import { MODELS, modelLabel } from "../lib/presets";

const RECENT_STORAGE_KEY = "workbench.recentModels";
const MAX_RECENT = 8;

export type GenerateConfigDict = {
  reasoning_effort?: "none" | "low" | "medium" | "high";
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  seed?: number;
  stop_sequences?: string[];
};

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

export function readStoredConfig(role: string): Partial<GenerateConfigDict> {
  try {
    const raw = localStorage.getItem(configStorageKey(role));
    if (!raw) return {};
    return JSON.parse(raw) as Partial<GenerateConfigDict>;
  } catch {
    return {};
  }
}

function writeStoredConfig(role: string, config: Partial<GenerateConfigDict>): void {
  try {
    localStorage.setItem(configStorageKey(role), JSON.stringify(config));
  } catch {
    // ignore
  }
}

/** Build param suffix string for non-default config values. */
function configSuffix(config: Partial<GenerateConfigDict>): string {
  const parts: string[] = [];
  if (config.reasoning_effort != null) parts.push(config.reasoning_effort);
  if (config.temperature != null) parts.push(`t=${config.temperature}`);
  if (config.max_tokens != null) parts.push(`tok=${config.max_tokens}`);
  if (config.top_p != null) parts.push(`p=${config.top_p}`);
  if (config.seed != null) parts.push(`seed=${config.seed}`);
  if (config.stop_sequences?.length) parts.push(`stop=${config.stop_sequences.length}`);
  return parts.length > 0 ? " · " + parts.join(" · ") : "";
}

type Props = {
  role: "auditor" | "target";
  value: string;
  config: Partial<GenerateConfigDict>;
  onChange: (value: string, config: Partial<GenerateConfigDict>) => void;
  compact?: boolean;
};

export function ModelPicker({ role, value, config, onChange, compact = false }: Props): JSX.Element {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [configOpen, setConfigOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
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

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    function handle(e: KeyboardEvent): void {
      if (e.key === "Escape") {
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
    onChange(modelId, config);
    setOpen(false);
    setFilter("");
  }

  function updateConfig(patch: Partial<GenerateConfigDict>): void {
    const next = { ...config, ...patch };
    // remove undefined/null values
    for (const k of Object.keys(next) as (keyof GenerateConfigDict)[]) {
      if (next[k] == null) delete next[k];
    }
    writeStoredConfig(role, next);
    onChange(value, next);
  }

  const lowerFilter = filter.toLowerCase();
  const matchingPresets = filter
    ? MODELS.filter(
        (m) =>
          m.toLowerCase().includes(lowerFilter) ||
          modelLabel(m).toLowerCase().includes(lowerFilter),
      )
    : MODELS;

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

  return (
    <div ref={containerRef} className="model-picker-wrap">
      <button
        className={`model-chip${open ? " open" : ""}${compact ? " compact" : ""}`}
        onClick={() => setOpen((v) => !v)}
        type="button"
      >
        {!compact && <span className="chip-label">{role}</span>}
        <span className="chip-model">{compact ? chipLabel : `${modelLabel(value)}${configSuffix(config)}`}</span>
        <span className="chip-caret">⌄</span>
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

          {/* Presets */}
          {matchingPresets.length > 0 && (
            <>
              <div className="picker-section-label">Models</div>
              {matchingPresets.map((m) => (
                <button
                  key={m}
                  className={`picker-row${value === m ? " selected" : ""}`}
                  onClick={() => select(m)}
                  type="button"
                >
                  {value === m && <span className="picker-bullet">●</span>}
                  {value !== m && <span className="picker-bullet" />}
                  {modelLabel(m)}
                  <span className="picker-model-id">{m}</span>
                </button>
              ))}
            </>
          )}

          {/* Recent */}
          {recentToShow.length > 0 && (
            <>
              <div className="picker-section-label">Recent</div>
              {recentToShow.map((m) => (
                <button
                  key={`recent-${m}`}
                  className={`picker-row${value === m ? " selected" : ""}`}
                  onClick={() => select(m)}
                  type="button"
                >
                  {value === m && <span className="picker-bullet">●</span>}
                  {value !== m && <span className="picker-bullet" />}
                  {modelLabel(m)}
                  <span className="picker-model-id">{m}</span>
                </button>
              ))}
            </>
          )}

          {/* Config disclosure */}
          <div className="picker-config">
            <button
              className="picker-config-toggle"
              onClick={() => setConfigOpen((v) => !v)}
              type="button"
            >
              <span className="picker-config-arrow">{configOpen ? "▾" : "▸"}</span>
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
                        onClick={() => {
                          const { reasoning_effort: _re, ...rest } = config;
                          writeStoredConfig(role, rest);
                          onChange(value, rest);
                        }}
                        type="button"
                      >
                        ✕
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
                        onClick={() => {
                          const { temperature: _t, ...rest } = config;
                          writeStoredConfig(role, rest);
                          onChange(value, rest);
                        }}
                        type="button"
                      >
                        ✕
                      </button>
                    )}
                  </div>
                </div>

                {/* max_tokens */}
                <div className="picker-cfg-row">
                  <span className="picker-cfg-label">max_tokens</span>
                  <input
                    type="number"
                    className="picker-cfg-number"
                    min={1}
                    placeholder="unset"
                    value={config.max_tokens ?? ""}
                    onChange={(e) => {
                      const v = e.target.value;
                      updateConfig({ max_tokens: v === "" ? undefined : parseInt(v, 10) });
                    }}
                  />
                </div>

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
                    {/* top_p */}
                    <div className="picker-cfg-row">
                      <span className="picker-cfg-label">top_p</span>
                      <input
                        type="number"
                        className="picker-cfg-number"
                        min={0}
                        max={1}
                        step={0.01}
                        placeholder="unset"
                        value={config.top_p ?? ""}
                        onChange={(e) => {
                          const v = e.target.value;
                          updateConfig({ top_p: v === "" ? undefined : parseFloat(v) });
                        }}
                      />
                    </div>

                    {/* seed */}
                    <div className="picker-cfg-row">
                      <span className="picker-cfg-label">seed</span>
                      <input
                        type="number"
                        className="picker-cfg-number"
                        placeholder="unset"
                        value={config.seed ?? ""}
                        onChange={(e) => {
                          const v = e.target.value;
                          updateConfig({ seed: v === "" ? undefined : parseInt(v, 10) });
                        }}
                      />
                    </div>

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
                  </>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
