"use client";

/**
 * Shared "工单分析结果" view — the chat-style AI analysis conversation used by
 * BOTH the Ticket Analysis page (/) and the Ticket Tracking page (/tracking).
 *
 * Canonical UI = the richer首页 card: problem type, confidence, model + API/CLI,
 * needs-engineer tri-state badge, root cause, key evidence, agent trace, suggested
 * reply, deep-analysis CTA, and the follow-up conversation input.
 *
 * Page-specific interactive bits are injected via slots:
 *   - renderEngineerFeedback(r): the客服反馈 widget (首页 only — needs its own submit wiring)
 *   - onSetLang: when provided, renders the in-panel CN/EN toggle (首页); omit to follow site lang
 */

import type { Dispatch, ReactNode, SetStateAction } from "react";
import { useState } from "react";
import { useT } from "@/lib/i18n";
import MarkdownText from "@/components/MarkdownText";
import { AgentTraceBlock } from "@/components/AgentTraceBlock";
import { S } from "@/components/IssueComponents";
import { formatLocalTime, type AnalysisResult, type TaskProgress } from "@/lib/api";

function CodeRoutingBadge({ cr, t }: { cr: NonNullable<NonNullable<AnalysisResult["log_metadata"]>["code_routing"]>; t: (k: string) => string }) {
  const { source, family, version, repo } = cr;
  let emoji = "⚪";
  let label = t("无源码（logs-only）");
  let bg = "rgba(107,114,128,0.08)";
  let color = "#6B7280";
  let border = "rgba(107,114,128,0.2)";

  if (source === "resolved") {
    const ver = version ? ` ${version}` : "";
    const repoSuffix = repo ? ` · ${repo}` : "";
    if (family === "native") {
      emoji = "🟢"; label = `Native${ver}${repoSuffix}`;
      bg = "rgba(34,197,94,0.10)"; color = "#16A34A"; border = "rgba(34,197,94,0.25)";
    } else if (family === "flutter") {
      emoji = "🔵"; label = `Flutter${ver}${repoSuffix}`;
      bg = "rgba(96,165,250,0.10)"; color = "#2563EB"; border = "rgba(96,165,250,0.25)";
    } else if (family === "web") {
      emoji = "🟢"; label = `Web${ver}${repoSuffix}`;
      bg = "rgba(34,197,94,0.10)"; color = "#16A34A"; border = "rgba(34,197,94,0.25)";
    } else if (family === "desktop") {
      emoji = "🟢"; label = `Desktop${ver}${repoSuffix}`;
      bg = "rgba(34,197,94,0.10)"; color = "#16A34A"; border = "rgba(34,197,94,0.25)";
    }
  } else if (source === "fallback-app") {
    const repoSuffix = repo ? ` · ${repo}` : "";
    emoji = "🟡"; label = `${t("Flutter（默认兜底）")}${repoSuffix}`;
    bg = "rgba(234,179,8,0.10)"; color = "#B45309"; border = "rgba(234,179,8,0.30)";
  }

  return (
    <span
      className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-[10px] font-medium"
      style={{ background: bg, color, border: `1px solid ${border}` }}
      title={`source: ${source || "none"}, family: ${family || "-"}, confidence: ${cr.confidence || "-"}`}
    >
      {emoji} {label}
    </span>
  );
}

function ConfBadge({ c }: { c: string }) {
  const m: Record<string, { bg: string; color: string; border: string }> = {
    high:   { bg: "rgba(34,197,94,0.12)",   color: "#16A34A", border: "rgba(34,197,94,0.25)" },
    medium: { bg: "rgba(234,179,8,0.12)",   color: "#FCD34D", border: "rgba(234,179,8,0.25)" },
    low:    { bg: "rgba(239,68,68,0.12)",   color: "#DC2626", border: "rgba(239,68,68,0.25)" },
  };
  const s = m[c] || m.low;
  return (
    <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
      {c}
    </span>
  );
}

// ---- Usage / cost formatting helpers (功能 1) ----
type UsageEntry = NonNullable<AnalysisResult["usage_breakdown"]>[string];

function entryTokens(e?: UsageEntry): number {
  if (!e) return 0;
  return (e.input_tokens || 0) + (e.output_tokens || 0)
    + (e.cache_read_input_tokens || 0) + (e.cache_creation_input_tokens || 0);
}

