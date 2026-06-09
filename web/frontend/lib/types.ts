// ── Structural log-filter telemetry ──────────────────────────────────────
//
// Mirrors the JSON the worker emits through ``filter_meta`` (see
// failure_analyzer_worker/filtering/orchestrator.py). All fields are
// optional because legacy rows (analyzed before the filter rewrite) lack
// the column, and the web-backend ships a trimmed projection.

export type FilterConfidence = "HIGH" | "MEDIUM" | "LOW";

export type FailureLocationKind =
  | "source"
  | "test"
  | "command"
  | "log"
  | "unknown";

export interface FailureLocation {
  kind: FailureLocationKind;
  file?: string;
  line?: number;
  column?: number;
  function?: string;
  test_name?: string;
  command?: string;
  detector?: string;
  confidence?: number;
  log_line_idx?: number;
  message?: string;
}

export interface FilterMetaSummary {
  confidence?: FilterConfidence;
  activated_detectors?: string[];
  primary_location?: FailureLocation | null;
  raw_chars?: number;
  body_chars?: number;
  body_tokens?: number;
  selected_count?: number;
  baseline_version?: string;
  collapse_stats?: Record<string, number>;
}

export interface DetectorInfo {
  name: string;
  priority: number;
  description: string;
  active: boolean;
}

export interface FilterConfigResponse {
  settings: {
    log_body_max_tokens: number;
    log_body_max_chars: number;
    filter_detectors: string;
    filter_max_active_detectors: number;
    use_self_baseline: boolean;
  };
  detectors: {
    active: DetectorInfo[];
    available: DetectorInfo[];
  };
  implementation: string;
}

export interface ProcessedStage {
  run_id: string;
  job_full_name: string;
  build_number: number;
  stage_name: string;
  stage_id: string | null;
  error_class: string;
  signature: string;
  cleaned_log: string;
  source: string;
  timestamp: string;
  analysis?: string;
  suggested_fix?: string;
  feedback_status?: string;
  filter_meta?: FilterMetaSummary;
}

export interface StatsResponse {
  by_error_class: Record<string, number>;
  by_feedback_status: Record<string, number>;
  total: number;
}

export interface ResultsResponse {
  count: number;
  results: ProcessedStage[];
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  sources?: { ci_logs: number; analyses: number; solutions: number };
  // True when this bubble is a derived summary (analysis / suggested fix),
  // not a real entry in session_messages.
  seed?: boolean;
}

export interface ChatResponse {
  answer: string;
  sources: { ci_logs: number; analyses: number; solutions: number };
}

export interface SessionSummary {
  id: string;
  job_full_name: string;
  build_number: number;
  stage_name: string;
  fingerprint: string;
  analysis: string;
  suggested_fix: string;
  filtered_logs: string;
  matched_solution: string;
  recommendation: string;
  feedback_status: string;
  created_at: string;
  updated_at: string;
}

export interface SessionMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
}

export interface SessionDetail {
  session: SessionSummary;
  messages: SessionMessage[];
}

export interface ListenerPollPayload {
  processed?: number;
  forwarded?: number;
  failures?: unknown[];
  analysis?: unknown;
}

export interface ListenerPollResponse {
  status: string;
  listener?: ListenerPollPayload;
}

export interface DiagnosticsResponse {
  database_target: { scheme?: string; host?: string | null; port?: number | null; dbname?: string };
  listener_base_url: string;
  listener_http_reachable: boolean;
  listener_http_error: string | null;
  worker_base_url: string;
  worker_http_reachable: boolean;
  worker_http_error: string | null;
}

export type KnowledgeNodeKind = "solution" | "error_class" | "job" | "stage";

export interface KnowledgeNode {
  id: string;
  kind: KnowledgeNodeKind;
  label: string;
  community_id?: number;
  degree?: number;
  // solution-only fields (optional everywhere else)
  doc_id?: string;
  fingerprint_text?: string;
  solution?: string;
  job_name?: string;
  stage_name?: string;
  build_number?: number;
  solution_score?: number;
  created_at?: string;
  error_class?: string;
  pg_session_count?: number;
}

export type KnowledgeEdgeKind =
  | "similar"
  | "resolves"
  | "occurs_in"
  | "in_job";

export interface KnowledgeEdge {
  kind: KnowledgeEdgeKind;
  source: string;
  target: string;
  weight?: number;
}

export interface KnowledgeCommunity {
  id: number;
  size: number;
  members: string[];
  label: string;
}

export interface KnowledgeGodNode {
  id: string;
  degree: number;
  label: string;
  error_class: string;
}

export interface KnowledgeStats {
  total_solutions: number;
  total_error_classes: number;
  total_jobs: number;
  total_stages: number;
  total_similarity_edges: number;
  isolated_solutions: number;
  communities: number;
  largest_community_size: number;
  avg_neighbours: number;
  god_nodes: KnowledgeGodNode[];
  top_error_classes: { error_class: string; count: number }[];
  // From Postgres enrichment (web-backend layer):
  accepted_sessions?: number;
  matched_sessions?: number;
  total_sessions?: number;
  kb_coverage?: number;
}

export interface KnowledgeGraphResponse {
  nodes: KnowledgeNode[];
  edges: KnowledgeEdge[];
  communities: KnowledgeCommunity[];
  stats: KnowledgeStats;
  params: { similarity_threshold: number; max_neighbours: number };
}
