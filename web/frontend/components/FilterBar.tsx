"use client";

import type { DatePreset, FailureFilter, FeedbackFilter } from "@/lib/filters";
import InfoHint from "./ui/info-hint";

interface Props {
  filter: FailureFilter;
  onChange: (next: FailureFilter) => void;
  /** Number of rows currently matching the scope filters (for the summary line). */
  matchCount: number;
  totalCount: number;
}

const DATE_PRESETS: { id: DatePreset; label: string }[] = [
  { id: "all", label: "All time" },
  { id: "today", label: "Today" },
  { id: "yesterday", label: "Yesterday" },
  { id: "thisWeek", label: "This Week" },
  { id: "lastWeek", label: "Last Week" },
  { id: "custom", label: "Custom" },
];

const FEEDBACK_OPTIONS: { id: FeedbackFilter; label: string }[] = [
  { id: "all", label: "All statuses" },
  { id: "new", label: "Unresolved" },
  { id: "accepted", label: "Accepted" },
  { id: "rejected", label: "Rejected" },
];

export default function FilterBar({ filter, onChange, matchCount, totalCount }: Props) {
  function patch(part: Partial<FailureFilter>) {
    onChange({ ...filter, ...part });
  }

  const isFiltered =
    filter.search.trim() !== "" ||
    filter.datePreset !== "all" ||
    filter.feedback !== "all" ||
    filter.errorClass !== null;

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900/70">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">Filters</h2>
          <InfoHint
            side="right"
            text="Filters are applied to the matrix, the error breakdown, and the table together. Date, search, and status narrow the whole view; clicking an error-class badge narrows just the table."
          />
        </div>
        <p className="text-xs tabular-nums text-slate-500 dark:text-slate-400">
          {matchCount} of {totalCount} failed stage{totalCount !== 1 ? "s" : ""}
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_auto]">
        <div className="space-y-3">
          <label className="block">
            <span className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
              Search
            </span>
            <input
              type="text"
              value={filter.search}
              onChange={(e) => patch({ search: e.target.value })}
              placeholder="Job, stage, error class, or signature…"
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder-slate-400 outline-none transition-colors focus:border-indigo-500 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100 dark:placeholder-slate-500"
            />
          </label>

          <div>
            <span className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
              Date range
            </span>
            <div className="flex flex-wrap items-center gap-1.5">
              {DATE_PRESETS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => patch({ datePreset: p.id })}
                  className={`rounded-md border px-2.5 py-1 text-xs font-medium transition ${
                    filter.datePreset === p.id
                      ? "border-indigo-400 bg-indigo-50 text-indigo-700 dark:border-indigo-600 dark:bg-indigo-950/50 dark:text-indigo-200"
                      : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-300 dark:hover:bg-slate-900"
                  }`}
                >
                  {p.label}
                </button>
              ))}
            </div>
            {filter.datePreset === "custom" && (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <label className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
                  From
                  <input
                    type="date"
                    value={filter.from}
                    onChange={(e) => patch({ from: e.target.value })}
                    className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-900 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                  />
                </label>
                <label className="flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
                  To
                  <input
                    type="date"
                    value={filter.to}
                    onChange={(e) => patch({ to: e.target.value })}
                    className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-900 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                  />
                </label>
              </div>
            )}
          </div>
        </div>

        <div className="flex flex-col gap-3 lg:w-56">
          <label className="block">
            <span className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
              Status
            </span>
            <select
              value={filter.feedback}
              onChange={(e) => patch({ feedback: e.target.value as FeedbackFilter })}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition-colors focus:border-indigo-500 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
            >
              {FEEDBACK_OPTIONS.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>

          {isFiltered && (
            <button
              type="button"
              onClick={() => onChange({ search: "", datePreset: "all", from: "", to: "", feedback: "all", errorClass: null })}
              className="mt-auto self-start rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-900"
            >
              Clear all filters
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
