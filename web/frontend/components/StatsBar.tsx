import type { StatsResponse } from "@/lib/types";
import ErrorClassBadge from "./ErrorClassBadge";

interface Props {
  stats: StatsResponse | null;
  activeFilter: string | null;
  onFilterChange: (errorClass: string | null) => void;
}

export default function StatsBar({ stats, activeFilter, onFilterChange }: Props) {
  if (!stats || stats.total === 0) {
    return (
      <div className="rounded-xl border border-slate-200 bg-white px-5 py-4 dark:border-slate-800 dark:bg-slate-900/60">
        <p className="text-sm text-slate-500 dark:text-slate-400">No failure data yet.</p>
      </div>
    );
  }

  const entries = Object.entries(stats.by_error_class).sort(([, a], [, b]) => b - a);
  const accepted = stats.by_feedback_status.accepted ?? 0;
  const rejected = stats.by_feedback_status.rejected ?? 0;
  const unresolved = Math.max(0, stats.total - accepted - rejected);

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Total failures" value={stats.total} />
        <MetricCard label="Accepted fixes" value={accepted} tone="text-emerald-700 dark:text-emerald-300" />
        <MetricCard label="Rejected fixes" value={rejected} tone="text-rose-700 dark:text-rose-300" />
        <MetricCard label="Unresolved" value={unresolved} tone="text-amber-700 dark:text-amber-300" />
      </div>
      <div className="rounded-xl border border-slate-200 bg-white px-5 py-4 dark:border-slate-800 dark:bg-slate-900/60">
        <div className="mb-3 flex items-center justify-between">
          <p className="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">Error Breakdown</p>
          <span className="rounded-md bg-slate-100 px-2 py-0.5 text-xs font-bold tabular-nums text-slate-700 dark:bg-slate-800 dark:text-slate-300">
            {entries.length} classes
          </span>
        </div>
        <div className="flex flex-wrap gap-2">
          {entries.map(([cls, count]) => (
            <ErrorClassBadge
              key={cls}
              errorClass={cls}
              count={count}
              active={activeFilter === cls}
              onClick={() => onFilterChange(activeFilter === cls ? null : cls)}
            />
          ))}
          {activeFilter && (
            <button
              type="button"
              onClick={() => onFilterChange(null)}
              className="self-center text-xs text-slate-500 underline decoration-slate-400 underline-offset-2 transition-colors hover:text-slate-900 dark:text-slate-400 dark:decoration-slate-600 dark:hover:text-slate-200"
            >
              clear filter
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function MetricCard({
  label,
  value,
  tone = "text-slate-900 dark:text-slate-100",
}: {
  label: string;
  value: number;
  tone?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900/60">
      <p className="text-[11px] uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</p>
      <p className={`mt-1 text-2xl font-semibold tabular-nums ${tone}`}>{value}</p>
    </div>
  );
}
