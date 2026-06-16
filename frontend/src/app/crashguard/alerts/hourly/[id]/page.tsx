"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import MarkdownText from "@/components/MarkdownText";
import { fetchCrashHourlyAlertDetail } from "@/lib/api";
import { useT } from "@/lib/i18n";

const D = {
  bg: "#F1F4F3",
  surface: "#FFFFFF",
  border: "rgba(0,0,0,0.08)",
  text1: "#15181E",
  text2: "#5B6470",
  accent: "#0E7C86",
  danger: "#DC2626",
  dangerBg: "rgba(220,38,38,0.08)",
} as const;

type DetailState = {
  loading: boolean;
  error: string | null;
  markdown: string;
  hourUtc: string | null;
  newCount: number;
  surgeCount: number;
};

export default function HourlyAlertDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const t = useT();
  const { id: idStr } = use(params);
  const alertId = parseInt(idStr, 10);
  const [state, setState] = useState<DetailState>({
    loading: true,
    error: null,
    markdown: "",
    hourUtc: null,
    newCount: 0,
    surgeCount: 0,
  });

  useEffect(() => {
    if (!Number.isFinite(alertId) || alertId <= 0) {
      setState((s) => ({ ...s, loading: false, error: "无效的告警 ID" }));
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await fetchCrashHourlyAlertDetail(alertId);
        if (cancelled) return;
        setState({
          loading: false,
          error: null,
          markdown: r.markdown,
          hourUtc: r.hour_utc,
          newCount: r.new_count,
          surgeCount: r.surge_count,
        });
      } catch (e) {
        if (cancelled) return;
        setState({
          loading: false,
          error: String(e),
          markdown: "",
          hourUtc: null,
          newCount: 0,
          surgeCount: 0,
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [alertId]);

  const hourLabel = state.hourUtc
    ? new Date(state.hourUtc).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";

  return (
    <div style={{ minHeight: "100vh", background: D.bg, padding: "24px" }}>
      <div style={{ maxWidth: 900, margin: "0 auto" }}>
        <div style={{ marginBottom: 16, display: "flex", gap: 12, alignItems: "center" }}>
          <Link
            href="/crashguard/reports?type=hourly_alert"
            style={{ color: D.text2, fontSize: 13, textDecoration: "none" }}
          >
            ← {t("返回告警列表")}
          </Link>
          <span style={{ color: D.text2, fontSize: 13 }}>·</span>
          <Link
            href="/crashguard"
            style={{ color: D.text2, fontSize: 13, textDecoration: "none" }}
          >
            {t("Crashguard 主页")}
          </Link>
        </div>

        <div
          style={{
            background: D.surface,
            border: `1px solid ${D.border}`,
            borderRadius: 12,
            padding: 24,
          }}
        >
          <div style={{ marginBottom: 16, paddingBottom: 16, borderBottom: `1px solid ${D.border}` }}>
            <div style={{ fontSize: 12, color: D.text2, marginBottom: 4 }}>
              {t("实时告警 · Hourly Alert")} #{alertId}
            </div>
            <div style={{ fontSize: 20, fontWeight: 600, color: D.text1 }}>
              {hourLabel}
            </div>
            {!state.loading && !state.error && (
              <div style={{ marginTop: 8, fontSize: 13, color: D.text2 }}>
                {t("新增")} <b style={{ color: D.text1 }}>{state.newCount}</b>
                {"  ·  "}
                {t("上涨")} <b style={{ color: D.text1 }}>{state.surgeCount}</b>
              </div>
            )}
          </div>

          {state.loading && (
            <div style={{ color: D.text2, fontSize: 14 }}>{t("加载中…")}</div>
          )}
          {state.error && (
            <div
              style={{
                color: D.danger,
                background: D.dangerBg,
                padding: 12,
                borderRadius: 8,
                fontSize: 13,
              }}
            >
              {t("加载失败")}：{state.error}
            </div>
          )}
          {!state.loading && !state.error && state.markdown && (
            <MarkdownText>{state.markdown}</MarkdownText>
          )}
        </div>
      </div>
    </div>
  );
}
