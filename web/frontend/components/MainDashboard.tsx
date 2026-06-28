"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchDiagnostics, fetchResults, pollListenerOnce } from "@/lib/api";
import {
  applyErrorClassFilter,
  applyScopeFilters,
  computeMetrics,
  defaultFilter,
  groupByBuild,
  type FailureFilter,
} from "@/lib/filters";
import type { DiagnosticsResponse, ProcessedStage } from "@/lib/types";
import FailuresTable from "./FailuresTable";
import FilterBar from "./FilterBar";
import KnowledgeMap from "./KnowledgeMap";
import StatsBar from "./StatsBar";
import ThemeToggle from "./ui/theme-toggle";
import InfoHint from "./ui/info-hint";
import { Button } from "./ui/button";

const POLL_INTERVAL_MS = 10000;
// Backend caps /api/results at 200 (le=200).
const RESULTS_LIMIT = 200;

type DashboardView = "builds" | "knowledge";

export default function MainDashboard() {
  const router = useRouter();
  const [results, setResults] = useState<ProcessedStage[]>([]);
  const [filter, setFilter] = useState<FailureFilter>(() => defaultFilter());
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [polling, setPolling] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pollWarning, setPollWarning] = useState<string | null>(null);
  const [lastPollSummary, setLastPollSummary] = useState<string | null>(null);
  const [diag, setDiag] = useState<DiagnosticsResponse | null>(null);
  const [view, setView] = useState<DashboardView>("builds");
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async (isBackground = false) => {
    if (isBackground) setRefreshing(true);
    else setLoading(true);
    setError(null);
    try {
      const data = await fetchResults(RESULTS_LIMIT);
      setResults(data.results);
      setLastUpdated(new Date());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load data");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

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
          : "Listener poll failed. Restart the web API after changing LISTENER_BASE_URL, and run the listener in API mode.",
      );
    }
  }, []);

  const pollAndRefresh = useCallback(async () => {
    await runListenerPoll();
    await load(true);
  }, [load, runListenerPoll]);

  useEffect(() => {
    void load();
  }, [load]);

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

  const scoped = useMemo(() => applyScopeFilters(results, filter), [results, filter]);
  const metrics = useMemo(() => computeMetrics(scoped), [scoped]);
  const tableGroups = useMemo(
    () => groupByBuild(applyErrorClassFilter(scoped, filter.errorClass)),
    [scoped, filter.errorClass],
  );

  const openRca = useCallback(
    (stage: ProcessedStage) => {
      router.push(`/rca?run=${encodeURIComponent(stage.run_id)}`);
    },
    [router],
  );

  return (
    <div className="flex min-h-screen flex-col bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white/90 px-6 py-4 backdrop-blur-sm dark:border-slate-800 dark:bg-slate-950/80">
        <div className="flex items-center gap-4">
          <div className="flex h-10 w-10 items-center justify-center overflow-hidden rounded-xl bg-gradient-to-br from-rose-500 to-orange-500 shadow-lg shadow-rose-500/20">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/DOX.D.svg" alt="CI Failure Analyzer logo" className="h-6 w-6 object-contain" />
          </div>
          <div>
            <div className="flex items-center gap-1.5">
              <h1 className="text-xl font-bold tracking-tight">CI Failure Analyzer</h1>
              {diag && (
                <InfoHint
                  side="bottom"
                  label="System health"
                  text={`DB ${diag.database_target.host}:${diag.database_target.port}/${diag.database_target.dbname} · Listener ${
                    diag.listener_http_reachable ? "OK" : "DOWN"
                  } (${diag.listener_base_url}${diag.listener_http_error ? `: ${diag.listener_http_error}` : ""}) · Worker ${
                    diag.worker_http_reachable ? "OK" : "DOWN"
                  } (${diag.worker_base_url}${diag.worker_http_error ? `: ${diag.worker_http_error}` : ""})`}
                />
              )}
            </div>
            <p className="text-xs text-slate-500 dark:text-slate-400">Jenkins build failure overview</p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div
            className="mr-1 inline-flex overflow-hidden rounded-md border border-slate-300 dark:border-slate-700"
            role="tablist"
            aria-label="Dashboard view"
          >
            <button
              type="button"
              role="tab"
              aria-selected={view === "builds"}
              onClick={() => setView("builds")}
              className={`px-3 py-1 text-xs font-medium transition ${
                view === "builds"
                  ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
                  : "bg-white text-slate-700 hover:bg-slate-100 dark:bg-slate-950 dark:text-slate-300 dark:hover:bg-slate-900"
              }`}
            >
              Overview
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={view === "knowledge"}
              onClick={() => setView("knowledge")}
              className={`px-3 py-1 text-xs font-medium transition ${
                view === "knowledge"
                  ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
                  : "bg-white text-slate-700 hover:bg-slate-100 dark:bg-slate-950 dark:text-slate-300 dark:hover:bg-slate-900"
              }`}
            >
              Knowledge Map
            </button>
          </div>
          {lastUpdated && view === "builds" && (
            <span className="mr-2 text-xs tabular-nums text-slate-500 dark:text-slate-400">
              {lastUpdated.toLocaleTimeString()}
            </span>
          )}
          <ThemeToggle />
          {view === "builds" && (
            <>
              <Button
                variant="secondary"
                size="sm"
                onClick={() =>
                  void (async () => {
                    await runListenerPoll();
                    await load(true);
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
              <Button variant={polling ? "default" : "outline"} size="sm" onClick={() => setPolling((v) => !v)}>
                <span
                  className={`inline-block h-1.5 w-1.5 rounded-full ${
                    polling ? "animate-pulse bg-emerald-400" : "bg-slate-400 dark:bg-slate-500"
                  }`}
                />
                {polling ? "Live" : "Auto-poll"}
              </Button>
            </>
          )}
          <Link href="/settings" aria-label="Open settings page">
            <Button variant="outline" size="sm">
              Settings
            </Button>
          </Link>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-7xl space-y-6 p-6">
          {view === "knowledge" ? (
            <div className="flex min-h-[70vh] flex-col">
              <KnowledgeMap />
            </div>
          ) : (
            <>
              <StatsBar
                total={metrics.total}
                accepted={metrics.accepted}
                rejected={metrics.rejected}
                unresolved={metrics.unresolved}
                byErrorClass={metrics.byErrorClass}
                activeFilter={filter.errorClass}
                onFilterChange={(cls) => setFilter((f) => ({ ...f, errorClass: cls }))}
              />

              <FilterBar
                filter={filter}
                onChange={setFilter}
                matchCount={scoped.length}
                totalCount={results.length}
              />

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

              {loading ? (
                <div className="py-20 text-center">
                  <div className="inline-block h-8 w-8 animate-spin rounded-full border-2 border-slate-300 border-t-slate-700 dark:border-slate-700 dark:border-t-slate-200" />
                  <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">Loading failure data...</p>
                </div>
              ) : (
                <FailuresTable groups={tableGroups} onSelect={openRca} />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
