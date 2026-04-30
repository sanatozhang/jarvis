"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  fetchCrashPullRequests,
  refreshCrashPr,
  syncAllCrashPrs,
  fetchAutoPrQueue,
  backfillAutoPr,
  type CrashPullRequestItem,
  type AutoPrQueueResponse,
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

function StatCard({ label, value, fg, bg }: { label: string; value: number; fg: string; bg: string }) {
  return (
    <div style={{ background: bg, border: `1px solid ${fg}33`, borderRadius: 6, padding: "10px 12px" }}>
      <div style={{ fontSize: 11, color: fg, fontWeight: 600 }}>{label}</div>
      <div style={{ fontSize: 22, color: fg, fontWeight: 700, marginTop: 2 }}>{value}</div>
    </div>
  );
}

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
  const [queue, setQueue] = useState<AutoPrQueueResponse | null>(null);
  const [backfilling, setBackfilling] = useState(false);
  const [backfillMsg, setBackfillMsg] = useState<string | null>(null);

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

  // 自动 PR 队列状态（每 15s 刷新）
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const q = await fetchAutoPrQueue();
        if (!cancelled) setQueue(q);
      } catch {}
    };
    tick();
    const id = setInterval(tick, 15000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [reloadKey]);

  const onBackfill = async () => {
    if (!confirm("将对所有未建过 PR 的 success 分析（feasibility≥阈值）补建 draft PR。继续？")) return;
    setBackfilling(true);
    setBackfillMsg(null);
    try {
      const r = await backfillAutoPr({ days: 14, dry_run: false, limit: 0 });
      setBackfillMsg(
        `扫描 ${r.scanned} · 触发 ${r.triggered} · 重复跳过 ${r.skipped_dup} · 失败 ${r.failed.length}`
      );
      setReloadKey((k) => k + 1);
    } catch (e) {
      setBackfillMsg(`补建失败：${String(e)}`);
    } finally {
      setBackfilling(false);
    }
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

        {/* 自动 PR 队列状态面板 */}
        {queue && (
          <div
            style={{
              background: D.surface,
              border: `1px solid ${D.border}`,
              borderRadius: 8,
              padding: "14px 16px",
              marginBottom: 16,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>
                🤖 {t("自动 PR 队列")} <span style={{ color: D.text3, fontWeight: 400 }}>· feasibility ≥ {queue.threshold.toFixed(2)}</span>
              </div>
              <button
                onClick={onBackfill}
                disabled={backfilling || queue.summary.pending === 0}
                style={{
                  padding: "5px 12px",
                  borderRadius: 6,
                  border: "none",
                  background: backfilling ? "#9CA3AF" : (queue.summary.pending > 0 ? D.accent : "#D1D5DB"),
                  color: "#FFFFFF",
                  fontSize: 12,
                  fontWeight: 600,
                  cursor: backfilling ? "wait" : (queue.summary.pending > 0 ? "pointer" : "not-allowed"),
                }}
                title={t("对未建 PR 的成功分析批量补建 draft PR")}
              >
                {backfilling ? t("补建中...") : `🔧 ${t("一键补建")} (${queue.summary.pending})`}
              </button>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12 }}>
              <StatCard label={t("待生成")} value={queue.summary.pending} fg={D.warn} bg={D.warnBg} />
              <StatCard label={t("分析中")} value={queue.summary.running} fg={D.info} bg={D.infoBg} />
              <StatCard label={t("近期 PR")} value={queue.summary.recent_prs} fg={D.ok} bg={D.okBg} />
              <StatCard label={t("近期失败")} value={queue.summary.recent_failures} fg={D.danger} bg="rgba(220,38,38,0.10)" />
            </div>
            {backfillMsg && (
              <div style={{ marginTop: 10, padding: "6px 10px", background: D.infoBg, borderRadius: 6, fontSize: 12, color: D.text1 }}>
                {backfillMsg}
              </div>
            )}
            {queue.recent_failures.length > 0 && (
              <details style={{ marginTop: 10, fontSize: 12 }}>
                <summary style={{ cursor: "pointer", color: D.text2 }}>
                  {t("展开近期失败原因")} ({queue.recent_failures.length})
                </summary>
                <div style={{ marginTop: 6, maxHeight: 200, overflowY: "auto" }}>
                  {queue.recent_failures.map((f, i) => (
                    <div key={i} style={{ padding: "4px 0", borderBottom: `1px solid ${D.border}`, color: D.text2 }}>
                      <code style={{ color: D.danger }}>ana={String(f.analysis_id)}</code> · {f.error}
                    </div>
                  ))}
                </div>
              </details>
            )}
            {queue.pending.length > 0 && (
              <details style={{ marginTop: 6, fontSize: 12 }}>
                <summary style={{ cursor: "pointer", color: D.text2 }}>
                  {t("展开待生成清单")} ({queue.pending.length})
                </summary>
                <div style={{ marginTop: 6, maxHeight: 240, overflowY: "auto" }}>
                  {queue.pending.map((p, i) => (
                    <div key={i} style={{ padding: "4px 0", borderBottom: `1px solid ${D.border}` }}>
                      <span style={{ color: D.text3, marginRight: 6 }}>[{p.platform}]</span>
                      <span style={{ color: D.text1 }}>{p.title || p.datadog_issue_id}</span>
                      <span style={{ marginLeft: 8, color: D.text3 }}>
                        feas={(p.feasibility_score ?? 0).toFixed(2)}
                      </span>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </div>
        )}

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
