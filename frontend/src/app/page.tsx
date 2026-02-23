"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { v4 as uuidv4 } from "uuid";
import { SessionSetup } from "@/components/SessionSetup";

export default function HomePage() {
  const router = useRouter();
  const [isCreating, setIsCreating] = useState(false);

  const handleStartSession = async (
    initialPrompt: string,
    auditorModel: string,
    targetModel: string
  ) => {
    setIsCreating(true);
    // Generate a session ID and navigate to the session page
    const sessionId = uuidv4();
    // Store session params in URL or we could use session storage
    const params = new URLSearchParams({
      prompt: initialPrompt,
      auditor: auditorModel,
      target: targetModel,
    });
    router.push(`/session?id=${sessionId}&${params.toString()}`);
  };

  return (
    <main className="min-h-screen flex items-center justify-center p-4">
      <div className="w-full max-w-2xl">
        <h1 className="text-3xl font-semibold tracking-tight text-center mb-2">
          Collaborative Auditor
        </h1>
        <p className="text-center text-sm text-[var(--muted-foreground)] mb-8">AI safety research audit interface</p>
        <SessionSetup onStart={handleStartSession} isCreating={isCreating} />
      </div>
    </main>
  );
}
