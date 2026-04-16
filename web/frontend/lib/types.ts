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
  role: "user" | "assistant";
  content: string;
  sources?: { ci_logs: number; analyses: number; solutions: number };
}

export interface ChatResponse {
  answer: string;
  sources: { ci_logs: number; analyses: number; solutions: number };
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
