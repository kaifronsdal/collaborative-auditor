/**
 * P1.7 — global settings panel.
 *
 * A `<Modal>` over the `Settings` dataclass in `workbench/config.py`.
 * Fetches `GET /settings` on open, edits a local draft, `PATCH /settings`
 * on save. Model defaults use `<ModelPicker>` (all four roles); numeric
 * caps use `<input type=number>` styled per `.picker-cfg-*`. Nothing here
 * touches the running orchestrator — thresholds apply to the *next*
 * `start_orchestrator` (the prompt is rendered once at start).
 */
import type { JSX } from "react";
import { useEffect, useState } from "react";
import { Modal } from "@tsmono/react/components/Modal";

import { ModelPicker, type PickerRole } from "./ModelPicker";

/** Mirror of ``workbench.config.Settings`` (flat, ``extra`` inlined). */
export type Settings = {
  default_orchestrator: string;
  default_auditor: string;
  default_target: string;
  default_judge: string;
  seed_review_cost_threshold: number;
  seed_review_count_threshold: number;
  auto_approve_under_threshold: boolean;
  bash_timeout: number;
  model_text_cap: number;
  read_file_limit: number;
  attach_grace: number;
  [extra: string]: unknown;
};

type NumField =
  | "seed_review_cost_threshold"
  | "seed_review_count_threshold"
  | "bash_timeout"
  | "model_text_cap"
  | "read_file_limit"
  | "attach_grace";

const MODEL_ROLES: { key: keyof Settings; role: PickerRole }[] = [
  { key: "default_orchestrator", role: "orchestrator" },
  { key: "default_auditor", role: "auditor" },
  { key: "default_target", role: "target" },
  { key: "default_judge", role: "judge" },
];

const NUM_FIELDS: {
  key: NumField; label: string; hint: string; step?: number; min?: number;
}[] = [
  { key: "seed_review_cost_threshold", label: "seed-review $ threshold",
    hint: "Runs estimated over this cost require review_seeds approval.",
    step: 0.5, min: 0 },
  { key: "seed_review_count_threshold", label: "seed-review sample threshold",
    hint: "Runs with more samples than this require review_seeds approval.",
    min: 1 },
  { key: "bash_timeout", label: "bash timeout (s)",
    hint: "Default timeout for the orchestrator's bash tool.", min: 1 },
  { key: "model_text_cap", label: "model text cap (chars)",
    hint: "Tool-result truncation cap fed back to the orchestrator model.",
    min: 200 },
  { key: "read_file_limit", label: "read_file limit (lines)",
    hint: "Default line limit for the read_file tool.", min: 1 },
  { key: "attach_grace", label: "attach grace (s)",
    hint: "How long wb.attach waits for a settled .eval after the subprocess exits.",
    step: 0.5, min: 0 },
];

export function SettingsModal({ onClose }: { onClose: () => void }): JSX.Element {
  const [draft, setDraft] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    fetch("/settings")
      .then((r) => r.json())
      .then((s: Settings) => { if (live) setDraft(s); })
      .catch((e) => { if (live) setError(String(e)); });
    return () => { live = false; };
  }, []);

  function patch<K extends keyof Settings>(key: K, value: Settings[K]): void {
    setDraft((d) => (d ? { ...d, [key]: value } : d));
  }

  async function save(): Promise<void> {
    if (!draft) return;
    setSaving(true);
    setError(null);
    try {
      const r = await fetch("/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      onClose();
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      show
      onHide={onClose}
      title="Settings"
      width="min(640px, 92vw)"
      bodyClassName="settings-modal"
    >
      {draft === null ? (
        <div className="settings-loading">{error ?? "loading…"}</div>
      ) : (
        <>
          <div className="settings-section">Default models</div>
          <div className="settings-models">
            {MODEL_ROLES.map(({ key, role }) => (
              <ModelPicker
                key={role}
                role={role}
                value={draft[key] as string}
                config={{}}
                onChange={(v) => patch(key, v)}
              />
            ))}
          </div>

          <div className="settings-section">Seed review</div>
          {NUM_FIELDS.slice(0, 2).map((f) => (
            <NumRow key={f.key} field={f} draft={draft} patch={patch} />
          ))}
          <label className="settings-row" title="When enabled, the prompt tells the orchestrator to launch runs under the thresholds without a review_seeds gate.">
            <span className="settings-label">auto-approve under threshold</span>
            <input
              type="checkbox"
              checked={draft.auto_approve_under_threshold}
              onChange={(e) => patch("auto_approve_under_threshold", e.target.checked)}
            />
          </label>

          <div className="settings-section">Tool caps</div>
          {NUM_FIELDS.slice(2).map((f) => (
            <NumRow key={f.key} field={f} draft={draft} patch={patch} />
          ))}

          {error && <div className="settings-error">{error}</div>}
          <div className="settings-actions">
            <button type="button" className="gate-btn ghost" onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className="gate-btn primary"
              disabled={saving}
              onClick={() => void save()}
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

function NumRow({
  field, draft, patch,
}: {
  field: { key: NumField; label: string; hint: string; step?: number; min?: number };
  draft: Settings;
  patch: <K extends keyof Settings>(key: K, value: Settings[K]) => void;
}): JSX.Element {
  const isFloat = field.step !== undefined && field.step < 1;
  return (
    <label className="settings-row" title={field.hint}>
      <span className="settings-label">{field.label}</span>
      <input
        type="number"
        className="picker-cfg-number settings-num"
        min={field.min}
        step={field.step ?? 1}
        value={draft[field.key]}
        onChange={(e) => {
          const v = e.target.value;
          patch(field.key, v === "" ? 0 : isFloat ? parseFloat(v) : parseInt(v, 10));
        }}
      />
    </label>
  );
}
