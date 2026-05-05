"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchSessionDetail, sendChatMessage } from "@/lib/api";
import type { ChatMessage, SessionDetail } from "@/lib/types";

interface Props {
  open: boolean;
  activeRunId?: string;
  onClose: () => void;
}

const GREETING: ChatMessage = {
  role: "assistant",
  content:
    "Hi! I can help investigate CI failures. Ask about root cause, impacted stage, or recommended remediation.",
  seed: true,
};

function buildSeedMessages(detail: SessionDetail | null): ChatMessage[] {
  if (!detail) return [GREETING];
  const { session } = detail;
  const header = [
    `Investigating ${session.job_full_name} #${session.build_number}`,
    session.stage_name ? `Stage: ${session.stage_name}` : null,
  ]
    .filter(Boolean)
    .join("  ·  ");

  const analysisBlock = session.analysis
    ? `\n\n## Analysis\n${session.analysis.trim()}`
    : "";
  const fixBlock = session.suggested_fix
    ? `\n\n## Suggested Fix\n${session.suggested_fix.trim()}`
    : "";

  const seed: ChatMessage = {
    role: "assistant",
    content: `${header}${analysisBlock}${fixBlock}`.trim() || GREETING.content,
    seed: true,
  };

  const persisted: ChatMessage[] = detail.messages.map((m) => ({
    role: m.role,
    content: m.content,
  }));

  return [seed, ...persisted];
}

export default function ChatPanel({ open, activeRunId, onClose }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([GREETING]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [useFullLog, setUseFullLog] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Load persisted messages whenever the panel is opened for a (different) run.
  // We intentionally re-fetch when `open` flips to true so the user never sees
  // a stale conversation after minimising + reopening the panel.
  const loadHistory = useCallback(async (runId: string | undefined) => {
    if (!runId) {
      setMessages([GREETING]);
      setHistoryError(null);
      return;
    }
    setLoadingHistory(true);
    setHistoryError(null);
    try {
      const detail = await fetchSessionDetail(runId);
      setMessages(buildSeedMessages(detail));
    } catch (err) {
      setHistoryError(err instanceof Error ? err.message : "Failed to load chat history.");
      setMessages([GREETING]);
    } finally {
      setLoadingHistory(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void loadHistory(activeRunId);
  }, [open, activeRunId, loadHistory]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loadingHistory]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;

    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setInput("");
    setSending(true);
    try {
      const resp = await sendChatMessage(text, activeRunId, { useFullLog });
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: resp.answer, sources: resp.sources },
      ]);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Chat service is unavailable.";
      setMessages((prev) => [...prev, { role: "assistant", content: message }]);
    } finally {
      setSending(false);
    }
  };

  if (!open) return null;

  return (
    <div className="flex h-full flex-col border-l border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-950">
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3 dark:border-slate-800">
        <div>
          <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">CI Assistant</h2>
          <p className="text-[10px] text-slate-500 dark:text-slate-400">
            {activeRunId ? `Context: ${activeRunId}` : "General mode"}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md p-1 text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200"
        >
          x
        </button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
        {loadingHistory && (
          <div className="rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-[11px] text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
            Loading chat history...
          </div>
        )}

        {historyError && (
          <div className="rounded-2xl border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] text-rose-700 dark:border-rose-900/60 dark:bg-rose-950/40 dark:text-rose-300">
            {historyError}
          </div>
        )}

        {messages.map((msg, i) => (
          <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
            <div
              className={`max-w-[90%] rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed ${
                msg.role === "user"
                  ? "rounded-br-md bg-indigo-600 text-white"
                  : msg.seed
                    ? "rounded-bl-md border border-indigo-200 bg-indigo-50 text-slate-800 dark:border-indigo-900/60 dark:bg-indigo-950/40 dark:text-slate-200"
                    : "rounded-bl-md border border-slate-200 bg-slate-50 text-slate-800 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200"
              }`}
            >
              <pre className="whitespace-pre-wrap break-words font-sans">{msg.content}</pre>
            </div>
          </div>
        ))}

        {sending && (
          <div className="flex justify-start">
            <div className="rounded-2xl rounded-bl-md border border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
              Thinking...
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="border-t border-slate-200 p-3 dark:border-slate-800">
        {activeRunId && (
          <label className="mb-2 flex cursor-pointer items-center gap-2 text-[11px] text-slate-600 dark:text-slate-300">
            <input
              type="checkbox"
              checked={useFullLog}
              onChange={(e) => setUseFullLog(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500 dark:border-slate-600"
            />
            <span>Include raw log excerpt (more context, slower, uses more LLM tokens)</span>
          </label>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void handleSend();
          }}
          className="flex items-center gap-2"
        >
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask about a failure..."
            disabled={sending || loadingHistory}
            className="flex-1 rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-sm text-slate-900 placeholder-slate-500 outline-none transition-colors focus:border-indigo-500 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:placeholder-slate-500"
          />
          <button
            type="submit"
            disabled={sending || loadingHistory || !input.trim()}
            className="rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-medium text-white transition-all hover:bg-indigo-500 disabled:opacity-30"
          >
            Send
          </button>
        </form>
      </div>
    </div>
  );
}
