"use client";

/**
 * Shared UI components used across issue pages (main page + tracking).
 *
 * Centralizes design tokens, badge components, and the Feishu link button
 * that were previously duplicated between page.tsx and tracking/page.tsx.
 */

import { useState, useEffect } from "react";
import { useT } from "@/lib/i18n";
import type { RecurrenceSummary } from "@/lib/api";

// ── Shared design tokens ─────────────────────────────────────
// Theme-aware tokens — resolve to light or dark "console mode" via CSS vars (globals.css)
export const S = {
  surface: "var(--j-surface)",
  overlay: "var(--j-panel)",
  hover: "var(--j-hover)",
  border: "var(--j-border)",
  borderSm: "var(--j-border-sm)",
  accent: "var(--j-accent)",
  accentBg: "var(--j-accent-soft)",
  text1: "var(--j-ink)",
  text2: "var(--j-graphite)",
  text3: "var(--j-faint)",
  orange: "#EA580C",
  orangeBg: "rgba(234,88,12,0.08)",
  orangeBorder: "rgba(234,88,12,0.25)",
};

// ── PriorityBadge ───────────────────────────────────────────
interface PriorityBadgeProps {
  p: string;
}

export function PriorityBadge({ p }: PriorityBadgeProps) {
  const t = useT();
  if (p === "H") {
    return (
      <span
        className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold"
        style={{
          background: "rgba(239,68,68,0.15)",
          color: "#DC2626",
          border: "1px solid rgba(239,68,68,0.25)",
        }}
      >
        {t("高")}
      </span>
    );
  }
  return (
    <span
      className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{
        background: "rgba(0,0,0,0.04)",
        color: S.text3,
        border: `1px solid ${S.border}`,
      }}
    >
      {t("低")}
    </span>
  );
}

// ── SourceBadge ─────────────────────────────────────────────
interface SourceBadgeProps {
  source?: string;
  linearUrl?: string;
}

export function SourceBadge({ source, linearUrl }: SourceBadgeProps) {
  const t = useT();
  const cfg: Record<string, { label: string; bg: string; color: string; border: string }> = {
    feishu: { label: t("飞书"), bg: "rgba(96,165,250,0.12)", color: "#2563EB", border: "rgba(96,165,250,0.25)" },
    feishu_import: { label: t("飞书导入"), bg: "rgba(96,165,250,0.12)", color: "#2563EB", border: "rgba(96,165,250,0.25)" },
    linear: { label: "Linear", bg: "rgba(167,139,250,0.12)", color: "#7C3AED", border: "rgba(167,139,250,0.25)" },
    api: { label: "API", bg: "rgba(52,211,153,0.12)", color: "#059669", border: "rgba(52,211,153,0.25)" },
    local: { label: t("手动提交"), bg: "rgba(251,146,60,0.12)", color: "#EA580C", border: "rgba(251,146,60,0.25)" },
  };
  const s = source || "feishu";
  const c = cfg[s] || cfg.feishu;
  const badge = (
    <span
      className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: c.bg, color: c.color, border: `1px solid ${c.border}` }}
    >
      {c.label}
    </span>
  );
  if (s === "linear" && linearUrl) {
    return (
      <a href={linearUrl} target="_blank" onClick={(e) => e.stopPropagation()} className="hover:opacity-80">
        {badge}
      </a>
    );
  }
  return badge;
}

// ── FeishuLinkBadge ─────────────────────────────────────────
interface FeishuLinkBadgeProps {
  href: string;
}

