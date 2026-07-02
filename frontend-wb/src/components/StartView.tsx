import type { JSX } from "react";
import { useEffect, useRef, useState } from "react";

import { DEFAULT_AUDITOR, MODELS, SEED_PRESETS, modelLabel } from "../lib/presets";
import { useSession } from "../store/session";
import { ModelPicker, readStoredConfig } from "./ModelPicker";
import type { GenerateConfigDict } from "./ModelPicker";

/**
 * Empty-state landing screen. Two tabs: the M0 "Desk" seed form (unchanged)
 * and the M1 "Orchestrator" launcher (model dropdown → `start_orchestrator`).
 */
export function StartView(): JSX.Element {
  const [tab, setTab] = useState<"desk" | "orch">("desk");
  return (
    <div className="start-view">
      <div className="start-tabs">
        <button
          type="button"
          className={`start-tab${tab === "desk" ? " on" : ""}`}
          onClick={() => setTab("desk")}
        >
          Collaborative Auditor
          <span className="start-tab-sub">
            you and a model co-write probes, turn by turn
          </span>
        </button>
        <button
          type="button"
          className={`start-tab${tab === "orch" ? " on" : ""}`}
          onClick={() => setTab("orch")}
        >
          Orchestrator
          <span className="start-tab-sub">
            an agent runs audits, analyzes results, and drafts findings for you
          </span>
        </button>
      </div>
      {tab === "desk" ? <DeskStartCard /> : <OrchStartCard />}
    </div>
  );
}

/** M1 orchestrator launcher — system-prompt override + model dropdown + Start. */
function OrchStartCard(): JSX.Element {
  const send = useSession((s) => s.send);
  const [model, setModel] = useState(DEFAULT_AUDITOR);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [isStarting, setIsStarting] = useState(false);

  function handleStart(): void {
    if (isStarting) return;
    setIsStarting(true);
    // Clear the new-audit shield so the incoming `state` broadcast (which
    // carries `orchestrator != null`) flips App into DeskView.
    useSession.setState({ pendingNewAudit: false });
    send({
      t: "start_orchestrator",
      model,
      ...(systemPrompt.trim() ? { system_prompt: systemPrompt.trim() } : {}),
    });
  }

  return (
    <div className="start-card">
      <textarea
        className="start-textarea"
        rows={4}
        placeholder="Optional: override the orchestrator's system prompt…"
        value={systemPrompt}
        onChange={(e) => setSystemPrompt(e.target.value)}
      />
      <div className="start-controls">
        {/* The tab already says "Orchestrator" — the role chip was redundant. */}
        <label className="mp-field">
          <span className="mp-role">model</span>
          <select
            className="mp-select"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          >
            {MODELS.map((m) => (
              <option key={m} value={m}>
                {modelLabel(m)}
              </option>
            ))}
          </select>
        </label>
        <button className="start-btn" disabled={isStarting} onClick={handleStart}>
          Start
        </button>
      </div>
    </div>
  );
}

/** The original M0 seed → auditor/target form. */
function DeskStartCard(): JSX.Element {
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
  );
}
