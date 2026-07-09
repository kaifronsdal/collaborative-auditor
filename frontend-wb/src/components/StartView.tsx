import type { JSX } from "react";
import { useEffect, useRef, useState } from "react";

import {
  DEFAULT_AUDITOR,
  DEFAULT_JUDGE,
  DEFAULT_ORCHESTRATOR,
  DEFAULT_TARGET,
  SEED_PRESETS,
} from "../lib/presets";
import { useIsPending } from "../lib/selectors";
import type { AuditDefaults } from "../lib/wire";
import { useSession } from "../store/session";
import { ModelPicker, readStoredConfig } from "./ModelPicker";
import type { GenerateConfigDict, ModelArgs } from "./ModelPicker";
import { Chevron } from "./icons";

/**
 * Empty-state landing screen. Mode selection lives in the sidebar's MODES
 * section; this just renders whichever card `store.mode` points at.
 */
export function StartView(): JSX.Element {
  const mode = useSession((s) => s.mode);
  return (
    <div className="start-view">
      {mode === "desk" ? <DeskStartCard /> : <OrchStartCard />}
    </div>
  );
}

/** One `ModelPicker`'s state bundle — model id + open `GenerateConfig` +
 *  provider `model_args`. Collapses the six local `useState`s per picker
 *  into one; `set` is used as `setX((p) => ({...p, ...}))`. */
type PickerState = { model: string; config: GenerateConfigDict; args: ModelArgs };

/** `initial` may change once (`settings` fetch resolves after mount). If the
 *  picker is still at the previous default — user hasn't touched it — adopt
 *  the new one. */
function usePicker(role: string, initial: string): [PickerState, (p: PickerState) => void] {
  const [s, set] = useState<PickerState>(() => ({
    model: initial,
    config: readStoredConfig(role),
    args: {},
  }));
  const prevInitial = useRef(initial);
  useEffect(() => {
    if (initial !== prevInitial.current) {
      const was = prevInitial.current;
      prevInitial.current = initial;
      set((p) => (p.model === was ? { ...p, model: initial } : p));
    }
  }, [initial]);
  return [s, set];
}

/** Drop empty-object values so the wire payload stays compact. */
function nonEmpty<T extends object>(v: T): T | undefined {
  return Object.keys(v).length > 0 ? v : undefined;
}

/** M1 orchestrator launcher (P1.1 + P1.2 + P1.4): a full `ModelPicker` for
 *  the orchestrator model (with `GenerateConfig` + `model_args`), an
 *  expandable "Audit defaults" section with three more pickers for the
 *  subprocess audit roles + `max_turns`/`judge_dimensions`, and the optional
 *  system-prompt override. */
