"use client";

/**
 * Shared UI components used across issue pages (main page + tracking).
 *
 * Centralizes design tokens, badge components, and the Feishu link button
 * that were previously duplicated between page.tsx and tracking/page.tsx.
 */

import { useT } from "@/lib/i18n";

// ── Shared design tokens ─────────────────────────────────────
export const S = {
  surface: "#F8F9FA",
  overlay: "#FFFFFF",
  hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)",
  borderSm: "rgba(0,0,0,0.04)",
  accent: "#B8922E",
  accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827",
  text2: "#6B7280",
  text3: "#9CA3AF",
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
