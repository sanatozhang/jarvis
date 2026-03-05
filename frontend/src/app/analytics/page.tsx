"use client";

import { useT } from "@/lib/i18n";
import { useEffect, useState, useCallback } from "react";
import { fetchRuleAccuracy, fetchIssueDetail, formatLocalTime, type RuleAccuracyStat, type LocalIssueItem } from "@/lib/api";

interface FailReasonItem {
  issue_id?: string;
  reason?: string;
  error?: string;
  username?: string;
  duration_ms?: number;
  created_at?: string;
}

interface Analytics {
  date_from: string; date_to: string;
  event_counts: Record<string, number>;
  unique_users: number;
  avg_analysis_duration_ms: number; avg_analysis_duration_min: number;
  total_analyses: number; successful_analyses: number; failed_analyses: number;
  feedback_submitted: number; escalations: number;
  fail_reasons: FailReasonItem[];
  daily: Record<string, Record<string, number>>;
  top_users: { username: string; count: number }[];
  value_metrics: {
    time_saved_hours: number; time_saved_per_ticket_min: number;
    success_rate: number; estimated_manual_hours: number; estimated_ai_hours: number;
  };
}

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", accent: "#B8922E", accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
};

function StatCard({ label, value, sub, color }: { label: string; value: string | number; sub?: string; color?: string }) {
  return (
    <div className="rounded-xl px-4 py-4" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
      <p className="text-xs" style={{ color: S.text3 }}>{label}</p>
      <p className="mt-1 text-2xl font-bold tabular-nums" style={{ color: color || S.text1 }}>{value}</p>
      {sub && <p className="mt-0.5 text-[11px] font-mono" style={{ color: S.text3 }}>{sub}</p>}
    </div>
  );
}

function Bar({ value, max, color }: { value: number; max: number; color: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="h-4 w-full overflow-hidden rounded-full" style={{ background: S.hover }}>
      <div className="h-full rounded-full transition-all duration-700" style={{ width: `${pct}%`, background: color }} />
    </div>
  );
}

