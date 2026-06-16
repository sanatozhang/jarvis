"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import MarkdownText from "@/components/MarkdownText";
import {
  fetchCrashReportHistory,
  fetchCrashReportDetail,
  fetchCrashHourlyAlertDetail,
  fetchCoreMetricAlertDetail,
  formatSGT,
  type CrashReportHistoryItem,
  type CrashWindowHours,
} from "@/lib/api";
import { useT } from "@/lib/i18n";

const D = {
  bg: "var(--j-surface)",
  surface: "var(--j-panel)",
  surfaceAlt: "var(--j-surface)",
  border: "var(--j-border)",
  text1: "var(--j-ink)",
  text2: "var(--j-graphite)",
  text3: "var(--j-faint)",
  accent: "var(--j-accent)",
  ok: "#16A34A",
  warn: "#D97706",
  warnBg: "rgba(217,119,6,0.10)",
  danger: "#DC2626",
  dangerBg: "rgba(220,38,38,0.08)",
} as const;

type FilterKey = "all" | "morning" | "evening" | "hourly_alert" | "core_metric_alert";

const PAGE_SIZE = 20;
const FILTER_VALUES: FilterKey[] = ["all", "morning", "evening", "hourly_alert", "core_metric_alert"];

function parseFilter(v: string | null): FilterKey {
  return FILTER_VALUES.includes((v || "") as FilterKey) ? (v as FilterKey) : "all";
}
function parseDays(v: string | null): number {
  const n = parseInt(v || "", 10);
  return [7, 30, 90, 180].includes(n) ? n : 30;
}
function parsePage(v: string | null): number {
  const n = parseInt(v || "", 10);
  return Number.isFinite(n) && n > 0 ? n : 1;
}

