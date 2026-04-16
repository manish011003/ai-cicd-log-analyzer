import { useMemo, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { setFeedback } from "@/lib/api";
import type { ProcessedStage } from "@/lib/types";
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
    if (feedback === "accepted") return "text-emerald-300";
    if (feedback === "rejected") return "text-red-300";
    return "text-zinc-400";
  }, [feedback]);

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
    <div className="group rounded-xl border border-zinc-800/60 bg-zinc-900/50 p-5 transition-all duration-200 hover:border-zinc-700/80 hover:bg-zinc-900/70 hover:shadow-lg hover:shadow-zinc-950/50">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate text-sm font-semibold text-zinc-100">{stage.job_full_name}</h3>
            <span className="shrink-0 rounded-md bg-zinc-800 px-1.5 py-0.5 text-[11px] font-bold tabular-nums text-zinc-400">
              #{stage.build_number}
            </span>
          </div>
          <p className="mt-1.5 text-xs text-zinc-500">
            <span className="text-zinc-400">{stage.stage_name || "Unknown stage"}</span>
          </p>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          <ErrorClassBadge errorClass={stage.error_class} />
          <time className="text-[10px] tabular-nums text-zinc-600">{formatTimestamp(stage.timestamp)}</time>
        </div>
      </div>

      <div className="mb-3 rounded-lg border border-zinc-800 bg-zinc-900 p-3">
        <p className="text-[11px] uppercase tracking-wide text-zinc-500">Signature</p>
        <p className="mt-1 break-all text-xs text-zinc-300">{stage.signature || "N/A"}</p>
      </div>

      <div className="mb-3 rounded-lg border border-zinc-800 bg-zinc-900 p-4">
        <p className="mb-2 text-[11px] uppercase tracking-wide text-zinc-500">Analysis</p>
        <div className="space-y-2 text-sm leading-relaxed text-zinc-200">
          <Markdown remarkPlugins={[remarkGfm]}>{stage.analysis || "No analysis available."}</Markdown>
        </div>
      </div>

      <div className="mb-3 rounded-lg border border-zinc-800 bg-zinc-900 p-4">
        <p className="mb-2 text-[11px] uppercase tracking-wide text-zinc-500">Suggested Fix</p>
        <div className="space-y-2 text-sm leading-relaxed text-zinc-200">
          <Markdown remarkPlugins={[remarkGfm]}>{stage.suggested_fix || "No fix suggested."}</Markdown>
        </div>
      </div>

      <div className="mb-3 flex items-center justify-between">
        <button
          type="button"
          onClick={() => setShowLog((v) => !v)}
          className="rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
        >
          {showLog ? "Hide log excerpt" : "Show log excerpt"}
        </button>
        <p className={`text-xs capitalize ${statusTone}`}>status: {feedback || "new"}</p>
      </div>

      {showLog && (
        <pre className="mb-3 max-h-72 overflow-auto rounded-lg border border-zinc-800 bg-black/50 p-3 text-xs leading-relaxed text-zinc-300">
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
          className="rounded-lg bg-red-700 px-3 py-2 text-xs font-medium text-white hover:bg-red-600 disabled:opacity-50"
        >
          {saving === "reject" ? "Saving..." : "Reject"}
        </button>
      </div>
    </div>
  );
}
