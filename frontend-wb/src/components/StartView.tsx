import type { JSX } from "react";
import { useState } from "react";

import { SEED_PRESETS } from "../lib/presets";
import { useSession } from "../store/session";
import { ModelPicker, readStoredConfig } from "./ModelPicker";
import type { GenerateConfigDict } from "./ModelPicker";

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
  const [auditorConfig, setAuditorConfig] = useState<Partial<GenerateConfigDict>>(
    () => readStoredConfig("auditor"),
  );
  const [targetConfig, setTargetConfig] = useState<Partial<GenerateConfigDict>>(
    () => readStoredConfig("target"),
  );

  const canStart = seed.trim().length > 0;

  function handleStart(): void {
    if (!canStart) return;
    startAudit({
      seed: seed.trim(),
      auditor_model: auditorModel,
      target_model: targetModel,
      auditor_config: auditorConfig,
      target_config: targetConfig,
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

        {/* one control row: auditor · target · Start */}
        <div className="start-controls">
          <ModelPicker
            role="auditor"
            value={auditorModel}
            config={auditorConfig}
            onChange={(m, cfg) => {
              setAuditorModel(m);
              setAuditorConfig(cfg);
              setNextConfig({ auditor_model: m, auditor_config: cfg });
            }}
          />

          <span className="controls-sep">·</span>

          <ModelPicker
            role="target"
            value={targetModel}
            config={targetConfig}
            onChange={(m, cfg) => {
              setTargetModel(m);
              setTargetConfig(cfg);
              setNextConfig({ target_model: m, target_config: cfg });
            }}
          />

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
