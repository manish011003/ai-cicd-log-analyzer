import ErrorClassBadge from "./ErrorClassBadge";
import InfoHint from "./ui/info-hint";

interface Props {
  total: number;
  accepted: number;
  rejected: number;
  unresolved: number;
  byErrorClass: Record<string, number>;
  activeFilter: string | null;
  onFilterChange: (errorClass: string | null) => void;
}

export default function StatsBar({
  total,
  accepted,
  rejected,
  unresolved,
  byErrorClass,
  activeFilter,
  onFilterChange,
}: Props) {
  const entries = Object.entries(byErrorClass).sort(([, a], [, b]) => b - a);

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <MetricCard
          label="Total failures"
          value={total}
          hint="Failed stages matching the current date, search, and status filters."
        />
        <MetricCard
          label="Accepted fixes"
          value={accepted}
          tone="text-emerald-700 dark:text-emerald-300"
          hint="Failures whose suggested fix an operator accepted. Accepted fixes are stored in the knowledge base for reuse."
        />
        <MetricCard
          label="Rejected fixes"
          value={rejected}
          tone="text-rose-700 dark:text-rose-300"
          hint="Failures whose suggested fix an operator rejected as unhelpful."
        />
        <MetricCard
          label="Unresolved"
          value={unresolved}
          tone="text-amber-700 dark:text-amber-300"
          hint="Failures still awaiting an accept/reject decision."
        />
      </div>

      {total > 0 && (
        <div className="rounded-xl border border-slate-200 bg-white px-5 py-4 dark:border-slate-800 dark:bg-slate-900/60">
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-center gap-1.5">
              <p className="text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Error breakdown
              </p>
              <InfoHint
                side="right"
                text="Distribution of failures by error class within the current scope. Click a class to narrow the table to just those failures."
              />
            </div>
            <span className="rounded-md bg-slate-100 px-2 py-0.5 text-xs font-bold tabular-nums text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              {entries.length} class{entries.length !== 1 ? "es" : ""}
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
                clear class filter
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function MetricCard({
  label,
  value,
  tone = "text-slate-900 dark:text-slate-100",
  hint,
}: {
  label: string;
  value: number;
  tone?: string;
  hint?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900/60">
      <div className="flex items-center gap-1.5">
        <p className="text-[11px] uppercase tracking-wider text-slate-500 dark:text-slate-400">{label}</p>
        {hint && <InfoHint text={hint} />}
      </div>
      <p className={`mt-1 text-2xl font-semibold tabular-nums ${tone}`}>{value}</p>
    </div>
  );
}
