"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchDiagnostics, fetchResults, fetchStats, pollListenerOnce } from "@/lib/api";
import type { DiagnosticsResponse, ProcessedStage, StatsResponse } from "@/lib/types";
import ChatPanel from "./ChatPanel";
import FailureCard from "./FailureCard";
import StatsBar from "./StatsBar";

const POLL_INTERVAL_MS = 10000;

export default function Dashboard() {
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [results, setResults] = useState<ProcessedStage[]>([]);
  const [activeFilter, setActiveFilter] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [polling, setPolling] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pollWarning, setPollWarning] = useState<string | null>(null);
  const [lastPollSummary, setLastPollSummary] = useState<string | null>(null);
  const [diag, setDiag] = useState<DiagnosticsResponse | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | undefined>(undefined);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(
    async (errorClass: string | null, isBackground = false) => {
      if (isBackground) setRefreshing(true);
      else setLoading(true);
      setError(null);
      try {
        const [statsData, resultsData] = await Promise.all([
          fetchStats(),
          fetchResults(40, undefined, errorClass ?? undefined),
        ]);
        setStats(statsData);
        setResults(resultsData.results);
        setLastUpdated(new Date());
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load data");
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [],
  );

  const runListenerPoll = useCallback(async () => {
    try {
      const data = await pollListenerOnce();
      setPollWarning(null);
      const L = data.listener;
      if (L && typeof L.forwarded === "number") {
        setLastPollSummary(
          L.forwarded === 0
            ? "Jenkins poll finished: 0 new failures forwarded (build may already be in listener state, not in /rssFailed yet, or not a Pipeline failure with extractable logs)."
            : `Jenkins poll finished: forwarded ${L.forwarded} failure build(s) to the worker for analysis.`,
        );
      } else {
        setLastPollSummary("Jenkins poll finished (no listener payload in response).");
      }
    } catch (err) {
      setLastPollSummary(null);
      setPollWarning(
        err instanceof Error
          ? err.message
          : "Listener poll failed. Restart the web API after changing LISTENER_BASE_URL, and run the listener in API mode: uvicorn app.main:app --host 127.0.0.1 --port 8088",
      );
    }
  }, []);

  const pollAndRefresh = useCallback(async () => {
    await runListenerPoll();
    await load(activeFilter, true);
  }, [activeFilter, load, runListenerPoll]);

  useEffect(() => {
    void load(activeFilter);
  }, [activeFilter, load]);

  useEffect(() => {
    void fetchDiagnostics()
      .then(setDiag)
      .catch(() => setDiag(null));
  }, []);

  useEffect(() => {
    if (polling) {
      timerRef.current = setInterval(() => {
        void pollAndRefresh();
      }, POLL_INTERVAL_MS);
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
    };
  }, [polling, pollAndRefresh]);

  const topNoisy = useMemo(() => {
    const byJob = new Map<string, number>();
    for (const row of results) {
      const key = `${row.job_full_name} / ${row.stage_name || "unknown-stage"}`;
      byJob.set(key, (byJob.get(key) || 0) + 1);
    }
    return Array.from(byJob.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5);
  }, [results]);

  return (
    <div className="flex h-screen">
      <div className="flex flex-1 flex-col overflow-hidden">
        <header className="flex items-center justify-between border-b border-zinc-800/80 bg-zinc-950/80 px-6 py-4 backdrop-blur-sm">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-red-500 to-orange-500 shadow-lg shadow-red-500/20">
              <span className="text-xs font-bold text-white">CI</span>
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight text-zinc-50">CI Failure Analyzer</h1>
              <p className="text-xs text-zinc-500">Jenkins build analysis</p>
              {diag && (
                <p className="mt-1 max-w-xl text-[10px] leading-snug text-zinc-600">
                  DB {diag.database_target.host}:{diag.database_target.port}/{diag.database_target.dbname} · Listener{" "}
                  {diag.listener_http_reachable ? "OK" : "DOWN"} ({diag.listener_base_url}
                  {diag.listener_http_error ? `: ${diag.listener_http_error}` : ""}) · Worker{" "}
                  {diag.worker_http_reachable ? "OK" : "DOWN"} ({diag.worker_base_url}
                  {diag.worker_http_error ? `: ${diag.worker_http_error}` : ""})
                </p>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2">
            {lastUpdated && (
              <span className="mr-1 text-[10px] tabular-nums text-zinc-600">{lastUpdated.toLocaleTimeString()}</span>
            )}
            <button
              type="button"
              onClick={() =>
                void (async () => {
                  await runListenerPoll();
                  await load(activeFilter, true);
                  try {
                    setDiag(await fetchDiagnostics());
                  } catch {
                    /* ignore */
                  }
                })()
              }
              disabled={refreshing}
              className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs font-medium text-zinc-400 transition-all hover:border-zinc-700 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-50"
            >
              {refreshing ? "Refreshing..." : "Refresh"}
            </button>
            <button
              type="button"
              onClick={() => setPolling((v) => !v)}
              className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-xs font-medium transition-all ${
                polling
                  ? "border-emerald-700 bg-emerald-950 text-emerald-400"
                  : "border-zinc-800 bg-zinc-900 text-zinc-400 hover:border-zinc-700 hover:bg-zinc-800 hover:text-zinc-200"
              }`}
            >
              <span className={`inline-block h-1.5 w-1.5 rounded-full ${polling ? "animate-pulse bg-emerald-400" : "bg-zinc-600"}`} />
              {polling ? "Live" : "Auto-poll"}
            </button>
            <button
              type="button"
              onClick={() => setChatOpen((v) => !v)}
              className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-2 text-xs font-medium transition-all ${
                chatOpen
                  ? "border-indigo-700 bg-indigo-950 text-indigo-400"
                  : "border-zinc-800 bg-zinc-900 text-zinc-400 hover:border-zinc-700 hover:bg-zinc-800 hover:text-zinc-200"
              }`}
            >
              Chat
            </button>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-6xl space-y-5 p-6">
            <div className="animate-fade-in">
              <StatsBar stats={stats} activeFilter={activeFilter} onFilterChange={setActiveFilter} />
            </div>

            {topNoisy.length > 0 && (
              <div className="rounded-xl border border-zinc-800/60 bg-zinc-900/40 px-5 py-4">
                <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">Top noisy jobs/stages</h2>
                <div className="grid gap-2 sm:grid-cols-2">
                  {topNoisy.map(([key, count]) => (
                    <div key={key} className="rounded-md border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-zinc-300">
                      <span className="font-medium">{key}</span>
                      <span className="ml-2 text-zinc-500">({count})</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {pollWarning && (
              <div className="animate-fade-in rounded-xl border border-amber-800/60 bg-amber-950/25 px-4 py-3 text-sm text-amber-200">
                <span className="mr-2 font-semibold">Listener:</span>
                {pollWarning}
              </div>
            )}

            {lastPollSummary && !pollWarning && (
              <div className="animate-fade-in rounded-xl border border-zinc-700/60 bg-zinc-900/50 px-4 py-3 text-sm text-zinc-300">
                {lastPollSummary}
              </div>
            )}

            {error && (
              <div className="animate-fade-in rounded-xl border border-red-900/50 bg-red-950/30 px-4 py-3 text-sm text-red-400">
                <span className="mr-2 font-semibold">Error:</span>
                {error}
              </div>
            )}

            {loading && (
              <div className="py-16 text-center">
                <div className="inline-block h-8 w-8 animate-spin rounded-full border-2 border-zinc-700 border-t-zinc-300" />
                <p className="mt-3 text-sm text-zinc-500">Loading failure data...</p>
              </div>
            )}

            {!loading && results.length === 0 && !error && (
              <div className="py-16 text-center text-sm text-zinc-500">No failures found.</div>
            )}

            {!loading && results.length > 0 && (
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <p className="text-xs font-medium text-zinc-500">
                    {results.length} failure{results.length !== 1 ? "s" : ""}
                    {activeFilter && <span className="ml-1 text-zinc-600">filtered by "{activeFilter}"</span>}
                  </p>
                </div>
                {results.map((stage, i) => (
                  <div
                    key={`${stage.run_id}-${stage.stage_name}-${i}`}
                    className="animate-fade-in"
                    style={{ animationDelay: `${i * 40}ms` }}
                    onMouseEnter={() => setActiveRunId(stage.run_id)}
                  >
                    <FailureCard stage={stage} onFeedbackSaved={() => load(activeFilter, true)} />
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>

      {chatOpen && (
        <div className="w-[390px] shrink-0 animate-slide-in-right">
          <ChatPanel open={chatOpen} activeRunId={activeRunId} onClose={() => setChatOpen(false)} />
        </div>
      )}
    </div>
  );
}
