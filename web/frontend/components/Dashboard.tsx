"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchDiagnostics, fetchResults, fetchStats, pollListenerOnce } from "@/lib/api";
import type { DiagnosticsResponse, ProcessedStage, StatsResponse } from "@/lib/types";
import ChatPanel from "./ChatPanel";
import FailureCard from "./FailureCard";
import StatsBar from "./StatsBar";
import ThemeToggle from "./ui/theme-toggle";
import { Button } from "./ui/button";
import { ScrollArea } from "./ui/scroll-area";

const POLL_INTERVAL_MS = 10000;

function stageRowKey(stage: ProcessedStage, flatIndex: number) {
  return `${stage.run_id}-${stage.stage_name}-${flatIndex}`;
}

type BuildGroup = {
  key: string;
  job_full_name: string;
  build_number: number;
  latestTs: string;
  stages: { stage: ProcessedStage; flatIndex: number }[];
};

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
  const [selectedStageRowKey, setSelectedStageRowKey] = useState<string | null>(null);
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

  const buildGroups = useMemo(() => {
    const order: string[] = [];
    const groups = new Map<
      string,
      { key: string; job_full_name: string; build_number: number; stages: BuildGroup["stages"]; latestTs: string }
    >();

    results.forEach((stage, flatIndex) => {
      const gk = `${stage.job_full_name}\u0001${stage.build_number}`;
      if (!groups.has(gk)) {
        groups.set(gk, {
          key: gk,
          job_full_name: stage.job_full_name,
          build_number: stage.build_number,
          stages: [],
          latestTs: stage.timestamp,
        });
        order.push(gk);
      }
      const g = groups.get(gk)!;
      g.stages.push({ stage, flatIndex });
      if (new Date(stage.timestamp) > new Date(g.latestTs)) {
        g.latestTs = stage.timestamp;
      }
    });

    const list: BuildGroup[] = order.map((k) => {
      const g = groups.get(k)!;
      return {
        key: g.key,
        job_full_name: g.job_full_name,
        build_number: g.build_number,
        latestTs: g.latestTs,
        stages: [...g.stages].sort((a, b) => {
          const ta = new Date(a.stage.timestamp).getTime();
          const tb = new Date(b.stage.timestamp).getTime();
          if (tb !== ta) return tb - ta;
          return (a.stage.stage_name || "").localeCompare(b.stage.stage_name || "");
        }),
      };
    });

    list.sort((a, b) => new Date(b.latestTs).getTime() - new Date(a.latestTs).getTime());
    return list;
  }, [results]);

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

  const selectedStage = useMemo(() => {
    if (results.length === 0) return null;
    if (!selectedStageRowKey) return results[0];
    return (
      results.find((stage, idx) => stageRowKey(stage, idx) === selectedStageRowKey) ?? results[0]
    );
  }, [results, selectedStageRowKey]);

  useEffect(() => {
    if (results.length === 0) {
      setSelectedStageRowKey(null);
      return;
    }
    if (!selectedStageRowKey) {
      setSelectedStageRowKey(stageRowKey(results[0], 0));
      return;
    }
    const hasSelection = results.some((stage, idx) => stageRowKey(stage, idx) === selectedStageRowKey);
    if (!hasSelection) {
      setSelectedStageRowKey(stageRowKey(results[0], 0));
    }
  }, [results, selectedStageRowKey]);

  const selectedRunId = selectedStage?.run_id;

  return (
    <div className="flex h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white/90 px-6 py-4 backdrop-blur-sm dark:border-slate-800 dark:bg-slate-950/80">
          <div className="flex items-center gap-4">
            <div className="flex h-10 w-10 items-center justify-center overflow-hidden rounded-xl bg-gradient-to-br from-rose-500 to-orange-500 shadow-lg shadow-rose-500/20">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src="/DOX.D.svg"
                alt="CI Failure Analyzer logo"
                className="h-6 w-6 object-contain"
              />
            </div>
            <div>
              <h1 className="text-xl font-bold tracking-tight">CI Failure Analyzer</h1>
              <p className="text-xs text-slate-500 dark:text-slate-400">Jenkins build analysis</p>
              {diag && (
                <p className="mt-1 max-w-3xl text-[11px] leading-snug text-slate-500 dark:text-slate-400">
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
              <span className="mr-2 text-xs tabular-nums text-slate-500 dark:text-slate-400">{lastUpdated.toLocaleTimeString()}</span>
            )}
            <ThemeToggle />
            <Button
              variant="secondary"
              size="sm"
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
            >
              {refreshing ? "Refreshing..." : "Refresh"}
            </Button>
            <Button
              variant={polling ? "default" : "outline"}
              size="sm"
              onClick={() => setPolling((v) => !v)}
            >
              <span
                className={`inline-block h-1.5 w-1.5 rounded-full ${
                  polling ? "animate-pulse bg-emerald-400" : "bg-slate-400 dark:bg-slate-500"
                }`}
              />
              {polling ? "Live" : "Auto-poll"}
            </Button>
            <Button
              variant={chatOpen ? "default" : "outline"}
              size="sm"
              onClick={() => setChatOpen((v) => !v)}
            >
              Chat
            </Button>
          </div>
        </header>

        <div className="flex min-h-0 flex-1">
          <aside className="w-[330px] shrink-0 border-r border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-950/70">
            <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-800">
              <h2 className="text-sm font-semibold">Build Index</h2>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                {buildGroups.length} build{buildGroups.length !== 1 ? "s" : ""} · {results.length} failed stage
                {results.length !== 1 ? "s" : ""}
                {activeFilter ? ` · filtered by ${activeFilter}` : ""}
              </p>
            </div>
            <ScrollArea className="h-[calc(100vh-126px)]">
              <div className="space-y-3 p-3">
                {buildGroups.map((group) => {
                  const groupHasSelection = group.stages.some(
                    ({ stage, flatIndex }) => stageRowKey(stage, flatIndex) === selectedStageRowKey,
                  );
                  return (
                    <div
                      key={group.key}
                      className={`rounded-xl border bg-white p-2 dark:bg-slate-900/30 ${
                        groupHasSelection
                          ? "border-indigo-300 shadow-sm dark:border-indigo-800"
                          : "border-slate-200 dark:border-slate-800"
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => {
                          const first = group.stages[0];
                          if (!first) return;
                          setSelectedStageRowKey(stageRowKey(first.stage, first.flatIndex));
                        }}
                        className="w-full cursor-pointer rounded-lg border-b border-slate-100 px-2 pb-2 text-left transition hover:bg-slate-50 dark:border-slate-800/80 dark:hover:bg-slate-900/50"
                        aria-label={`Open ${group.job_full_name} #${group.build_number}`}
                      >
                        <div className="flex items-start justify-between gap-2">
                          <p className="min-w-0 truncate text-sm font-semibold text-slate-800 dark:text-slate-100">
                            {group.job_full_name}
                          </p>
                          <span className="shrink-0 rounded-md bg-slate-100 px-1.5 py-0.5 text-[11px] font-semibold tabular-nums text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                            #{group.build_number}
                          </span>
                        </div>
                        <time className="mt-1 block text-[11px] tabular-nums text-slate-500 dark:text-slate-400">
                          {new Date(group.latestTs).toLocaleString()}
                        </time>
                        <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-500">
                          {group.stages.length} failed stage{group.stages.length !== 1 ? "s" : ""}
                        </p>
                      </button>
                      <ul className="mt-2 space-y-1 border-l-2 border-slate-200 pl-3 dark:border-slate-700">
                        {group.stages.map(({ stage, flatIndex }) => {
                          const itemKey = stageRowKey(stage, flatIndex);
                          const selected = itemKey === selectedStageRowKey;
                          const feedback = (stage.feedback_status || "new").toLowerCase();
                          return (
                            <li key={itemKey}>
                              <button
                                type="button"
                                onClick={() => setSelectedStageRowKey(itemKey)}
                                className={`w-full rounded-lg border px-2.5 py-2 text-left text-[12px] transition ${
                                  selected
                                    ? "border-indigo-400 bg-indigo-50 dark:border-indigo-600 dark:bg-indigo-950/50"
                                    : "border-transparent bg-slate-50 hover:border-slate-300 hover:bg-white dark:bg-slate-950/50 dark:hover:border-slate-600 dark:hover:bg-slate-900/80"
                                }`}
                              >
                                <div className="flex items-center justify-between gap-2">
                                  <span className="truncate font-medium text-slate-800 dark:text-slate-100">
                                    {stage.stage_name || "Unknown stage"}
                                  </span>
                                  <span
                                    className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium capitalize ${
                                      feedback === "accepted"
                                        ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300"
                                        : feedback === "rejected"
                                          ? "bg-rose-100 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300"
                                          : "bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300"
                                    }`}
                                  >
                                    {feedback}
                                  </span>
                                </div>
                                <time className="mt-0.5 block text-[10px] tabular-nums text-slate-500 dark:text-slate-400">
                                  {new Date(stage.timestamp).toLocaleString()}
                                </time>
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  );
                })}
              </div>
            </ScrollArea>
          </aside>

          <div className="min-w-0 flex-1 overflow-y-auto">
            <div className="mx-auto w-full max-w-7xl space-y-6 p-6">
              <StatsBar stats={stats} activeFilter={activeFilter} onFilterChange={setActiveFilter} />

              {pollWarning && (
                <div className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-700 dark:border-amber-800/60 dark:bg-amber-950/25 dark:text-amber-200">
                  <span className="mr-2 font-semibold">Listener:</span>
                  {pollWarning}
                </div>
              )}

              {lastPollSummary && !pollWarning && (
                <div className="rounded-xl border border-slate-300 bg-slate-100 px-4 py-3 text-sm text-slate-700 dark:border-slate-700/60 dark:bg-slate-900/50 dark:text-slate-300">
                  {lastPollSummary}
                </div>
              )}

              {error && (
                <div className="rounded-xl border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
                  <span className="mr-2 font-semibold">Error:</span>
                  {error}
                </div>
              )}

              {loading && (
                <div className="py-20 text-center">
                  <div className="inline-block h-8 w-8 animate-spin rounded-full border-2 border-slate-300 border-t-slate-700 dark:border-slate-700 dark:border-t-slate-200" />
                  <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">Loading failure data...</p>
                </div>
              )}

              {!loading && results.length === 0 && !error && (
                <div className="py-20 text-center text-sm text-slate-500 dark:text-slate-400">No failures found.</div>
              )}

              {!loading && selectedStage && (
                <div className="grid gap-6 2xl:grid-cols-[minmax(0,1fr)_280px]">
                  <FailureCard stage={selectedStage} onFeedbackSaved={() => load(activeFilter, true)} />
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
                      <p className="text-sm text-slate-700 dark:text-slate-300">{selectedStage.job_full_name}</p>
                      <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">run id: {selectedStage.run_id}</p>
                    </div>
                  </aside>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {chatOpen && (
        <div className="w-[380px] shrink-0 animate-slide-in-right border-l border-slate-200 dark:border-slate-800">
          <ChatPanel open={chatOpen} activeRunId={selectedRunId} onClose={() => setChatOpen(false)} />
        </div>
      )}
    </div>
  );
}
