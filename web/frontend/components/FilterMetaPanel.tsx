import type { FailureLocation, FilterMetaSummary } from "@/lib/types";

interface Props {
  meta?: FilterMetaSummary;
  className?: string;
}

const CONFIDENCE_STYLES: Record<string, string> = {
  HIGH: "border-emerald-300 bg-emerald-100 text-emerald-800 dark:border-emerald-800/60 dark:bg-emerald-950/40 dark:text-emerald-300",
  MEDIUM:
    "border-amber-300 bg-amber-100 text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/40 dark:text-amber-300",
  LOW: "border-rose-300 bg-rose-100 text-rose-700 dark:border-rose-800/60 dark:bg-rose-950/40 dark:text-rose-300",
};

function detectorLabel(name: string): string {
  switch (name) {
    case "java_stack":
      return "Java / JVM";
    case "python_traceback":
      return "Python";
    case "generic_shell":
      return "Shell";
    default:
      return name;
  }
}

function renderLocation(loc: FailureLocation | null | undefined): string {
  if (!loc) return "";
  if (loc.kind === "source" && loc.file) {
    const head = loc.line ? `${loc.file}:${loc.line}` : loc.file;
    return loc.function ? `${loc.function} @ ${head}` : head;
  }
  if (loc.kind === "test" && loc.test_name) return loc.test_name;
  if (loc.kind === "command" && loc.command) return `$ ${loc.command}`;
  if (loc.kind === "log" && typeof loc.log_line_idx === "number" && loc.log_line_idx >= 0) {
    return `line ${loc.log_line_idx}`;
  }
  return loc.message || "";
}

function compressionRatio(raw?: number, body?: number): string | null {
  if (!raw || !body || body <= 0) return null;
  const ratio = raw / body;
  if (!Number.isFinite(ratio) || ratio < 1.05) return null;
  return `${ratio.toFixed(1)}x`;
}

export default function FilterMetaPanel({ meta, className }: Props) {
  if (!meta || Object.keys(meta).length === 0) return null;

  const confidence = meta.confidence;
  const confidenceCls =
    (confidence && CONFIDENCE_STYLES[confidence]) ||
    "border-slate-300 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300";

  const detectors = (meta.activated_detectors || []).filter((d) => d && d !== "generic_shell");
  const showAllDetectors = (meta.activated_detectors || []).length > 0 && detectors.length === 0;
  const renderedDetectors = showAllDetectors
    ? (meta.activated_detectors || [])
    : detectors;

  const locText = renderLocation(meta.primary_location ?? null);
  const ratio = compressionRatio(meta.raw_chars, meta.body_chars);

  return (
    <div
      className={`rounded-xl border border-slate-200 bg-slate-50/80 px-4 py-3 dark:border-slate-800 dark:bg-slate-900/60 ${
        className ?? ""
      }`}
    >
      <p className="mb-2 text-xs uppercase tracking-widest text-slate-500 dark:text-slate-400">
        Detected by structural filter
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {confidence && (
          <span
            className={`rounded-md border px-2 py-0.5 text-[11px] font-semibold ${confidenceCls}`}
            title="Confidence the filter resolved the actual root cause"
          >
            {confidence}
          </span>
        )}
        {renderedDetectors.map((d) => (
          <span
            key={d}
            className="rounded-md border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[11px] font-medium text-indigo-700 dark:border-indigo-900/60 dark:bg-indigo-950/40 dark:text-indigo-300"
            title="Detector that contributed to this analysis"
          >
            {detectorLabel(d)}
          </span>
        ))}
        {ratio && (
          <span
            className="rounded-md border border-slate-300 bg-white px-2 py-0.5 text-[11px] tabular-nums text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
            title="Raw log size vs. filtered body — higher means more compression"
          >
            {ratio} smaller
          </span>
        )}
        {typeof meta.body_tokens === "number" && meta.body_tokens > 0 && (
          <span
            className="rounded-md border border-slate-300 bg-white px-2 py-0.5 text-[11px] tabular-nums text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
            title="Approximate tokens sent to the LLM"
          >
            ~{meta.body_tokens.toLocaleString()} tok
          </span>
        )}
      </div>
      {locText && (
        <p className="mt-2 break-all font-mono text-[12.5px] text-slate-700 dark:text-slate-300">
          <span className="mr-1 text-slate-500 dark:text-slate-400">@</span>
          {locText}
          {meta.primary_location?.message && meta.primary_location?.kind === "source" && (
            <span className="ml-2 text-slate-500 dark:text-slate-400">
              -- {meta.primary_location.message}
            </span>
          )}
        </p>
      )}
    </div>
  );
}