function CrashReportsHistoryInner() {
  const t = useT();
  const router = useRouter();
  const searchParams = useSearchParams();

  // URL → state（深链入口）
  const filter = parseFilter(searchParams.get("type"));
  const days = parseDays(searchParams.get("days"));
  const page = parsePage(searchParams.get("page"));

  const [items, setItems] = useState<CrashReportHistoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openItem, setOpenItem] = useState<CrashReportHistoryItem | null>(null);
  const [detailMd, setDetailMd] = useState<string>("");
  const [detailLoading, setDetailLoading] = useState(false);
  // 详情 modal 内的展示窗口（与首页选定档位独立；初始读首页 ?win= 同步）
  const initialWin = ((): CrashWindowHours => {
    const n = parseInt(searchParams.get("win") || "", 10);
    return n === 168 || n === 336 || n === 720 ? (n as CrashWindowHours) : 24;
  })();
  const [detailWindow, setDetailWindow] = useState<CrashWindowHours>(initialWin);

  // 写 query（router.replace 不进历史栈）
  const updateQuery = useCallback(
    (patch: { type?: FilterKey; days?: number; page?: number }) => {
      const next = new URLSearchParams(searchParams.toString());
      const setOrDel = (key: string, val: string, isDefault: boolean) => {
        if (isDefault) next.delete(key);
        else next.set(key, val);
      };
      if (patch.type !== undefined) {
        setOrDel("type", patch.type, patch.type === "all");
      }
      if (patch.days !== undefined) {
        setOrDel("days", String(patch.days), patch.days === 30);
      }
      if (patch.page !== undefined) {
        setOrDel("page", String(patch.page), patch.page === 1);
      }
      const qs = next.toString();
      router.replace(qs ? `?${qs}` : "?", { scroll: false });
    },
    [router, searchParams],
  );

  const setFilter = (v: FilterKey) => updateQuery({ type: v, page: 1 });
  const setDays = (v: number) => updateQuery({ days: v, page: 1 });
  const setPage = (v: number) => updateQuery({ page: v });

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchCrashReportHistory({
      days,
      report_type: filter === "all" ? undefined : filter,
      page,
      page_size: PAGE_SIZE,
    })
      .then((r) => {
        if (!cancelled) {
          setItems(r.items);
          setTotal(r.total);
          setTotalPages(r.total_pages || 1);
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
  }, [filter, days, page]);

  // 深链：?alert_id={id} 直接打开对应告警 modal（不依赖 list 命中，老告警跨页也能开）
  const [autoOpenAlertId, setAutoOpenAlertId] = useState<number | null>(() => {
    const v = parseInt(searchParams.get("alert_id") || "", 10);
    return Number.isFinite(v) && v > 0 ? v : null;
  });
  useEffect(() => {
    if (autoOpenAlertId === null) return;
    const aid = autoOpenAlertId;
    setAutoOpenAlertId(null);
    // 构造最小 item 触发 modal（type query 决定 kind）
    const typeParam = searchParams.get("type") || "";
    const stubKind: CrashReportHistoryItem["kind"] =
      typeParam === "core_metric_alert" ? "core_metric_alert" : "hourly_alert";
    const stub: CrashReportHistoryItem = {
      kind: stubKind,
      id: aid,
      report_date: null,
      report_type: stubKind === "core_metric_alert" ? "core_metric_alert" : "hourly_alert",
      top_n: 0,
      new_count: 0,
      regression_count: 0,
      surge_count: 0,
      feishu_message_id: "",
      created_at: null,
      summary: "",
      attention_total: 0,
    };
    onOpen(stub);
    // 摘掉 alert_id query（modal 已打开，无需再触发）
    const next = new URLSearchParams(searchParams.toString());
    next.delete("alert_id");
    const qs = next.toString();
    router.replace(qs ? `?${qs}` : "?", { scroll: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoOpenAlertId]);

  const loadDetail = useCallback(
    async (it: CrashReportHistoryItem, win: CrashWindowHours) => {
      setDetailLoading(true);
      setDetailMd("");
      try {
        if (it.kind === "hourly_alert") {
          const r = await fetchCrashHourlyAlertDetail(it.id);
          setDetailMd(r.markdown);
        } else if (it.kind === "core_metric_alert") {
          const r = await fetchCoreMetricAlertDetail(it.id);
          setDetailMd(r.markdown);
        } else {
          const r = await fetchCrashReportDetail(it.id, win);
          setDetailMd(r.markdown);
        }
      } catch (e) {
        setDetailMd(`_加载失败：${String(e)}_`);
      } finally {
        setDetailLoading(false);
      }
    },
    [],
  );

  const onOpen = (it: CrashReportHistoryItem) => {
    setOpenItem(it);
    loadDetail(it, detailWindow);
  };

  // 切换窗口时若 modal 已打开且是早晚报（非即时告警），则重新拉取
  useEffect(() => {
    if (openItem && openItem.kind !== "hourly_alert" && openItem.kind !== "core_metric_alert") {
      loadDetail(openItem, detailWindow);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detailWindow]);

  const renderItem = (it: CrashReportHistoryItem) => {
    const isAlert = it.kind === "hourly_alert";
    const isMetric = it.kind === "core_metric_alert";
    const isMorning = it.report_type === "morning";
    const icon = isMetric ? "📉" : isAlert ? "🚨" : isMorning ? "🌅" : "🌇";
    const metricDir = it.direction === "down" ? "🔻" : it.direction === "up" ? "🔺" : "";
    const metricLabel = it.platforms_alerted
      ? `${it.platforms_alerted.toUpperCase()} ${metricDir}`
      : t("核心指标");
    const title = isMetric
      ? `${it.window_start ? formatSGT(it.window_start) : (it.report_date || "")} SGT · ${t("核心指标告警")} · ${metricLabel}`
      : isAlert
        ? `${formatSGT(it.hour_utc)} SGT · ${t("实时告警")}`
        : `${it.report_date} · ${isMorning ? t("日报（昨日 24h）") : t("日内增量（vs 上周同段）")}`;
    const total = it.attention_total;
    const hasAnomaly = isMetric ? (it.direction === "down") : total > 0;
    return (
      <div
        key={`${it.kind}-${it.id}`}
        onClick={() => onOpen(it)}
        style={{
          background: D.surface,
          border: `1px solid ${isAlert && hasAnomaly ? D.danger : D.border}`,
          borderRadius: 8,
          padding: "14px 18px",
          marginBottom: 8,
          cursor: "pointer",
          transition: "border-color 0.15s",
        }}
        onMouseEnter={(e) => (e.currentTarget.style.borderColor = D.accent)}
        onMouseLeave={(e) =>
          (e.currentTarget.style.borderColor = isAlert && hasAnomaly ? D.danger : D.border)
        }
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ fontSize: 22 }}>{icon}</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 600 }}>{title}</div>
            <div style={{ fontSize: 12, color: D.text2, marginTop: 2 }}>
              {isAlert ? (
                <>
                  <span style={{ color: hasAnomaly ? D.danger : D.ok }}>
                    {t("异常")} {total} {t("项")}
                  </span>{" "}
                  （{t("新增")} {it.new_count} · {t("上涨")} {it.surge_count}）
                </>
              ) : (
                <>
                  Top {it.top_n} ·{" "}
                  <span style={{ color: hasAnomaly ? D.danger : D.ok }}>
                    {t("关注点")} {total} {t("项")}
                  </span>{" "}
                  （{t("新增")} {it.new_count} · {t("突增")} {it.surge_count} · {t("下降")}{" "}
                  {it.regression_count}）
                </>
              )}
            </div>
          </div>
          {it.feishu_message_id ? (
            <span
              style={{
                padding: "3px 8px",
                borderRadius: 4,
                background: "rgba(22,163,74,0.10)",
                color: D.ok,
                fontSize: 11,
              }}
            >
              {t("已推送飞书")}
            </span>
          ) : isAlert && !hasAnomaly ? (
            <span
              style={{
                padding: "3px 8px",
                borderRadius: 4,
                background: D.surfaceAlt,
                color: D.text2,
                fontSize: 11,
              }}
            >
              {t("无异常")}
            </span>
          ) : null}
        </div>
      </div>
    );
  };

  return (
    <div style={{ background: D.bg, minHeight: "100vh", color: D.text1 }}>
      <div style={{ maxWidth: 1200, margin: "0 auto", padding: "24px 32px" }}>
        <div
          className="j-rise"
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 16,
          }}
        >
          <div>
            <h1 style={{ fontSize: 22, fontWeight: 600, margin: 0 }}>📋 {t("报告与告警历史")}</h1>
            <p style={{ color: D.text2, fontSize: 13, marginTop: 4 }}>
              {t("最近")} {days} {t("天")} · {total} {t("条")}
              {totalPages > 1 && (
                <>
                  {" · "}
                  {t("第 X / Y 页").replace("X", String(page)).replace("Y", String(totalPages))}
                </>
              )}
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
          {(["all", "morning", "evening", "hourly_alert", "core_metric_alert"] as const).map((k) => (
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
              {k === "all"
                ? t("全部")
                : k === "morning"
                ? "🌅 日报"
                : k === "evening"
                ? "🌇 日内增量"
                : k === "hourly_alert"
                ? "🚨 实时告警"
                : "📉 核心指标"}
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
          <div style={{ color: D.text2, padding: 24, textAlign: "center" }}>
            {t("加载中…")}
          </div>
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
        {!loading && items.map(renderItem)}

        {/* 分页器 */}
        {!loading && totalPages > 1 && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 12,
              marginTop: 16,
              padding: "12px 0",
            }}
          >
            <button
              onClick={() => setPage(Math.max(1, page - 1))}
              disabled={page <= 1}
              style={{
                padding: "6px 14px",
                borderRadius: 6,
                border: `1px solid ${D.border}`,
                background: D.surface,
                color: page <= 1 ? D.text3 : D.text1,
                fontSize: 13,
                cursor: page <= 1 ? "not-allowed" : "pointer",
                opacity: page <= 1 ? 0.5 : 1,
              }}
            >
              ← {t("上一页")}
            </button>
            <span style={{ fontSize: 13, color: D.text2 }}>
              {page} / {totalPages}
            </span>
            <button
              onClick={() => setPage(Math.min(totalPages, page + 1))}
              disabled={page >= totalPages}
              style={{
                padding: "6px 14px",
                borderRadius: 6,
                border: `1px solid ${D.border}`,
                background: D.surface,
                color: page >= totalPages ? D.text3 : D.text1,
                fontSize: 13,
                cursor: page >= totalPages ? "not-allowed" : "pointer",
                opacity: page >= totalPages ? 0.5 : 1,
              }}
            >
              {t("下一页")} →
            </button>
          </div>
        )}
      </div>

      {/* 详情 modal */}
      {openItem !== null && (
        <div
          onClick={() => setOpenItem(null)}
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
              <strong style={{ fontSize: 15 }}>
                {openItem.kind === "hourly_alert" || openItem.kind === "core_metric_alert" ? t("告警详情") : t("报告详情")}
              </strong>
              {/* 早晚报详情：时间窗口切换；即时告警（hourly / core_metric）不需要 */}
              {openItem.kind !== "hourly_alert" && openItem.kind !== "core_metric_alert" && (
                <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                  <span style={{ fontSize: 11, color: D.text2 }}>{t("展示窗口")}：</span>
                  {([
                    [24, t("近 1 天")],
                    [168, t("近 7 天")],
                    [336, t("近 14 天")],
                    [720, t("近 30 天")],
                  ] as [CrashWindowHours, string][]).map(([w, label]) => (
                    <button
                      key={w}
                      onClick={() => setDetailWindow(w)}
                      style={{
                        padding: "2px 8px",
                        borderRadius: 4,
                        border: `1px solid ${detailWindow === w ? D.accent : D.border}`,
                        background: detailWindow === w ? "var(--j-accent-soft)" : "transparent",
                        color: detailWindow === w ? D.text1 : D.text2,
                        fontSize: 11,
                        cursor: "pointer",
                      }}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              )}
              <button
                onClick={() => setOpenItem(null)}
                style={{
                  border: "none",
                  background: "transparent",
                  cursor: "pointer",
                  fontSize: 18,
                  color: D.text2,
                }}
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

export default function CrashReportsHistoryPage() {
  return (
    <Suspense
      fallback={
        <div style={{ background: D.bg, minHeight: "100vh", color: D.text2, padding: 24 }}>
          加载中…
        </div>
      }
    >
      <CrashReportsHistoryInner />
    </Suspense>
  );
}
