"use client";

import { useMemo, useState } from "react";
import Markdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";

interface Props {
  content: string;
  className?: string;
}

function CodeBlock({ code, language }: { code: string; language?: string }) {
  const [copied, setCopied] = useState(false);

  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="overflow-hidden rounded-lg border border-zinc-300/80 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-950/70">
      <div className="flex items-center justify-between border-b border-zinc-300/80 px-3 py-2 dark:border-zinc-800">
        <span className="font-mono text-xs text-zinc-600 dark:text-zinc-400">{language || "text"}</span>
        <Button variant="ghost" size="sm" onClick={() => void copyCode()} className="h-7 px-2 text-xs">
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <pre className="overflow-x-auto p-3 text-[0.85rem] leading-relaxed">
        <code className="font-mono">{code}</code>
      </pre>
    </div>
  );
}

export default function MarkdownRenderer({ content, className }: Props) {
  const safeContent = useMemo(() => content || "", [content]);

  return (
    <div className={className}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          pre({ children }) {
            const child = children && Array.isArray(children) ? children[0] : children;
            if (!child || typeof child !== "object" || !("props" in child)) return <pre>{children}</pre>;
            const codeChild = child as { props?: { className?: string; children?: string | string[] } };
            const raw = codeChild.props?.children;
            const code = Array.isArray(raw) ? raw.join("") : String(raw ?? "");
            const className = codeChild.props?.className ?? "";
            const language = className.replace("language-", "");
            return <CodeBlock code={code.replace(/\n$/, "")} language={language} />;
          },
          code({ children, className }) {
            const isInline = !className;
            if (!isInline) return <code className={className}>{children}</code>;
            return (
              <code className="rounded bg-zinc-200 px-1.5 py-0.5 font-mono text-[0.85em] text-zinc-800 dark:bg-zinc-800 dark:text-zinc-100">
                {children}
              </code>
            );
          },
          p({ children }) {
            return <p className="leading-[1.6] text-zinc-800 dark:text-zinc-100">{children}</p>;
          },
          li({ children }) {
            return <li className="leading-[1.6] text-zinc-800 dark:text-zinc-100">{children}</li>;
          },
        }}
      >
        {safeContent}
      </Markdown>
    </div>
  );
}
