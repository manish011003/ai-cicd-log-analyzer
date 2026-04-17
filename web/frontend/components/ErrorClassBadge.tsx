interface Props {
  errorClass: string;
  count?: number;
  active?: boolean;
  onClick?: () => void;
}

function tone(errorClass: string): string {
  const k = errorClass.toLowerCase();
  if (k.includes("timeout") || k.includes("connection")) {
    return "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-700/50 dark:bg-amber-950/40 dark:text-amber-300";
  }
  if (k.includes("assert") || k.includes("test")) {
    return "border-purple-300 bg-purple-50 text-purple-800 dark:border-purple-700/50 dark:bg-purple-950/40 dark:text-purple-300";
  }
  return "border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-700/50 dark:bg-rose-950/40 dark:text-rose-300";
}

export default function ErrorClassBadge({ errorClass, count, active, onClick }: Props) {
  const base = tone(errorClass);
  const cls = active ? "ring-1 ring-slate-400/50 dark:ring-zinc-100/30" : "";

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`inline-flex items-center gap-2 rounded-lg border px-2.5 py-1 text-xs font-medium transition ${base} ${cls}`}
      >
        <span className="truncate">{errorClass || "Unknown"}</span>
        {typeof count === "number" && <span className="tabular-nums opacity-80">{count}</span>}
      </button>
    );
  }

  return (
    <span className={`inline-flex items-center gap-2 rounded-lg border px-2.5 py-1 text-xs font-medium ${base}`}>
      <span className="truncate">{errorClass || "Unknown"}</span>
      {typeof count === "number" && <span className="tabular-nums opacity-80">{count}</span>}
    </span>
  );
}
