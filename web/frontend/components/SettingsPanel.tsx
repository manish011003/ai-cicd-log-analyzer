"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { fetchFilterConfig } from "@/lib/api";
import type { DetectorInfo, FilterConfigResponse } from "@/lib/types";
import ThemeToggle from "./ui/theme-toggle";
import { Button } from "./ui/button";
import InfoHint from "./ui/info-hint";

function detectorLabel(name: string): string {
  switch (name) {
    case "java_stack":
      return "Java / JVM";
    case "python_traceback":
      return "Python";
    case "generic_shell":
      return "Shell (universal floor)";
    default:
      return name;
  }
}

function KnobRow({
  label,
  value,
  envVar,
  hint,
}: {
  label: string;
  value: string;
  envVar: string;
  hint: string;
}) {
  return (
    <div className="grid grid-cols-1 gap-1 border-b border-slate-200 px-4 py-3 last:border-b-0 sm:grid-cols-[1fr_minmax(140px,_240px)_minmax(160px,_220px)] sm:items-baseline sm:gap-4 dark:border-slate-800">
      <div className="flex items-center gap-1.5">
        <p className="text-sm font-medium text-slate-800 dark:text-slate-100">{label}</p>
        <InfoHint side="right" text={hint} />
      </div>
      <p className="font-mono text-sm tabular-nums text-slate-900 dark:text-slate-100">{value}</p>
      <code className="rounded bg-slate-100 px-2 py-1 text-[11px] text-slate-600 dark:bg-slate-900 dark:text-slate-300">
        {envVar}
      </code>
    </div>
  );
}

function DetectorRow({ d }: { d: DetectorInfo }) {
  return (
    <li className="flex items-start gap-3 border-b border-slate-200 px-4 py-3 last:border-b-0 dark:border-slate-800">
      <span
        className={`mt-1 inline-block h-2 w-2 shrink-0 rounded-full ${
          d.active ? "bg-emerald-500" : "bg-slate-300 dark:bg-slate-700"
        }`}
        aria-label={d.active ? "active" : "inactive"}
      />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="font-mono text-sm font-medium text-slate-900 dark:text-slate-100">
            {d.name}
          </span>
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {detectorLabel(d.name)}
          </span>
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] tabular-nums text-slate-700 dark:bg-slate-900 dark:text-slate-300">
            priority {d.priority}
          </span>
          {!d.active && (
            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-950/40 dark:text-amber-300">
              disabled
            </span>
          )}
        </div>
        <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">{d.description}</p>
      </div>
    </li>
  );
}

