import type { JSX } from "react";
import { useEffect, useRef, useState } from "react";

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
  // Read nextConfig from the store so sidebar picker changes propagate here.
  const nextConfig = useSession((s) => s.nextConfig);

  const [seed, setSeed] = useState("");
  const [auditorModel, setAuditorModel] = useState(() => nextConfig.auditor_model);
  const [targetModel, setTargetModel] = useState(() => nextConfig.target_model);
  const [auditorConfig, setAuditorConfig] = useState<Partial<GenerateConfigDict>>(
    () => readStoredConfig("auditor"),
  );
  const [targetConfig, setTargetConfig] = useState<Partial<GenerateConfigDict>>(
    () => readStoredConfig("target"),
  );

  // Track previous nextConfig to detect external updates (e.g. sidebar picker).
  const prevNextConfigRef = useRef(nextConfig);
  useEffect(() => {
    const prev = prevNextConfigRef.current;
    if (nextConfig !== prev) {
      prevNextConfigRef.current = nextConfig;
      if (nextConfig.auditor_model !== prev.auditor_model) {
        setAuditorModel(nextConfig.auditor_model);
      }
      if (nextConfig.target_model !== prev.target_model) {
        setTargetModel(nextConfig.target_model);
      }
      if (nextConfig.auditor_config !== prev.auditor_config) {
        setAuditorConfig(nextConfig.auditor_config);
      }
      if (nextConfig.target_config !== prev.target_config) {
        setTargetConfig(nextConfig.target_config);
      }
    }
  }, [nextConfig]);

  // Guard against double-click: prevent sending two `start` messages.
  const [isStarting, setIsStarting] = useState(false);

  const canStart = seed.trim().length > 0 && !isStarting;

  function handleStart(): void {
    if (!canStart) return;
    setIsStarting(true);
    startAudit({
      seed: seed.trim(),
      auditor_model: auditorModel,
      target_model: targetModel,
      auditor_config: auditorConfig,
      target_config: targetConfig,
    });
    // The component will unmount as soon as `current` is set by the backend
    // state broadcast, so we don't need to reset isStarting.
  }

  return (
    <div className="start-view">
      <div className="start-card">
        {/* preset chips above the textarea */}
        <div className="seed-chips">
          {SEED_PRESETS.map((p) => (
            <button
              key={p.label}
              className="seed-chip"
              onClick={() => setSeed(p.seed)}
              title={p.seed}
            >
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
            Start
          </button>
        </div>
      </div>
    </div>
  );
}
