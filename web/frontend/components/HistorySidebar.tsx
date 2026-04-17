"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchSessions } from "@/lib/api";
import type { SessionListItem } from "@/lib/types";

interface Props {
  activeSessionId?: string;
  onSelectSession: (id: string) => void;
  onNewChat: () => void;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

function feedbackBadge(status: string) {
  if (status === "accepted")
    return <span className="ml-auto shrink-0 rounded bg-emerald-900/60 px-1.5 py-0.5 text-[9px] font-semibold text-emerald-300">Accepted</span>;
  if (status === "rejected")
    return <span className="ml-auto shrink-0 rounded bg-red-900/60 px-1.5 py-0.5 text-[9px] font-semibold text-red-300">Rejected</span>;
  return null;
}

export default function HistorySidebar({ activeSessionId, onSelectSession, onNewChat }: Props) {
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [loading, setLoading] = useState(true);

  const loadSessions = useCallback(async () => {
    try {
      const data = await fetchSessions();
      setSessions(data);
    } catch {
      /* non-critical */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const grouped = useMemo(() => {
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterdayStart = new Date(todayStart.getTime() - 86_400_000);

    const groups: { label: string; items: SessionListItem[] }[] = [
      { label: "Today", items: [] },
      { label: "Yesterday", items: [] },
      { label: "Older", items: [] },
    ];

    for (const s of sessions) {
      const ts = new Date(s.created_at);
      if (ts >= todayStart) groups[0].items.push(s);
      else if (ts >= yesterdayStart) groups[1].items.push(s);
      else groups[2].items.push(s);
    }

    return groups.filter((g) => g.items.length > 0);
  }, [sessions]);

  return (
    <div className="flex h-full w-[250px] shrink-0 flex-col border-r border-zinc-800 bg-zinc-950">
      <div className="border-b border-zinc-800 p-3">
        <button
          type="button"
          onClick={onNewChat}
          className="w-full rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs font-medium text-zinc-300 transition-colors hover:border-zinc-600 hover:bg-zinc-800 hover:text-zinc-100"
        >
          + New Chat
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {loading && (
          <div className="p-4 text-center text-xs text-zinc-600">Loading sessions...</div>
        )}

        {!loading && sessions.length === 0 && (
          <div className="p-4 text-center text-xs text-zinc-600">No sessions yet.</div>
        )}

        {grouped.map((group) => (
          <div key={group.label}>
            <div className="sticky top-0 bg-zinc-950/90 px-3 py-2 backdrop-blur-sm">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                {group.label}
              </p>
            </div>
            {group.items.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => onSelectSession(s.id)}
                className={`flex w-full items-start gap-2 px-3 py-2.5 text-left transition-colors ${
                  activeSessionId === s.id
                    ? "bg-zinc-800/80 text-zinc-100"
                    : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                }`}
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-xs font-medium">{s.job_full_name}</p>
                  <p className="mt-0.5 truncate text-[10px] text-zinc-500">
                    #{s.build_number} &middot; {s.stage_name || "unknown"} &middot; {formatTime(s.created_at)}
                  </p>
                </div>
                {feedbackBadge(s.feedback_status)}
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
