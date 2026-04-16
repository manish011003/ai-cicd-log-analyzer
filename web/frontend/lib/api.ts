import type {
  ChatResponse,
  DiagnosticsResponse,
  ListenerPollResponse,
  ResultsResponse,
  StatsResponse,
} from "./types";

const BASE = "/api";
const AGENT = "/agent";

async function readJson<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const res = await fetch(input, init);
  if (!res.ok) {
    const message = await res.text();
    throw new Error(message || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export async function fetchResults(
  limit = 20,
  job?: string,
  errorClass?: string,
): Promise<ResultsResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (job) params.set("job", job);
  if (errorClass) params.set("error_class", errorClass);
  return readJson<ResultsResponse>(`${BASE}/results?${params}`);
}

export async function fetchStats(): Promise<StatsResponse> {
  return readJson<StatsResponse>(`${BASE}/stats`);
}

export async function pollListenerOnce(): Promise<ListenerPollResponse> {
  return readJson<ListenerPollResponse>(`${BASE}/listener/poll-once`, {
    method: "POST",
  });
}

export async function fetchDiagnostics(): Promise<DiagnosticsResponse> {
  return readJson<DiagnosticsResponse>(`${BASE}/diagnostics`);
}

export async function setFeedback(
  runId: string,
  decision: "accept" | "reject",
): Promise<{ status: string }> {
  return readJson<{ status: string }>(`${BASE}/sessions/${encodeURIComponent(runId)}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });
}

export async function sendChatMessage(
  message: string,
  runId?: string,
): Promise<ChatResponse> {
  return readJson<ChatResponse>(`${AGENT}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, run_id: runId ?? "" }),
  });
}
