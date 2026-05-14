"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  fetchCrashJobsStatus,
  fetchCrashJobHeartbeats,
  triggerCrashJobNow,
  fetchAlertChannelsStatus,
  formatSGT,
  type CrashJobStatusItem,
  type CrashJobHeartbeatItem,
  type AlertChannelsStatus,
} from "@/lib/api";
import { useT } from "@/lib/i18n";

const D = {
  bg: "#F8F9FA",
  surface: "#FFFFFF",
  border: "rgba(0,0,0,0.08)",
  text1: "#111827",
  text2: "#6B7280",
  text3: "#9CA3AF",
  accent: "#B8922E",
  ok: "#16A34A",
  warn: "#D97706",
  danger: "#DC2626",
} as const;

const HEALTH_COLORS: Record<CrashJobStatusItem["health"], string> = {
  ok: D.ok,
  degraded: D.warn,
  failing: D.danger,
  stale: D.danger,
};

const HEALTH_LABEL: Record<CrashJobStatusItem["health"], string> = {
  ok: "正常",
  degraded: "降级",
  failing: "连续失败",
  stale: "超期未跑",
};

function _fmtTime(s: string | null): string {
  if (!s) return "—";
  // server 给的是 naive UTC datetime；展示统一锚定到 SGT（UTC+8）
  return formatSGT(s);
}

function _fmtAgo(s: string | null, now: Date): string {
  if (!s) return "—";
  const dt = new Date(s + (s.endsWith("Z") ? "" : "Z"));
  const diffSec = Math.floor((now.getTime() - dt.getTime()) / 1000);
  if (diffSec < 60) return `${diffSec} 秒前`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  return `${Math.floor(diffSec / 86400)} 天前`;
}

