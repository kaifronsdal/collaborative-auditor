import type { JSX } from "react";
import { useState } from "react";

import {
  MODELS,
  SEED_PRESETS,
  modelLabel,
} from "../lib/presets";
import { useSession } from "../store/session";

/**
 * Empty-state landing screen. Form-first: seed textarea is the dominant element,
 * controls live in one row below it, preset chips above.
 */
export function StartView(): JSX.Element {
  const startAudit = useSession((s) => s.start);
  const setNextConfig = useSession((s) => s.setNextConfig);

  const [seed, setSeed] = useState("");
  const [auditorModel, setAuditorModel] = useState(() => useSession.getState().nextConfig.auditor_model);
  const [targetModel, setTargetModel] = useState(() => useSession.getState().nextConfig.target_model);
  const [maxTurns, setMaxTurns] = useState(() => useSession.getState().nextConfig.max_turns);

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
      <div className="start-card">
        <div className="start-card-label">New audit</div>

        {/* preset chips above the textarea */}
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

        <textarea
          className="start-textarea"
          rows={6}
          placeholder="Describe the scenario the auditor should set up…"
          value={seed}
          onChange={(e) => setSeed(e.target.value)}
        />

        {/* one control row: auditor · target · turns · Start */}
        <div className="start-controls">
          <label className="model-chip">
            <span className="chip-label">auditor</span>
            <select
              value={auditorModel}
              onChange={(e) => { setAuditorModel(e.target.value); setNextConfig({ auditor_model: e.target.value }); }}
            >
              {MODELS.map((m) => (
                <option key={m} value={m}>
                  {modelLabel(m)}
                </option>
              ))}
            </select>
            <span className="chip-caret">⌄</span>
          </label>

          <span className="controls-sep">·</span>

          <label className="model-chip">
            <span className="chip-label">target</span>
            <select
              value={targetModel}
              onChange={(e) => { setTargetModel(e.target.value); setNextConfig({ target_model: e.target.value }); }}
            >
              {MODELS.map((m) => (
                <option key={m} value={m}>
                  {modelLabel(m)}
                </option>
              ))}
            </select>
            <span className="chip-caret">⌄</span>
          </label>

          <span className="controls-sep">·</span>

          <label className="turns-chip">
            <span className="chip-label">turns</span>
            <input
              type="number"
              min={1}
              max={30}
              value={maxTurns}
              onChange={(e) => { const v = Math.max(1, Number(e.target.value)); setMaxTurns(v); setNextConfig({ max_turns: v }); }}
              className="turns-input"
            />
          </label>

          <button
            className="start-btn"
            disabled={!canStart}
            onClick={handleStart}
          >
            Start →
          </button>
        </div>
      </div>
    </div>
  );
}
