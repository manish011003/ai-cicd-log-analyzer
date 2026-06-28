"use client";

import type { BuildGroup } from "@/lib/filters";
import type { ProcessedStage } from "@/lib/types";
import ErrorClassBadge from "./ErrorClassBadge";

interface Props {
  groups: BuildGroup[];
  onSelect: (stage: ProcessedStage) => void;
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

function StatusPill({ status }: { status: string }) {
  const fb = (status || "new").toLowerCase();
  const tone =
    fb === "accepted"
      ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300"
      : fb === "rejected"
        ? "bg-rose-100 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300"
        : "bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300";
  const label = fb === "new" ? "unresolved" : fb;
  return (
    <span className={`inline-flex rounded px-1.5 py-0.5 text-[10px] font-medium capitalize ${tone}`}>
      {label}
    </span>
  );
}

export default function FailuresTable({ groups, onSelect }: Props) {
  if (groups.length === 0) {
    return (
      <div className="rounded-2xl border border-dashed border-slate-300 bg-white py-16 text-center text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-400">
        No failures match the current filters.
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900/70">
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-slate-200 bg-slate-50 text-[11px] uppercase tracking-wider text-slate-500 dark:border-slate-800 dark:bg-slate-950/50 dark:text-slate-400">
            <th className="px-4 py-2.5 font-medium">Stage</th>
            <th className="px-4 py-2.5 font-medium">Error class</th>
            <th className="px-4 py-2.5 font-medium">Status</th>
            <th className="px-4 py-2.5 font-medium">Detected</th>
            <th className="px-4 py-2.5 text-right font-medium">Action</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => (
            <FragmentGroup key={group.key} group={group} onSelect={onSelect} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FragmentGroup({ group, onSelect }: { group: BuildGroup; onSelect: Props["onSelect"] }) {
  return (
    <>
      <tr className="border-b border-slate-200 bg-slate-100/70 dark:border-slate-800 dark:bg-slate-950/40">
        <td colSpan={5} className="px-4 py-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="truncate font-semibold text-slate-800 dark:text-slate-100">
              {group.job_full_name}
            </span>
            <span className="rounded-md bg-slate-200 px-1.5 py-0.5 text-[11px] font-semibold tabular-nums text-slate-700 dark:bg-slate-800 dark:text-slate-300">
              #{group.build_number}
            </span>
            <span className="text-[11px] text-slate-500 dark:text-slate-400">
              {group.stages.length} failed stage{group.stages.length !== 1 ? "s" : ""}
            </span>
            <time className="ml-auto text-[11px] tabular-nums text-slate-500 dark:text-slate-400">
              {formatTimestamp(group.latestTs)}
            </time>
          </div>
        </td>
      </tr>
      {group.stages.map((stage) => (
        <tr
          key={stage.run_id}
          onClick={() => onSelect(stage)}
          className="group cursor-pointer border-b border-slate-100 transition-colors last:border-b-0 hover:bg-indigo-50/60 dark:border-slate-800/60 dark:hover:bg-indigo-950/20"
        >
          <td className="px-4 py-2.5">
            <span className="font-medium text-slate-800 dark:text-slate-100">
              {stage.stage_name || "Unknown stage"}
            </span>
          </td>
          <td className="px-4 py-2.5">
            <ErrorClassBadge errorClass={stage.error_class} />
          </td>
          <td className="px-4 py-2.5">
            <StatusPill status={stage.feedback_status || "new"} />
          </td>
          <td className="px-4 py-2.5 text-xs tabular-nums text-slate-500 dark:text-slate-400">
            {formatTimestamp(stage.timestamp)}
          </td>
          <td className="px-4 py-2.5 text-right">
            <span className="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-600 transition group-hover:border-indigo-400 dark:border-slate-700 dark:text-slate-300">
              Analyse
              <span aria-hidden>→</span>
            </span>
          </td>
        </tr>
      ))}
    </>
  );
}
