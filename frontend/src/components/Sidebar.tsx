import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useSessionStore, useSessionList } from "@/store/session";
import type { SessionSummary } from "@/lib/types";

function formatRelativeTime(isoString: string): string {
  const date = new Date(isoString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  return date.toLocaleDateString();
}

function formatModelShort(model: string): string {
  const name = model.split("/").pop() ?? model;
  if (name.length <= 18) return name;
  return name.slice(0, 16) + "...";
}

function SessionListItem({
  session,
  isActive,
  onSelect,
  onDelete,
}: {
  session: SessionSummary;
  isActive: boolean;
  onSelect: () => void;
  onDelete: () => void;
}) {
  const prompt = session.initial_prompt || "(no prompt)";
  const truncated = prompt.length > 60 ? prompt.slice(0, 57) + "..." : prompt;

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`group/item w-full text-left px-3 py-2.5 rounded-lg transition-colors duration-150 ${
        isActive
          ? "bg-[var(--muted)]"
          : "hover:bg-[var(--muted)]/60"
      }`}
    >
      <div className="flex items-start justify-between gap-1">
        <div className="min-w-0 flex-1">
          <div className="text-sm truncate text-[var(--foreground)]">
            {truncated}
          </div>
          <div className="flex items-center gap-1 mt-0.5">
            <span className="text-[10px] text-[var(--muted-foreground)] truncate">
              {formatModelShort(session.auditor_model)} &rarr; {formatModelShort(session.target_model)}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <span className="text-[10px] text-[var(--muted-foreground)] group-hover/item:hidden">
            {formatRelativeTime(session.updated_at)}
          </span>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            className="hidden group-hover/item:flex w-5 h-5 items-center justify-center text-[var(--muted-foreground)] hover:text-red-400 rounded transition-colors"
            title="Delete audit"
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <path d="M3 4h10M6 4V3a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v1m2 0v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4h8Z" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
        </div>
      </div>
    </button>
  );
}

export function Sidebar() {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeSessionId = searchParams.get("session");
  const sessionList = useSessionList();
  const fetchSessionList = useSessionStore((s) => s.fetchSessionList);
  const deleteSessionFromList = useSessionStore((s) => s.deleteSessionFromList);
  const [search, setSearch] = useState("");

  useEffect(() => {
    fetchSessionList();
  }, [fetchSessionList]);

  const filtered = search.trim()
    ? sessionList.filter((s) =>
        s.initial_prompt.toLowerCase().includes(search.toLowerCase())
      )
    : sessionList;

  const handleSelect = (id: string) => {
    setSearchParams({ session: id });
  };

  const handleNewAudit = () => {
    setSearchParams({});
  };

  const handleDelete = async (id: string) => {
    await deleteSessionFromList(id);
    if (activeSessionId === id) {
      setSearchParams({});
    }
  };

  return (
    <aside className="w-[260px] shrink-0 h-screen flex flex-col border-r border-0.5 border-[var(--border)] bg-[var(--background)]">
      {/* Header */}
      <div className="p-3 space-y-2">
        <button
          type="button"
          onClick={handleNewAudit}
          className="w-full flex items-center gap-2 px-3 py-2 text-sm font-medium rounded-lg border border-0.5 border-[var(--border)] hover:bg-[var(--muted)] transition-colors duration-150"
        >
          <svg className="w-4 h-4" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M8 3v10M3 8h10" strokeLinecap="round" />
          </svg>
          New audit
        </button>
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search..."
          className="w-full px-3 py-1.5 text-xs bg-[var(--muted)] border border-0.5 border-[var(--border)] rounded-lg focus:outline-none focus:border-[var(--primary)] transition-colors placeholder:text-[var(--muted-foreground)]/60"
        />
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto px-2 pb-3">
        {filtered.length === 0 ? (
          <div className="text-xs text-[var(--muted-foreground)] text-center py-8">
            {search.trim() ? "No matching audits" : "No audits yet"}
          </div>
        ) : (
          <div className="space-y-0.5">
            {filtered.map((session) => (
              <SessionListItem
                key={session.id}
                session={session}
                isActive={session.id === activeSessionId}
                onSelect={() => handleSelect(session.id)}
                onDelete={() => handleDelete(session.id)}
              />
            ))}
          </div>
        )}
      </div>
    </aside>
  );
}