export default function CrashguardJobsPage() {
  const t = useT();
  const [items, setItems] = useState<CrashJobStatusItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(new Date());
  const [openJob, setOpenJob] = useState<string | null>(null);
  const [history, setHistory] = useState<CrashJobHeartbeatItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [running, setRunning] = useState<Record<string, boolean>>({});
  const [toast, setToast] = useState<string | null>(null);
  const [alertChannels, setAlertChannels] = useState<AlertChannelsStatus | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetchCrashJobsStatus();
      setItems(r.items);
      setError(null);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadAlertChannels = useCallback(async () => {
    try {
      const r = await fetchAlertChannelsStatus();
      setAlertChannels(r);
    } catch {
      // 静默失败：tile 显示 "—" 不影响主表格
    }
  }, []);

  // 初次加载 + 每 30s 自动刷新（让运维放着看）
  useEffect(() => {
    load();
    loadAlertChannels();
    const t1 = setInterval(load, 30_000);
    const t2 = setInterval(() => setNow(new Date()), 5_000);
    const t3 = setInterval(loadAlertChannels, 30_000);
    return () => {
      clearInterval(t1);
      clearInterval(t2);
      clearInterval(t3);
    };
  }, [load, loadAlertChannels]);

  const runNow = async (jobName: string) => {
    if (running[jobName]) return;
    setRunning((m) => ({ ...m, [jobName]: true }));
    setToast(null);
    try {
      const r = await triggerCrashJobNow(jobName);
      const summary = (r.result as any) || {};
      // 简短摘要：skipped / alerted / ok
      let label = "ok";
      if (summary.skipped) label = `skipped: ${summary.skipped}`;
      else if (summary.alerted) label = "alerted ✅";
      else if (summary.error) label = `error: ${String(summary.error).slice(0, 80)}`;
      setToast(`✓ ${jobName} → ${label}`);
      await load();  // 刷新状态表
    } catch (e: any) {
      setToast(`✗ ${jobName} 失败：${String(e?.message || e).slice(0, 120)}`);
    } finally {
      setRunning((m) => ({ ...m, [jobName]: false }));
    }
  };

  const openHistory = async (jobName: string) => {
    setOpenJob(jobName);
    setHistoryLoading(true);
    setHistory([]);
    try {
      const r = await fetchCrashJobHeartbeats(jobName, 50);
      setHistory(r.items);
    } catch (e: any) {
      setError(String(e?.message || e));
    } finally {
      setHistoryLoading(false);
    }
  };

  return (
    <div style={{ background: D.bg, minHeight: "100vh", padding: 24 }}>
      <div style={{ maxWidth: 1200, margin: "0 auto" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <div>
            <h1 style={{ fontSize: 22, fontWeight: 600 }}>📊 {t("定时任务监控")}</h1>
            <p style={{ fontSize: 13, color: D.text2, marginTop: 4 }}>
              {t("每 30 秒自动刷新；")}{t("超期未跑/连续失败会以红色高亮")}
            </p>
          </div>
          <Link href="/crashguard" style={{ color: D.accent, fontSize: 13, textDecoration: "none" }}>
            ← {t("返回主页")}
          </Link>
        </div>

        {/* Alert Channels Tile */}
        <div style={{ background: D.surface, borderRadius: 8, border: `1px solid ${D.border}`, padding: "16px 20px", marginBottom: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <span style={{ fontSize: 13, fontWeight: 600, color: D.text1 }}>
              📡 {t("Alert Channels")} — {t("近 24h 命中")}
            </span>
            {alertChannels && (
              <span style={{ fontSize: 11, color: D.text3 }}>
                {t("更新于")} {formatSGT(alertChannels.as_of)} SGT
                &nbsp;·&nbsp;
                {t("cache")} {alertChannels.datadog_cache.count} {t("keys")}
                &nbsp;·&nbsp;
                {t("审计行")} {alertChannels.audit_rows_24h}
              </span>
            )}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
            {alertChannels
              ? alertChannels.channels.map((ch) => {
                  const badgeColor = !ch.enabled ? D.danger : ch.shadow_mode ? D.text3 : D.ok;
                  const badgeLabel = !ch.enabled ? "OFF" : ch.shadow_mode ? t("影子") : "ON";
                  const badgeBg = !ch.enabled ? "rgba(220,38,38,0.10)" : ch.shadow_mode ? "rgba(156,163,175,0.15)" : "rgba(22,163,74,0.10)";
                  return (
                    <div
                      key={ch.name}
                      style={{
                        background: "#F9FAFB",
                        borderRadius: 6,
                        padding: "12px 14px",
                        border: `1px solid ${D.border}`,
                      }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                        <span style={{ fontSize: 11, color: D.text2, fontWeight: 500 }}>{ch.label}</span>
                        <span
                          style={{
                            fontSize: 10,
                            fontWeight: 700,
                            padding: "1px 6px",
                            borderRadius: 3,
                            background: badgeBg,
                            color: badgeColor,
                          }}
                        >
                          {badgeLabel}
                        </span>
                      </div>
                      <div style={{ fontSize: 28, fontWeight: 700, color: D.text1, lineHeight: 1 }}>
                        {ch.count_24h}
                      </div>
                      <div style={{ fontSize: 10, color: D.text3, marginTop: 4 }}>
                        {t("命中")}
                      </div>
                    </div>
                  );
                })
              : Array.from({ length: 4 }).map((_, i) => (
                  <div
                    key={i}
                    style={{
                      background: "#F9FAFB",
                      borderRadius: 6,
                      padding: "12px 14px",
                      border: `1px solid ${D.border}`,
                      minHeight: 70,
                    }}
                  >
                    <div style={{ color: D.text3, fontSize: 12 }}>{t("加载中…")}</div>
                  </div>
                ))}
          </div>
        </div>

        {error && (
          <div style={{ padding: 12, background: "rgba(220,38,38,0.08)", color: D.danger, borderRadius: 6, marginBottom: 12 }}>
            {error}
          </div>
        )}
        {toast && (
          <div
            style={{
              padding: 10,
              background: toast.startsWith("✓") ? "rgba(22,163,74,0.10)" : "rgba(220,38,38,0.10)",
              color: toast.startsWith("✓") ? D.ok : D.danger,
              borderRadius: 6,
              marginBottom: 12,
              fontSize: 13,
            }}
          >
            {toast}
          </div>
        )}

        <div style={{ background: D.surface, borderRadius: 8, border: `1px solid ${D.border}`, overflow: "hidden" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ background: "#F9FAFB", textAlign: "left" }}>
                <th style={{ padding: "10px 14px" }}>{t("任务")}</th>
                <th style={{ padding: "10px 14px" }}>Cron</th>
                <th style={{ padding: "10px 14px" }}>{t("健康度")}</th>
                <th style={{ padding: "10px 14px" }}>{t("上次")}</th>
                <th style={{ padding: "10px 14px" }}>{t("下次")}</th>
                <th style={{ padding: "10px 14px" }}>{t("连续失败")}</th>
                <th style={{ padding: "10px 14px" }}>{t("耗时")}</th>
                <th style={{ padding: "10px 14px" }}>{t("操作")}</th>
              </tr>
            </thead>
            <tbody>
              {loading && items.length === 0 && (
                <tr>
                  <td colSpan={8} style={{ padding: 24, textAlign: "center", color: D.text2 }}>
                    {t("加载中…")}
                  </td>
                </tr>
              )}
              {items.map((it) => {
                const color = HEALTH_COLORS[it.health];
                return (
                  <tr key={it.name} style={{ borderTop: `1px solid ${D.border}` }}>
                    <td style={{ padding: "12px 14px" }}>
                      <div style={{ fontWeight: 600 }}>{it.label}</div>
                      <div style={{ fontSize: 11, color: D.text3, marginTop: 2 }}>{it.desc}</div>
                    </td>
                    <td style={{ padding: "12px 14px", fontFamily: "monospace", color: D.text2 }}>
                      {it.cron || "—"}
                      {!it.enabled && (
                        <div style={{ color: D.text3, fontSize: 11 }}>({t("已禁用")})</div>
                      )}
                    </td>
                    <td style={{ padding: "12px 14px" }}>
                      <span
                        style={{
                          padding: "2px 8px",
                          borderRadius: 4,
                          background: `${color}1A`,
                          color,
                          fontSize: 11,
                          fontWeight: 600,
                        }}
                      >
                        ● {HEALTH_LABEL[it.health]}
                      </span>
                    </td>
                    <td style={{ padding: "12px 14px" }}>
                      <div style={{ fontSize: 12 }}>{_fmtAgo(it.last_fired_at, now)}</div>
                      <div style={{ fontSize: 11, color: D.text3 }}>{_fmtTime(it.last_fired_at)} SGT</div>
                      {it.last_status === "failed" && it.last_error && (
                        <div style={{ fontSize: 11, color: D.danger, marginTop: 2, maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                          ⚠️ {it.last_error}
                        </div>
                      )}
                    </td>
                    <td style={{ padding: "12px 14px", fontSize: 12, color: D.text2 }}>
                      {_fmtTime(it.next_fire_at)}
                    </td>
                    <td style={{ padding: "12px 14px", textAlign: "center" }}>
                      <span style={{ color: it.consecutive_failures > 0 ? D.danger : D.text3, fontWeight: it.consecutive_failures > 0 ? 600 : 400 }}>
                        {it.consecutive_failures}
                      </span>
                      <span style={{ color: D.text3, fontSize: 11 }}> / {it.fail_count_in_recent_50} {t("近 50 次")}</span>
                    </td>
                    <td style={{ padding: "12px 14px", color: D.text2 }}>
                      {it.last_duration_ms ? `${it.last_duration_ms} ms` : "—"}
                    </td>
                    <td style={{ padding: "12px 14px", display: "flex", gap: 6 }}>
                      <button
                        onClick={() => runNow(it.name)}
                        disabled={!!running[it.name]}
                        style={{
                          padding: "4px 10px",
                          fontSize: 11,
                          border: `1px solid ${D.accent}`,
                          borderRadius: 4,
                          background: D.accent + "1A",
                          color: D.accent,
                          cursor: running[it.name] ? "wait" : "pointer",
                          opacity: running[it.name] ? 0.6 : 1,
                        }}
                        title={t("立即触发该任务一次")}
                      >
                        {running[it.name] ? "⏳" : "▶"} {t("立即触发")}
                      </button>
                      <button
                        onClick={() => openHistory(it.name)}
                        style={{
                          padding: "4px 10px",
                          fontSize: 11,
                          border: `1px solid ${D.border}`,
                          borderRadius: 4,
                          background: "transparent",
                          color: D.text1,
                          cursor: "pointer",
                        }}
                      >
                        {t("查看历史")}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* 历史 modal */}
      {openJob && (
        <div
          onClick={() => setOpenJob(null)}
          style={{
            position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 50,
            display: "flex", alignItems: "center", justifyContent: "center", padding: 24,
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              width: "min(900px, 95vw)", maxHeight: "85vh",
              background: D.surface, borderRadius: 12, display: "flex", flexDirection: "column", overflow: "hidden",
            }}
          >
            <div style={{ padding: "14px 20px", borderBottom: `1px solid ${D.border}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <strong>{openJob} {t("最近 50 次心跳")}</strong>
              <button onClick={() => setOpenJob(null)} style={{ border: "none", background: "transparent", fontSize: 18, cursor: "pointer", color: D.text2 }}>✕</button>
            </div>
            <div style={{ overflow: "auto", padding: 16, flex: 1 }}>
              {historyLoading ? (
                <div style={{ color: D.text2 }}>{t("加载中…")}</div>
              ) : history.length === 0 ? (
                <div style={{ color: D.text3 }}>{t("暂无心跳记录")}</div>
              ) : (
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                  <thead>
                    <tr style={{ background: "#F9FAFB", textAlign: "left" }}>
                      <th style={{ padding: "8px 10px" }}>{t("时间 (SGT)")}</th>
                      <th style={{ padding: "8px 10px" }}>{t("状态")}</th>
                      <th style={{ padding: "8px 10px" }}>{t("耗时")}</th>
                      <th style={{ padding: "8px 10px" }}>{t("摘要")} / Error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((h) => {
                      const color = h.status === "success" ? D.ok
                        : h.status === "failed" ? D.danger
                        : h.status === "degraded" ? D.warn
                        : D.text3;
                      return (
                        <tr key={h.id} style={{ borderTop: `1px solid ${D.border}` }}>
                          <td style={{ padding: "8px 10px", color: D.text2 }}>{_fmtTime(h.fired_at)}</td>
                          <td style={{ padding: "8px 10px", color, fontWeight: 600 }}>{h.status}</td>
                          <td style={{ padding: "8px 10px", color: D.text2 }}>{h.duration_ms} ms</td>
                          <td style={{ padding: "8px 10px", fontFamily: "monospace", fontSize: 11, color: (h.status === "failed" || h.status === "degraded") ? color : D.text2, maxWidth: 460, wordBreak: "break-word" }}>
                            {(h.status === "failed" || h.status === "degraded") && h.error ? h.error : JSON.stringify(h.summary)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
