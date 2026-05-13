"use client";

import { useEffect, useState } from "react";
import { fetchTaskTrace, type AgentTraceResponse, type AgentTraceTurn } from "@/lib/api";
import { useT } from "@/lib/i18n";

interface Props {
  taskId: string;
  /** Theme override (page-specific colors). Falls back to neutral palette. */
  palette?: {
    surface?: string;
    overlay?: string;
    border?: string;
    text1?: string;
    text2?: string;
    text3?: string;
    accent?: string;
  };
}

const DEFAULT_PALETTE = {
  surface: "#F8F9FA",
  overlay: "#FFFFFF",
  border: "rgba(0,0,0,0.08)",
  text1: "#111827",
  text2: "#6B7280",
  text3: "#9CA3AF",
  accent: "#B8922E",
};

function fmtTokens(n: number | undefined): string {
  if (!n) return "0";
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function fmtDuration(ms: number | undefined): string {
  if (!ms) return "0ms";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function summarizeToolCall(tc: { name: string; input: Record<string, any>; summary?: string }): string {
  const inp = tc.input || {};
  switch (tc.name) {
    case "grep": {
      const p = String(inp.pattern ?? "");
      const path = inp.path ? ` in ${inp.path}` : "";
      return `grep("${p}"${path})`;
    }
    case "read_file": {
      const path = inp.path ?? "";
      const off = inp.offset ? `, offset=${inp.offset}` : "";
      return `read_file("${path}"${off})`;
    }
    case "write_file":
      return `write_file("${inp.path ?? ""}")`;
    case "glob":
      return `glob("${inp.pattern ?? ""}")`;
    default:
      return tc.name + "(...)";
  }
}

export function AgentTraceBlock({ taskId, palette }: Props) {
  const t = useT();
  const P = { ...DEFAULT_PALETTE, ...(palette || {}) };
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [trace, setTrace] = useState<AgentTraceResponse | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [expandedTurns, setExpandedTurns] = useState<Record<number, boolean>>({});

  useEffect(() => {
    if (!open || trace || loading) return;
    setLoading(true);
    fetchTaskTrace(taskId)
      .then((data) => {
        if (data) setTrace(data);
        else setUnavailable(true);
      })
      .catch(() => setUnavailable(true))
      .finally(() => setLoading(false));
  }, [open, taskId, trace, loading]);

  if (!taskId) return null;

  const totalTurns = trace?.summary.total_turns ?? 0;
  const totalIn = trace?.summary.total_input_tokens ?? 0;
  const totalOut = trace?.summary.total_output_tokens ?? 0;
  const cacheRatio = trace?.summary.cache_hit_ratio ?? 0;
  const totalDur = trace?.summary.total_duration_ms ?? 0;

  return (
    <div>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-wider transition-colors"
        style={{
          background: open ? P.surface : P.overlay,
          border: `1px solid ${P.border}`,
          color: P.text2,
        }}
      >
        <span className="flex items-center gap-2">
          <svg
            className={`h-3 w-3 transition-transform ${open ? "rotate-90" : ""}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
          {t("Agent 执行轨迹")}
          {trace && (
            <span className="font-normal" style={{ color: P.text3 }}>
              · {totalTurns} turns · {fmtTokens(totalIn)}↑ {fmtTokens(totalOut)}↓ · cache {Math.round(cacheRatio * 100)}% · {fmtDuration(totalDur)}
            </span>
          )}
        </span>
        <span className="text-[10px] font-normal" style={{ color: P.text3 }}>
          {open ? t("折叠") : t("展开")}
        </span>
      </button>

      {open && (
        <div className="mt-2 space-y-1.5">
          {loading && (
            <div className="rounded-lg px-3 py-2 text-xs" style={{ background: P.overlay, color: P.text3 }}>
              {t("加载中...")}
            </div>
          )}
          {unavailable && !loading && (
            <div className="rounded-lg px-3 py-2 text-xs" style={{ background: P.overlay, color: P.text3 }}>
              {t("此任务无 Agent 轨迹（运行在 CLI 模式或尚未开始）")}
            </div>
          )}
          {trace && trace.turns.length === 0 && !loading && (
            <div className="rounded-lg px-3 py-2 text-xs" style={{ background: P.overlay, color: P.text3 }}>
              {t("Agent 还未执行任何 turn")}
            </div>
          )}
          {trace?.turns.map((turn: AgentTraceTurn) => {
            const isExpanded = expandedTurns[turn.turn];
            const hasError = !!turn.error;
            const reasonColor =
              hasError ? "#DC2626"
                : turn.stop_reason === "end_turn" ? "#16A34A"
                : turn.stop_reason === "tool_use" ? "#7C3AED"
                : turn.stop_reason === "max_tokens" ? "#CA8A04"
                : P.text3;
            return (
              <div
                key={turn.turn}
                className="rounded-lg px-3 py-2"
                style={{ background: P.overlay, border: `1px solid ${P.border}` }}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs font-mono" style={{ color: P.text2 }}>
                      Turn {turn.turn + 1}
                    </span>
                    <span
                      className="rounded px-1.5 py-0.5 text-[10px] font-medium"
                      style={{ background: "rgba(0,0,0,0.04)", color: reasonColor }}
                    >
                      {hasError ? turn.error : (turn.stop_reason || "—")}
                    </span>
                    {turn.duration_ms != null && (
                      <span className="text-[10px] font-mono" style={{ color: P.text3 }}>
                        {fmtDuration(turn.duration_ms)}
                      </span>
                    )}
                    {turn.usage && (turn.usage.input_tokens || turn.usage.output_tokens) ? (
                      <span className="text-[10px] font-mono" style={{ color: P.text3 }}>
                        {fmtTokens(turn.usage.input_tokens)}↑ / {fmtTokens(turn.usage.output_tokens)}↓
                        {turn.usage.cache_read_input_tokens
                          ? ` · cache ${fmtTokens(turn.usage.cache_read_input_tokens)}`
                          : ""}
                      </span>
                    ) : null}
                  </div>
                  {turn.tool_calls && turn.tool_calls.length > 0 && (
                    <button
                      onClick={() => setExpandedTurns((p) => ({ ...p, [turn.turn]: !isExpanded }))}
                      className="text-[10px]"
                      style={{ color: P.accent, background: "none", border: "none", cursor: "pointer", padding: 0 }}
                    >
                      {isExpanded ? t("收起参数") : t("展开参数")}
                    </button>
                  )}
                </div>

                {turn.tool_calls && turn.tool_calls.length > 0 && (
                  <div className="mt-1.5 space-y-1">
                    {turn.tool_calls.map((tc, i) => (
                      <div key={i} className="text-[11px]">
                        <span className="font-mono" style={{ color: tc.ok ? P.text2 : "#DC2626" }}>
                          {summarizeToolCall(tc)}
                        </span>
                        <span className="ml-1.5" style={{ color: P.text3 }}>
                          → {tc.ok ? (tc.summary || "ok") : `error: ${tc.error || "?"}`}
                        </span>
                        {isExpanded && (
                          <pre
                            className="mt-1 rounded px-2 py-1 text-[10px] overflow-x-auto"
                            style={{ background: P.surface, color: P.text3 }}
                          >
                            {JSON.stringify(tc.input, null, 2)}
                          </pre>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {hasError && turn.msg && (
                  <pre
                    className="mt-1.5 rounded px-2 py-1 text-[10px] overflow-x-auto"
                    style={{ background: "rgba(220,38,38,0.06)", color: "#991B1B" }}
                  >
                    {turn.msg}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
