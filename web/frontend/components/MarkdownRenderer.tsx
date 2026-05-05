"use client";

import React, { useMemo, useState } from "react";
import Markdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";

interface Props {
  content: string;
  className?: string;
}

/**
 * Recursively walk a React node tree and pull out the underlying text.
 *
 * Why this exists: ``rehype-highlight`` rewrites the children of every
 * fenced ``<code>`` element from a single text string into a tree of
 * ``<span class="hljs-…">`` tokens. Naively doing ``children.join("")``
 * stringifies each React element to ``"[object Object]"``, which is how
 * the broken "[object Object][object Object]" code blocks were appearing
 * in the UI. We use this only to recover the plain text for the Copy
 * button — the highlighted JSX is still rendered as-is.
 */
function getNodeText(node: React.ReactNode): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(getNodeText).join("");
  if (React.isValidElement(node)) {
    const props = node.props as { children?: React.ReactNode };
    return getNodeText(props.children);
  }
  return "";
}

function CodeBlock({
  language,
  text,
  children,
}: {
  language: string;
  text: string;
  children: React.ReactNode;
}) {
  const [copied, setCopied] = useState(false);

  const copyCode = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      setCopied(false);
    }
  };

  const label = language && language !== "text" ? language : "text";

  return (
    <div className="overflow-hidden rounded-lg border border-zinc-300/80 bg-zinc-50 dark:border-zinc-800 dark:bg-zinc-950/70">
      <div className="flex items-center justify-between border-b border-zinc-300/80 px-3 py-2 dark:border-zinc-800">
        <span className="font-mono text-xs text-zinc-600 dark:text-zinc-400">{label}</span>
        <Button variant="ghost" size="sm" onClick={() => void copyCode()} className="h-7 px-2 text-xs">
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <pre className="overflow-x-auto p-3 text-[0.85rem] leading-relaxed">
        <code className={`hljs font-mono${language && language !== "text" ? ` language-${language}` : ""}`}>
          {children}
        </code>
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
            // ``children`` is the React rendering of the inner ``<code>`` element
            // already produced by react-markdown + rehype-highlight. We extract
            // it, parse the language out of its className, and forward the
            // highlighted children verbatim to ``CodeBlock`` so colours survive.
            const arr = React.Children.toArray(children);
            const codeNode = arr.find(React.isValidElement) as
              | React.ReactElement<{ className?: string; children?: React.ReactNode }>
              | undefined;
            if (!codeNode) return <pre>{children}</pre>;

            const cls = codeNode.props.className ?? "";
            // After rehype-highlight the class is e.g. "hljs language-bash";
            // a plain replace("language-", "") would yield "hljs bash" (which
            // is what the UI was showing). Match the language token directly.
            const langMatch = cls.match(/language-([\w-]+)/);
            const language = langMatch ? langMatch[1] : "text";
            const text = getNodeText(codeNode.props.children);

            return (
              <CodeBlock language={language} text={text.replace(/\n+$/, "")}>
                {codeNode.props.children}
              </CodeBlock>
            );
          },
          code({ children, className }) {
            // Only inline (single-backtick) code reaches here without a class.
            // Fenced blocks have a "language-…" class injected by rehype-highlight
            // and are handled by the ``pre`` override above; we still pass them
            // through unchanged in case ``pre`` ever renders them directly.
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
