"use client";

import { useEffect, useRef, useState } from "react";
import { fetchSession, sendChatMessage } from "@/lib/api";
import type { ChatMessage } from "@/lib/types";

interface Props {
  open: boolean;
  activeRunId?: string;
  sessionId?: string;
  onClose: () => void;
}

interface SessionContext {
  job_full_name: string;
  build_number: number;
  stage_name: string;
  feedback_status: string;
}

const WELCOME: ChatMessage = {
  role: "assistant",
  content: "Hi! I can help investigate CI failures. Ask about root cause, impacted stage, or recommended remediation.",
};

export default function ChatPanel({ open, activeRunId, sessionId, onClose }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([WELCOME]);
  const [sessionCtx, setSessionCtx] = useState<SessionContext | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const prevSessionRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (sessionId === prevSessionRef.current) return;
    prevSessionRef.current = sessionId;

    if (!sessionId) {
      setMessages([WELCOME]);
      setSessionCtx(null);
      return;
    }

    let cancelled = false;
    setLoadingHistory(true);
    void fetchSession(sessionId)
      .then((data) => {
        if (cancelled) return;
        const s = data.session;
        setSessionCtx({
          job_full_name: String(s.job_full_name ?? ""),
          build_number: Number(s.build_number ?? 0),
          stage_name: String(s.stage_name ?? ""),
          feedback_status: String(s.feedback_status ?? ""),
        });
        if (data.messages.length > 0) {
          setMessages(
            data.messages.map((m) => ({
              role: m.role as "user" | "assistant",
              content: m.content,
            })),
          );
        } else {
          setMessages([
            {
              role: "assistant",
              content: `Loaded session for **${s.job_full_name} #${s.build_number}** (${s.stage_name || "unknown stage"}). Ask me anything about this failure.`,
            },
          ]);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setMessages([WELCOME]);
          setSessionCtx(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });

    return () => { cancelled = true; };
  }, [sessionId]);

  const effectiveRunId = sessionId ?? activeRunId;

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;

    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setInput("");
    setSending(true);
    try {
      const resp = await sendChatMessage(text, effectiveRunId);
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
    <div className="flex h-full flex-col border-l border-zinc-800 bg-zinc-950">
      <div className="flex items-center justify-between border-b border-zinc-800 px-4 py-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-zinc-100">CI Assistant</h2>
          {sessionCtx ? (
            <p className="truncate text-[10px] text-zinc-500">
              {sessionCtx.job_full_name} #{sessionCtx.build_number} &middot; {sessionCtx.stage_name || "unknown"}
              {sessionCtx.feedback_status ? ` &middot; ${sessionCtx.feedback_status}` : ""}
            </p>
          ) : (
            <p className="text-[10px] text-zinc-500">
              {effectiveRunId ? `Context: ${effectiveRunId.slice(0, 8)}...` : "General mode"}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 rounded-md p-1 text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-300"
        >
          x
        </button>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
        {loadingHistory && (
          <div className="py-8 text-center">
            <div className="inline-block h-5 w-5 animate-spin rounded-full border-2 border-zinc-700 border-t-zinc-300" />
            <p className="mt-2 text-xs text-zinc-500">Loading conversation...</p>
          </div>
        )}

        {!loadingHistory &&
          messages.map((msg, i) => (
            <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[90%] rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed ${
                  msg.role === "user"
                    ? "rounded-br-md bg-indigo-600 text-white"
                    : "rounded-bl-md border border-zinc-800 bg-zinc-900 text-zinc-200"
                }`}
              >
                <pre className="whitespace-pre-wrap break-words font-sans">{msg.content}</pre>
              </div>
            </div>
          ))}

        {sending && (
          <div className="flex justify-start">
            <div className="rounded-2xl rounded-bl-md border border-zinc-800 bg-zinc-900 px-4 py-3 text-xs text-zinc-400">
              Thinking...
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <div className="border-t border-zinc-800 p-3">
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
            className="flex-1 rounded-xl border border-zinc-700 bg-zinc-900 px-4 py-2.5 text-sm text-zinc-100 placeholder-zinc-600 outline-none transition-colors focus:border-indigo-500 disabled:opacity-50"
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
