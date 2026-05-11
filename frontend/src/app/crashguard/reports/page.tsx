"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import MarkdownText from "@/components/MarkdownText";
import {
  fetchCrashReportHistory,
  fetchCrashReportDetail,
  type CrashReportHistoryItem,
} from "@/lib/api";
import { useT } from "@/lib/i18n";

const D = {
  bg: "#F8F9FA",
  surface: "#FFFFFF",
  surfaceAlt: "#F8F9FA",
  border: "rgba(0,0,0,0.08)",
  text1: "#111827",
  text2: "#6B7280",
  text3: "#9CA3AF",
  accent: "#B8922E",
  ok: "#16A34A",
  warn: "#D97706",
  warnBg: "rgba(217,119,6,0.10)",
  danger: "#DC2626",
} as const;

export default function CrashReportsHistoryPage() {
  const t = useT();
  const [items, setItems] = useState<CrashReportHistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "morning" | "evening">("all");
  const [days, setDays] = useState(30);
  const [openId, setOpenId] = useState<number | null>(null);
  const [detailMd, setDetailMd] = useState<string>("");
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchCrashReportHistory({
      days,
      report_type: filter === "all" ? undefined : filter,
      limit: 100,
    })
      .then((r) => {
        if (!cancelled) {
          setItems(r.items);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [filter, days]);

  const onOpen = async (id: number) => {
    setOpenId(id);
    setDetailLoading(true);
    setDetailMd("");
    try {
      const r = await fetchCrashReportDetail(id);
      setDetailMd(r.markdown);
    } catch (e) {
      setDetailMd(`_加载失败：${String(e)}_`);
    } finally {
      setDetailLoading(false);
    }
  };

  return (
    <div style={{ background: D.bg, minHeight: "100vh", color: D.text1 }}>
      <div style={{ maxWidth: 1200, margin: "0 auto", padding: "24px 32px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
          <div>
            <h1 style={{ fontSize: 22, fontWeight: 600, margin: 0 }}>📋 早晚报历史</h1>
            <p style={{ color: D.text2, fontSize: 13, marginTop: 4 }}>
              {t("最近")} {days} {t("天")} · {items.length} {t("份报告")}
            </p>
          </div>
          <Link
            href="/crashguard"
            style={{ color: D.accent, fontSize: 13, textDecoration: "none" }}
          >
            ← {t("返回主页")}
          </Link>
        </div>

        {/* 过滤器 */}
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            background: D.surface,
            border: `1px solid ${D.border}`,
            borderRadius: 8,
            padding: "10px 14px",
            marginBottom: 16,
          }}
        >
          <span style={{ color: D.text2, fontSize: 13 }}>{t("类型")}：</span>
          {(["all", "morning", "evening"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setFilter(k)}
              style={{
                padding: "4px 12px",
                borderRadius: 6,
                border: `1px solid ${filter === k ? D.accent : D.border}`,
                background: filter === k ? D.accent : "transparent",
                color: filter === k ? "white" : D.text1,
                fontSize: 12,
                cursor: "pointer",
              }}
            >
              {k === "all" ? t("全部") : k === "morning" ? "🌅 早报" : "🌇 晚报"}
            </button>
          ))}
          <span style={{ flex: 1 }} />
          <span style={{ color: D.text2, fontSize: 13 }}>{t("时间窗")}：</span>
          <select
            value={days}
            onChange={(e) => setDays(parseInt(e.target.value, 10))}
            style={{
              border: `1px solid ${D.border}`,
              borderRadius: 6,
              padding: "4px 8px",
              fontSize: 12,
              background: D.surface,
              color: D.text1,
            }}
          >
            <option value={7}>{t("最近 7 天")}</option>
            <option value={30}>{t("最近 30 天")}</option>
            <option value={90}>{t("最近 90 天")}</option>
            <option value={180}>{t("最近 180 天")}</option>
          </select>
        </div>

        {loading && (
          <div style={{ color: D.text2, padding: 24, textAlign: "center" }}>{t("加载中…")}</div>
        )}
        {error && (
          <div
            style={{
              padding: 12,
              background: D.warnBg,
              border: `1px solid ${D.warn}`,
              borderRadius: 6,
              color: D.warn,
              fontSize: 13,
              marginBottom: 12,
            }}
          >
            {error}
          </div>
        )}
        {!loading && !error && items.length === 0 && (
          <div style={{ color: D.text3, padding: 32, textAlign: "center", fontSize: 14 }}>
            {t("暂无历史报告")}
          </div>
        )}

        {/* 列表 */}
        {!loading &&
          items.map((it) => {
            const total = it.attention_total;
            const hasAttn = total > 0;
            return (
              <div
                key={it.id}
                onClick={() => onOpen(it.id)}
                style={{
                  background: D.surface,
                  border: `1px solid ${D.border}`,
                  borderRadius: 8,
                  padding: "14px 18px",
                  marginBottom: 8,
                  cursor: "pointer",
                  transition: "border-color 0.15s",
                }}
                onMouseEnter={(e) => (e.currentTarget.style.borderColor = D.accent)}
                onMouseLeave={(e) => (e.currentTarget.style.borderColor = D.border)}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <span style={{ fontSize: 22 }}>{it.report_type === "morning" ? "🌅" : "🌇"}</span>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 14, fontWeight: 600 }}>
                      {it.report_date} · {it.report_type === "morning" ? "早报" : "晚报"}
                    </div>
                    <div style={{ fontSize: 12, color: D.text2, marginTop: 2 }}>
                      Top {it.top_n} ·{" "}
                      <span style={{ color: hasAttn ? D.danger : D.ok }}>
                        关注点 {total} 项
                      </span>{" "}
                      （新增 {it.new_count} · 突增 {it.surge_count} · 下降 {it.regression_count}）
                    </div>
                  </div>
                  {it.feishu_message_id && (
                    <span
                      style={{
                        padding: "3px 8px",
                        borderRadius: 4,
                        background: "rgba(22,163,74,0.10)",
                        color: D.ok,
                        fontSize: 11,
                      }}
                    >
                      已推送飞书
                    </span>
                  )}
                </div>
              </div>
            );
          })}
      </div>

      {/* 详情 modal */}
      {openId !== null && (
        <div
          onClick={() => setOpenId(null)}
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.5)",
            zIndex: 50,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 24,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              width: "min(900px, 95vw)",
              maxHeight: "90vh",
              background: D.surface,
              borderRadius: 12,
              boxShadow: "0 20px 50px rgba(0,0,0,0.25)",
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                padding: "14px 20px",
                borderBottom: `1px solid ${D.border}`,
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <strong style={{ fontSize: 15 }}>{t("报告详情")}</strong>
              <button
                onClick={() => setOpenId(null)}
                style={{ border: "none", background: "transparent", cursor: "pointer", fontSize: 18, color: D.text2 }}
              >
                ✕
              </button>
            </div>
            <div style={{ overflow: "auto", padding: 20, flex: 1 }}>
              {detailLoading ? (
                <div style={{ color: D.text2 }}>{t("加载中…")}</div>
              ) : (
                <div style={{ fontSize: 13, lineHeight: 1.7 }}>
                  <MarkdownText>{detailMd}</MarkdownText>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