export default function SettingsPanel() {
  const [config, setConfig] = useState<FilterConfigResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (refresh = false) => {
    if (refresh) setRefreshing(true);
    else setLoading(true);
    setError(null);
    try {
      const data = await fetchFilterConfig(refresh);
      setConfig(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load filter config");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  return (
    <div className="flex h-screen flex-col bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white/90 px-6 py-4 backdrop-blur-sm dark:border-slate-800 dark:bg-slate-950/80">
        <div className="flex items-center gap-4">
          <div className="flex h-10 w-10 items-center justify-center overflow-hidden rounded-xl bg-gradient-to-br from-rose-500 to-orange-500 shadow-lg shadow-rose-500/20">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/DOX.D.svg" alt="CI Failure Analyzer logo" className="h-6 w-6 object-contain" />
          </div>
          <div>
            <div className="flex items-center gap-1.5">
              <h1 className="text-xl font-bold tracking-tight">Settings</h1>
              <InfoHint
                side="bottom"
                text="Read-only view of the worker's structural log filter. Every failure analysis runs the raw Jenkins log through a four-pass pipeline (tokenize → structural collapse → baseline diff → anchor selection); stack-specific detectors pin down the precise file/line. To change a value, edit the worker's .env and restart the container — see docs/filtering.md for the full reference."
              />
            </div>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              Structural log filter configuration
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <ThemeToggle />
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void load(true)}
            disabled={refreshing || loading}
          >
            {refreshing ? "Refreshing..." : "Refresh"}
          </Button>
          <Link href="/">
            <Button variant="outline" size="sm">
              Back to dashboard
            </Button>
          </Link>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-5xl space-y-6 p-6">
          {error && (
            <div className="rounded-xl border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
              <span className="mr-2 font-semibold">Error:</span>
              {error}
            </div>
          )}

          {loading && (
            <div className="py-20 text-center">
              <div className="inline-block h-8 w-8 animate-spin rounded-full border-2 border-slate-300 border-t-slate-700 dark:border-slate-700 dark:border-t-slate-200" />
              <p className="mt-3 text-sm text-slate-500 dark:text-slate-400">
                Loading filter configuration...
              </p>
            </div>
          )}

          {!loading && config && (
            <>
              <section className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900/70">
                <div className="flex items-center justify-between gap-2 border-b border-slate-200 px-5 py-3 dark:border-slate-800">
                  <div className="flex items-center gap-1.5">
                    <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                      Tweakable knobs
                    </h2>
                    <InfoHint
                      side="right"
                      text="These control the structural filter pipeline. Hover any row's info icon for what it does. Changing a value requires editing the worker's .env and restarting the container."
                    />
                  </div>
                  <p className="text-xs text-slate-500 dark:text-slate-400">
                    <code className="rounded bg-slate-100 px-1 text-[11px] dark:bg-slate-900">
                      {config.implementation}
                    </code>
                  </p>
                </div>
                <div>
                  <KnobRow
                    label="Token budget for filtered body"
                    value={config.settings.log_body_max_tokens.toLocaleString()}
                    envVar="LOG_BODY_MAX_TOKENS"
                    hint="Soft cap on tokens sent to the LLM. Pass 4 fills lines round-robin until the budget is exhausted."
                  />
                  <KnobRow
                    label="Hard character ceiling"
                    value={config.settings.log_body_max_chars.toLocaleString()}
                    envVar="LOG_BODY_MAX_CHARS"
                    hint="Final safety stop in case the token budget overshoots."
                  />
                  <KnobRow
                    label="Detector selection"
                    value={config.settings.filter_detectors}
                    envVar="FILTER_DETECTORS"
                    hint="'auto' loads every bundled detector, 'none' disables them, or pass a comma-list to allowlist by name."
                  />
                  <KnobRow
                    label="Max active detectors per log"
                    value={config.settings.filter_max_active_detectors.toLocaleString()}
                    envVar="FILTER_MAX_ACTIVE_DETECTORS"
                    hint="Cap on how many detectors actually contribute on one log. Activations happen in priority order."
                  />
                  <KnobRow
                    label="Self-baseline (Pass 3)"
                    value={config.settings.use_self_baseline ? "enabled" : "disabled"}
                    envVar="(in-code default)"
                    hint="Suppresses lines that recur in the same log; helps fold setup banners and progress chatter."
                  />
                </div>
              </section>

              <section className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900/70">
                <div className="flex items-center gap-1.5 border-b border-slate-200 px-5 py-3 dark:border-slate-800">
                  <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                    Active detectors
                  </h2>
                  <InfoHint
                    side="right"
                    text="Detectors loaded right now, sorted by priority. Each runs a cheap probe before doing work, so they only contribute when their stack actually shows up in the log."
                  />
                </div>
                {config.detectors.active.length === 0 ? (
                  <p className="px-5 py-4 text-sm text-slate-500 dark:text-slate-400">
                    No detectors loaded. The universal core still runs, but
                    failures will lack stack-specific source locations.
                  </p>
                ) : (
                  <ul>
                    {config.detectors.active.map((d) => (
                      <DetectorRow key={d.name} d={d} />
                    ))}
                  </ul>
                )}
              </section>

              <section className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900/70">
                <div className="flex items-center gap-1.5 border-b border-slate-200 px-5 py-3 dark:border-slate-800">
                  <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                    All bundled detectors
                  </h2>
                  <InfoHint
                    side="right"
                    text="Every detector shipped with the worker. Inactive entries are excluded by the FILTER_DETECTORS env var; set it to 'auto' to re-enable everything."
                  />
                </div>
                {config.detectors.available.length === 0 ? (
                  <p className="px-5 py-4 text-sm text-slate-500 dark:text-slate-400">
                    No bundled detectors discovered.
                  </p>
                ) : (
                  <ul>
                    {config.detectors.available.map((d) => (
                      <DetectorRow key={d.name} d={d} />
                    ))}
                  </ul>
                )}
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
