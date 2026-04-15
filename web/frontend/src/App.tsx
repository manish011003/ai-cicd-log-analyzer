import { useCallback, useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "./api";

type SimilarPast = { score: number; solution: string; fingerprint_text: string };

type SessionSummary = {
  id: string;
  job_full_name: string;
  build_number: number;
  stage_name: string;
  feedback_status: string;
  created_at: string;
};

type SessionDetail = {
  id: string;
  job_full_name: string;
  build_number: number;
  stage_name: string;
  build_url: string;
  fingerprint: string;
  analysis: string;
  suggested_fix: string;
  filtered_logs: string;
  matched_solution: string;
  match_score: number;
  recommendation: string;
  similar_past: SimilarPast[];
  feedback_status: string;
};

type MessageRow = { id: string; role: string; content: string; created_at: string };

function parseWorkerPayload(raw: string): Record<string, unknown> {
  const o = JSON.parse(raw) as { results?: Array<Record<string, unknown>> };
  const r = o.results?.[0];
  if (!r) throw new Error("JSON must include results[0] from the worker.");
  return {
    job_full_name: String(r.job_name ?? ""),
    build_number: Number(r.build_number ?? 0),
    stage_name: String(r.stage_name ?? ""),
    build_url: String(r.build_url ?? ""),
    fingerprint: String(r.fingerprint ?? ""),
    analysis: String(r.analysis ?? ""),
    suggested_fix: String(r.suggested_fix ?? ""),
    filtered_logs: String(r.filtered_logs ?? ""),
    matched_solution: String(r.matched_solution ?? ""),
    match_score: Number(r.match_score ?? 0),
    recommendation: String(r.recommendation ?? ""),
    similar_past: Array.isArray(r.similar_past) ? r.similar_past : [],
  };
}

function statusBadge(s: string) {
  if (!s) return <span className="badge badge-pending">new</span>;
  if (s === "accepted") return <span className="badge badge-accepted">accepted</span>;
  return <span className="badge badge-rejected">rejected</span>;
}

export function App() {
  const [list, setList] = useState<SessionSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(false);
  const [importJson, setImportJson] = useState("");
  const [showLogs, setShowLogs] = useState(false);
  const chatEnd = useRef<HTMLDivElement>(null);

  const [form, setForm] = useState({
    job_full_name: "",
    build_number: 0,
    stage_name: "",
    build_url: "",
    fingerprint: "",
    analysis: "",
    suggested_fix: "",
    filtered_logs: "",
    matched_solution: "",
    match_score: 0,
    recommendation: "",
    similar_past: [] as SimilarPast[],
  });

  const refreshList = useCallback(async () => {
    const rows = await api<SessionSummary[]>("/api/sessions");
    setList(rows);
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    const data = await api<{ session: SessionDetail; messages: MessageRow[] }>(`/api/sessions/${id}`);
    setSession(data.session);
    setMessages(data.messages);
  }, []);

  useEffect(() => {
    const sid = new URLSearchParams(window.location.search).get("session");
    if (sid) setSelected(sid);
  }, []);

  useEffect(() => { refreshList().catch((e: Error) => setErr(e.message)); }, [refreshList]);

  useEffect(() => {
    if (!selected) { setSession(null); setMessages([]); return; }
    loadDetail(selected).catch((e: Error) => setErr(e.message));
  }, [selected, loadDetail]);

  useEffect(() => { chatEnd.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  async function createSession() {
    setErr(null);
    const res = await api<{ id: string }>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ ...form }),
    });
    setSelected(res.id);
    await refreshList();
  }

  function applyImport() {
    setErr(null);
    try {
      const p = parseWorkerPayload(importJson);
      setForm(f => ({
        ...f, ...p,
        build_number: Number(p.build_number),
        match_score: Number(p.match_score),
        similar_past: (p.similar_past as SimilarPast[]) || [],
      }));
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
  }

  async function sendChat() {
    if (!selected || !chatInput.trim()) return;
    setErr(null);
    setSending(true);
    try {
      await api(`/api/sessions/${selected}/messages`, {
        method: "POST",
        body: JSON.stringify({ content: chatInput.trim() }),
      });
      setChatInput("");
      await loadDetail(selected);
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
    setSending(false);
  }

  async function feedback(decision: "accept" | "reject") {
    if (!selected) return;
    setErr(null);
    try {
      await api(`/api/sessions/${selected}/feedback`, {
        method: "POST",
        body: JSON.stringify({ decision }),
      });
      await loadDetail(selected);
      await refreshList();
    } catch (e) { setErr(String(e instanceof Error ? e.message : e)); }
  }

  return (
    <div className="app-shell">
      {/* ── Sidebar ── */}
      <aside className="sidebar">
        <div className="sidebar-header">
          <h1>CI Failure Analyzer</h1>
          <p>Jenkins build analysis</p>
        </div>
        <div className="session-list">
          {list.map(s => (
            <button
              key={s.id}
              type="button"
              className={`session-item${selected === s.id ? " active" : ""}`}
              onClick={() => setSelected(s.id)}
            >
              <div className="job-name">{s.job_full_name || "(unnamed)"} #{s.build_number}</div>
              <div className="meta">
                {s.stage_name ? <span>{s.stage_name}</span> : null}
                {statusBadge(s.feedback_status)}
              </div>
            </button>
          ))}
        </div>
      </aside>

      {/* ── Main ── */}
      <main className="main-content">
        {err && <div className="error-bar">{err}</div>}

        {!session ? (
          <>
            {/* ── Create form ── */}
            <div className="card">
              <div className="card-header">
                New session
                <button
                  type="button"
                  className="import-toggle"
                  style={{ float: "right" }}
                  onClick={() => setShowImport(v => !v)}
                >
                  {showImport ? "Hide" : "Import"} worker JSON
                </button>
              </div>
              <div className="card-body">
                {showImport && (
                  <div className="import-body" style={{ marginBottom: 16 }}>
                    <textarea
                      placeholder='{"status":"analyzed","results":[{...}]}'
                      value={importJson}
                      onChange={e => setImportJson(e.target.value)}
                    />
                    <button type="button" className="btn btn-ghost" style={{ marginTop: 8 }} onClick={applyImport}>
                      Parse into form
                    </button>
                  </div>
                )}
                <div className="form-grid">
                  <div className="form-field">
                    <label>Job name</label>
                    <input value={form.job_full_name} onChange={e => setForm({ ...form, job_full_name: e.target.value })} />
                  </div>
                  <div className="form-field">
                    <label>Build #</label>
                    <input type="number" value={form.build_number} onChange={e => setForm({ ...form, build_number: Number(e.target.value) })} />
                  </div>
                  <div className="form-field">
                    <label>Stage</label>
                    <input value={form.stage_name} onChange={e => setForm({ ...form, stage_name: e.target.value })} />
                  </div>
                  <div className="form-field">
                    <label>Build URL</label>
                    <input value={form.build_url} onChange={e => setForm({ ...form, build_url: e.target.value })} />
                  </div>
                  <div className="form-field full">
                    <label>Fingerprint</label>
                    <input value={form.fingerprint} onChange={e => setForm({ ...form, fingerprint: e.target.value })} />
                  </div>
                  <div className="form-field full">
                    <label>Analysis</label>
                    <textarea value={form.analysis} onChange={e => setForm({ ...form, analysis: e.target.value })} />
                  </div>
                  <div className="form-field full">
                    <label>Suggested fix</label>
                    <textarea value={form.suggested_fix} onChange={e => setForm({ ...form, suggested_fix: e.target.value })} />
                  </div>
                  <div className="form-field full">
                    <label>Filtered logs</label>
                    <textarea value={form.filtered_logs} onChange={e => setForm({ ...form, filtered_logs: e.target.value })} />
                  </div>
                </div>
                <div style={{ marginTop: 16 }}>
                  <button type="button" className="btn btn-primary" onClick={() => void createSession().catch((e: Error) => setErr(e.message))}>
                    Create session
                  </button>
                </div>
              </div>
            </div>

            <div className="empty-state" style={{ height: "auto", marginTop: 40 }}>
              Select a session from the sidebar or create a new one.
            </div>
          </>
        ) : (
          <>
            {/* ── Header ── */}
            <div className="detail-header">
              <h2>
                {session.job_full_name} #{session.build_number}
                <span className="stage-tag">{session.stage_name || "Build"}</span>
              </h2>
              <div className="detail-meta">
                {session.build_url && (
                  <a href={session.build_url} target="_blank" rel="noreferrer">View in Jenkins ↗</a>
                )}
                <span>{session.recommendation === "verified_past_solution" ? "Matched past solution" : "Fresh analysis"}</span>
                {session.match_score > 0 && <span>Score: {session.match_score.toFixed(2)}</span>}
                {statusBadge(session.feedback_status)}
              </div>
            </div>

            {/* ── Feedback ── */}
            <div className="feedback-bar">
              <span style={{ flex: 1, fontSize: 13, color: "var(--text-secondary)" }}>
                Is this fix correct?
              </span>
              <button type="button" className="btn btn-accept" onClick={() => void feedback("accept")}>
                ✓ Accept &amp; store
              </button>
              <button type="button" className="btn btn-reject" onClick={() => void feedback("reject")}>
                ✗ Reject
              </button>
            </div>

            {/* ── Analysis ── */}
            <div className="card">
              <div className="card-header">Analysis</div>
              <div className="card-body">
                <div className="markdown-body">
                  <Markdown remarkPlugins={[remarkGfm]}>{session.analysis || "No analysis available."}</Markdown>
                </div>
              </div>
            </div>

            {/* ── Suggested fix ── */}
            <div className="card">
              <div className="card-header">Suggested fix</div>
              <div className="card-body">
                <div className="markdown-body">
                  <Markdown remarkPlugins={[remarkGfm]}>{session.suggested_fix || "No fix suggested."}</Markdown>
                </div>
              </div>
            </div>

            {/* ── Similar past solutions ── */}
            {session.similar_past?.length > 0 && (
              <div className="card">
                <div className="card-header">Similar past solutions (ES)</div>
                <div className="card-body">
                  {session.similar_past.map((sp, i) => (
                    <div key={i} style={{ marginBottom: 12, paddingBottom: 12, borderBottom: i < session.similar_past.length - 1 ? "1px solid var(--border)" : "none" }}>
                      <div style={{ fontSize: 12, fontWeight: 700, color: "var(--text-secondary)", marginBottom: 4 }}>
                        Match #{i + 1} — score {Number(sp.score).toFixed(4)}
                      </div>
                      <div className="markdown-body">
                        <Markdown remarkPlugins={[remarkGfm]}>{sp.solution}</Markdown>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* ── Filtered logs ── */}
            {session.filtered_logs && (
              <div className="card">
                <div className="card-header">
                  Filtered logs
                  <button
                    type="button"
                    className="import-toggle"
                    style={{ float: "right" }}
                    onClick={() => setShowLogs(v => !v)}
                  >
                    {showLogs ? "Collapse" : "Expand"}
                  </button>
                </div>
                {showLogs && (
                  <div className="card-body" style={{ padding: 0 }}>
                    <pre className="logs-pre">{session.filtered_logs}</pre>
                  </div>
                )}
              </div>
            )}

            {/* ── Fingerprint ── */}
            {session.fingerprint && (
              <div className="card">
                <div className="card-header">Fingerprint</div>
                <div className="card-body">
                  <code style={{ fontSize: 13, wordBreak: "break-all" }}>{session.fingerprint}</code>
                </div>
              </div>
            )}

            {/* ── Chat ── */}
            <div className="card">
              <div className="card-header">
                Chat history
                <span style={{ float: "right", fontWeight: 400, textTransform: "none", letterSpacing: 0, fontSize: 12 }}>
                  {messages.length} message{messages.length !== 1 ? "s" : ""}
                </span>
              </div>
              {messages.length === 0 ? (
                <div className="chat-empty">
                  No messages yet. Ask a question about the analysis or request a different approach.
                </div>
              ) : (
                <div className="chat-messages">
                  {messages.map(m => (
                    <div key={m.id} className={`chat-bubble ${m.role}`}>
                      <div className="role-label">{m.role}</div>
                      <Markdown remarkPlugins={[remarkGfm]}>{m.content}</Markdown>
                    </div>
                  ))}
                  <div ref={chatEnd} />
                </div>
              )}
              <div className="chat-input-area">
                <textarea
                  placeholder="Ask for clarification, reject reasoning, or provide more context…"
                  value={chatInput}
                  onChange={e => setChatInput(e.target.value)}
                  onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void sendChat(); } }}
                />
                <button type="button" className="btn btn-primary" disabled={sending} onClick={() => void sendChat()}>
                  {sending ? "…" : "Send"}
                </button>
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
