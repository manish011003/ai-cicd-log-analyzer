interface Props {
  errorClass: string;
  count?: number;
  active?: boolean;
  onClick?: () => void;
}

function tone(errorClass: string): string {
  const k = errorClass.toLowerCase();
  if (k.includes("timeout") || k.includes("connection")) {
    return "border-amber-700/50 bg-amber-950/40 text-amber-300";
  }
  if (k.includes("assert") || k.includes("test")) {
    return "border-purple-700/50 bg-purple-950/40 text-purple-300";
  }
  return "border-red-700/50 bg-red-950/40 text-red-300";
}

export default function ErrorClassBadge({ errorClass, count, active, onClick }: Props) {
  const base = tone(errorClass);
  const cls = active ? "ring-1 ring-offset-0 ring-zinc-100/30" : "";

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
