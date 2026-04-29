"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  fetchCrashPullRequests,
  refreshCrashPr,
  syncAllCrashPrs,
  type CrashPullRequestItem,
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
  okBg: "rgba(22,163,74,0.10)",
  warn: "#D97706",
  warnBg: "rgba(217,119,6,0.10)",
  danger: "#DC2626",
  info: "#2563EB",
  infoBg: "rgba(37,99,235,0.10)",
} as const;

const STATUS_COLOR: Record<string, { fg: string; bg: string }> = {
  draft: { fg: D.warn, bg: D.warnBg },
  open: { fg: D.info, bg: D.infoBg },
  merged: { fg: D.ok, bg: D.okBg },
  closed: { fg: D.text3, bg: "rgba(0,0,0,0.05)" },
};

export default function CrashPullRequestsPage() {
  const t = useT();
  const [items, setItems] = useState<CrashPullRequestItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<"all" | "draft" | "open" | "merged" | "closed">("all");
  const [repoFilter, setRepoFilter] = useState<"all" | "flutter" | "android" | "ios" | "app">("all");
  const [days, setDays] = useState(30);
  const [syncingIds, setSyncingIds] = useState<Set<number>>(new Set());
  const [syncingAll, setSyncingAll] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  const refreshOne = async (prId: number) => {
    setSyncingIds((s) => new Set(s).add(prId));
    try {
      await refreshCrashPr(prId);
    } catch (e) {
      console.error("refresh pr failed", e);
    } finally {
      setSyncingIds((s) => {
        const n = new Set(s);
        n.delete(prId);
        return n;
      });
      setReloadKey((k) => k + 1);
    }
  };

  const refreshAll = async () => {
    setSyncingAll(true);
    try {
      await syncAllCrashPrs();
    } catch (e) {
      console.error("sync all failed", e);
    } finally {
      setSyncingAll(false);
      setReloadKey((k) => k + 1);
    }
  };

  const fmtSyncTime = (iso: string | null): string => {
    if (!iso) return "";
    const d = new Date(iso);
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "刚刚";
    if (mins < 60) return `${mins}分钟前`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}小时前`;
    return `${Math.floor(hrs / 24)}天前`;
  };

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchCrashPullRequests({
      days,
      status: statusFilter === "all" ? undefined : statusFilter,
      repo: repoFilter === "all" ? undefined : repoFilter,
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
  }, [statusFilter, repoFilter, days, reloadKey]);

  return (
    <div style={{ background: D.bg, minHeight: "100vh", color: D.text1 }}>
      <div style={{ maxWidth: 1200, margin: "0 auto", padding: "24px 32px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
          <div>
            <h1 style={{ fontSize: 22, fontWeight: 600, margin: 0 }}>🔧 自动 PR 列表</h1>
            <p style={{ color: D.text2, fontSize: 13, marginTop: 4 }}>
              {t("最近")} {days} {t("天")} · {items.length} {t("个 PR")}
            </p>
          </div>
          <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
            <button
              onClick={refreshAll}
              disabled={syncingAll}
              style={{
                padding: "6px 12px",
                borderRadius: 6,
                border: `1px solid ${D.border}`,
                background: syncingAll ? "rgba(0,0,0,0.04)" : D.surface,
                color: D.text1,
                fontSize: 12,
                cursor: syncingAll ? "wait" : "pointer",
              }}
            >
              {syncingAll ? t("同步中…") : t("⟳ 同步全部状态")}
            </button>
            <Link href="/crashguard" style={{ color: D.accent, fontSize: 13, textDecoration: "none" }}>
              ← {t("返回主页")}
            </Link>
          </div>
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
            flexWrap: "wrap",
          }}
        >
          <span style={{ color: D.text2, fontSize: 13 }}>{t("状态")}：</span>
          {(["all", "draft", "open", "merged", "closed"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setStatusFilter(k)}
              style={{
                padding: "4px 10px",
                borderRadius: 6,
                border: `1px solid ${statusFilter === k ? D.accent : D.border}`,
                background: statusFilter === k ? D.accent : "transparent",
                color: statusFilter === k ? "white" : D.text1,
                fontSize: 12,
                cursor: "pointer",
              }}
            >
              {k === "all" ? t("全部") : k}
            </button>
          ))}
          <span style={{ color: D.text2, fontSize: 13, marginLeft: 12 }}>{t("仓库")}：</span>
          {(["all", "flutter", "android", "ios", "app"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setRepoFilter(k)}
              style={{
                padding: "4px 10px",
                borderRadius: 6,
                border: `1px solid ${repoFilter === k ? D.accent : D.border}`,
                background: repoFilter === k ? D.accent : "transparent",
                color: repoFilter === k ? "white" : D.text1,
                fontSize: 12,
                cursor: "pointer",
              }}
            >
              {k}
            </button>
          ))}
          <span style={{ flex: 1 }} />
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
            {t("暂无自动 PR — 早报触发的高 feasibility 分析才会自动建 PR")}
          </div>
        )}

        {/* 列表 */}
        {!loading &&
          items.map((pr) => {
            const sc = STATUS_COLOR[pr.pr_status] || STATUS_COLOR.draft;
            return (
              <div
                key={pr.id}
                style={{
                  background: D.surface,
                  border: `1px solid ${D.border}`,
                  borderRadius: 8,
                  padding: "14px 18px",
                  marginBottom: 8,
                  display: "flex",
                  alignItems: "center",
                  gap: 14,
                }}
              >
                <span
                  style={{
                    padding: "3px 9px",
                    borderRadius: 4,
                    background: sc.bg,
                    color: sc.fg,
                    fontSize: 11,
                    fontWeight: 600,
                    textTransform: "uppercase",
                    minWidth: 56,
                    textAlign: "center",
                  }}
                >
                  {pr.pr_status}
                </span>
                <span
                  style={{
                    padding: "3px 8px",
                    borderRadius: 4,
                    background: "rgba(184,146,46,0.08)",
                    color: D.accent,
                    fontSize: 11,
                  }}
                >
                  {pr.repo || "—"}
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 14,
                      fontWeight: 500,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {pr.title || pr.datadog_issue_id}
                  </div>
                  <div
                    style={{
                      fontSize: 11,
                      color: D.text3,
                      marginTop: 3,
                      display: "flex",
                      gap: 12,
                    }}
                  >
                    <span>{pr.branch_name}</span>
                    <span>·</span>
                    <span>feasibility {pr.feasibility.toFixed(2)}</span>
                    <span>·</span>
                    <span>{pr.triggered_by}</span>
                    {pr.created_at && (
                      <>
                        <span>·</span>
                        <span>{pr.created_at.slice(0, 10)}</span>
                      </>
                    )}
                    {pr.last_synced_at && (
                      <>
                        <span>·</span>
                        <span title={pr.last_synced_at}>
                          {t("同步")} {fmtSyncTime(pr.last_synced_at)}
                        </span>
                      </>
                    )}
                  </div>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {pr.pr_status !== "merged" && pr.pr_status !== "closed" && (
                    <button
                      onClick={() => refreshOne(pr.id)}
                      disabled={syncingIds.has(pr.id)}
                      title={t("刷新此 PR 状态")}
                      style={{
                        padding: "6px 10px",
                        borderRadius: 6,
                        border: `1px solid ${D.border}`,
                        background: syncingIds.has(pr.id) ? "rgba(0,0,0,0.04)" : D.surface,
                        color: D.text1,
                        fontSize: 12,
                        cursor: syncingIds.has(pr.id) ? "wait" : "pointer",
                      }}
                    >
                      {syncingIds.has(pr.id) ? "…" : "⟳"}
                    </button>
                  )}
                  <Link
                    href={`/crashguard?issue=${encodeURIComponent(pr.datadog_issue_id)}`}
                    style={{
                      padding: "6px 12px",
                      borderRadius: 6,
                      border: `1px solid ${D.border}`,
                      color: D.text1,
                      fontSize: 12,
                      textDecoration: "none",
                    }}
                  >
                    Issue
                  </Link>
                  {pr.pr_url && (
                    <a
                      href={pr.pr_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      style={{
                        padding: "6px 12px",
                        borderRadius: 6,
                        background: D.accent,
                        color: "white",
                        fontSize: 12,
                        textDecoration: "none",
                      }}
                    >
                      PR ↗
                    </a>
                  )}
                </div>
              </div>
            );
          })}
      </div>
    </div>
  );
}
