"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchResults } from "@/lib/api";
import type { ProcessedStage } from "@/lib/types";
import ChatPanel from "./ChatPanel";
import FailureCard from "./FailureCard";
import ThemeToggle from "./ui/theme-toggle";
import { Button } from "./ui/button";

// Backend caps /api/results at 200 (le=200).
const RESULTS_LIMIT = 200;

export default function RcaView() {
  const searchParams = useSearchParams();
  const runId = searchParams.get("run") ?? "";

  const [results, setResults] = useState<ProcessedStage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [chatOpen, setChatOpen] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchResults(RESULTS_LIMIT);
      setResults(data.results);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load analysis");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const stage = useMemo(
    () => results.find((s) => s.run_id === runId) ?? null,
    [results, runId],
  );

  // "Similar past clusters" derived from the full corpus, excluding the
  // currently-viewed stage's own job/stage so it surfaces related noise.
  const topNoisy = useMemo(() => {
    const byKey = new Map<string, number>();
    for (const row of results) {
      const key = `${row.job_full_name} / ${row.stage_name || "unknown-stage"}`;
      byKey.set(key, (byKey.get(key) || 0) + 1);
    }
    return Array.from(byKey.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
  }, [results]);

  return (
    <div className="flex h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white/90 px-6 py-4 backdrop-blur-sm dark:border-slate-800 dark:bg-slate-950/80">
          <div className="flex items-center gap-4">
            <div className="flex h-10 w-10 items-center justify-center overflow-hidden rounded-xl bg-gradient-to-br from-rose-500 to-orange-500 shadow-lg shadow-rose-500/20">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src="/DOX.D.svg" alt="CI Failure Analyzer logo" className="h-6 w-6 object-contain" />
            </div>
            <div>
              <h1 className="text-xl font-bold tracking-tight">Root Cause Analysis</h1>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {stage ? `${stage.job_full_name} #${stage.build_number}` : "Jenkins build analysis"}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <ThemeToggle />
            <Button variant={chatOpen ? "default" : "outline"} size="sm" onClick={() => setChatOpen((v) => !v)}>
              Chat
            </Button>
            <Link href="/" aria-label="Back to dashboard">
              <Button variant="outline" size="sm">
                ← Back to dashboard
              </Button>
            </Link>
          </div>
        </header>

        <div className="min-w-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-7xl space-y-6 p-6">
            {loading && (
              <div className="py-20 text-center">
                <div className="inline-block h-8 w-8 animate-spin rounded-full border-2 border-slate-300 border-t-slate-700 dark:border-slate-700 dark:border-t-slate-200" />
                <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">Loading analysis...</p>
              </div>
            )}

            {error && (
              <div className="rounded-xl border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
                <span className="mr-2 font-semibold">Error:</span>
                {error}
              </div>
            )}

            {!loading && !error && !stage && (
              <div className="rounded-2xl border border-dashed border-slate-300 bg-white py-16 text-center dark:border-slate-700 dark:bg-slate-900/40">
                <p className="text-sm text-slate-600 dark:text-slate-300">
                  {runId
                    ? "This analysis was not found. It may have aged out of the results window."
                    : "No failure selected."}
                </p>
                <Link href="/" className="mt-3 inline-block">
                  <Button variant="outline" size="sm">
                    Back to dashboard
                  </Button>
                </Link>
              </div>
            )}

            {!loading && stage && (
              <div className="grid gap-6 2xl:grid-cols-[minmax(0,1fr)_280px]">
                <FailureCard stage={stage} onFeedbackSaved={() => load()} />
                <aside className="space-y-4">
                  {topNoisy.length > 0 && (
                    <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900/70">
                      <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                        Similar past clusters
                      </h3>
                      <div className="space-y-2">
                        {topNoisy.map(([key, count]) => (
                          <div
                            key={key}
                            className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs dark:border-slate-800 dark:bg-slate-950/70"
                          >
                            <span className="font-medium text-slate-700 dark:text-slate-200">{key}</span>
                            <span className="ml-2 text-slate-500 dark:text-slate-400">({count})</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900/70">
                    <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                      Selection context
                    </h3>
                    <p className="text-sm text-slate-700 dark:text-slate-300">{stage.job_full_name}</p>
                    <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">run id: {stage.run_id}</p>
                  </div>
                </aside>
              </div>
            )}
          </div>
        </div>
      </div>

      {chatOpen && (
        <div className="w-[380px] shrink-0 animate-slide-in-right border-l border-slate-200 dark:border-slate-800">
          <ChatPanel open={chatOpen} activeRunId={stage?.run_id} onClose={() => setChatOpen(false)} />
        </div>
      )}
    </div>
  );
}
