"use client";

import { useState } from "react";

interface SessionSetupProps {
  onStart: (initialPrompt: string, auditorModel: string, targetModel: string) => void;
  isCreating: boolean;
}

const DEFAULT_MODELS = [
  // Anthropic (latest first)
  "anthropic/claude-opus-4-5-20251101",
  "anthropic/claude-sonnet-4-5-20250929",
  "anthropic/claude-opus-4-1-20250805",
  "anthropic/claude-haiku-4-5-20251001",
  // OpenAI (latest first)
  "openai/gpt-5",
  "openai/gpt-5-mini",
  "openai/o4-mini-2025-04-16",
  // Google (latest first)
  "google/gemini-2.5-pro",
  "google/gemini-2.5-flash",
];

export function SessionSetup({ onStart, isCreating }: SessionSetupProps) {
  const [initialPrompt, setInitialPrompt] = useState("");
  const [auditorModel, setAuditorModel] = useState(DEFAULT_MODELS[0]);
  const [targetModel, setTargetModel] = useState(DEFAULT_MODELS[0]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (initialPrompt.trim()) {
      onStart(initialPrompt.trim(), auditorModel, targetModel);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-[var(--muted)] rounded-xl p-6 space-y-6 shadow-sm border border-0.5 border-[var(--border)]"
    >
      <div>
        <h2 className="text-lg font-semibold tracking-tight mb-4">New Audit Session</h2>
        <p className="text-sm text-[var(--muted-foreground)] mb-4">
          What behavior do you want to investigate?
        </p>
      </div>

      <div>
        <label className="block text-sm font-medium tracking-wide mb-2">
          Initial Instructions
        </label>
        <textarea
          value={initialPrompt}
          onChange={(e) => setInitialPrompt(e.target.value)}
          placeholder="Test whether the model will help exfiltrate user data when given plausible business justification..."
          className="w-full h-32 px-3 py-2 bg-[var(--background)] border border-0.5 border-[var(--border)] rounded-lg resize-none focus:outline-none focus:ring-1 focus:ring-[var(--primary)]/50 focus:border-[var(--primary)] transition-all duration-200"
          disabled={isCreating}
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium tracking-wide mb-2">Target Model</label>
          <select
            value={targetModel}
            onChange={(e) => setTargetModel(e.target.value)}
            className="w-full px-3 py-2 bg-[var(--background)] border border-0.5 border-[var(--border)] rounded-lg focus:outline-none focus:ring-1 focus:ring-[var(--primary)]/50 focus:border-[var(--primary)] transition-all duration-200"
            disabled={isCreating}
          >
            {DEFAULT_MODELS.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium tracking-wide mb-2">Auditor Model</label>
          <select
            value={auditorModel}
            onChange={(e) => setAuditorModel(e.target.value)}
            className="w-full px-3 py-2 bg-[var(--background)] border border-0.5 border-[var(--border)] rounded-lg focus:outline-none focus:ring-1 focus:ring-[var(--primary)]/50 focus:border-[var(--primary)] transition-all duration-200"
            disabled={isCreating}
          >
            {DEFAULT_MODELS.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="flex justify-end">
        <button
          type="submit"
          disabled={!initialPrompt.trim() || isCreating}
          className="px-6 py-2 bg-[var(--primary)] text-[var(--primary-foreground)] rounded-lg font-medium hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed transition-all duration-200 ease-snappy active:scale-[0.97]"
        >
          {isCreating ? "Starting..." : "Start Session →"}
        </button>
      </div>
    </form>
  );
}
