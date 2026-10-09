"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import MarkdownText from "@/components/MarkdownText";
import {
  listGraygateReports,
  getGraygateReport,
  type GraygateReportItem,
  type GraygateReportDetail,
} from "@/lib/api";
import { useT } from "@/lib/i18n";

const D = {
  bg: "var(--j-surface)",
  surface: "var(--j-panel)",
  border: "var(--j-border)",
  text1: "var(--j-ink)",
  text2: "var(--j-graphite)",
  text3: "var(--j-faint)",
  accent: "var(--j-accent)",
  accentSoft: "var(--j-accent-soft)",
  ok: "#16A34A",
  danger: "#DC2626",
  dangerBg: "rgba(220,38,38,0.08)",
} as const;

function StatusDot({ red }: { red: boolean }) {
  return (
    <span
      style={{
        display: "inline-block",
        width: 8,
        height: 8,
        borderRadius: 999,
        background: red ? D.danger : D.ok,
        flexShrink: 0,
      }}
    />
  );
}

function Badges({ item }: { item: GraygateReportItem }) {
  const t = useT();
  if (!item.worsen_count && !item.new_crash_count) {
    return <span style={{ color: D.text3, fontSize: 11 }}>{t("无恶化")}</span>;
  }
  const chip = (text: string) => (
    <span
      style={{
        fontSize: 11,
        padding: "1px 6px",
        borderRadius: 4,
        background: D.dangerBg,
        color: D.danger,
        whiteSpace: "nowrap",
      }}
    >
      {text}
    </span>
  );
  return (
    <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
      {item.worsen_count > 0 && chip(`🔴 ${item.worsen_count} ${t("项恶化")}`)}
      {item.new_crash_count > 0 && chip(`🆕 ${item.new_crash_count} ${t("个新增崩溃")}`)}
    </span>
  );
}

function GraygateReportsInner() {
  const t = useT();
  const router = useRouter();
  const searchParams = useSearchParams();
  const dateParam = searchParams.get("date") || "";

  const [items, setItems] = useState<GraygateReportItem[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [detail, setDetail] = useState<GraygateReportDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    listGraygateReports()
      .then((r) => setItems(r.items))
      .catch(() => setItems([]))
      .finally(() => setListLoading(false));
  }, []);

  // 没带 date 时默认打开最新一份
  const selected = dateParam || items[0]?.date || "";

  const [generating, setGenerating] = useState(false);

  useEffect(() => {
    if (!selected) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setDetail(null);
    setDetailLoading(true);
    setGenerating(false);
    setError("");
    const load = () =>
      getGraygateReport(selected)
        .then((r) => {
          if (!alive) return;
          if (r.status === "generating") {
            // 后端在后台现算（重新查 Datadog，要几分钟），隔 5s 再拉
            setGenerating(true);
            timer = setTimeout(load, 5000);
            return;
          }
          setGenerating(false);
          setDetail(r);
          setDetailLoading(false);
        })
        .catch((e) => {
          if (!alive) return;
          setError(String(e?.message || e));
          setDetailLoading(false);
        });
    load();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [selected]);


  return (
    <div style={{ background: D.bg, minHeight: "100vh", color: D.text1 }}>
      <div style={{ maxWidth: 1280, margin: "0 auto", padding: "24px 16px" }}>
        <div style={{ marginBottom: 16 }}>
          <h1 style={{ fontSize: 20, fontWeight: 600, margin: 0 }}>{t("4.0.3 灰度 · 每日指标")}</h1>
          <div style={{ color: D.text2, fontSize: 12, marginTop: 4 }}>
            {t("Slack 只推核心指标，这里是完整日报（全部指标 · 新增崩溃堆栈 · Top5 崩溃 / 卡顿）")}
          </div>
        </div>

        <div className="gg-layout">
          {/* 日期列表 */}
          <div
            className="gg-list"
            style={{
              background: D.surface,
              border: `1px solid ${D.border}`,
              borderRadius: 10,
              overflow: "auto",
            }}
          >
            {listLoading ? (
              <div style={{ padding: 16, color: D.text2, fontSize: 13 }}>{t("加载中…")}</div>
            ) : items.length === 0 ? (
              <div style={{ padding: 16, color: D.text3, fontSize: 13 }}>{t("暂无日报")}</div>
            ) : (
              items.map((it) => {
                const active = it.date === selected;
                return (
                  <button
                    key={it.date}
                    onClick={() => router.replace(`/graygate/reports?date=${it.date}`)}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: 4,
                      width: "100%",
                      textAlign: "left",
                      padding: "10px 14px",
                      border: "none",
                      borderBottom: `1px solid ${D.border}`,
                      borderLeft: `3px solid ${active ? D.accent : "transparent"}`,
                      background: active ? D.accentSoft : "transparent",
                      cursor: "pointer",
                      color: D.text1,
                    }}
                  >
                    <span style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, fontWeight: 500 }}>
                      <StatusDot red={it.is_red} />
                      <span style={{ fontVariantNumeric: "tabular-nums" }}>{it.date}</span>
                    </span>
                    <Badges item={it} />
                  </button>
                );
              })
            )}
          </div>

          {/* 详情 */}
          <div
            style={{
              background: D.surface,
              border: `1px solid ${D.border}`,
              borderRadius: 10,
              padding: 20,
              minWidth: 0,
            }}
          >
            {!selected ? (
              <div style={{ color: D.text3, fontSize: 13 }}>
                {listLoading ? t("加载中…") : t("选择左侧日期查看日报")}
              </div>
            ) : detailLoading ? (
              <div style={{ color: D.text2, fontSize: 13 }}>
                {generating ? t("这天的日报还没有缓存，正在后台生成（重新查询 Datadog，约几分钟），完成后自动显示…") : t("加载中…")}
              </div>
            ) : error ? (
              <div style={{ color: D.danger, fontSize: 13 }}>
                {t("加载失败")}：{error}
              </div>
            ) : detail ? (
              <>
                <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
                  <StatusDot red={detail.is_red} />
                  <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>
                    {detail.title || detail.date}
                  </h2>
                </div>
                <div style={{ fontSize: 13, lineHeight: 1.7 }}>
                  <MarkdownText>{detail.markdown}</MarkdownText>
                </div>
              </>
            ) : null}
          </div>
        </div>
      </div>

      <style>{`
        .gg-layout { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 16px; align-items: start; }
        .gg-list { max-height: calc(100vh - 140px); position: sticky; top: 16px; }
        @media (max-width: 768px) {
          .gg-layout { grid-template-columns: minmax(0, 1fr); }
          .gg-list { max-height: 240px; position: static; }
        }
      `}</style>
    </div>
  );
}

export default function GraygateReportsPage() {
  const t = useT();
  return (
    <Suspense
      fallback={
        <div style={{ background: D.bg, minHeight: "100vh", color: D.text2, padding: 24 }}>
          {t("加载中…")}
        </div>
      }
    >
      <GraygateReportsInner />
    </Suspense>
  );
}
