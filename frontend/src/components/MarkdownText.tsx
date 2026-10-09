"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Renders text as Markdown (GFM: tables / strikethrough / autolinks). Falls back to plain text if content is empty.
 * Inherits parent text/color styles via CSS; table/heading/quote styles live in globals.css `.md-text`.
 */
export default function MarkdownText({ children }: { children: string }) {
  if (!children) return null;
  return (
    <div className="md-text">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Keep inline styles consistent with parent — no extra margins on <p>
          p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
          ul: ({ children }) => <ul className="mb-2 list-disc pl-5 last:mb-0">{children}</ul>,
          ol: ({ children }) => <ol className="mb-2 list-decimal pl-5 last:mb-0">{children}</ol>,
          li: ({ children }) => <li className="mb-0.5">{children}</li>,
          strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
          code: ({ children }) => (
            <code className="rounded bg-black/5 px-1 py-0.5 text-[0.9em] dark:bg-white/10">{children}</code>
          ),
          // 宽表横向滚动，不撑破抽屉
          table: ({ children }) => (
            <div className="md-table-wrap">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