export function FeishuLinkBadge({ href }: FeishuLinkBadgeProps) {
  const t = useT();
  return (
    <a
      href={href}
      target="_blank"
      className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors hover:opacity-80"
      style={{
        background: "rgba(52,120,246,0.10)",
        color: "#2563EB",
        border: "1px solid rgba(52,120,246,0.25)",
        textDecoration: "none",
      }}
    >
      <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="currentColor">
        <path
          d="M2.3 7.7l7.8-4.5c.3-.2.7-.2 1 0l7.8 4.5c.4.2.4.8 0 1L11.1 13c-.3.2-.7.2-1 0L2.3 8.7c-.4-.2-.4-.8 0-1z"
          opacity=".7"
        />
        <path d="M11.1 13.8l-7.8-4.5c-.5-.3-1 .1-1 .7v7c0 .3.2.6.4.7l7.8 4.5c.3.2.7.2 1 0l7.8-4.5c.3-.2.4-.4.4-.7v-7c0-.6-.6-1-1-.7l-7.8 4.5c-.2.1-.5.1-.8 0z" />
      </svg>
      {t("飞书工单")} ↗
    </a>
  );
}

// ── RecurrenceBadge ─────────────────────────────────────────
// Deliberately NOT using S.orange — that token is already "已转交" (escalated)
// elsewhere in the same badge stack; reusing it here would conflate two
// unrelated states.
interface RecurrenceBadgeProps {
  recurrence?: RecurrenceSummary | null;
}

export function RecurrenceBadge({ recurrence }: RecurrenceBadgeProps) {
  const t = useT();
  if (!recurrence) return null;
  const isRed = recurrence.severity === "red";
  const cfg = isRed
    ? { label: t("疑似复发"), bg: "rgba(239,68,68,0.15)", color: "#DC2626", border: "rgba(239,68,68,0.25)" }
    : { label: t("历史类似"), bg: "rgba(234,179,8,0.12)", color: "#CA8A04", border: "rgba(234,179,8,0.25)" };
  return (
    <span
      className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: cfg.bg, color: cfg.color, border: `1px solid ${cfg.border}` }}
    >
      {cfg.label}{recurrence.count > 1 ? ` ×${recurrence.count}` : ""}
    </span>
  );
}

// ── RecurrenceBanner ────────────────────────────────────────
// Detail-page banner surfacing the top recurrence hit — same "this needs
// attention" placement convention as the existing escalation section.
interface RecurrenceBannerProps {
  recurrence?: RecurrenceSummary | null;
  onOpenPrior?: (issueId: string) => void;
}

export function RecurrenceBanner({ recurrence, onOpenPrior }: RecurrenceBannerProps) {
  const t = useT();
  if (!recurrence) return null;
  const isRed = recurrence.severity === "red";
  const top = recurrence.top;
  const colors = isRed
    ? { bg: "rgba(239,68,68,0.08)", border: "rgba(239,68,68,0.25)", accent: "#DC2626" }
    : { bg: "rgba(234,179,8,0.08)", border: "rgba(234,179,8,0.25)", accent: "#CA8A04" };

  const priorLink = (
    <button
      type="button"
      onClick={() => onOpenPrior?.(top.prior_issue_id)}
      className="font-mono underline hover:opacity-80"
      style={{ color: colors.accent, background: "none", border: "none", padding: 0, cursor: onOpenPrior ? "pointer" : "default" }}
      disabled={!onOpenPrior}
    >
      {top.prior_issue_id}
    </button>
  );

  return (
    <div className="rounded-xl p-4 text-xs space-y-1.5" style={{ background: colors.bg, border: `1px solid ${colors.border}` }}>
      <p className="font-semibold" style={{ color: colors.accent }}>
        {isRed ? t("疑似复发") : t("历史类似")}
      </p>
      {isRed ? (
        <>
          <p style={{ color: "var(--j-ink)" }}>
            {t("原工单")} {priorLink} {t("于")} {top.prior_resolved_at || "?"} {t("标记完成")}
          </p>
          <p style={{ color: "var(--j-graphite)" }}>
            {t("修复版本")}：{top.fix_target === "app" ? "APP" : top.fix_target === "firmware" ? t("固件") : t("其他")} {top.fix_version}
          </p>
          <p style={{ color: "var(--j-graphite)" }}>
            {t("本单版本")}：{top.compared_version || "?"}
          </p>
          {top.prior_resolve_reason && (
            <p style={{ color: "var(--j-graphite)" }}>{t("上次完成原因")}：{top.prior_resolve_reason}</p>
          )}
        </>
      ) : (
        <p style={{ color: "var(--j-graphite)" }}>
          {t("历史上有类似问题已标记完成（未记录修复版本）")} — {t("原工单")} {priorLink}
          {top.prior_resolve_reason && `：${top.prior_resolve_reason}`}
        </p>
      )}
    </div>
  );
}

