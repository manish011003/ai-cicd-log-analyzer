import { useMemo, useState } from "react";
import { setFeedback } from "@/lib/api";
import type { ProcessedStage } from "@/lib/types";
import { stripMatchStatusFromAnalysis } from "@/lib/sanitizeAnalysis";
import MarkdownRenderer from "./MarkdownRenderer";
import ErrorClassBadge from "./ErrorClassBadge";

interface Props {
  stage: ProcessedStage;
  onFeedbackSaved: () => Promise<void> | void;
}

function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return ts;
  }
}

export default function FailureCard({ stage, onFeedbackSaved }: Props) {
  const [showLog, setShowLog] = useState(false);
  const [saving, setSaving] = useState<"accept" | "reject" | null>(null);
  const feedback = (stage.feedback_status || "").toLowerCase();

  const statusTone = useMemo(() => {
    if (feedback === "accepted") return "text-emerald-700 dark:text-emerald-300";
    if (feedback === "rejected") return "text-rose-700 dark:text-rose-300";
    return "text-slate-500 dark:text-slate-400";
  }, [feedback]);

  const analysisForDisplay = useMemo(() => {
    const raw = stage.analysis || "";
    const cleaned = stripMatchStatusFromAnalysis(raw);
    if (!cleaned.trim()) {
      return "The build failed in this stage; see the step-by-step fix for remediation.";
    }
    return cleaned;
  }, [stage.analysis]);

  async function handleFeedback(decision: "accept" | "reject") {
    setSaving(decision);
    try {
      await setFeedback(stage.run_id, decision);
      await onFeedbackSaved();
    } finally {
      setSaving(null);
    }
  }

  return (
    <article className="rounded-2xl border border-slate-200/90 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900/70">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-3">
            <h3 className="truncate text-lg font-semibold tracking-tight text-slate-900 dark:text-slate-100">
              {stage.job_full_name}
            </h3>
            <span className="shrink-0 rounded-md bg-slate-100 px-2 py-1 text-xs font-semibold tabular-nums text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              #{stage.build_number}
            </span>
          </div>
          <p className="mt-1.5 text-sm text-slate-600 dark:text-slate-400">
            <span>{stage.stage_name || "Unknown stage"}</span>
          </p>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          <ErrorClassBadge errorClass={stage.error_class} />
          <time className="text-xs tabular-nums text-slate-500 dark:text-slate-400">{formatTimestamp(stage.timestamp)}</time>
        </div>
      </div>

      <div className="mb-6 rounded-xl border border-slate-300/90 bg-slate-100 px-4 py-3 dark:border-slate-700 dark:bg-slate-950/80">
        <p className="text-xs uppercase tracking-widest text-slate-600 dark:text-slate-400">Signature</p>
        <p className="mt-1.5 break-all font-mono text-[0.9rem] text-slate-800 dark:text-slate-200">
          {stage.signature || "N/A"}
        </p>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <p className="mb-3 text-xs uppercase tracking-widest text-slate-500 dark:text-slate-400">Analysis</p>
          <MarkdownRenderer
            content={analysisForDisplay}
            className="space-y-3 text-[1.02rem] leading-[1.6] text-slate-700 dark:text-slate-200"
          />
        </div>

        <div className="rounded-xl border border-emerald-300/70 bg-emerald-50/80 p-5 shadow-sm dark:border-emerald-900/60 dark:bg-emerald-950/20">
          <p className="mb-3 text-xs uppercase tracking-widest text-emerald-700 dark:text-emerald-300">Suggested Fix</p>
          <MarkdownRenderer
            content={stage.suggested_fix || "No fix suggested."}
            className="space-y-3 text-[1.02rem] leading-[1.6] text-slate-800 dark:text-slate-100"
          />
        </div>
      </div>

      <div className="mb-3 mt-6 flex items-center justify-between">
        <button
          type="button"
          onClick={() => setShowLog((v) => !v)}
          className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800"
        >
          {showLog ? "Hide log excerpt" : "Show log excerpt"}
        </button>
        <p className={`text-xs capitalize ${statusTone}`}>status: {feedback || "new"}</p>
      </div>

      {showLog && (
        <pre className="mb-4 max-h-72 overflow-auto rounded-lg border border-slate-300 bg-slate-50 p-3 font-mono text-xs leading-relaxed text-slate-700 dark:border-slate-700 dark:bg-black/30 dark:text-slate-300">
          {stage.cleaned_log || "No cleaned log available."}
        </pre>
      )}

      <div className="flex gap-2">
        <button
          type="button"
          disabled={saving !== null}
          onClick={() => void handleFeedback("accept")}
          className="rounded-lg bg-emerald-700 px-3 py-2 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
        >
          {saving === "accept" ? "Saving..." : "Accept"}
        </button>
        <button
          type="button"
          disabled={saving !== null}
          onClick={() => void handleFeedback("reject")}
          className="rounded-lg bg-rose-700 px-3 py-2 text-xs font-medium text-white hover:bg-rose-600 disabled:opacity-50"
        >
          {saving === "reject" ? "Saving..." : "Reject"}
        </button>
      </div>
    </article>
  );
}