function fmtTokens(n: number): string {
  return n.toLocaleString("en-US");
}

function fmtCost(c: number): string {
  if (c <= 0) return "$0.00";
  if (c < 0.01) return "<$0.01";
  return `$${c.toFixed(2)}`;
}

/** Per-card usage meter: 「本次：1,234 tokens · $0.05」, hover/expand → breakdown. */
function UsageMeter({ r }: { r: AnalysisResult }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const tokens = r.total_tokens || 0;
  const cost = r.total_cost_usd || 0;
  const missing = !tokens || r.cost_source === "partial";
  const breakdown = r.usage_breakdown || {};
  const hasBreakdown = Object.keys(breakdown).length > 0;

  if (missing) {
    return (
      <div className="text-[10px]" style={{ color: S.text3 }}>
        {t("本次")}：—
      </div>
    );
  }

  return (
    <div className="text-[10px]" style={{ color: S.text3 }}>
      <button
        type="button"
        onClick={() => hasBreakdown && setOpen((v) => !v)}
        title={hasBreakdown ? t("用量明细") : undefined}
        className="inline-flex items-center gap-1"
        style={{ background: "none", border: "none", padding: 0, color: S.text3, cursor: hasBreakdown ? "pointer" : "default" }}>
        <span className="tabular-nums">
          {t("本次")}：{fmtTokens(tokens)} tokens · {fmtCost(cost)}
        </span>
        {hasBreakdown && (
          <svg className={`h-2.5 w-2.5 transition-transform ${open ? "rotate-90" : ""}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
        )}
      </button>
      {open && hasBreakdown && (
        <div className="mt-1 space-y-0.5 rounded-md px-2 py-1.5"
          style={{ background: S.overlay, border: `1px solid ${S.borderSm}` }}>
          {Object.entries(breakdown).map(([key, e]) => {
            const label = key === "condenser" ? t("凝缩器") : key === "agent" ? t("Agent") : key;
            return (
              <div key={key} className="flex items-center justify-between gap-3">
                <span style={{ color: S.text2 }}>
                  {label}
                  {e?.model && <span className="ml-1" style={{ color: S.text3 }}>({e.model.replace(/^claude-/, "").replace(/-\d{8}$/, "")})</span>}
                </span>
                <span className="tabular-nums" style={{ color: S.text3 }}>
                  {fmtTokens(entryTokens(e))} tokens · {fmtCost(e?.cost_usd || 0)}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export interface AnalysisResultViewProps {
  analyses: AnalysisResult[];
  issueId: string;
  /** Content language (root cause / reply). Local panel state on 首页, site lang on tracking. */
  lang: "cn" | "en";
  /** When provided, renders the in-panel CN/EN toggle. */
  onSetLang?: (l: "cn" | "en") => void;
  collapsedEvidence: Record<string, boolean>;
  setCollapsedEvidence: Dispatch<SetStateAction<Record<string, boolean>>>;
  /** Latest task for this issue — drives follow-up progress + input disabling. */
  activeTask?: TaskProgress | null;
  followupText: string;
  setFollowupText: (s: string) => void;
  followupSubmitting: boolean;
  onStartFollowup: (issueId: string, text: string) => void;
  /** direction = optional new analysis direction collected from the deep-analysis dialog. */
  onDeepAnalysis: (issueId: string, direction?: string) => void;
  onCopy: (text: string) => void;
  /** 首页-only interactive engineer-label feedback widget. */
  renderEngineerFeedback?: (r: AnalysisResult) => ReactNode;
}

// Known Chinese system-error strings that have no _en field
const ZH_SYS: Record<string, string> = {
  "未知": "Unknown",
  "分析未产出结构化结果": "Analysis did not produce structured results",
  "服务器重启，任务中断": "Server restart — task interrupted",
  "分析超时": "Analysis timeout",
  "Agent 不可用": "Agent unavailable",
};

export function AnalysisResultView({
  analyses,
  issueId,
  lang,
  onSetLang,
  collapsedEvidence,
  setCollapsedEvidence,
  activeTask,
  followupText,
  setFollowupText,
  followupSubmitting,
  onStartFollowup,
  onDeepAnalysis,
  onCopy,
  renderEngineerFeedback,
}: AnalysisResultViewProps) {
  const t = useT();
  const isAnalyzing = !!activeTask && !["done", "failed"].includes(activeTask.status);
  // Deep-analysis dialog: collect an optional "new analysis direction" before firing onDeepAnalysis.
  const [deepDialogOpen, setDeepDialogOpen] = useState(false);
  const [deepDirection, setDeepDirection] = useState("");

  return (
    <>
      {/* Section header (+ optional language toggle) */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
            {lang === "cn" ? "AI 分析结果" : "AI Analysis"}
            {analyses.length > 1 && <span className="ml-1.5 text-[10px] font-normal" style={{ color: S.text3 }}>({analyses.length})</span>}
          </h3>
          {onSetLang && (
            <div className="flex items-center gap-0.5 rounded-md p-0.5" style={{ background: S.overlay }}>
              <button onClick={() => onSetLang("cn")}
                className="rounded px-2 py-0.5 text-[11px] font-medium transition-all"
                style={lang === "cn" ? { background: S.surface, color: S.text1 } : { color: S.text3 }}>
                中文
              </button>
              <button onClick={() => onSetLang("en")}
                className="rounded px-2 py-0.5 text-[11px] font-medium transition-all"
                style={lang === "en" ? { background: S.surface, color: S.text1 } : { color: S.text3 }}>
                EN
              </button>
            </div>
          )}
        </div>
      </section>

      {/* Chat-style conversation flow (chronological: oldest first) */}
      {[...analyses].reverse().map((r, idx) => {
        const chronoIdx = analyses.length - 1 - idx;
        const isLatest = chronoIdx === 0;
        const problemType = lang === "en"
          ? (r.problem_type_en || ZH_SYS[r.problem_type] || r.problem_type)
          : r.problem_type;
        const rootCause = lang === "en"
          ? (r.root_cause_en || ZH_SYS[r.root_cause] || r.root_cause)
          : r.root_cause;
        const userReply = lang === "en" ? (r.user_reply_en || r.user_reply) : r.user_reply;
        const hasEnTranslation = !!(r.problem_type_en && r.root_cause_en);
        const isFollowup = !!(r as { followup_question?: string }).followup_question;
        const followupQuestion = (r as { followup_question?: string }).followup_question;
        const createdAt = (r as { created_at?: string }).created_at;
        const evidenceKey = r.task_id || `ev-${idx}`;
        const evidenceCollapsed = collapsedEvidence[evidenceKey] !== false; // default collapsed
        return (
          <div key={r.task_id || idx} className="space-y-3">
            {/* User's follow-up question — right-aligned bubble */}
            {isFollowup && (
              <div className="flex justify-end">
                <div className="max-w-[85%] space-y-1">
                  <div className="rounded-2xl rounded-br-sm px-4 py-2.5 text-sm"
                    style={{ background: "rgba(167,139,250,0.10)", color: S.text1, border: "1px solid rgba(167,139,250,0.18)" }}>
                    {followupQuestion}
                  </div>
                  {createdAt && (
                    <div className="text-right text-[10px]" style={{ color: S.text3 }}>{formatLocalTime(createdAt)}</div>
                  )}
                </div>
              </div>
            )}

            {/* AI analysis card — left-aligned */}
            <div className="space-y-3 rounded-lg p-4"
              style={{
                background: S.surface,
                border: `1px solid ${S.border}`,
                borderLeft: isLatest ? `3px solid ${S.accent}` : `3px solid ${S.border}`,
              }}>
              {/* Badge row */}
              <div className="flex items-center gap-2 flex-wrap">
                <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
                  style={isFollowup
                    ? { background: "rgba(167,139,250,0.12)", color: "#C4B5FD", border: "1px solid rgba(167,139,250,0.25)" }
                    : { background: "rgba(14,124,134,0.08)", color: S.accent, border: "1px solid rgba(14,124,134,0.2)" }
                  }>
                  <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714a2.25 2.25 0 0 0 .659 1.591L19 14.5M14.25 3.104c.251.023.501.05.75.082M19 14.5l-2.47 2.47a2.25 2.25 0 0 1-1.591.659H9.061a2.25 2.25 0 0 1-1.591-.659L5 14.5m14 0H5" />
                  </svg>
                  {isFollowup ? t("追问分析") : t("初次分析")}
                </span>
                {r.is_deep_analysis === true && (
                  <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
                    style={{ background: "rgba(99,102,241,0.12)", color: "#6366F1", border: "1px solid rgba(99,102,241,0.3)" }}>
                    <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                    {t("Deep Analysis")}
                  </span>
                )}
                {!isFollowup && createdAt && (
                  <span className="text-[10px]" style={{ color: S.text3 }}>{formatLocalTime(createdAt)}</span>
                )}
              </div>

              {lang === "en" && !hasEnTranslation && (
                <p className="text-[10px]" style={{ color: S.accent }}>English translation not available. Showing Chinese.</p>
              )}
              <div className="flex flex-wrap gap-2">
                <span className="rounded-lg px-2.5 py-1 text-xs font-semibold" style={{ background: S.overlay, color: S.text1 }}>
                  {problemType}
                </span>
                <ConfBadge c={r.confidence} />
                {r.agent_model && (
                  <span className="rounded-lg px-2.5 py-1 text-[10px] font-medium"
                    style={{ background: "rgba(96,165,250,0.1)", color: "rgba(96,165,250,0.8)", border: "1px solid rgba(96,165,250,0.2)" }}>
                    {r.agent_model.replace(/^claude-/, "").replace(/-\d{8}$/, "")}
                  </span>
                )}
                {r.agent_type && (
                  <span className="rounded-lg px-2 py-1 text-[10px] font-medium"
                    style={r.agent_type === "claude_api"
                      ? { background: "rgba(167,139,250,0.1)", color: "rgba(167,139,250,0.85)", border: "1px solid rgba(167,139,250,0.2)" }
                      : { background: "rgba(107,114,128,0.08)", color: "rgba(107,114,128,0.7)", border: "1px solid rgba(107,114,128,0.15)" }}>
                    {r.agent_type === "claude_api" ? "API" : "CLI"}
                  </span>
                )}
                {r.needs_engineer && (() => {
                  // tri-state: 未反馈 橙 / 反馈 false 灰（已纠偏）/ 反馈 true 绿（已确认）
                  const fb = r.engineer_label_feedback;
                  let bg = S.accentBg, color = S.accent, border = "rgba(14,124,134,0.25)";
                  const text = lang === "cn" ? "🤖 AI 标：需工程师" : "🤖 AI: Engineer needed";
                  let suffix = "";
                  if (fb === false) {
                    bg = "rgba(107,114,128,0.08)"; color = "#5B6470"; border = "rgba(107,114,128,0.25)";
                    suffix = lang === "cn" ? "（客服已纠偏）" : "(CS overrode)";
                  } else if (fb === true) {
                    bg = "rgba(34,197,94,0.10)"; color = "#16A34A"; border = "rgba(34,197,94,0.30)";
                    suffix = lang === "cn" ? " ✅ 客服已确认" : " ✅ Confirmed";
                  }
                  return (
                    <span className="rounded-lg px-2.5 py-1 text-xs font-semibold"
                      style={{ background: bg, color, border: `1px solid ${border}` }}
                      title={r.engineer_label_feedback_by ? `${lang === "cn" ? "反馈人" : "by"}: ${r.engineer_label_feedback_by}` : undefined}>
                      {text}{suffix}
                    </span>
                  );
                })()}
                {/* Code-version badge — only render when code_routing exists (hides on pre-feature results) */}
                {r.log_metadata?.code_routing && <CodeRoutingBadge cr={r.log_metadata.code_routing} t={t} />}
              </div>

              {/* Per-run usage meter — 本次 tokens · cost (功能 1) */}
              <UsageMeter r={r} />

              {/* 客服反馈闭环 widget — 仅当宿主页提供 slot 且未反馈时显示 */}
              {r.needs_engineer
                && (r.engineer_label_feedback === null || r.engineer_label_feedback === undefined)
                && renderEngineerFeedback?.(r)}

              {/* Lost recording tool hint */}
              {r.problem_type && /录音.{0,8}找不到|找不到.{0,8}录音|recording.*lost|lost.*recording|missing.*recording/i.test(r.problem_type + " " + (r.problem_type_en || "")) && (
                <a href="/tools"
                  className="flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium transition-colors"
                  style={{ background: "rgba(96,165,250,0.08)", color: "#2563EB", border: "1px solid rgba(96,165,250,0.2)", textDecoration: "none" }}>
                  <svg className="h-3.5 w-3.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                  </svg>
                  {lang === "cn" ? "录音找不到？试试录音丢失排查工具 →" : "Can't find the recording? Try the Lost Recording Finder →"}
                </a>
              )}

              <div>
                <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                  {lang === "cn" ? "问题原因" : "Root Cause"}
                </h3>
                <div className="rounded-lg p-3 text-sm" style={{ background: S.overlay, color: S.text2 }}>
                  <MarkdownText>{rootCause}</MarkdownText>
                </div>
              </div>

              {/* Collapsible evidence */}
              {r.key_evidence && r.key_evidence.length > 0 && (
                <div>
                  <button
                    onClick={() => setCollapsedEvidence((prev) => ({ ...prev, [evidenceKey]: !evidenceCollapsed }))}
                    className="mb-1.5 flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider"
                    style={{ color: S.text3, background: "none", border: "none", cursor: "pointer", padding: 0 }}>
                    <svg className={`h-3 w-3 transition-transform ${evidenceCollapsed ? "" : "rotate-90"}`}
                      fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                    </svg>
                    {lang === "cn" ? "关键证据" : "Key Evidence"} ({r.key_evidence.length})
                  </button>
                  {!evidenceCollapsed && (
                    <div className="space-y-2">
                      {r.key_evidence.map((ev, i) => {
                        // Split evidence into explanation + log line if pattern matches
                        const logSep = ev.match(/^(.+?)\s*(?:——|--|→|=>|日志[:：])\s*([\s\S]+)$/);
                        return (
                          <div key={i} className="rounded-lg px-3 py-2 text-[11px]"
                            style={{ background: S.overlay, border: `1px solid ${S.borderSm}` }}>
                            {logSep ? (
                              <>
                                <div className="mb-1 text-xs" style={{ color: S.text2 }}>{logSep[1].trim()}</div>
                                <div className="font-mono text-[10px] rounded px-2 py-1" style={{ background: S.surface, color: S.text3 }}>{logSep[2].trim()}</div>
                              </>
                            ) : (
                              <div className="whitespace-pre-wrap" style={{ color: S.text2 }}>{ev}</div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}

              {/* Agent execution trace — only for claude_api tasks */}
              {r.agent_type === "claude_api" && r.task_id && (
                <AgentTraceBlock
                  taskId={r.task_id}
                  palette={{
                    surface: S.surface,
                    overlay: S.overlay,
                    border: S.border,
                    text1: S.text1,
                    text2: S.text2,
                    text3: S.text3,
                    accent: S.accent,
                  }}
                />
              )}

              {userReply && (
                <div>
                  <div className="mb-1.5 flex items-center justify-between">
                    <h3 className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                      {lang === "cn" ? "建议回复" : "Suggested Reply"}
                    </h3>
                    <button onClick={() => onCopy(userReply)}
                      className="rounded-lg px-3 py-1 text-[11px] font-medium"
                      style={{ background: "rgba(34,197,94,0.15)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
                      {lang === "cn" ? "一键复制" : "Copy"}
                    </button>
                  </div>
                  <div className="rounded-lg p-3 text-sm"
                    style={{ background: S.overlay, color: S.text2, borderLeft: "2px solid rgba(34,197,94,0.4)" }}>
                    <MarkdownText>{userReply}</MarkdownText>
                  </div>
                </div>
              )}
            </div>
          </div>
        );
      })}

      {/* Deep-analysis CTA — nudges when latest confidence is low; available for all analyzed tickets */}
      {(() => {
        const isLow = (analyses[0]?.confidence || "").toLowerCase() === "low";
        return (
          <section className="rounded-lg p-3 space-y-2"
            style={isLow
              ? { background: "rgba(99,102,241,0.06)", border: "1px solid rgba(99,102,241,0.25)" }
              : { background: S.overlay, border: `1px solid ${S.border}` }}>
            {isLow && (
              <div className="flex items-start gap-2">
                <svg className="h-4 w-4 flex-shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="#6366F1" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                </svg>
                <p className="text-xs leading-relaxed" style={{ color: "#6366F1" }}>{t("分析可信度较低提示")}</p>
              </div>
            )}
            <button onClick={() => { setDeepDirection(""); setDeepDialogOpen(true); }}
              className="w-full rounded-lg py-2 text-sm font-semibold"
              style={{ background: "rgba(99,102,241,0.15)", color: "#6366F1", border: "1px solid rgba(99,102,241,0.3)" }}>
              {t("深度分析")}
            </button>
          </section>
        );
      })()}

      {/* Deep-analysis dialog: warn + collect optional new direction (功能 2) */}
      {deepDialogOpen && (
        <div className="j-fade fixed inset-0 z-[60] flex items-center justify-center p-4"
          style={{ background: "rgba(0,0,0,0.55)" }} onClick={() => setDeepDialogOpen(false)}>
          <div className="j-pop w-full max-w-md rounded-xl p-5"
            style={{ background: "var(--j-panel)", border: `1px solid ${S.border}` }} onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start gap-3">
              <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg" style={{ background: "rgba(99,102,241,0.12)" }}>
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="#6366F1" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <div className="flex-1">
                <h3 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("确认开始深度分析")}</h3>
                <p className="mt-2 text-xs leading-relaxed" style={{ color: S.text2 }}>{t("深度分析风险提示")}</p>
              </div>
            </div>
            <div className="mt-4">
              <label className="mb-1.5 block text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                {t("新的分析方向（可选）")}
              </label>
              <textarea
                value={deepDirection}
                onChange={(e) => setDeepDirection(e.target.value)}
                placeholder={t("输入新的分析方向…")}
                rows={3}
                className="w-full resize-none rounded-lg px-3 py-2 text-sm outline-none"
                style={{ background: S.overlay, border: `1px solid ${S.borderSm}`, color: S.text1 }}
              />
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setDeepDialogOpen(false)}
                className="rounded-lg px-4 py-2 text-sm font-medium"
                style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                {t("取消")}
              </button>
              <button onClick={() => { const d = deepDirection.trim(); setDeepDialogOpen(false); onDeepAnalysis(issueId, d || undefined); }}
                className="rounded-lg px-4 py-2 text-sm font-semibold"
                style={{ background: "#6366F1", color: "#FFFFFF" }}>
                {t("确定开始")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Follow-up progress — above input */}
      {isAnalyzing && activeTask && (
        <div className="rounded-lg p-3" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
          <div className="mb-2 flex justify-between text-xs" style={{ color: S.text2 }}>
            <span>{activeTask.message}</span>
            <span className="tabular-nums">{activeTask.progress}%</span>
          </div>
          <div className="j-scan h-1.5 rounded-full overflow-hidden" style={{ background: S.hover }}>
            <div className="h-full rounded-full transition-all duration-700"
              style={{ width: `${activeTask.progress}%`, background: S.accent }} />
          </div>
        </div>
      )}

      {/* Follow-up input — anchored at bottom of conversation */}
      <section className="rounded-lg p-3" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
        <div className="flex gap-2 items-end">
          <textarea
            value={followupText}
            onChange={(e) => setFollowupText(e.target.value)}
            placeholder={isAnalyzing ? t("请等待当前分析完成...") : t("请输入追问内容...")}
            rows={1}
            disabled={followupSubmitting || isAnalyzing}
            className="flex-1 resize-none rounded-xl px-3 py-2 text-sm outline-none"
            style={{ background: S.surface, border: `1px solid ${S.borderSm}`, color: S.text1, minHeight: "38px", maxHeight: "120px" }}
          />
          <button
            onClick={() => onStartFollowup(issueId, followupText)}
            disabled={!followupText.trim() || followupSubmitting || isAnalyzing}
            className="flex-shrink-0 rounded-xl p-2 transition-colors disabled:opacity-30"
            style={{ background: S.accent, color: "#FFFFFF" }}>
            {followupSubmitting ? (
              <div className="h-4 w-4 animate-spin rounded-full border-2"
                style={{ borderColor: "rgba(0,0,0,0.2)", borderTopColor: "#0A0B0E" }} />
            ) : (
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
              </svg>
            )}
          </button>
        </div>
      </section>
    </>
  );
}