// ── MarkCompleteDialog ──────────────────────────────────────
// Shared "mark complete" confirmation dialog — reason (required) + optional
// fix type/version. Previously duplicated verbatim between page.tsx and
// tracking/page.tsx (and entirely absent from the two oncall one-click
// resolve buttons); now the single source for all 4 entry points.
export interface CompletionPayload {
  reason: string;
  fixTarget: string;   // "" | app | firmware | other
  fixVersion: string;
}

interface MarkCompleteDialogProps {
  open: boolean;
  onCancel: () => void;
  onConfirm: (p: CompletionPayload) => void;
  onError?: (msg: string) => void;
  submitting?: boolean;
}

export function MarkCompleteDialog({ open, onCancel, onConfirm, onError, submitting }: MarkCompleteDialogProps) {
  const t = useT();
  const [reason, setReason] = useState("");
  const [fixTarget, setFixTarget] = useState("");
  const [fixVersion, setFixVersion] = useState("");

  useEffect(() => {
    if (open) {
      setReason("");
      setFixTarget("");
      setFixVersion("");
    }
  }, [open]);

  if (!open) return null;

  const handleConfirm = () => {
    const trimmedReason = reason.trim();
    if (!trimmedReason) return;
    if (fixVersion.trim() && !fixTarget) {
      onError?.(t("填写修复版本时必须同时选择修复类型（APP/固件/其他）"));
      return;
    }
    onConfirm({ reason: trimmedReason, fixTarget, fixVersion: fixVersion.trim() });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.4)" }}>
      <div className="w-full max-w-md rounded-2xl p-5 space-y-4" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
        <div>
          <h3 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("标记完成")}</h3>
          <p className="mt-1 text-xs" style={{ color: S.text2 }}>{t("请输入标记完成的原因")}</p>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium" style={{ color: S.text2 }}>{t("标记完成原因")}</label>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={t("输入标记完成的原因…")}
            rows={3}
            className="w-full rounded-lg px-3 py-2 text-sm outline-none resize-none"
            style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }}
          />
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium" style={{ color: S.text2 }}>{t("修复版本（选填）")}</label>
          <div className="flex gap-2">
            <select
              value={fixTarget}
              onChange={(e) => setFixTarget(e.target.value)}
              className="rounded-lg px-2 py-2 text-sm outline-none"
              style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }}
            >
              <option value="">{t("未填写")}</option>
              <option value="app">APP</option>
              <option value="firmware">{t("固件")}</option>
              <option value="other">{t("其他")}</option>
            </select>
            {fixTarget && (
              <input
                type="text"
                value={fixVersion}
                onChange={(e) => setFixVersion(e.target.value)}
                placeholder={t("版本号，如 3.16.0")}
                className="flex-1 rounded-lg px-3 py-2 text-sm font-mono outline-none"
                style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }}
              />
            )}
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onCancel}
            disabled={submitting}
            className="rounded-lg px-3 py-1.5 text-sm font-medium"
            style={{ color: S.text2, background: S.overlay, border: `1px solid ${S.border}` }}
          >
            {t("取消")}
          </button>
          <button
            onClick={handleConfirm}
            disabled={!reason.trim() || submitting}
            className="rounded-lg px-3 py-1.5 text-sm font-semibold"
            style={{
              background: S.accent, color: "#FFFFFF",
              opacity: !reason.trim() || submitting ? 0.5 : 1,
            }}
          >
            {t("确定")}
          </button>
        </div>
      </div>
    </div>
  );
}