function OrchStartCard(): JSX.Element {
  const send = useSession((s) => s.send);
  const settings = useSession((s) => s.settings);
  const [orch, setOrch] = usePicker(
    "orchestrator", settings?.default_orchestrator ?? DEFAULT_ORCHESTRATOR,
  );
  const [target, setTarget] = usePicker(
    "target", settings?.default_target ?? DEFAULT_TARGET,
  );
  const [auditor, setAuditor] = usePicker(
    "auditor", settings?.default_auditor ?? DEFAULT_AUDITOR,
  );
  const [judge, setJudge] = usePicker(
    "judge", settings?.default_judge ?? DEFAULT_JUDGE,
  );
  const [maxTurns, setMaxTurns] = useState<number>(30);
  const [judgeDims, setJudgeDims] = useState("");
  const [defaultsOpen, setDefaultsOpen] = useState(false);
  const [systemPrompt, setSystemPrompt] = useState("");
  // A2: replaces the R1 local `isStarting` flag. Either card's launch is
  // in-flight → both disable (they share one Session).
  const isStarting = useIsPending(
    (c) => c.t === "start" || c.t === "start_orchestrator"
  );

  function handleStart(): void {
    if (isStarting) return;
    // Clear the new-audit shield so the incoming `state` broadcast (which
    // carries `orchestrator != null`) flips App into DeskView.
    useSession.setState({ pendingNewAudit: false });
    const audit_defaults: AuditDefaults = {
      target: target.model,
      auditor: auditor.model,
      judge: judge.model,
      target_config: nonEmpty(target.config),
      auditor_config: nonEmpty(auditor.config),
      judge_config: nonEmpty(judge.config),
      max_turns: maxTurns,
      ...(judgeDims.trim() ? { judge_dimensions: judgeDims.trim() } : {}),
    };
    send({
      t: "start_orchestrator",
      model: orch.model,
      config: nonEmpty(orch.config),
      model_args: nonEmpty(orch.args),
      audit_defaults,
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
        <ModelPicker
          role="orchestrator"
          value={orch.model}
          config={orch.config}
          modelArgs={orch.args}
          onChange={(m, cfg, args) => setOrch({ model: m, config: cfg, args })}
        />
        <button className="start-btn" disabled={isStarting} onClick={handleStart}>
          Start
        </button>
      </div>

      {/* P1.2 — audit-role defaults. Interpolated into the system prompt and
          exposed as `wb.DEFAULTS` in the kernel. */}
      <div className="start-defaults">
        <button
          className="picker-config-toggle"
          onClick={() => setDefaultsOpen((v) => !v)}
          type="button"
        >
          <Chevron open={defaultsOpen} size={10} className="picker-config-arrow" />
          {" Audit defaults"}
        </button>
        {defaultsOpen && (
          <div className="start-defaults-body">
            <div className="start-defaults-pickers">
              <ModelPicker
                role="target"
                value={target.model}
                config={target.config}
                modelArgs={target.args}
                onChange={(m, cfg, args) => setTarget({ model: m, config: cfg, args })}
              />
              <ModelPicker
                role="auditor"
                value={auditor.model}
                config={auditor.config}
                modelArgs={auditor.args}
                onChange={(m, cfg, args) => setAuditor({ model: m, config: cfg, args })}
              />
              <ModelPicker
                role="judge"
                value={judge.model}
                config={judge.config}
                modelArgs={judge.args}
                onChange={(m, cfg, args) => setJudge({ model: m, config: cfg, args })}
              />
            </div>
            <div className="picker-cfg-row">
              <span className="picker-cfg-label">max_turns</span>
              <input
                type="number"
                className="picker-cfg-number"
                min={1}
                value={maxTurns}
                onChange={(e) => setMaxTurns(parseInt(e.target.value, 10) || 1)}
              />
            </div>
            <div className="picker-cfg-row">
              <span className="picker-cfg-label">judge_dimensions</span>
              <input
                type="text"
                className="picker-cfg-text"
                style={{ width: "100%" }}
                placeholder="(default)"
                value={judgeDims}
                onChange={(e) => setJudgeDims(e.target.value)}
              />
            </div>
          </div>
        )}
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
  const [auditorConfig, setAuditorConfig] = useState<GenerateConfigDict>(
    () => readStoredConfig("auditor"),
  );
  const [targetConfig, setTargetConfig] = useState<GenerateConfigDict>(
    () => readStoredConfig("target"),
  );
  const [auditorArgs, setAuditorArgs] = useState<ModelArgs>({});
  const [targetArgs, setTargetArgs] = useState<ModelArgs>({});
  // P1.8(c) — comma-separated scanner/group names to fire on each target
  // reply. Autocomplete via `GET /scanners` is a follow-up; plain text for now.
  const [liveScannersRaw, setLiveScannersRaw] = useState("");

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

  // A2: replaces the R1 local `isStarting` flag.
  const isStarting = useIsPending(
    (c) => c.t === "start" || c.t === "start_orchestrator"
  );

  const canStart = seed.trim().length > 0 && !isStarting;

  function handleStart(): void {
    if (!canStart) return;
    const live_scanners = liveScannersRaw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    startAudit({
      seed: seed.trim(),
      auditor_model: auditorModel,
      target_model: targetModel,
      auditor_config: auditorConfig,
      target_config: targetConfig,
      auditor_model_args: nonEmpty(auditorArgs),
      target_model_args: nonEmpty(targetArgs),
      ...(live_scanners.length > 0 ? { live_scanners } : {}),
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
            modelArgs={auditorArgs}
            onChange={(m, cfg, args) => {
              setAuditorModel(m);
              setAuditorConfig(cfg);
              setAuditorArgs(args);
              setNextConfig({ auditor_model: m, auditor_config: cfg });
            }}
          />

          <ModelPicker
            role="target"
            value={targetModel}
            config={targetConfig}
            modelArgs={targetArgs}
            onChange={(m, cfg, args) => {
              setTargetModel(m);
              setTargetConfig(cfg);
              setTargetArgs(args);
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

        {/* P1.8(c) — live per-turn scanners. */}
        <div className="picker-cfg-row start-live-scanners">
          <span className="picker-cfg-label">live scanners</span>
          <input
            type="text"
            className="picker-cfg-number"
            style={{ width: "100%" }}
            placeholder="scanner or group names, comma-separated (optional)"
            value={liveScannersRaw}
            onChange={(e) => setLiveScannersRaw(e.target.value)}
          />
        </div>
      </div>
  );
}
