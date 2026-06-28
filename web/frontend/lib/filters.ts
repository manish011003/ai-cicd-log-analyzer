import type { ProcessedStage } from "./types";

// ── Client-side filtering for the failures overview ───────────────────────
//
// Everything here runs in the browser over the full set of failed stages the
// backend returns. We deliberately keep date/search/feedback filtering on the
// client so the matrix, error breakdown, and table all stay in sync with the
// same predicate without extra round-trips.

export type DatePreset =
  | "all"
  | "today"
  | "yesterday"
  | "thisWeek"
  | "lastWeek"
  | "custom";

export type FeedbackFilter = "all" | "new" | "accepted" | "rejected";

export interface FailureFilter {
  search: string;
  datePreset: DatePreset;
  /** ISO `yyyy-mm-dd` (local) — only used when datePreset === "custom". */
  from: string;
  to: string;
  feedback: FeedbackFilter;
  errorClass: string | null;
}

export function defaultFilter(): FailureFilter {
  return {
    search: "",
    datePreset: "all",
    from: "",
    to: "",
    feedback: "all",
    errorClass: null,
  };
}

export interface DateRange {
  start: Date | null;
  end: Date | null;
}

function startOfDay(d: Date): Date {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  return x;
}

/** Monday-anchored start of the calendar week containing `d`. */
function startOfWeek(d: Date): Date {
  const x = startOfDay(d);
  const dow = (x.getDay() + 6) % 7; // 0 = Monday … 6 = Sunday
  x.setDate(x.getDate() - dow);
  return x;
}

function parseLocalDate(value: string, endOfDay = false): Date | null {
  if (!value) return null;
  const [y, m, d] = value.split("-").map((n) => Number(n));
  if (!y || !m || !d) return null;
  return endOfDay ? new Date(y, m - 1, d, 23, 59, 59, 999) : new Date(y, m - 1, d, 0, 0, 0, 0);
}

export function dateRangeForFilter(filter: FailureFilter, now = new Date()): DateRange {
  switch (filter.datePreset) {
    case "today":
      return { start: startOfDay(now), end: now };
    case "yesterday": {
      const start = startOfDay(now);
      start.setDate(start.getDate() - 1);
      return { start, end: startOfDay(now) };
    }
    case "thisWeek":
      return { start: startOfWeek(now), end: now };
    case "lastWeek": {
      const thisWeek = startOfWeek(now);
      const start = new Date(thisWeek);
      start.setDate(start.getDate() - 7);
      return { start, end: thisWeek };
    }
    case "custom":
      return { start: parseLocalDate(filter.from), end: parseLocalDate(filter.to, true) };
    case "all":
    default:
      return { start: null, end: null };
  }
}

function withinRange(ts: string, range: DateRange): boolean {
  if (!range.start && !range.end) return true;
  const t = new Date(ts).getTime();
  if (Number.isNaN(t)) return true;
  if (range.start && t < range.start.getTime()) return false;
  if (range.end && t > range.end.getTime()) return false;
  return true;
}

function normaliseFeedback(status: string | undefined): FeedbackFilter {
  const s = (status || "new").toLowerCase();
  if (s === "accepted") return "accepted";
  if (s === "rejected") return "rejected";
  return "new";
}

/**
 * Apply the "scope" predicate: search + date + feedback. Intentionally excludes
 * the error-class filter so the matrix and error-breakdown badges reflect the
 * full scope while clicking a badge only narrows the table.
 */
export function applyScopeFilters(stages: ProcessedStage[], filter: FailureFilter): ProcessedStage[] {
  const range = dateRangeForFilter(filter);
  const q = filter.search.trim().toLowerCase();
  return stages.filter((s) => {
    if (!withinRange(s.timestamp, range)) return false;
    if (filter.feedback !== "all" && normaliseFeedback(s.feedback_status) !== filter.feedback) {
      return false;
    }
    if (q) {
      const haystack = `${s.job_full_name} ${s.stage_name} ${s.error_class} ${s.signature}`.toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  });
}

export function applyErrorClassFilter(
  stages: ProcessedStage[],
  errorClass: string | null,
): ProcessedStage[] {
  if (!errorClass) return stages;
  return stages.filter((s) => s.error_class === errorClass);
}

export interface FailureMetrics {
  total: number;
  accepted: number;
  rejected: number;
  unresolved: number;
  byErrorClass: Record<string, number>;
}

export function computeMetrics(stages: ProcessedStage[]): FailureMetrics {
  let accepted = 0;
  let rejected = 0;
  const byErrorClass: Record<string, number> = {};
  for (const s of stages) {
    const fb = normaliseFeedback(s.feedback_status);
    if (fb === "accepted") accepted += 1;
    else if (fb === "rejected") rejected += 1;
    const cls = s.error_class || "Unknown";
    byErrorClass[cls] = (byErrorClass[cls] ?? 0) + 1;
  }
  const total = stages.length;
  return { total, accepted, rejected, unresolved: Math.max(0, total - accepted - rejected), byErrorClass };
}

export interface BuildGroup {
  key: string;
  job_full_name: string;
  build_number: number;
  latestTs: string;
  stages: ProcessedStage[];
}

const GROUP_SEP = "\u0001";

/** Group failed stages by their parent build, newest build first. */
export function groupByBuild(stages: ProcessedStage[]): BuildGroup[] {
  const order: string[] = [];
  const groups = new Map<string, BuildGroup>();

  for (const stage of stages) {
    const key = `${stage.job_full_name}${GROUP_SEP}${stage.build_number}`;
    let group = groups.get(key);
    if (!group) {
      group = {
        key,
        job_full_name: stage.job_full_name,
        build_number: stage.build_number,
        latestTs: stage.timestamp,
        stages: [],
      };
      groups.set(key, group);
      order.push(key);
    }
    group.stages.push(stage);
    if (new Date(stage.timestamp) > new Date(group.latestTs)) {
      group.latestTs = stage.timestamp;
    }
  }

  const list = order.map((k) => {
    const g = groups.get(k)!;
    g.stages.sort((a, b) => {
      const ta = new Date(a.timestamp).getTime();
      const tb = new Date(b.timestamp).getTime();
      if (tb !== ta) return tb - ta;
      return (a.stage_name || "").localeCompare(b.stage_name || "");
    });
    return g;
  });

  list.sort((a, b) => new Date(b.latestTs).getTime() - new Date(a.latestTs).getTime());
  return list;
}