export default function AnalyticsPage() {
  const t = useT();
  const [data, setData] = useState<Analytics | null>(null);
  const [days, setDays] = useState(7);
  const [customDays, setCustomDays] = useState("");
  const [loading, setLoading] = useState(true);
  const [ruleAccuracy, setRuleAccuracy] = useState<RuleAccuracyStat[]>([]);
  const [expandedReason, setExpandedReason] = useState<string | null>(null);
  const [issueDetails, setIssueDetails] = useState<Record<string, LocalIssueItem | "loading" | "error">>({});

  const loadIssueDetail = useCallback(async (issueId: string) => {
    if (!issueId || issueDetails[issueId]) return;
    setIssueDetails((prev) => ({ ...prev, [issueId]: "loading" }));
    try {
      const detail = await fetchIssueDetail(issueId);
      setIssueDetails((prev) => ({ ...prev, [issueId]: detail }));
    } catch {
      setIssueDetails((prev) => ({ ...prev, [issueId]: "error" }));
    }
  }, [issueDetails]);

  const load = async (d: number) => {
    setLoading(true);
    try {
      const [res, ra] = await Promise.all([
        fetch(`/api/analytics/dashboard?days=${d}`),
        fetchRuleAccuracy(d).catch(() => []),
      ]);
      if (res.ok) setData(await res.json());
      setRuleAccuracy(ra);
    } catch {} finally { setLoading(false); }
  };

  useEffect(() => { load(days); }, [days]);

  const dailyDates = data ? Object.keys(data.daily).sort() : [];
  const maxDaily = data ? Math.max(1, ...dailyDates.map((d) => {
    const day = data.daily[d];
    return (day.analysis_start || 0) + (day.feedback_submit || 0);
  })) : 1;

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("数据看板")}</h1>
            <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("项目价值 & 使用情况统计")}</p>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 rounded-lg p-1" style={{ background: S.overlay }}>
              {[7, 30, 90, 180, 365].map((d) => (
                <button key={d} onClick={() => { setDays(d); setCustomDays(""); }}
                  className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
                  style={days === d && !customDays
                    ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }
                    : { color: S.text3 }}>
                  {d >= 365 ? `${d / 365}${t("年")}` : d >= 30 ? `${d / 30}${t("月")}` : `${d}${t("天")}`}
                </button>
              ))}
            </div>
            <form onSubmit={(e) => {
              e.preventDefault();
              const v = parseInt(customDays);
              if (v > 0) setDays(v);
            }} className="flex items-center gap-1">
              <input
                type="number" min={1} max={3650}
                value={customDays}
                onChange={(e) => setCustomDays(e.target.value)}
                placeholder={t("自定义天数")}
                className="w-24 rounded-lg px-2 py-1.5 text-sm font-mono outline-none"
                style={{ background: S.overlay, border: `1px solid ${customDays ? S.accent : S.border}`, color: S.text1 }}
              />
              {customDays && (
                <button type="submit"
                  className="rounded-lg px-2 py-1.5 text-sm font-medium"
                  style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(184,146,46,0.3)" }}>
                  ↵
                </button>
              )}
            </form>
          </div>
        </div>
      </header>

      {loading && !data ? (
        <div className="flex items-center justify-center py-24">
          <div className="h-8 w-8 animate-spin rounded-full border-4"
            style={{ borderColor: "rgba(0,0,0,0.08)", borderTopColor: S.accent }} />
        </div>
      ) : !data ? (
        <p className="py-24 text-center text-sm" style={{ color: S.text3 }}>{t("暂无数据")}</p>
      ) : (
        <div className="mx-auto max-w-4xl px-6 py-6 space-y-5">

          {/* Value metrics hero */}
          <section className="rounded-2xl p-6 relative overflow-hidden"
            style={{ background: "linear-gradient(135deg, #FFFFFF 0%, #F8F9FA 60%, rgba(184,146,46,0.06) 100%)", border: `1px solid ${S.border}` }}>
            {/* Decorative accent */}
            <div className="absolute top-0 right-0 h-32 w-32 rounded-full opacity-10 blur-3xl"
              style={{ background: S.accent }} />
            <div className="flex items-center gap-2 mb-4">
              <span className="rounded-lg px-2 py-0.5 text-[11px] font-semibold"
                style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(184,146,46,0.25)" }}>
                {t("项目价值")}
              </span>
              <span className="text-xs" style={{ color: S.text3 }}>{t("过去")} {days} {t("天")}</span>
            </div>
            <div className="grid grid-cols-3 gap-6 relative">
              <div>
                <p className="text-4xl font-bold tabular-nums" style={{ color: S.text1 }}>
                  {data.value_metrics.time_saved_hours}
                  <span className="text-xl font-normal ml-1" style={{ color: S.text3 }}>{t("小时")}</span>
                </p>
                <p className="mt-1.5 text-xs" style={{ color: S.text3 }}>{t("预估节省工时")}</p>
              </div>
              <div>
                <p className="text-4xl font-bold tabular-nums" style={{ color: S.accent }}>
                  {data.value_metrics.time_saved_per_ticket_min}
                  <span className="text-xl font-normal ml-1" style={{ color: S.text3 }}>{t("分钟/单")}</span>
                </p>
                <p className="mt-1.5 text-xs" style={{ color: S.text3 }}>{t("每单节省时间")}</p>
              </div>
              <div>
                <p className="text-4xl font-bold tabular-nums" style={{ color: "#16A34A" }}>
                  {data.value_metrics.success_rate}
                  <span className="text-xl font-normal ml-0.5" style={{ color: S.text3 }}>%</span>
                </p>
                <p className="mt-1.5 text-xs" style={{ color: S.text3 }}>{t("分析成功率")}</p>
              </div>
            </div>
            <p className="mt-4 text-[11px] font-mono" style={{ color: S.text3 }}>
              {t("对比")}: {t("人工处理")} ~{data.value_metrics.estimated_manual_hours}h → {t("AI 处理")} ~{data.value_metrics.estimated_ai_hours}h
            </p>
          </section>

          {/* Key metrics */}
          <div className="grid grid-cols-5 gap-3">
            <StatCard label={t("总分析次数")} value={data.total_analyses} />
            <StatCard label={t("分析成功")} value={data.successful_analyses} color="#16A34A" />
            <StatCard label={t("分析失败")} value={data.failed_analyses} color="#DC2626" />
            <StatCard label={t("反馈提交")} value={data.feedback_submitted} color="#2563EB" />
            <StatCard label={t("活跃用户")} value={data.unique_users} color="#7C3AED" />
          </div>

          <div className="grid grid-cols-3 gap-3">
            <StatCard label={t("平均分析耗时")} value={`${data.avg_analysis_duration_min} ${t("分钟")}`} sub={`${data.avg_analysis_duration_ms}ms`} />
            <StatCard label={t("工单转工程师")} value={data.escalations} color={S.accent} />
            <StatCard label={t("页面访问")} value={data.event_counts.page_visit || 0} />
          </div>

          {/* Daily trend */}
          <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <h2 className="mb-4 text-sm font-semibold" style={{ color: S.text1 }}>{t("每日趋势")}</h2>
            {dailyDates.length === 0 ? (
              <p className="py-8 text-center text-sm" style={{ color: S.text3 }}>{t("暂无数据")}</p>
            ) : (
              <div className="space-y-2.5">
                {dailyDates.map((date) => {
                  const day = data.daily[date];
                  const analyses = day.analysis_start || 0;
                  const success = day.analysis_done || 0;
                  const fail = day.analysis_fail || 0;
                  const feedback = day.feedback_submit || 0;
                  return (
                    <div key={date} className="flex items-center gap-3">
                      <span className="w-20 flex-shrink-0 font-mono text-xs" style={{ color: S.text3 }}>{date.slice(5)}</span>
                      <div className="flex-1">
                        <Bar value={analyses + feedback} max={maxDaily} color={S.accent} />
                      </div>
                      <div className="flex w-44 flex-shrink-0 items-center gap-3 text-[11px] font-mono">
                        <span style={{ color: S.text2 }}>{analyses} {t("分析")}</span>
                        <span style={{ color: "#16A34A" }}>{success} ✓</span>
                        {fail > 0 && <span style={{ color: "#DC2626" }}>{fail} ✗</span>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* Top users + fail reasons */}
          <div className="grid grid-cols-2 gap-4">
            <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
              <h2 className="mb-3 text-sm font-semibold" style={{ color: S.text1 }}>{t("活跃用户 Top 10")}</h2>
              {data.top_users.length === 0 ? (
                <p className="py-4 text-center text-sm" style={{ color: S.text3 }}>{t("暂无数据")}</p>
              ) : (
                <div className="space-y-1.5">
                  {data.top_users.map((u, i) => (
                    <a key={u.username} href={`/tracking?created_by=${encodeURIComponent(u.username)}`}
                      className="flex items-center justify-between rounded-lg px-2 py-1.5 transition-colors"
                      style={{ color: "inherit" }}
                      onMouseEnter={(e) => (e.currentTarget.style.background = S.overlay)}
                      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
                      <div className="flex items-center gap-2">
                        <span className="flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold"
                          style={{ background: i < 3 ? S.accentBg : S.overlay, color: i < 3 ? S.accent : S.text3 }}>
                          {i + 1}
                        </span>
                        <span className="text-sm hover:underline" style={{ color: "#2563EB" }}>{u.username}</span>
                      </div>
                      <span className="text-xs tabular-nums font-mono" style={{ color: S.text3 }}>
                        {u.count} {t("次操作")}
                      </span>
                    </a>
                  ))}
                </div>
              )}
            </section>

            <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
              <h2 className="mb-3 text-sm font-semibold" style={{ color: S.text1 }}>{t("失败原因分布")}</h2>
              {data.fail_reasons.length === 0 ? (
                <p className="py-4 text-center text-sm" style={{ color: S.text3 }}>{t("暂无失败记录")}</p>
              ) : (() => {
                const grouped: Record<string, FailReasonItem[]> = {};
                data.fail_reasons.forEach((f) => {
                  const r = f.reason || t("未知");
                  if (!grouped[r]) grouped[r] = [];
                  grouped[r].push(f);
                });
                return (
                  <div className="space-y-1">
                    {Object.entries(grouped).sort((a, b) => b[1].length - a[1].length).map(([reason, items]) => {
                      const isExpanded = expandedReason === reason;
                      return (
                        <div key={reason}>
                          <button
                            onClick={() => {
                              setExpandedReason(isExpanded ? null : reason);
                              if (!isExpanded) {
                                items.forEach((item) => { if (item.issue_id) loadIssueDetail(item.issue_id); });
                              }
                            }}
                            className="flex w-full items-center justify-between rounded-lg px-2 py-1.5 transition-colors text-left"
                            onMouseEnter={(e) => (e.currentTarget.style.background = S.overlay)}
                            onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
                            <div className="flex items-center gap-1.5">
                              <span className="text-[10px]" style={{ color: S.text3 }}>{isExpanded ? "▼" : "▶"}</span>
                              <span className="text-sm" style={{ color: S.text2 }}>{reason}</span>
                            </div>
                            <span className="rounded-full px-2 py-0.5 text-[11px] font-medium"
                              style={{ background: "rgba(239,68,68,0.12)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
                              {items.length}
                            </span>
                          </button>
                          {isExpanded && (
                            <div className="ml-4 mt-1 mb-2 space-y-1">
                              {items.map((item, idx) => {
                                const detail = item.issue_id ? issueDetails[item.issue_id] : undefined;
                                const desc = detail && typeof detail === "object" ? detail.description : "";
                                const durationMin = item.duration_ms ? (item.duration_ms / 60000).toFixed(1) : "—";
                                return (
                                  <div key={item.issue_id || idx}
                                    className="rounded-lg px-3 py-2 text-xs"
                                    style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                                    <div className="flex items-center justify-between gap-2">
                                      <div className="flex items-center gap-2 min-w-0 flex-1">
                                        {item.issue_id ? (
                                          <a href={`/tracking?detail=${encodeURIComponent(item.issue_id)}`}
                                            className="font-mono font-medium shrink-0 hover:underline"
                                            style={{ color: "#2563EB" }}>
                                            {item.issue_id.length > 12 ? item.issue_id.slice(0, 12) + "…" : item.issue_id}
                                          </a>
                                        ) : (
                                          <span className="font-mono" style={{ color: S.text3 }}>—</span>
                                        )}
                                        {item.username && (
                                          <span className="shrink-0" style={{ color: S.text3 }}>{item.username}</span>
                                        )}
                                        <span className="shrink-0 tabular-nums font-mono" style={{ color: S.text3 }}>
                                          {durationMin}{t("分钟")}
                                        </span>
                                        {item.created_at && (
                                          <span className="shrink-0 font-mono" style={{ color: S.text3 }}>
                                            {formatLocalTime(item.created_at)}
                                          </span>
                                        )}
                                      </div>
                                      {item.issue_id && (
                                        <a href={`/tracking?detail=${encodeURIComponent(item.issue_id)}`}
                                          className="shrink-0 text-[10px] font-medium hover:underline"
                                          style={{ color: S.accent }}>
                                          {t("查看详情")}
                                        </a>
                                      )}
                                    </div>
                                    {item.error && (
                                      <p className="mt-1 font-mono truncate" style={{ color: "#DC2626" }}
                                        title={item.error}>
                                        {t("错误信息")}: {item.error}
                                      </p>
                                    )}
                                    {detail === "loading" && (
                                      <p className="mt-1" style={{ color: S.text3 }}>{t("加载中")}...</p>
                                    )}
                                    {desc && (
                                      <p className="mt-1 truncate" style={{ color: S.text2 }}
                                        title={desc}>
                                        {t("原始输入")}: {desc}
                                      </p>
                                    )}
                                  </div>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                );
              })()}
            </section>
          </div>

          {/* Rule accuracy */}
          {ruleAccuracy.length > 0 && (
            <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
              <h2 className="mb-4 text-sm font-semibold" style={{ color: S.text1 }}>{t("规则准确率")}</h2>
              <div className="overflow-hidden rounded-lg" style={{ border: `1px solid ${S.border}` }}>
                <table className="min-w-full">
                  <thead>
                    <tr style={{ background: "rgba(0,0,0,0.02)" }}>
                      {[t("关联规则"), t("分析量"), t("成功"), t("不准确"), t("准确率"), t("平均置信度")].map((h) => (
                        <th key={h} className="px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {ruleAccuracy.map((r) => (
                      <tr key={r.rule_type} style={{ borderTop: `1px solid ${S.border}` }}>
                        <td className="px-3 py-2">
                          <span className="rounded px-1.5 py-0.5 text-xs font-medium"
                            style={{ background: S.accentBg, color: S.accent }}>{r.rule_type}</span>
                        </td>
                        <td className="px-3 py-2 text-xs tabular-nums" style={{ color: S.text2 }}>{r.total}</td>
                        <td className="px-3 py-2 text-xs tabular-nums" style={{ color: "#16A34A" }}>{r.done}</td>
                        <td className="px-3 py-2 text-xs tabular-nums" style={{ color: "#DC2626" }}>{r.inaccurate}</td>
                        <td className="px-3 py-2">
                          <div className="flex items-center gap-2">
                            <div className="w-16 h-2 rounded-full overflow-hidden" style={{ background: S.hover }}>
                              <div className="h-full rounded-full" style={{
                                width: `${r.accuracy_rate}%`,
                                background: r.accuracy_rate >= 80 ? "#16A34A" : r.accuracy_rate >= 50 ? "#EA580C" : "#DC2626",
                              }} />
                            </div>
                            <span className="text-xs font-mono font-semibold" style={{
                              color: r.accuracy_rate >= 80 ? "#16A34A" : r.accuracy_rate >= 50 ? "#EA580C" : "#DC2626",
                            }}>{r.accuracy_rate}%</span>
                          </div>
                        </td>
                        <td className="px-3 py-2 text-xs tabular-nums" style={{ color: S.text2 }}>{r.avg_confidence_score.toFixed(1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
