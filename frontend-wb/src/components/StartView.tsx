import type { JSX } from "react";
import { useState } from "react";

import {
  DEFAULT_AUDITOR,
  DEFAULT_TARGET,
  MODELS,
  SEED_PRESETS,
  modelLabel,
} from "../lib/presets";
import { useSession } from "../store/session";

/**
 * Empty-state landing screen (claude.ai-style). Shown when `current` is null.
 * Large centered composer card with model pickers and quick-seed chips below.
 */
export function StartView(): JSX.Element {
  // Use the store's `start` so it records a Recents entry before the WS send.
  const startAudit = useSession((s) => s.start);

  const [seed, setSeed] = useState("");
  const [auditorModel, setAuditorModel] = useState(DEFAULT_AUDITOR);
  const [targetModel, setTargetModel] = useState(DEFAULT_TARGET);
  const [maxTurns, setMaxTurns] = useState(6);

  const canStart = seed.trim().length > 0;

  function handleStart(): void {
    if (!canStart) return;
    startAudit({
      seed: seed.trim(),
      auditor_model: auditorModel,
      target_model: targetModel,
      max_turns: maxTurns,
    });
  }

  return (
    <div className="start-view">
      <h1 className="start-heading">
        <span className="accent">✻</span> Start an audit
      </h1>

      <div className="start-card">
        <textarea
          className="start-textarea"
          rows={4}
          placeholder="Describe the scenario the auditor should set up…"
          value={seed}
          onChange={(e) => setSeed(e.target.value)}
          onKeyDown={(e) => {
            // Cmd/Ctrl-Enter submits
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") handleStart();
          }}
        />

        <div className="start-card-lower">
          {/* left stub — file attach / future actions */}
          <button className="start-attach" title="Attach (coming soon)" disabled>
            +
          </button>

          <div className="start-pickers">
            {/* auditor model chip */}
            <label className="model-chip">
              <span className="chip-label">auditor</span>
              <select
                value={auditorModel}
                onChange={(e) => setAuditorModel(e.target.value)}
              >
                {MODELS.map((m) => (
                  <option key={m} value={m}>
                    {modelLabel(m)}
                  </option>
                ))}
              </select>
              <span className="chip-caret">⌄</span>
            </label>

            {/* target model chip */}
            <label className="model-chip">
              <span className="chip-label">target</span>
              <select
                value={targetModel}
                onChange={(e) => setTargetModel(e.target.value)}
              >
                {MODELS.map((m) => (
                  <option key={m} value={m}>
                    {modelLabel(m)}
                  </option>
                ))}
              </select>
              <span className="chip-caret">⌄</span>
            </label>

            {/* max_turns numeric input */}
            <label className="turns-chip">
              <span className="chip-label">turns</span>
              <input
                type="number"
                min={1}
                max={30}
                value={maxTurns}
                onChange={(e) => setMaxTurns(Math.max(1, Number(e.target.value)))}
                className="turns-input"
              />
            </label>
          </div>

          {/* circular accent Start button */}
          <button
            className="start-send"
            title="Start audit"
            disabled={!canStart}
            onClick={handleStart}
          >
            ↑
          </button>
        </div>
      </div>

      {/* quick-seed chips */}
      <div className="seed-chips">
        {SEED_PRESETS.map((p) => (
          <button
            key={p.label}
            className="seed-chip"
            onClick={() => setSeed(p.seed)}
            title={p.seed}
          >
            <span className="seed-icon">{p.icon}</span>
            {p.label}
          </button>
        ))}
      </div>
    </div>
  );
}
